use crate::notifications::NotifyCtx;
use crate::power::{self, KeepAwake, PowerStatus};
use crate::procguard;
use crate::rpc::{self, Client, RpcError};
use crate::tray_model::TrayModel;
use log::{error, warn};
use serde_json::{json, Value};
use std::{
    collections::VecDeque,
    sync::atomic::{AtomicBool, AtomicU32, Ordering},
    sync::{Arc, Mutex as StdMutex},
    time::{Duration, Instant, SystemTime},
};
use tauri::{menu::MenuItem, AppHandle, Emitter, Manager, Runtime};
use tauri_plugin_notification::NotificationExt;
use tokio::{
    io::{AsyncBufReadExt, BufReader},
    process::Command,
    sync::{mpsc, Mutex},
    time::sleep,
};

pub fn method_timeout(method: &str) -> Duration {
    if method == "chat/send" {
        Duration::from_secs(2 * 60 * 60)
    } else {
        Duration::from_secs(30)
    }
}

pub struct Engine<R: Runtime = tauri::Wry> {
    inner: Arc<Inner<R>>,
}

trait HealthKill: Send + Sync {
    fn terminate(&self);
}

impl HealthKill for procguard::Guard {
    fn terminate(&self) {
        procguard::Guard::terminate(self);
    }
}
struct Inner<R: Runtime> {
    app: Option<AppHandle<R>>,
    rpc: Mutex<Option<Arc<Client<tokio::process::ChildStdin>>>>,
    status: Mutex<String>,
    restarts: Mutex<RestartTracker>,
    active_pid: AtomicU32,
    generation: std::sync::atomic::AtomicU64,
    shutting_down: AtomicBool,
    guard: Mutex<Option<Arc<procguard::Guard>>>,
    power: KeepAwake<Box<dyn power::PowerOs>>,
    notifications: StdMutex<NotifyCtx>,
    pending_title_lookups:
        Arc<StdMutex<std::collections::HashMap<String, Arc<tokio::sync::Notify>>>>,
    tray_model: StdMutex<TrayModel>,
    pause_item: StdMutex<Option<MenuItem<R>>>,
    last_tray: StdMutex<Option<(String, bool)>>,
}

struct TitleLookupGuard {
    pending: Arc<StdMutex<std::collections::HashMap<String, Arc<tokio::sync::Notify>>>>,
    project: String,
    notify: Arc<tokio::sync::Notify>,
    armed: bool,
}

impl TitleLookupGuard {
    fn disarm(&mut self) {
        self.armed = false;
    }
}

impl Drop for TitleLookupGuard {
    fn drop(&mut self) {
        if !self.armed {
            return;
        }
        let pending = self.pending.clone();
        let project = self.project.clone();
        let notify = self.notify.clone();
        tokio::spawn(async move {
            pending
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner)
                .remove(&project);
            notify.notify_waiters();
        });
    }
}

#[derive(Debug)]
pub struct RestartTracker {
    attempts: VecDeque<Instant>,
}
impl RestartTracker {
    pub fn new() -> Self {
        Self {
            attempts: VecDeque::new(),
        }
    }
    pub fn allow(&mut self, now: Instant) -> bool {
        while self
            .attempts
            .front()
            .is_some_and(|t| now.duration_since(*t) > Duration::from_secs(600))
        {
            self.attempts.pop_front();
        }
        if self.attempts.len() >= 5 {
            return false;
        }
        self.attempts.push_back(now);
        true
    }
    pub fn delay(&self) -> Duration {
        Duration::from_secs(
            2u64.saturating_pow(self.attempts.len().saturating_sub(1) as u32)
                .min(60),
        )
    }
}

impl<R: Runtime> Clone for Engine<R> {
    fn clone(&self) -> Self {
        Self {
            inner: self.inner.clone(),
        }
    }
}

