mod engine;
mod notifications;
mod paths;
mod power;
mod procguard;
pub mod rpc;
mod sleepwatch;
mod tray;
mod tray_model;
use serde::Serialize;
use serde_json::{json, Value};
use std::time::{Duration, Instant};
use std::{
    path::PathBuf,
    sync::atomic::{AtomicBool, Ordering},
    sync::Mutex,
    sync::OnceLock,
};
use tauri::RunEvent;
use tauri::{Manager, State};
use tokio::sync::Notify;

#[derive(Debug, PartialEq, Eq)]
enum ExitAction {
    KeepRunning,
    Shutdown,
}

#[derive(Debug, PartialEq, Eq)]
enum RunEventAction {
    KeepRunning,
    StartShutdown,
    WaitForShutdown,
}

fn exit_action(code: Option<i32>) -> ExitAction {
    if code.is_some() {
        ExitAction::Shutdown
    } else {
        ExitAction::KeepRunning
    }
}

fn run_event_action(is_exit: bool, code: Option<i32>) -> RunEventAction {
    if is_exit {
        RunEventAction::WaitForShutdown
    } else {
        match exit_action(code) {
            ExitAction::KeepRunning => RunEventAction::KeepRunning,
            ExitAction::Shutdown => RunEventAction::StartShutdown,
        }
    }
}

#[doc(hidden)]
pub use engine::Engine;

static SHUTDOWN_STARTED: AtomicBool = AtomicBool::new(false);
static SHUTDOWN_DONE: OnceLock<std::sync::Arc<Notify>> = OnceLock::new();

struct AppLogger(Mutex<PathBuf>);
impl log::Log for AppLogger {
    fn enabled(&self, metadata: &log::Metadata<'_>) -> bool {
        metadata.level() <= log::Level::Info
    }
    fn log(&self, record: &log::Record<'_>) {
        if self.enabled(record.metadata()) {
            if let Ok(path) = self.0.lock() {
                if let Ok(mut file) = std::fs::OpenOptions::new()
                    .create(true)
                    .append(true)
                    .open(&*path)
                {
                    use std::io::Write;
                    let _ = writeln!(file, "{} {}", record.level(), record.args());
                }
            }
        }
    }
    fn flush(&self) {}
}

#[derive(Debug, Serialize)]
struct CommandError {
    code: Option<i64>,
    message: String,
    data: Option<Value>,
}

impl From<rpc::RpcError> for CommandError {
    fn from(error: rpc::RpcError) -> Self {
        let code = match &error {
            rpc::RpcError::Remote { code, .. } => Some(*code),
            _ => None,
        };
        let (message, data) = match error {
            rpc::RpcError::Timeout => ("RPC timed out".into(), Some(json!({"reason":"timeout"}))),
            rpc::RpcError::Closed
            | rpc::RpcError::Io(_)
            | rpc::RpcError::Malformed(_)
            | rpc::RpcError::WriteTimeout => (
                "The engine is unavailable".into(),
                Some(json!({"reason":"engine_unavailable"})),
            ),
            rpc::RpcError::Remote { message, data, .. } => (message, data),
        };
        Self {
            code,
            message,
            data,
        }
    }
}

const UI_METHODS: &[&str] = &[
    "app/status",
    "setup/checklist",
    "setup/runChecks",
    "setup/markManual",
    "settings/get",
    "settings/set",
    "projects/list",
    "projects/create",
    "projects/jobs",
    "files/list",
    "ids/attach",
    "chat/send",
    "chat/stop",
    "chat/history",
    "job/status",
    "job/snapshot",
    "job/plan",
    "job/sample",
    "job/outputs",
    "job/approvePlan",
    "job/approveSample",
    "job/startFull",
    "job/pause",
    "job/resume",
    "job/rebuildRules",
    "auth/claudeLogin",
    "auth/codexLogin",
    "auth/status",
    "allowance/status",
];

fn ui_method_allowed(method: &str) -> bool {
    UI_METHODS.contains(&method)
}

