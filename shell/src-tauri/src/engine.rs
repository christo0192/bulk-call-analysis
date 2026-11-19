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
use tauri::{AppHandle, Emitter, Manager};
use tokio::{
    io::{AsyncBufReadExt, BufReader},
    process::Command,
    sync::{mpsc, Mutex},
    time::sleep,
};

pub struct Engine {
    inner: Arc<Inner>,
}
struct Inner {
    app: AppHandle,
    rpc: Mutex<Option<Arc<Client<tokio::process::ChildStdin>>>>,
    status: Mutex<String>,
    restarts: Mutex<RestartTracker>,
    active_pid: AtomicU32,
    shutting_down: AtomicBool,
    guard: Mutex<Option<Arc<procguard::Guard>>>,
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

impl Clone for Engine {
    fn clone(&self) -> Self {
        Self {
            inner: self.inner.clone(),
        }
    }
}

impl Engine {
    pub fn new(app: AppHandle) -> Self {
        Self {
            inner: Arc::new(Inner {
                app,
                rpc: Mutex::new(None),
                status: Mutex::new("starting".into()),
                restarts: Mutex::new(RestartTracker::new()),
                active_pid: AtomicU32::new(0),
                shutting_down: AtomicBool::new(false),
                guard: Mutex::new(None),
            }),
        }
    }
    pub fn start(&self) {
        let this = self.clone();
        tauri::async_runtime::spawn(async move {
            this.supervise().await;
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
                    *self.inner.status.lock().await = "error".into();
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
                    sleep(wait).await;
                }
            }
        }
    }
    async fn spawn_once(&self) -> Result<(), RpcError> {
        let (program, args) = command_line(&self.inner.app);
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
                if let Ok(path) = app.path().app_log_dir() {
                    let _ = std::fs::create_dir_all(&path);
                    let _ = std::fs::OpenOptions::new()
                        .create(true)
                        .append(true)
                        .open(path.join("engine.log"))
                        .and_then(|mut f| std::io::Write::write_all(&mut f, line.as_bytes()));
                }
            }
        });
        let ping = client.clone();
        let ping_engine = self.clone();
        tokio::spawn(async move {
            let mut failures = 0;
            loop {
                if ping
                    .request("health/ping", json!({}), Duration::from_secs(10))
                    .await
                    .is_ok()
                {
                    failures = 0;
                    if *ping_engine.inner.status.lock().await != "ready" {
                        *ping_engine.inner.status.lock().await = "ready".into();
                        ping_engine.emit("engine/status", json!({"status":"ready"}));
                    }
                } else {
                    failures += 1;
                    if failures >= 3 {
                        procguard::terminate_process(pid);
                        break;
                    }
                }
                sleep(Duration::from_secs(30)).await;
            }
        });
        while let Some(notification) = rx.recv().await {
            if notification.get("method").and_then(Value::as_str) == Some("job/progress") {
                if let Some(tray) = self.inner.app.tray_by_id("main") {
                    let params = notification.get("params").cloned().unwrap_or_default();
                    let done = params.get("done").and_then(Value::as_u64).unwrap_or(0);
                    let total = params.get("total").and_then(Value::as_u64).unwrap_or(0);
                    let remaining = params
                        .get("remaining")
                        .and_then(Value::as_str)
                        .unwrap_or("");
                    let _ =
                        tray.set_tooltip(Some(format_tooltip(done, total, remaining.to_owned())));
                }
            }
            self.emit("engine-event", notification);
        }
        client.close().await;
        #[cfg(unix)]
        procguard::terminate_group(child.id().unwrap_or_default());
        #[cfg(windows)]
        let _ = child.kill().await;
        let _ = child.wait().await;
        self.inner.active_pid.store(0, Ordering::Release);
        *self.inner.guard.lock().await = None;
        *self.inner.rpc.lock().await = None;
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
        rpc.request(method, params, Duration::from_secs(30)).await
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
    pub async fn shutdown(&self) {
        if self.inner.shutting_down.swap(true, Ordering::AcqRel) {
            return;
        }
        if let Some(rpc) = self.inner.rpc.lock().await.clone() {
            let _ = rpc
                .request("shutdown", json!({}), Duration::from_secs(10))
                .await;
        }
        let stopped = tokio::time::timeout(Duration::from_secs(10), async {
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
    }
    fn emit(&self, event: &str, payload: Value) {
        let _ = self.inner.app.emit(event, payload);
    }
}

#[doc(hidden)]
pub struct TestEngine {
    client: Arc<Client<tokio::process::ChildStdin>>,
    child: tokio::process::Child,
    guard: Arc<procguard::Guard>,
    reader: tokio::task::JoinHandle<()>,
}

#[doc(hidden)]
pub async fn spawn_for_test(program: String, args: Vec<String>) -> Result<TestEngine, RpcError> {
    let mut child = configured_command(program, args)
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::null())
        .stdin(std::process::Stdio::piped())
        .spawn()?;
    let pid = child.id().ok_or(RpcError::Closed)?;
    let guard = Arc::new(procguard::attach(pid)?);
    let stdout = child.stdout.take().ok_or(RpcError::Closed)?;
    let stdin = child.stdin.take().ok_or(RpcError::Closed)?;
    let client = Client::new(stdin);
    let reader_client = client.clone();
    let reader = tokio::spawn(async move {
        let mut reader = BufReader::new(stdout);
        let mut line = Vec::new();
        let notifications = mpsc::channel(1).0;
        while let Ok(size) = reader.read_until(b'\n', &mut line).await {
            if size == 0 {
                break;
            }
            if let Ok(value) = rpc::decode(&line) {
                reader_client.dispatch(value, &notifications).await;
            }
            line.clear();
        }
        reader_client.close().await;
    });
    Ok(TestEngine {
        client,
        child,
        guard,
        reader,
    })
}

impl TestEngine {
    pub fn pid(&self) -> u32 {
        self.child.id().unwrap_or_default()
    }

    pub async fn call(&self, method: &str, params: Value) -> Result<Value, RpcError> {
        self.client
            .request(method, params, Duration::from_secs(20))
            .await
    }

    pub async fn shutdown(mut self) {
        let _ = self
            .client
            .request("shutdown", json!({}), Duration::from_secs(20))
            .await;
        if tokio::time::timeout(Duration::from_secs(10), self.child.wait())
            .await
            .is_err()
        {
            self.guard.terminate();
            let _ = self.child.wait().await;
        } else {
            self.guard.terminate();
        }
        let _ = self.reader.await;
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

pub fn command_line(app: &AppHandle) -> (String, Vec<String>) {
    if let Ok(program) = std::env::var("BCA_ENGINE_PROGRAM") {
        let args = std::env::var("BCA_ENGINE_ARGS")
            .ok()
            .and_then(|value| serde_json::from_str(&value).ok())
            .unwrap_or_default();
        return (program, args);
    }
    let root = app
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