impl<R: Runtime> Engine<R> {
    fn notification_ctx(&self) -> std::sync::MutexGuard<'_, NotifyCtx> {
        self.inner
            .notifications
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
    }

    pub fn new(app: AppHandle<R>) -> Self {
        Self {
            inner: Arc::new(Inner {
                app: Some(app),
                rpc: Mutex::new(None),
                status: Mutex::new("starting".into()),
                restarts: Mutex::new(RestartTracker::new()),
                active_pid: AtomicU32::new(0),
                generation: std::sync::atomic::AtomicU64::new(0),
                shutting_down: AtomicBool::new(false),
                guard: Mutex::new(None),
                power: power::platform(),
                notifications: StdMutex::new(NotifyCtx::default()),
                pending_title_lookups: Arc::new(StdMutex::new(std::collections::HashMap::new())),
                tray_model: StdMutex::new(TrayModel::default()),
                pause_item: StdMutex::new(None),
                last_tray: StdMutex::new(None),
            }),
        }
    }
    #[doc(hidden)]
    pub fn new_without_app() -> Self {
        Self {
            inner: Arc::new(Inner {
                app: None,
                rpc: Mutex::new(None),
                status: Mutex::new("starting".into()),
                restarts: Mutex::new(RestartTracker::new()),
                active_pid: AtomicU32::new(0),
                generation: std::sync::atomic::AtomicU64::new(0),
                shutting_down: AtomicBool::new(false),
                guard: Mutex::new(None),
                power: power::platform(),
                notifications: StdMutex::new(NotifyCtx::default()),
                pending_title_lookups: Arc::new(StdMutex::new(std::collections::HashMap::new())),
                tray_model: StdMutex::new(TrayModel::default()),
                pause_item: StdMutex::new(None),
                last_tray: StdMutex::new(None),
            }),
        }
    }
    pub fn start(&self) {
        let this = self.clone();
        tauri::async_runtime::spawn(async move {
            this.set_status("starting", None).await;
            this.supervise().await;
        });
        let this = self.clone();
        tauri::async_runtime::spawn(async move {
            this.power_status_loop().await;
        });
    }

    pub fn set_pause_item(&self, item: MenuItem<R>) {
        *self
            .inner
            .pause_item
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner) = Some(item);
        self.render_tray();
    }

    pub fn pausable_job(&self) -> Option<String> {
        self.inner
            .tray_model
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .pausable_job()
    }
    async fn supervise(&self) {
        loop {
            if self.inner.shutting_down.load(Ordering::Acquire) {
                break;
            }
            match self.spawn_once().await {
                Ok(()) => {}
                Err(e) => {
                    self.clear_tray();
                    if self.inner.shutting_down.load(Ordering::Acquire) {
                        break;
                    }
                    error!("engine stopped: {e}");
                    self.set_status("error", Some(short_reason(&e))).await;
                    let now = Instant::now();
                    let mut tracker = self.inner.restarts.lock().await;
                    if !tracker.allow(now) {
                        self.emit(
                            "engine/error",
                            json!({"message":"engine restart limit exceeded"}),
                        );
                        break;
                    }
                    let wait = tracker.delay();
                    drop(tracker);
                    self.set_status("restarting", None).await;
                    sleep(wait).await;
                }
            }
        }
        self.set_status("stopped", None).await;
    }
    async fn spawn_once(&self) -> Result<(), RpcError> {
        let (program, args) = command_line(self.inner.app.as_ref());
        let mut command = configured_command(program, args);
        let mut child = command
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::piped())
            .stdin(std::process::Stdio::piped())
            .spawn()?;
        let pid = child.id().ok_or(RpcError::Closed)?;
        let guard = Arc::new(procguard::attach(pid)?);
        *self.inner.guard.lock().await = Some(guard.clone());
        self.inner.active_pid.store(pid, Ordering::Release);
        let stdout = child.stdout.take().ok_or(RpcError::Closed)?;
        let stderr = child.stderr.take().ok_or(RpcError::Closed)?;
        let stdin = child.stdin.take().ok_or(RpcError::Closed)?;
        let client = Client::new(stdin);
        *self.inner.rpc.lock().await = Some(client.clone());
        let (tx, mut rx) = mpsc::channel(64);
        let reader_client = client.clone();
        tauri::async_runtime::spawn(async move {
            let mut reader = BufReader::new(stdout);
            let mut line = Vec::new();
            loop {
                line.clear();
                match reader.read_until(b'\n', &mut line).await {
                    Ok(0) => break,
                    Ok(_) => {}
                    Err(error) => {
                        warn!("engine stdout read failed: {error}");
                        break;
                    }
                }
                match rpc::decode(&line) {
                    Ok(v) => reader_client.dispatch(v, &tx).await,
                    Err(e) => warn!("engine RPC line ignored: {e}"),
                }
            }
            reader_client.close().await;
        });
        let app = self.inner.app.clone();
        tauri::async_runtime::spawn(async move {
            let mut reader = BufReader::new(stderr);
            let mut bytes = Vec::new();
            loop {
                bytes.clear();
                match reader.read_until(b'\n', &mut bytes).await {
                    Ok(0) => break,
                    Ok(_) => {}
                    Err(error) => {
                        warn!("engine stderr read failed: {error}");
                        break;
                    }
                }
                let line = String::from_utf8_lossy(&bytes);
                if let Some(app) = &app {
                    if let Ok(path) = app.path().app_log_dir() {
                        let _ = std::fs::create_dir_all(&path);
                        let _ = std::fs::OpenOptions::new()
                            .create(true)
                            .append(true)
                            .open(path.join("engine.log"))
                            .and_then(|mut f| std::io::Write::write_all(&mut f, line.as_bytes()));
                    }
                }
            }
        });
        let generation = self.inner.generation.fetch_add(1, Ordering::AcqRel) + 1;
        let seed_task = Arc::new(Mutex::new(None));
        let seed_started = Arc::new(AtomicBool::new(false));
        let ping_engine = self.clone();
        let ping_guard: Arc<dyn HealthKill> = guard.clone();
        let seed_client = client.clone();
        let seed_task_for_ping = seed_task.clone();
        let seed_started_for_ping = seed_started.clone();
        let ping_task =
            tauri::async_runtime::spawn(health_ping(client.clone(), ping_guard, move || {
                let ping_engine = ping_engine.clone();
                let seed_client = seed_client.clone();
                let seed_task = seed_task_for_ping.clone();
                let seed_started = seed_started_for_ping.clone();
                async move {
                    if *ping_engine.inner.status.lock().await != "ready" {
                        ping_engine.set_status("ready", None).await;
                    }
                    if !seed_started.swap(true, Ordering::AcqRel) {
                        let engine = ping_engine.clone();
                        let client = seed_client.clone();
                        *seed_task.lock().await = Some(tauri::async_runtime::spawn(async move {
                            engine.seed_active_jobs(client, generation).await;
                        }));
                    }
                }
            }));
        while let Some(notification) = rx.recv().await {
            self.apply_model_change(&notification).await;
            self.spawn_notification_refresh(notification.clone());
            self.emit("engine-event", notification);
        }
        if let Some(seed) = seed_task.lock().await.take() {
            seed.abort();
            let _ = seed.await;
        }
        ping_task.abort();
        let _ = ping_task.await;
        client.close().await;
        #[cfg(unix)]
        procguard::terminate_group(child.id().unwrap_or_default());
        #[cfg(windows)]
        let _ = child.kill().await;
        self.clear_tray();
        let _ = child.wait().await;
        self.inner.active_pid.store(0, Ordering::Release);
        *self.inner.guard.lock().await = None;
        *self.inner.rpc.lock().await = None;
        let _ = self.inner.power.release();
        Err(RpcError::Closed)
    }

    async fn seed_active_jobs<W>(&self, client: Arc<Client<W>>, generation: u64)
    where
        W: tokio::io::AsyncWrite + Unpin + Send + 'static,
    {
        let mut result = client
            .request("projects/list", json!({}), method_timeout("projects/list"))
            .await;
        if result.is_err() {
            sleep(Duration::from_secs(10)).await;
            result = client
                .request("projects/list", json!({}), method_timeout("projects/list"))
                .await;
        }
        let Ok(Value::Array(projects)) = result else {
            return;
        };
        if self.inner.generation.load(Ordering::Acquire) != generation {
            return;
        }
        {
            let mut model = self
                .inner
                .tray_model
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner);
            for project in projects {
                let Some(active) = project.get("activeJob") else {
                    continue;
                };
                let Some(job) = active.get("job").and_then(Value::as_str) else {
                    continue;
                };
                let state = active
                    .get("state")
                    .and_then(Value::as_str)
                    .unwrap_or("paused");
                if model.jobs.contains_key(job) {
                    continue;
                }
                model.apply(&json!({"method":"job/state","params":{"job":job,"state":state}}));
                if let (Some(done), Some(total)) = (active.get("done"), active.get("total")) {
                    model.apply(&json!({"method":"job/progress","params":{"job":job,"done":done,"total":total}}));
                }
                if let Some(reason) = active.get("pauseReason").and_then(Value::as_str) {
                    model.apply(&json!({"method":"engine/event","params":{"job":job,"kind":"pause","reason":reason}}));
                }
            }
        }
        self.sync_power_and_tray().await;
    }
    async fn apply_model_change(&self, notification: &Value) {
        self.inner
            .tray_model
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .apply(notification);
        self.sync_power_and_tray().await;
    }
    async fn sync_power_and_tray(&self) {
        let holds = self
            .inner
            .tray_model
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .holds_awake();
        let result = if holds {
            self.inner.power.hold()
        } else {
            self.inner.power.release()
        };
        if let Err(error) = result {
            log::warn!("failed to update system awake hold: {error}");
        }
        self.render_tray();
    }
    pub async fn call(&self, method: &str, params: Value) -> Result<Value, RpcError> {
        let rpc = self
            .inner
            .rpc
            .lock()
            .await
            .clone()
            .ok_or(RpcError::Closed)?;
        let result = rpc.request(method, params, method_timeout(method)).await;
        if matches!(result, Err(RpcError::WriteTimeout | RpcError::Io(_))) {
            if let Some(guard) = self.inner.guard.lock().await.clone() {
                let _ = tokio::task::spawn_blocking(move || guard.terminate()).await;
            }
        }
        result
    }
    pub async fn status(&self) -> String {
        self.inner.status.lock().await.clone()
    }
    pub async fn notify(&self, method: &str, params: Value) -> Result<(), RpcError> {
        let rpc = self
            .inner
            .rpc
            .lock()
            .await
            .clone()
            .ok_or(RpcError::Closed)?;
        let result = rpc.notify(method, params, Duration::from_secs(10)).await;
        if matches!(result, Err(RpcError::WriteTimeout | RpcError::Io(_))) {
            if let Some(guard) = self.inner.guard.lock().await.clone() {
                tauri::async_runtime::spawn_blocking(move || guard.terminate());
            }
        }
        result
    }
    pub async fn send_power_status(&self) -> Result<(), RpcError> {
        let status = self.power_status();
        self.notify(
            "os/powerStatus",
            json!({"onAc": status.on_ac, "percent": status.percent}),
        )
        .await
    }
    pub async fn shutdown(&self) {
        if self.inner.shutting_down.swap(true, Ordering::AcqRel) {
            return;
        }
        #[cfg(all(test, target_os = "macos"))]
        let shutdown_budget = Duration::from_millis(250);
        #[cfg(all(test, not(target_os = "macos")))]
        let shutdown_budget = Duration::from_millis(250);
        #[cfg(all(not(test), target_os = "macos"))]
        let shutdown_budget = Duration::from_secs(8);
        #[cfg(all(not(test), not(target_os = "macos")))]
        let shutdown_budget = Duration::from_secs(10);
        let deadline = tokio::time::Instant::now() + shutdown_budget;
        if let Some(rpc) = self.inner.rpc.lock().await.clone() {
            let remaining = deadline.saturating_duration_since(tokio::time::Instant::now());
            let _ = tokio::time::timeout(remaining, rpc.request("shutdown", json!({}), remaining))
                .await;
        }
        let remaining = deadline.saturating_duration_since(tokio::time::Instant::now());
        let stopped = tokio::time::timeout(remaining, async {
            while self.inner.active_pid.load(Ordering::Acquire) != 0 {
                sleep(Duration::from_millis(25)).await;
            }
        })
        .await
        .is_ok();
        if !stopped {
            if let Some(guard) = self.inner.guard.lock().await.clone() {
                #[cfg(target_os = "macos")]
                let pid = self.inner.active_pid.load(Ordering::Acquire);
                #[cfg(target_os = "macos")]
                let _ = tokio::task::spawn_blocking(move || {
                    drop(guard);
                    procguard::terminate_group_with_grace(pid, Duration::from_secs(1));
                })
                .await;
                #[cfg(not(target_os = "macos"))]
                let _ = tokio::task::spawn_blocking(move || guard.terminate()).await;
            }
        }
        let _ = self.inner.power.release();
        self.set_status("stopped", None).await;
    }
    pub(crate) async fn force_terminate(&self) {
        if let Some(guard) = self.inner.guard.lock().await.clone() {
            let grace = exit_backstop_grace();
            let _ = tokio::task::spawn_blocking(move || guard.terminate_with_grace(grace)).await;
        }
    }
    async fn set_status(&self, status: &str, reason: Option<String>) {
        let mut current = self.inner.status.lock().await;
        if *current == status && reason.is_none() {
            return;
        }
        *current = status.into();
        let mut payload = json!({"status": status});
        if let Some(reason) = reason {
            payload["reason"] = json!(reason);
        }
        self.emit("engine/status", payload);
    }

    fn clear_tray(&self) {
        self.inner
            .tray_model
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .engine_died();
        let holds = self
            .inner
            .tray_model
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .holds_awake();
        let result = if holds {
            self.inner.power.hold()
        } else {
            self.inner.power.release()
        };
        if let Err(error) = result {
            log::warn!("failed to update system awake hold: {error}");
        }
        self.render_tray();
    }

    fn render_tray(&self) {
        if self.inner.shutting_down.load(Ordering::Acquire) {
            return;
        }
        if let Some(app) = &self.inner.app {
            let (tooltip, enabled) = {
                let model = self
                    .inner
                    .tray_model
                    .lock()
                    .unwrap_or_else(std::sync::PoisonError::into_inner);
                (
                    model.tooltip(),
                    crate::tray_model::pause_item_enabled(&model),
                )
            };
            let changed = {
                let mut last = self
                    .inner
                    .last_tray
                    .lock()
                    .unwrap_or_else(std::sync::PoisonError::into_inner);
                let changed = last.as_ref() != Some(&(tooltip.clone(), enabled));
                if changed {
                    *last = Some((tooltip.clone(), enabled));
                }
                changed
            };
            if !changed {
                return;
            }
            let tray = app.tray_by_id("main");
            let item = self
                .inner
                .pause_item
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner)
                .clone();
            let app = app.clone();
            let _ = app.run_on_main_thread(move || {
                if let Some(tray) = tray {
                    let _ = tray.set_tooltip(Some(&tooltip));
                }
                if let Some(item) = item {
                    let _ = item.set_enabled(enabled);
                }
            });
        }
    }
    async fn power_status_loop(&self) {
        loop {
            if self.inner.shutting_down.load(Ordering::Acquire) {
                break;
            }
            let status = self.power_status();
            let battery_transition = {
                let mut ctx = self.notification_ctx();
                let transition =
                    battery_transition(ctx.previous_power, status, self.inner.power.is_held());
                ctx.previous_power = Some(status);
                transition
            };
            if battery_transition {
                self.send_battery_notification(status.percent);
            }
            if let Err(error) = self.send_power_status().await {
                log::debug!("could not send power status: {error}");
            }
            sleep(Duration::from_secs(30)).await;
        }
    }
    fn power_status(&self) -> PowerStatus {
        self.inner.power.status()
    }
    fn emit(&self, event: &str, payload: Value) {
        if let Some(app) = &self.inner.app {
            let _ = app.emit(event, payload);
        }
    }

    fn spawn_notification_refresh(&self, event: Value) {
        let this = self.clone();
        tauri::async_runtime::spawn(async move {
            this.notify_event(event).await;
        });
    }

    async fn notify_event(&self, event: Value) {
        let will_toast = {
            let ctx = self.notification_ctx();
            crate::notifications::notification_for(&event, &ctx).is_some()
        };
        if !will_toast {
            return;
        }
        let project = event
            .get("params")
            .and_then(|p| p.get("project"))
            .and_then(Value::as_str)
            .map(str::to_owned);
        if let Some(project) = project {
            let unknown = !self
                .notification_ctx()
                .project_titles
                .contains_key(&project);
            if unknown {
                let (notify, owner) = {
                    let mut pending = self
                        .inner
                        .pending_title_lookups
                        .lock()
                        .unwrap_or_else(std::sync::PoisonError::into_inner);
                    match pending.entry(project.clone()) {
                        std::collections::hash_map::Entry::Vacant(entry) => {
                            let notify = Arc::new(tokio::sync::Notify::new());
                            entry.insert(notify.clone());
                            (notify, true)
                        }
                        std::collections::hash_map::Entry::Occupied(entry) => {
                            (entry.get().clone(), false)
                        }
                    }
                };
                if owner {
                    let mut lookup_guard = TitleLookupGuard {
                        pending: self.inner.pending_title_lookups.clone(),
                        project: project.clone(),
                        notify: notify.clone(),
                        armed: true,
                    };
                    if let Ok(Ok(Value::Array(items))) = tokio::time::timeout(
                        Duration::from_secs(10),
                        self.call("projects/list", json!({})),
                    )
                    .await
                    {
                        let mut ctx = self.notification_ctx();
                        for item in items {
                            if let (Some(id), Some(title)) = (
                                item.get("project").and_then(Value::as_str),
                                item.get("title").and_then(Value::as_str),
                            ) {
                                ctx.project_titles.insert(id.into(), title.into());
                            }
                        }
                    }
                    self.inner
                        .pending_title_lookups
                        .lock()
                        .unwrap_or_else(std::sync::PoisonError::into_inner)
                        .remove(&project);
                    notify.notify_waiters();
                    lookup_guard.disarm();
                } else {
                    notify.notified().await;
                }
            }
        }
        self.dispatch_notification(event).await;
    }

    async fn dispatch_notification(&self, event: Value) {
        if let Some(app) = &self.inner.app {
            let mut ctx = self.notification_ctx();
            if let Err(error) = crate::notifications::dispatch_notification(
                &mut ctx,
                &event,
                SystemTime::now(),
                |title, body| app.notification().builder().title(title).body(body).show(),
            ) {
                log::debug!("could not show notification: {error}");
            }
        }
    }

    fn send_battery_notification(&self, percent: Option<u8>) {
        if let Some(app) = &self.inner.app {
            let mut ctx = self.notification_ctx();
            ctx.now = SystemTime::now();
            let job = self
                .inner
                .tray_model
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner)
                .pausable_job()
                .unwrap_or_default();
            let event = json!({"method":"engine/event","params":{"job":job,"kind":"battery"}});
            let held = self
                .inner
                .tray_model
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner)
                .holds_awake();
            if let Some((title, body)) = crate::notifications::battery_notification(held, percent) {
                if crate::notifications::should_notify(&mut ctx, &event) {
                    let _ = app.notification().builder().title(title).body(body).show();
                }
            }
        }
    }
}