fn method_not_allowed_error() -> CommandError {
    CommandError {
        code: Some(-32601),
        message: "Method not found".into(),
        data: Some(json!({"reason":"method_not_allowed"})),
    }
}

#[tauri::command]
async fn engine_call(
    engine: State<'_, Engine>,
    method: String,
    params: Value,
) -> Result<Value, CommandError> {
    if !ui_method_allowed(&method) {
        return Err(method_not_allowed_error());
    }
    engine
        .call(&method, params)
        .await
        .map_err(CommandError::from)
}
#[tauri::command]
async fn engine_status(engine: State<'_, Engine>) -> Result<String, String> {
    Ok(engine.status().await)
}

const PROJECT_EXTENSIONS: &[&str] = &["xlsx", "md", "csv", "html"];

fn valid_project_slug(project: &str) -> bool {
    let bytes = project.as_bytes();
    (4..=64).contains(&bytes.len())
        && bytes[0].is_ascii_lowercase()
        && (bytes[bytes.len() - 1].is_ascii_lowercase() || bytes[bytes.len() - 1].is_ascii_digit())
        && bytes[1..bytes.len() - 1]
            .iter()
            .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || *byte == b'-')
}

fn malformed_engine_reply() -> CommandError {
    CommandError {
        code: None,
        message: "The engine is unavailable".into(),
        data: Some(json!({"reason":"engine_unavailable"})),
    }
}

fn is_allowed_project_file(project_dir: &std::path::Path, path: &std::path::Path) -> bool {
    let Ok(relative) = path.strip_prefix(project_dir) else {
        return false;
    };
    let parts: Vec<_> = relative.components().collect();
    let name = path.file_name().and_then(|v| v.to_str()).unwrap_or("");
    if name.eq_ignore_ascii_case("summary.md") && parts.len() == 1 {
        return true;
    }
    parts.len() == 4
        && parts[0]
            .as_os_str()
            .to_string_lossy()
            .eq_ignore_ascii_case("jobs")
        && parts[2]
            .as_os_str()
            .to_string_lossy()
            .eq_ignore_ascii_case("output")
        && matches!(
            path.extension()
                .and_then(|v| v.to_str())
                .unwrap_or("")
                .to_ascii_lowercase()
                .as_str(),
            "xlsx" | "md" | "csv" | "html"
        )
}

async fn projects_root<R: tauri::Runtime>(
    engine: &Engine<R>,
    project: &str,
) -> Result<PathBuf, CommandError> {
    if !valid_project_slug(project) {
        return Err(path_error(paths::OpenError::InvalidSlug));
    }
    let status = engine
        .call("app/status", json!({}))
        .await
        .map_err(CommandError::from)?;
    let data_dir = status
        .get("dataDir")
        .and_then(Value::as_str)
        .ok_or_else(malformed_engine_reply)?;
    let data_dir = PathBuf::from(data_dir);
    let projects = data_dir.join("projects");
    if data_dir.as_os_str().is_empty() || paths::is_unsafe_path(&data_dir) {
        return Err(path_error(paths::OpenError::InvalidPath));
    }
    let root =
        dunce::canonicalize(&projects).map_err(|_| path_error(paths::OpenError::FileNotFound))?;
    let project_dir = dunce::canonicalize(root.join(project))
        .map_err(|_| path_error(paths::OpenError::FileNotFound))?;
    if project_dir.parent() != Some(root.as_path()) || !project_dir.is_dir() {
        return Err(path_error(paths::OpenError::OutsideProject));
    }
    Ok(project_dir)
}

