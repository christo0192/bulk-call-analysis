use crate::power::{self, KeepAwake, PowerStatus};
use crate::procguard;
use crate::rpc::{self, Client, RpcError};
use log::{error, warn};
use serde_json::{json, Value};
use std::{
    collections::VecDeque,
    sync::atomic::{AtomicBool, AtomicU32, Ordering},
    sync::Arc,
    time::{Duration, Instant},
};
use tauri::{AppHandle, Emitter, Manager, Runtime};
use tokio::{
    io::{AsyncBufReadExt, BufReader},
    process::Command,
    sync::{mpsc, Mutex},
    time::sleep,
};

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
    shutting_down: AtomicBool,
    guard: Mutex<Option<Arc<procguard::Guard>>>,
    power: KeepAwake<Box<dyn power::PowerOs>>,
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
    pub fn new(app: AppHandle<R>) -> Self {
        Self {
            inner: Arc::new(Inner {
                app: Some(app),
                rpc: Mutex::new(None),
                status: Mutex::new("starting".into()),
                restarts: Mutex::new(RestartTracker::new()),
                active_pid: AtomicU32::new(0),
                shutting_down: AtomicBool::new(false),
                guard: Mutex::new(None),
                power: power::platform(),
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
                shutting_down: AtomicBool::new(false),
                guard: Mutex::new(None),
                power: power::platform(),
            }),
        }
    }
    pub fn start(&self) {
        let this = self.clone();
        tokio::spawn(async move {
            this.set_status("starting", None).await;
            this.supervise().await;
        });
        let this = self.clone();
        tokio::spawn(async move {
            this.power_status_loop().await;
        });
    }
    async fn supervise(&self) {
        loop {
            if self.inner.shutting_down.load(Ordering::Acquire) {
                break;
            }
            match self.spawn_once().await {
                Ok(()) => {}
                Err(e) => {
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
        tokio::spawn(async move {
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
        tokio::spawn(async move {
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
        let ping_engine = self.clone();
        let ping_guard: Arc<dyn HealthKill> = guard.clone();
        let ping_task = tokio::spawn(health_ping(client.clone(), ping_guard, move || {
            let ping_engine = ping_engine.clone();
            async move {
                if *ping_engine.inner.status.lock().await != "ready" {
                    ping_engine.set_status("ready", None).await;
                }
            }
        }));
        while let Some(notification) = rx.recv().await {
            self.update_power_for_notification(&notification).await;
            if notification.get("method").and_then(Value::as_str) == Some("job/progress")
                && !self.inner.shutting_down.load(Ordering::Acquire)
            {
                if let Some(app) = &self.inner.app {
                    if let Some(tray) = app.tray_by_id("main") {
                        let params = notification.get("params").cloned().unwrap_or_default();
                        let done = params.get("done").and_then(Value::as_u64).unwrap_or(0);
                        let total = params.get("total").and_then(Value::as_u64).unwrap_or(0);
                        let remaining = params
                            .get("remaining")
                            .and_then(Value::as_str)
                            .unwrap_or("");
                        let _ = tray.set_tooltip(Some(format_tooltip(
                            done,
                            total,
                            remaining.to_owned(),
                        )));
                    }
                }
            }
            self.emit("engine-event", notification);
        }
        ping_task.abort();
        let _ = ping_task.await;
        client.close().await;
        #[cfg(unix)]
        procguard::terminate_group(child.id().unwrap_or_default());
        #[cfg(windows)]
        let _ = child.kill().await;
        let _ = child.wait().await;
        self.inner.active_pid.store(0, Ordering::Release);
        *self.inner.guard.lock().await = None;
        *self.inner.rpc.lock().await = None;
        let _ = self.inner.power.release();
        Err(RpcError::Closed)
    }
    pub async fn call(&self, method: &str, params: Value) -> Result<Value, RpcError> {
        let rpc = self
            .inner
            .rpc
            .lock()
            .await
            .clone()
            .ok_or(RpcError::Closed)?;
        let result = rpc.request(method, params, Duration::from_secs(30)).await;
        if matches!(result, Err(RpcError::Timeout | RpcError::Io(_))) {
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
        rpc.notify(method, params).await
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
        let deadline = tokio::time::Instant::now() + Duration::from_secs(10);
        if let Some(rpc) = self.inner.rpc.lock().await.clone() {
            let remaining = deadline.saturating_duration_since(tokio::time::Instant::now());
            let _ = rpc.request("shutdown", json!({}), remaining).await;
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
                let _ = tokio::task::spawn_blocking(move || guard.terminate()).await;
            }
        }
        let _ = self.inner.power.release();
        self.set_status("stopped", None).await;
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
    async fn power_status_loop(&self) {
        loop {
            if self.inner.shutting_down.load(Ordering::Acquire) {
                break;
            }
            if let Err(error) = self.send_power_status().await {
                log::debug!("could not send power status: {error}");
            }
            sleep(Duration::from_secs(30)).await;
        }
    }
    async fn update_power_for_notification(&self, notification: &Value) {
        match job_event_requires_keep_awake(notification) {
            Some(true) => {
                if let Err(error) = self.inner.power.hold() {
                    log::warn!("failed to hold system awake: {error}");
                }
            }
            Some(false) => {
                if let Err(error) = self.inner.power.release() {
                    log::warn!("failed to release system awake hold: {error}");
                }
            }
            None => {}
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
}

fn short_reason(error: &RpcError) -> String {
    let reason = error.to_string();
    reason.chars().take(120).collect()
}

pub fn is_running_job_state(state: &str) -> bool {
    matches!(state, "starting" | "running" | "active" | "in_progress")
}

pub fn job_event_requires_keep_awake(notification: &Value) -> Option<bool> {
    match notification.get("method").and_then(Value::as_str) {
        Some("job/progress") => Some(true),
        Some("job/state" | "job/status") => notification
            .get("params")
            .and_then(|params| params.get("status").or_else(|| params.get("state")))
            .and_then(Value::as_str)
            .map(is_running_job_state),
        _ => None,
    }
}

async fn health_ping<W, F, Fut>(
    client: Arc<Client<W>>,
    target: Arc<dyn HealthKill>,
    mut on_success: F,
) where
    W: tokio::io::AsyncWrite + Unpin + Send,
    F: FnMut() -> Fut,
    Fut: std::future::Future<Output = ()>,
{
    let mut failures = 0;
    loop {
        match client
            .request("health/ping", json!({}), Duration::from_secs(10))
            .await
        {
            Ok(_) => {
                failures = 0;
                on_success().await;
            }
            Err(RpcError::Closed) => break,
            Err(_) => {
                failures += 1;
                if failures >= 3 {
                    target.terminate();
                    break;
                }
            }
        }
        sleep(Duration::from_secs(30)).await;
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
            assert_eq!(target.kills.load(Ordering::Relaxed), 0);
        });
    }

    #[test]
    fn job_events_hold_and_release_power() {
        assert_eq!(
            job_event_requires_keep_awake(&json!({"method":"job/progress"})),
            Some(true)
        );
        assert_eq!(
            job_event_requires_keep_awake(
                &json!({"method":"job/state","params":{"status":"running"}})
            ),
            Some(true)
        );
        assert_eq!(
            job_event_requires_keep_awake(
                &json!({"method":"job/state","params":{"status":"done"}})
            ),
            Some(false)
        );
        assert_eq!(
            job_event_requires_keep_awake(&json!({"method":"other"})),
            None
        );
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
#[allow(dead_code)]
pub fn format_tooltip(done: u64, total: u64, remaining: String) -> String {
    format!(
        "{} / {} · {remaining}",
        format_number(done),
        format_number(total)
    )
}

#[allow(dead_code)]
fn format_number(value: u64) -> String {
    let digits = value.to_string();
    let first = digits.len() % 3;
    if first == 0 {
        return digits;
    }
    let mut result = String::with_capacity(digits.len() + (digits.len() - first) / 3);
    result.push_str(&digits[..first]);
    for chunk in digits.as_bytes()[first..].chunks(3) {
        result.push(',');
        result.push_str(std::str::from_utf8(chunk).expect("numeric string is UTF-8"));
    }
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn strips_api_keys() {
        std::env::set_var("OPENAI_API_KEY", "secret");
        assert!(!minimal_environment().keys().any(|k| k.contains("API_KEY")));
        std::env::remove_var("OPENAI_API_KEY");
    }
    #[test]
    fn tooltip() {
        assert_eq!(
            format_tooltip(4312, 10727, "~9 h 40 m".into()),
            "4,312 / 10,727 · ~9 h 40 m"
        );
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

    #[test]
    fn tooltip_handles_zero_and_large_numbers() {
        assert_eq!(
            format_tooltip(0, 1_000_000, "done".into()),
            "0 / 1,000,000 · done"
        );
    }
}