fn short_reason(error: &RpcError) -> String {
    let reason = error.to_string();
    reason.chars().take(120).collect()
}

pub fn battery_transition(previous: Option<PowerStatus>, current: PowerStatus, held: bool) -> bool {
    held && previous.is_some_and(|status| status.on_ac == Some(true))
        && current.on_ac == Some(false)
}

pub(crate) fn exit_backstop_grace() -> Duration {
    #[cfg(target_os = "macos")]
    {
        Duration::from_secs(1)
    }
    #[cfg(not(target_os = "macos"))]
    {
        Duration::from_secs(5)
    }
}

async fn health_ping<W, F, Fut>(client: Arc<Client<W>>, target: Arc<dyn HealthKill>, on_success: F)
where
    W: tokio::io::AsyncWrite + Unpin + Send + 'static,
    F: FnMut() -> Fut,
    Fut: std::future::Future<Output = ()>,
{
    health_ping_with_intervals(
        client,
        target,
        on_success,
        Duration::from_secs(10),
        Duration::from_secs(30),
    )
    .await;
}

async fn health_ping_with_intervals<W, F, Fut>(
    client: Arc<Client<W>>,
    target: Arc<dyn HealthKill>,
    mut on_success: F,
    ping_timeout: Duration,
    retry_delay: Duration,
) where
    W: tokio::io::AsyncWrite + Unpin + Send + 'static,
    F: FnMut() -> Fut,
    Fut: std::future::Future<Output = ()>,
{
    let mut failures = 0;
    loop {
        let ping = tokio::time::timeout(
            ping_timeout,
            client.request("health/ping", json!({}), ping_timeout),
        )
        .await
        .unwrap_or(Err(RpcError::Timeout));
        match ping {
            Ok(_) => {
                failures = 0;
                on_success().await;
            }
            Err(RpcError::Closed) => {
                target.terminate();
                break;
            }
            Err(_) => {
                failures += 1;
                if failures >= 3 {
                    target.terminate();
                    break;
                }
            }
        }
        sleep(retry_delay).await;
    }
}