#[tauri::command]
async fn open_project_file(
    app: tauri::AppHandle,
    engine: State<'_, Engine>,
    project: String,
    name: String,
) -> Result<(), CommandError> {
    if !valid_project_slug(&project) {
        return Err(path_error(paths::OpenError::InvalidSlug));
    }
    let result = engine
        .call("files/path", json!({"project": project, "name": name}))
        .await
        .map_err(CommandError::from)?;
    let project_dir = projects_root(&engine, &project).await?;
    let candidate = result
        .get("path")
        .and_then(Value::as_str)
        .ok_or_else(malformed_engine_reply)?;
    let path = paths::validate_project_path(
        &project_dir,
        std::path::Path::new(candidate),
        PROJECT_EXTENSIONS,
    )
    .map_err(path_error)?;
    if !is_allowed_project_file(&project_dir, &path) {
        return Err(path_error(paths::OpenError::TypeNotAllowed));
    }
    // The canonical path was checked immediately before this call; a same-user
    // process can still replace a directory in the remaining opener window.
    tauri_plugin_opener::OpenerExt::opener(&app)
        .open_path(path.to_string_lossy().into_owned(), None::<&str>)
        .map_err(|error| CommandError {
            code: None,
            message: error.to_string(),
            data: Some(json!({"reason":"open_failed"})),
        })
}

#[tauri::command]
async fn open_project_folder(
    app: tauri::AppHandle,
    engine: State<'_, Engine>,
    project: String,
) -> Result<(), CommandError> {
    let path = projects_root(&engine, &project).await?;
    let path = paths::validate_project_folder(&path).map_err(path_error)?;
    tauri_plugin_opener::OpenerExt::opener(&app)
        .open_path(path.to_string_lossy().into_owned(), None::<&str>)
        .map_err(|error| CommandError {
            code: None,
            message: error.to_string(),
            data: Some(json!({"reason":"open_failed"})),
        })
}

fn path_error(error: paths::OpenError) -> CommandError {
    let reason = match error {
        paths::OpenError::FileNotFound => "file_not_found",
        paths::OpenError::OutsideProject => "outside_project",
        paths::OpenError::TypeNotAllowed => "type_not_allowed",
        paths::OpenError::InvalidPath => "outside_project",
        paths::OpenError::InvalidSlug => "invalid_slug",
    };
    CommandError {
        code: None,
        message: error.to_string(),
        data: Some(json!({"reason": reason})),
    }
}

pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            let _ = crate::tray::show_main_window(app);
        }))
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_notification::init())
        .plugin(
            tauri_plugin_autostart::Builder::new()
                .args(["--autostart"])
                .build(),
        )
        .setup(|app| {
            let log_dir = app.path().app_log_dir()?;
            std::fs::create_dir_all(&log_dir)?;
            log::set_logger(Box::leak(Box::new(AppLogger(Mutex::new(
                log_dir.join("app.log"),
            )))))
            .map_err(|_| tauri::Error::AssetNotFound("app logger".into()))?;
            log::set_max_level(log::LevelFilter::Info);
            let engine = Engine::new(app.handle().clone());
            app.manage(engine.clone());
            tray::setup(app.handle(), engine.clone())?;
            let autostart = std::env::args().any(|arg| arg == "--autostart");
            if !autostart {
                tray::show_main_window(app.handle())?;
            }
            engine.start();
            sleepwatch::start(engine.clone());
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            engine_call,
            engine_status,
            open_project_file,
            open_project_folder
        ])
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { .. } = event {
                let _ = window.destroy();
            }
        })
        .build(tauri::generate_context!())
        .expect("error while building desktop application")
        .run(|app, event| match event {
            RunEvent::ExitRequested { api, code, .. } => match run_event_action(false, code) {
                RunEventAction::KeepRunning => api.prevent_exit(),
                RunEventAction::StartShutdown => {
                    if start_shutdown(app.clone()) {
                        api.prevent_exit();
                        hide_windows(app);
                    }
                }
                RunEventAction::WaitForShutdown => unreachable!("ExitRequested cannot wait"),
            },
            RunEvent::Exit => {
                hide_windows(app);
                handle_exit(
                    app.try_state::<Engine>().map(|engine| engine.inner()),
                    SHUTDOWN_STARTED.load(Ordering::Acquire),
                    SHUTDOWN_DONE.get().map(std::sync::Arc::as_ref),
                );
            }
            _ => {}
        })
}

