mod engine;
mod power;
mod procguard;
pub mod rpc;
mod sleepwatch;
mod tray;
use serde_json::Value;
use std::{
    path::PathBuf,
    sync::atomic::{AtomicBool, Ordering},
    sync::Mutex,
};
use tauri::RunEvent;
use tauri::{Manager, State};

#[derive(Debug, PartialEq, Eq)]
enum ExitAction {
    KeepRunning,
    Shutdown,
}

fn exit_action(code: Option<i32>) -> ExitAction {
    if code.is_some() {
        ExitAction::Shutdown
    } else {
        ExitAction::KeepRunning
    }
}

#[doc(hidden)]
pub use engine::{is_running_job_state, job_event_requires_keep_awake, Engine};

static SHUTDOWN_STARTED: AtomicBool = AtomicBool::new(false);

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

#[tauri::command]
async fn engine_call(
    engine: State<'_, Engine>,
    method: String,
    params: Value,
) -> Result<Value, String> {
    engine
        .call(&method, params)
        .await
        .map_err(|e| e.to_string())
}
#[tauri::command]
async fn engine_status(engine: State<'_, Engine>) -> Result<String, String> {
    Ok(engine.status().await)
}

pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            let _ = crate::tray::show_main_window(app);
        }))
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
        .invoke_handler(tauri::generate_handler![engine_call, engine_status])
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { .. } = event {
                let _ = window.destroy();
            }
        })
        .build(tauri::generate_context!())
        .expect("error while building desktop application")
        .run(|app, event| match event {
            RunEvent::ExitRequested { api, code, .. } => match exit_action(code) {
                ExitAction::KeepRunning => api.prevent_exit(),
                ExitAction::Shutdown => {
                    if start_shutdown(app.clone()) {
                        api.prevent_exit();
                        hide_windows(app);
                    }
                }
            },
            RunEvent::Exit => {
                hide_windows(app);
                let _ = start_shutdown(app.clone());
            }
            _ => {}
        })
}

fn hide_windows(app: &tauri::AppHandle) {
    for window in app.webview_windows().values() {
        let _ = window.hide();
    }
}

fn start_shutdown(app: tauri::AppHandle) -> bool {
    if SHUTDOWN_STARTED.swap(true, Ordering::AcqRel) {
        return false;
    }
    if let Some(engine) = app.try_state::<Engine>() {
        let engine = engine.inner().clone();
        let app = app.clone();
        tauri::async_runtime::spawn(async move {
            engine.shutdown().await;
            app.exit(0);
        });
    }
    true
}

#[cfg(test)]
mod tests {
    use super::{exit_action, ExitAction};

    #[test]
    fn closing_last_window_keeps_engine_running() {
        assert_eq!(exit_action(None), ExitAction::KeepRunning);
        assert_eq!(exit_action(Some(0)), ExitAction::Shutdown);
    }
}