#[cfg(test)]
mod health_tests {
    use super::*;
    use std::sync::atomic::AtomicUsize;

    struct FakeSpawner {
        kills: AtomicUsize,
    }

    impl HealthKill for FakeSpawner {
        fn terminate(&self) {
            self.kills.fetch_add(1, Ordering::Relaxed);
        }
    }

    #[test]
    fn health_ping_does_not_kill_after_instance_ends() {
        let runtime = tokio::runtime::Runtime::new().unwrap();
        runtime.block_on(async {
            let (writer, _reader) = tokio::io::duplex(1024);
            let client = Client::new(writer);
            let target = Arc::new(FakeSpawner {
                kills: AtomicUsize::new(0),
            });
            client.close().await;
            health_ping(client, target.clone(), || async {}).await;
            assert_eq!(target.kills.load(Ordering::Relaxed), 1);
        });
    }

    #[test]
    fn health_ping_counts_a_stuck_writer_as_a_failure() {
        let runtime = tokio::runtime::Runtime::new().unwrap();
        runtime.block_on(async {
            let (writer, _reader) = tokio::io::duplex(1);
            let client = Client::with_write_timeout(writer, Duration::from_millis(10));
            let target = Arc::new(FakeSpawner {
                kills: AtomicUsize::new(0),
            });
            health_ping_with_intervals(
                client,
                target.clone(),
                || async {},
                Duration::from_millis(25),
                Duration::from_millis(1),
            )
            .await;
            assert_eq!(target.kills.load(Ordering::Relaxed), 1);
        });
    }