fn handle_exit<R: tauri::Runtime>(
    engine: Option<&Engine<R>>,
    shutdown_started: bool,
    done: Option<&Notify>,
) {
    let exit_deadline = Instant::now() + Duration::from_secs(10);
    if !shutdown_started {
        if let Some(engine) = engine {
            let remaining = exit_deadline.saturating_duration_since(Instant::now());
            let completed = tauri::async_runtime::block_on(async move {
                tokio::time::timeout(remaining, engine.shutdown()).await
            })
            .is_ok();
            if !completed {
                tauri::async_runtime::block_on(async move { engine.force_terminate().await });
            }
        }
    } else if let Some(done) = done {
        let remaining = exit_deadline.saturating_duration_since(Instant::now());
        if tauri::async_runtime::block_on(async move {
            tokio::time::timeout(remaining, done.notified()).await
        })
        .is_err()
        {
            if let Some(engine) = engine {
                tauri::async_runtime::block_on(async move { engine.force_terminate().await });
            }
        }
    }
}

pub(crate) fn handle_tray_quit<R: tauri::Runtime>(engine: Engine<R>, exit: impl FnOnce()) {
    let done = SHUTDOWN_DONE
        .get_or_init(|| std::sync::Arc::new(Notify::new()))
        .clone();
    if SHUTDOWN_STARTED.swap(true, Ordering::AcqRel) {
        exit();
        return;
    }
    tauri::async_runtime::block_on(async { engine.shutdown().await });
    done.notify_one();
    exit();
}

fn hide_windows(app: &tauri::AppHandle) {
    for window in app.webview_windows().values() {
        let _ = window.hide();
    }
}

fn start_shutdown(app: tauri::AppHandle) -> bool {
    let done = SHUTDOWN_DONE
        .get_or_init(|| std::sync::Arc::new(Notify::new()))
        .clone();
    if SHUTDOWN_STARTED.swap(true, Ordering::AcqRel) {
        return false;
    }
    if let Some(engine) = app.try_state::<Engine>() {
        let engine = engine.inner().clone();
        let app = app.clone();
        tauri::async_runtime::spawn(async move {
            engine.shutdown().await;
            done.notify_one();
            app.exit(0);
        });
    }
    true
}

#[cfg(test)]
mod tests {
    use super::{
        exit_action, handle_exit, handle_tray_quit, is_allowed_project_file,
        method_not_allowed_error, run_event_action, ui_method_allowed, CommandError, Engine,
        ExitAction, RunEventAction, SHUTDOWN_STARTED,
    };
    use crate::rpc::RpcError;
    use serde_json::json;
    use std::time::{Duration, Instant};
    use tokio::sync::Notify;

    #[test]
    fn allowlist_accepts_ui_methods() {
        assert!(ui_method_allowed("chat/send"));
        assert!(ui_method_allowed("allowance/status"));
        assert!(ui_method_allowed("ids/attach"));
    }

    #[test]
    fn allowlist_rejects_internal_methods() {
        assert!(!ui_method_allowed("shutdown"));
        assert!(!ui_method_allowed("files/path"));
        assert!(!ui_method_allowed("health/ping"));
    }

    #[test]
    fn allowlist_is_case_sensitive() {
        assert!(!ui_method_allowed("Chat/send"));
        assert!(!ui_method_allowed("APP/status"));
    }

    #[test]
    fn open_project_folder_rejects_bad_slugs_unc_and_device_paths() {
        assert!(!super::valid_project_slug("Bad Project"));
        assert!(!super::valid_project_slug("bad"));
        for value in ["//host/share", r"\\?\C:\projects", r"\\.\C:\projects"] {
            assert!(crate::paths::is_unsafe_path(std::path::Path::new(value)));
        }
    }

