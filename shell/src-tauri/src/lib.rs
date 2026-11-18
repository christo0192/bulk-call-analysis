mod engine;
mod power;
mod procguard;
pub mod rpc;
mod sleepwatch;
mod tray;
use engine::Engine;
use serde_json::Value;
use std::{path::PathBuf, sync::Mutex};
use tauri::RunEvent;
use tauri::{Manager, State};

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
            RunEvent::ExitRequested { api, code, .. } => {
                if code.is_none() {
                    api.prevent_exit();
                }
                if let Some(engine) = app.try_state::<Engine>() {
                    tauri::async_runtime::block_on(engine.shutdown());
                }
            }
            RunEvent::Exit => {
                if let Some(engine) = app.try_state::<Engine>() {
                    tauri::async_runtime::block_on(engine.shutdown());
                }
            }
            _ => {}
        })
}