    #[test]
    fn health_ping_terminates_after_three_slow_replies() {
        let runtime = tokio::runtime::Runtime::new().unwrap();
        runtime.block_on(async {
            let (writer, server_io) = tokio::io::duplex(1024);
            let client = Client::with_write_timeout(writer, Duration::from_secs(1));
            let target = Arc::new(FakeSpawner {
                kills: AtomicUsize::new(0),
            });
            let mut server = BufReader::new(server_io);
            let health = tokio::spawn(health_ping_with_intervals(
                client,
                target.clone(),
                || async {},
                Duration::from_millis(10),
                Duration::from_millis(1),
            ));
            for _ in 0..3 {
                let mut line = String::new();
                server.read_line(&mut line).await.unwrap();
            }
            health.await.unwrap();
            assert_eq!(target.kills.load(Ordering::Relaxed), 1);
        });
    }

    #[test]
    fn health_ping_resets_failures_after_a_slow_reply() {
        let runtime = tokio::runtime::Runtime::new().unwrap();
        runtime.block_on(async {
            let (writer, server_io) = tokio::io::duplex(1024);
            let client = Client::with_write_timeout(writer, Duration::from_secs(1));
            let response_client = client.clone();
            let target = Arc::new(FakeSpawner {
                kills: AtomicUsize::new(0),
            });
            let success = Arc::new(tokio::sync::Notify::new());
            let success_for_callback = success.clone();
            let mut server = BufReader::new(server_io);
            let health = tokio::spawn(health_ping_with_intervals(
                client,
                target.clone(),
                move || {
                    let success = success_for_callback.clone();
                    async move { success.notify_one() }
                },
                Duration::from_millis(10),
                Duration::from_millis(1),
            ));
            for _ in 0..2 {
                let mut line = String::new();
                server.read_line(&mut line).await.unwrap();
            }
            let mut line = String::new();
            server.read_line(&mut line).await.unwrap();
            let request: Value = serde_json::from_str(&line).unwrap();
            let (notifications, _) = mpsc::channel(1);
            response_client
                .dispatch(json!({"id": request["id"], "result": true}), &notifications)
                .await;
            tokio::time::timeout(Duration::from_secs(1), success.notified())
                .await
                .unwrap();
            assert_eq!(target.kills.load(Ordering::Relaxed), 0);
            health.abort();
        });
    }