    #[test]
    fn allowlist_accepts_every_listed_output_and_refuses_the_rest() {
        let root = std::path::Path::new("project");
        for path in [
            "jobs/j1/output/results.v1.xlsx",
            "jobs/j1/output/results.v1.rebuild2.xlsx",
            "jobs/j1/output/verify.v1.md",
            "summary.md",
        ] {
            assert!(is_allowed_project_file(root, &root.join(path)), "{path}");
        }
        for path in [
            "jobs/j1/raw/call.json",
            "jobs/j1/masked/call.json",
            "jobs/j1/chat/chat.json",
            "inputs/source.xlsx",
            "jobs/j1/output/results.v1.rules.json",
            "jobs/j1/output/reconciliation.v1.json",
            "jobs/j1/output/state.sqlite3",
            "jobs/j1/output/x.xlsx.exe",
            "jobs/j1/output/X.XLSX:stream",
            "JOBS/j1/RAW/call.json",
            "Jobs/j1/Masked/call.json",
            "jobs/j1/Chat/chat.json",
        ] {
            assert!(!is_allowed_project_file(root, &root.join(path)), "{path}");
        }
    }

    #[test]
    fn rejected_method_has_structured_error() {
        let error = method_not_allowed_error();
        assert_eq!(error.code, Some(-32601));
        assert_eq!(error.data, Some(json!({"reason":"method_not_allowed"})));
    }

    #[test]
    fn local_errors_have_structured_reasons() {
        let timeout = CommandError::from(RpcError::Timeout);
        assert_eq!(timeout.code, None);
        assert_eq!(timeout.data, Some(json!({"reason":"timeout"})));
        let closed = CommandError::from(RpcError::Closed);
        assert_eq!(closed.data, Some(json!({"reason":"engine_unavailable"})));
    }

    #[test]
    fn remote_error_preserves_code_message_and_data() {
        let error = CommandError::from(RpcError::Remote {
            code: -32001,
            message: "failed".into(),
            data: Some(json!({"reason":"safe"})),
        });
        assert_eq!(error.code, Some(-32001));
        assert_eq!(error.message, "failed");
        assert_eq!(error.data, Some(json!({"reason":"safe"})));
    }

    #[test]
    fn closing_last_window_keeps_engine_running() {
        assert_eq!(exit_action(None), ExitAction::KeepRunning);
        assert_eq!(exit_action(Some(0)), ExitAction::Shutdown);
    }

    #[test]
    fn exit_event_requires_waiting_for_shutdown() {
        assert_eq!(
            run_event_action(true, None),
            RunEventAction::WaitForShutdown
        );
        assert_eq!(run_event_action(false, None), RunEventAction::KeepRunning);
        assert_eq!(
            run_event_action(false, Some(0)),
            RunEventAction::StartShutdown
        );
    }

    #[test]
    fn exit_handler_does_not_require_an_entered_tokio_runtime() {
        let engine = Engine::<tauri::Wry>::new_without_app();
        let started = Instant::now();
        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            handle_exit(Some(&engine), false, None);
        }));
        assert!(result.is_ok());
        assert!(started.elapsed() < Duration::from_secs(1));
    }

    #[test]
    fn tray_quit_handler_does_not_require_an_entered_tokio_runtime() {
        SHUTDOWN_STARTED.store(false, std::sync::atomic::Ordering::Release);
        let engine = Engine::<tauri::Wry>::new_without_app();
        let called = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
        let called_by_exit = called.clone();
        let started = Instant::now();
        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            handle_tray_quit(engine, || {
                called_by_exit.store(true, std::sync::atomic::Ordering::Release)
            });
        }));
        assert!(result.is_ok());
        assert!(called.load(std::sync::atomic::Ordering::Acquire));
        assert!(SHUTDOWN_STARTED.load(std::sync::atomic::Ordering::Acquire));
        assert!(started.elapsed() < Duration::from_secs(1));
    }

    #[test]
    fn exit_handler_waits_for_an_existing_shutdown_without_a_runtime() {
        let done = Notify::new();
        done.notify_one();
        let started = Instant::now();
        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            handle_exit::<tauri::Wry>(None, true, Some(&done));
        }));
        assert!(result.is_ok());
        assert!(started.elapsed() < Duration::from_secs(1));
    }
}
