#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
mod engine;
mod power;
mod procguard;
mod rpc;
mod sleepwatch;
mod tray;
use engine::Engine;
use serde_json::Value;
use tauri::{Manager, State};

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
            if let Some(w) = app.get_webview_window("main") {
                let _ = w.show();
                let _ = w.set_focus();
            }
        }))
        .plugin(
            tauri_plugin_autostart::Builder::new()
                .args(["--autostart"])
                .build(),
        )
        .setup(|app| {
            let engine = Engine::new(app.handle().clone());
            app.manage(engine.clone());
            tray::setup(&app.handle(), engine.clone())?;
            let autostart = std::env::args().any(|arg| arg == "--autostart");
            if !autostart {
                if let Some(w) = app.get_webview_window("main") {
                    let _ = w.show();
                }
            } else if let Some(w) = app.get_webview_window("main") {
                let _ = w.destroy();
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
        .run(tauri::generate_context!())
        .expect("error while running desktop application");
}
