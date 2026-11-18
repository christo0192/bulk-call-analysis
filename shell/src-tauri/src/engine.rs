use crate::procguard;
use crate::rpc::{self, Client, RpcError};
use log::{error, warn};
use serde_json::{json, Value};
use std::{
    collections::VecDeque,
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

#[derive(Clone)]
pub struct Engine {
    inner: Arc<Inner>,
}
struct Inner {
    app: AppHandle,
    rpc: Mutex<Option<Arc<Client<tokio::process::ChildStdin>>>>,
    status: Mutex<String>,
    restarts: Mutex<RestartTracker>,
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

impl Engine {
    pub fn new(app: AppHandle) -> Self {
        Self {
            inner: Arc::new(Inner {
                app,
                rpc: Mutex::new(None),
                status: Mutex::new("starting".into()),
                restarts: Mutex::new(RestartTracker::new()),
            }),
        }
    }
    pub async fn start(&self) {
        let this = self.clone();
        tokio::spawn(async move {
            this.supervise().await;
        });
    }
    async fn supervise(&self) {
        let mut failures = 0u32;
        loop {
            match self.spawn_once().await {
                Ok(()) => {
                    failures = 0;
                }
                Err(e) => {
                    failures += 1;
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
                    if failures > 5 {
                        break;
                    }
                }
            }
        }
    }
    async fn spawn_once(&self) -> Result<(), RpcError> {
        let (program, args) = command_line(&self.inner.app);
        let mut command = Command::new(program);
        command.args(args).env_clear().envs(minimal_environment());
        #[cfg(unix)]
        std::os::unix::process::CommandExt::process_group(&mut command, 0);
        let mut child = command
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::piped())
            .stdin(std::process::Stdio::piped())
            .spawn()?;
        let _guard = procguard::attach(child.id().ok_or(RpcError::Closed)?)?;
        let stdout = child.stdout.take().ok_or(RpcError::Closed)?;
        let stderr = child.stderr.take().ok_or(RpcError::Closed)?;
        let stdin = child.stdin.take().ok_or(RpcError::Closed)?;
        let client = Client::new(stdin);
        *self.inner.rpc.lock().await = Some(client.clone());
        *self.inner.status.lock().await = "ready".into();
        self.emit("engine/status", json!({"status":"ready"}));
        let (tx, mut rx) = mpsc::channel(64);
        let reader_client = client.clone();
        tokio::spawn(async move {
            let mut lines = BufReader::new(stdout).lines();
            while let Ok(Some(line)) = lines.next_line().await {
                match rpc::decode(line.as_bytes()) {
                    Ok(v) => reader_client.dispatch(v, &tx).await,
                    Err(e) => warn!("engine RPC line ignored: {e}"),
                }
            }
        });
        let app = self.inner.app.clone();
        tokio::spawn(async move {
            let mut lines = BufReader::new(stderr).lines();
            while let Ok(Some(line)) = lines.next_line().await {
                if let Ok(path) = app.path().app_log_dir() {
                    let _ = std::fs::create_dir_all(&path);
                    let _ = std::fs::OpenOptions::new()
                        .create(true)
                        .append(true)
                        .open(path.join("engine.log"))
                        .and_then(|mut f| {
                            std::io::Write::write_all(&mut f, format!("{line}\n").as_bytes())
                        });
                }
            }
        });
        let ping = client.clone();
        tokio::spawn(async move {
            loop {
                sleep(Duration::from_secs(30)).await;
                if ping
                    .request("health/ping", json!({}), Duration::from_secs(10))
                    .await
                    .is_err()
                {
                    break;
                }
            }
        });
        while let Some(notification) = rx.recv().await {
            self.emit("engine-event", notification);
        }
        let _ = client
            .request("shutdown", json!({}), Duration::from_secs(10))
            .await;
        #[cfg(unix)]
        procguard::terminate_group(child.id().unwrap_or_default());
        #[cfg(windows)]
        let _ = child.kill().await;
        #[cfg(unix)]
        let _ = child.wait().await;
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
    fn emit(&self, event: &str, payload: Value) {
        let _ = self.inner.app.emit(event, payload);
    }
}
pub fn command_line(app: &AppHandle) -> (String, Vec<String>) {
    if let Ok(value) = std::env::var("BCA_ENGINE_CMD") {
        let mut words = value.split_whitespace();
        return (
            words.next().unwrap_or("python").into(),
            words.map(str::to_owned).collect(),
        );
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