    #[test]
    fn status_transitions_update_engine_state() {
        let runtime = tokio::runtime::Runtime::new().unwrap();
        runtime.block_on(async {
            let engine = Engine::<tauri::Wry>::new_without_app();
            engine.set_status("starting", None).await;
            assert_eq!(engine.status().await, "starting");
            engine.set_status("ready", None).await;
            assert_eq!(engine.status().await, "ready");
            engine.set_status("error", Some("closed".into())).await;
            assert_eq!(engine.status().await, "error");
            engine.set_status("restarting", None).await;
            assert_eq!(engine.status().await, "restarting");
            engine.set_status("stopped", None).await;
            assert_eq!(engine.status().await, "stopped");
        });
    }

    #[test]
    fn shutdown_kills_a_process_with_a_stuck_stdin() {
        let runtime = tokio::runtime::Runtime::new().unwrap();
        runtime.block_on(async {
            let mut command = if cfg!(windows) {
                configured_command(
                    "cmd".into(),
                    vec!["/C".into(), "ping -n 60 127.0.0.1 > NUL".into()],
                )
            } else {
                configured_command("sleep".into(), vec!["60".into()])
            };
            let mut child = command
                .stdin(std::process::Stdio::piped())
                .stdout(std::process::Stdio::null())
                .stderr(std::process::Stdio::null())
                .spawn()
                .unwrap();
            let pid = child.id().unwrap();
            let guard = Arc::new(procguard::attach(pid).unwrap());
            let stdin = child.stdin.take().unwrap();
            let engine = Engine::<tauri::Wry>::new_without_app();
            let client = Client::with_write_timeout(stdin, Duration::from_secs(5));
            let jam = client.clone();
            let jammed = tokio::spawn(async move {
                jam.notify(
                    "jam",
                    json!({"data": "x".repeat(4 * 1024 * 1024)}),
                    Duration::from_secs(5),
                )
                .await
            });
            tokio::time::sleep(Duration::from_millis(50)).await;
            *engine.inner.rpc.lock().await = Some(client);
            *engine.inner.guard.lock().await = Some(guard);
            engine.inner.active_pid.store(pid, Ordering::Release);

            let started = std::time::Instant::now();
            engine.shutdown().await;
            assert!(started.elapsed() < Duration::from_secs(2));
            tokio::time::sleep(Duration::from_millis(100)).await;
            assert!(child.try_wait().unwrap().is_some());
            let _ = jammed.await;
        });
    }

    #[test]
    fn exit_backstop_uses_a_short_grace() {
        assert!(exit_backstop_grace() <= Duration::from_secs(5));
        #[cfg(target_os = "macos")]
        assert!(exit_backstop_grace() <= Duration::from_secs(1));
    }
}

#[cfg(test)]
mod start_tests {
    use super::Engine;

    #[test]
    fn start_does_not_require_an_entered_tokio_runtime() {
        std::env::set_var("BCA_ENGINE_PROGRAM", "bca-test-engine-not-started");
        let engine = Engine::<tauri::Wry>::new_without_app();
        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| engine.start()));
        assert!(result.is_ok());
    }
}

fn configured_command(program: String, args: Vec<String>) -> Command {
    let mut command = Command::new(program);
    command.args(args).env_clear().envs(minimal_environment());
    #[cfg(unix)]
    command.process_group(0);
    #[cfg(windows)]
    command.creation_flags(windows::Win32::System::Threading::CREATE_NO_WINDOW.0);
    command.kill_on_drop(true);
    command
}

pub fn command_line<R: Runtime>(app: Option<&AppHandle<R>>) -> (String, Vec<String>) {
    if let Ok(program) = std::env::var("BCA_ENGINE_PROGRAM") {
        let args = std::env::var("BCA_ENGINE_ARGS")
            .ok()
            .and_then(|value| serde_json::from_str(&value).ok())
            .unwrap_or_default();
        return (program, args);
    }
    let root = app
        .expect("an app handle is required when BCA_ENGINE_PROGRAM is unset")
        .path()
        .app_data_dir()
        .unwrap_or_else(|_| std::path::PathBuf::from("."));
    let python = if cfg!(windows) {
        root.join("venv").join("Scripts").join("python.exe")
    } else {
        root.join("venv").join("bin").join("python")
    };
    (
        python.to_string_lossy().into_owned(),
        vec!["-m".into(), "bca.api.stdio".into()],
    )
}
pub fn minimal_environment() -> std::collections::HashMap<String, String> {
    let allow = [
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "WINDIR",
        "COMSPEC",
        "TEMP",
        "TMP",
        "TMPDIR",
        "HOME",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TZ",
        "USER",
        "USERNAME",
        "LOGNAME",
        "SHELL",
    ];
    std::env::vars()
        .filter(|(k, _)| allow.iter().any(|a| a.eq_ignore_ascii_case(k)))
        .chain([
            (String::from("PYTHONUTF8"), String::from("1")),
            (String::from("PYTHONIOENCODING"), String::from("utf-8")),
        ])
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::sync::{
        atomic::{AtomicUsize, Ordering},
        Arc,
    };

    struct RecordingPower {
        holds: AtomicUsize,
        releases: AtomicUsize,
    }

    impl power::PowerOs for RecordingPower {
        fn hold(&self) -> Result<(), power::PowerError> {
            self.holds.fetch_add(1, Ordering::Relaxed);
            Ok(())
        }

        fn release(&self) -> Result<(), power::PowerError> {
            self.releases.fetch_add(1, Ordering::Relaxed);
            Ok(())
        }

        fn status(&self) -> PowerStatus {
            PowerStatus {
                on_ac: Some(true),
                percent: Some(100),
            }
        }
    }

    impl power::PowerOs for Arc<RecordingPower> {
        fn hold(&self) -> Result<(), power::PowerError> {
            self.as_ref().hold()
        }

        fn release(&self) -> Result<(), power::PowerError> {
            self.as_ref().release()
        }

        fn status(&self) -> PowerStatus {
            self.as_ref().status()
        }
    }

    fn test_engine() -> (Engine<tauri::Wry>, Arc<RecordingPower>) {
        let power = Arc::new(RecordingPower {
            holds: AtomicUsize::new(0),
            releases: AtomicUsize::new(0),
        });
        let mut engine = Engine::<tauri::Wry>::new_without_app();
        Arc::get_mut(&mut engine.inner).unwrap().power = KeepAwake::new(Box::new(power.clone()));
        (engine, power)
    }

    #[test]
    fn chat_send_has_two_hour_backstop() {
        assert_eq!(method_timeout("chat/send"), Duration::from_secs(7200));
    }

    #[test]
    fn other_methods_keep_thirty_second_timeout() {
        assert_eq!(method_timeout("chat/stop"), Duration::from_secs(30));
        assert_eq!(method_timeout("files/path"), Duration::from_secs(30));
    }

    #[test]
    fn keep_awake_stays_while_any_job_runs() {
        let runtime = tokio::runtime::Runtime::new().unwrap();
        runtime.block_on(async {
            let (engine, power) = test_engine();
            engine
                .apply_model_change(&json!({"method":"job/state","params":{"job":"job-a","project":"project-a","state":"running","step":"verifying"}}))
                .await;
            assert!(engine.inner.power.is_held());
            assert_eq!(power.holds.load(Ordering::Relaxed), 1);

            engine
                .apply_model_change(&json!({"method":"job/state","params":{"job":"job-b","project":"project-b","state":"plan_approved","step":"plan_approved"}}))
                .await;
            assert!(engine.inner.power.is_held());

            engine
                .apply_model_change(&json!({"method":"job/state","params":{"job":"job-b","project":"project-b","state":"done","step":"done"}}))
                .await;
            assert!(engine.inner.power.is_held());
            assert_eq!(power.releases.load(Ordering::Relaxed), 0);

            engine
                .apply_model_change(&json!({"method":"job/state","params":{"job":"job-a","project":"project-a","state":"done","step":"done"}}))
                .await;
            assert!(!engine.inner.power.is_held());
            assert_eq!(power.releases.load(Ordering::Relaxed), 1);
        });
    }

    #[test]
    fn recovered_run_progress_takes_the_hold() {
        let runtime = tokio::runtime::Runtime::new().unwrap();
        runtime.block_on(async {
            let (engine, power) = test_engine();
            engine
                .apply_model_change(&json!({"method":"job/progress","params":{"job":"job-recovered","project":"project-recovered","phase":"full","done":0,"inFlight":3,"retrying":0,"failed":0,"heldBack":0,"remaining":3,"total":6}}))
                .await;
            assert!(engine.inner.power.is_held());
            assert_eq!(power.holds.load(Ordering::Relaxed), 1);
        });
    }

    #[test]
    fn recovered_run_seeded_as_verifying_takes_the_hold() {
        let runtime = tokio::runtime::Runtime::new().unwrap();
        runtime.block_on(async {
            let (engine, power) = test_engine();
            let (client_io, mut server_io) = tokio::io::duplex(4096);
            let client = Client::new(client_io);
            let response_client = client.clone();
            let (notifications, _) = mpsc::channel(1);
            let server = tokio::spawn(async move {
                let mut line = Vec::new();
                tokio::io::AsyncBufReadExt::read_until(
                    &mut tokio::io::BufReader::new(&mut server_io),
                    b'\n',
                    &mut line,
                )
                .await
                .unwrap();
                let request: Value = serde_json::from_slice(&line).unwrap();
                let response = json!({
                    "id": request["id"],
                    "result": [{
                        "activeJob": {
                            "job": "job-seeded",
                            "project": "project-seeded",
                            "state": "running",
                            "step": "verifying"
                        }
                    }]
                });
                response_client.dispatch(response, &notifications).await;
            });
            engine.seed_active_jobs(client, 0).await;
            server.await.unwrap();
            assert!(engine.inner.power.is_held());
            assert_eq!(power.holds.load(Ordering::Relaxed), 1);
        });
    }

    #[test]
    fn pause_stall_failure_and_engine_death_release_the_hold() {
        let runtime = tokio::runtime::Runtime::new().unwrap();
        runtime.block_on(async {
            for event in [
                json!({"method":"engine/event","params":{"job":"job","project":"project","kind":"pause","reason":"quota","resume_at":1795517946}}),
                json!({"method":"job/state","params":{"job":"job","project":"project","state":"stalled","step":"stalled"}}),
                json!({"method":"job/state","params":{"job":"job","project":"project","state":"failed","step":"failed"}}),
            ] {
                let (engine, power) = test_engine();
                engine
                    .apply_model_change(&json!({"method":"job/state","params":{"job":"job","project":"project","state":"running","step":"full_running"}}))
                    .await;
                assert!(engine.inner.power.is_held());
                engine.apply_model_change(&event).await;
                assert!(!engine.inner.power.is_held());
                assert_eq!(power.releases.load(Ordering::Relaxed), 1);
            }

            let (engine, power) = test_engine();
            engine
                .apply_model_change(&json!({"method":"job/state","params":{"job":"job","project":"project","state":"running","step":"full_running"}}))
                .await;
            assert!(engine.inner.power.is_held());
            engine.clear_tray();
            assert!(!engine.inner.power.is_held());
            assert_eq!(power.releases.load(Ordering::Relaxed), 1);
        });
    }

    #[test]
    fn strips_api_keys() {
        std::env::set_var("OPENAI_API_KEY", "secret");
        assert!(!minimal_environment().keys().any(|k| k.contains("API_KEY")));
        std::env::remove_var("OPENAI_API_KEY");
    }
    #[test]
    fn backoff_and_storm() {
        let mut t = RestartTracker::new();
        let n = Instant::now();
        for _ in 0..5 {
            assert!(t.allow(n));
        }
        assert!(!t.allow(n));
        assert!(t.delay() <= Duration::from_secs(60));
    }

    #[test]
    fn restart_window_expires_attempts() {
        let mut tracker = RestartTracker::new();
        let start = Instant::now();
        for _ in 0..5 {
            assert!(tracker.allow(start));
        }
        assert!(!tracker.allow(start));
        assert!(tracker.allow(start + Duration::from_secs(601)));
    }

    #[test]
    fn restart_delay_grows() {
        let mut tracker = RestartTracker::new();
        let now = Instant::now();
        assert_eq!(tracker.delay(), Duration::from_secs(1));
        assert!(tracker.allow(now));
        assert_eq!(tracker.delay(), Duration::from_secs(1));
        for _ in 0..4 {
            assert!(tracker.allow(now));
        }
        assert_eq!(tracker.delay(), Duration::from_secs(16));
    }

    #[test]
    fn environment_allowlist_strips_unapproved_variables() {
        std::env::set_var("BCA_TEST_ALLOWED", "no");
        std::env::set_var("ANTHROPIC_API_KEY", "secret");
        let environment = minimal_environment();
        assert!(!environment.contains_key("BCA_TEST_ALLOWED"));
        assert!(!environment.contains_key("ANTHROPIC_API_KEY"));
        std::env::remove_var("BCA_TEST_ALLOWED");
        std::env::remove_var("ANTHROPIC_API_KEY");
    }
}
