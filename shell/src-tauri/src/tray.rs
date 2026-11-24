use crate::engine::Engine;
use serde_json::json;
use tauri::{
    menu::{Menu, MenuItem},
    AppHandle, Manager,
};

pub fn show_main_window(app: &AppHandle) -> tauri::Result<()> {
    let window = match app.get_webview_window("main") {
        Some(window) => window,
        None => tauri::WebviewWindowBuilder::new(
            app,
            "main",
            tauri::WebviewUrl::App("index.html".into()),
        )
        .title("Bulk Call Analysis")
        .inner_size(1280.0, 820.0)
        .min_inner_size(1024.0, 680.0)
        .center()
        .build()?,
    };
    window.show()?;
    window.set_focus()?;
    Ok(())
}

pub fn setup(app: &AppHandle, engine: Engine) -> tauri::Result<()> {
    let open = MenuItem::with_id(app, "open", "Open window", true, None::<&str>)?;
    let pause = MenuItem::with_id(
        app,
        "pause",
        "Pause job",
        engine.active_job_id().is_some(),
        None::<&str>,
    )?;
    let quit = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&open, &pause, &quit])?;
    if let Some(tray) = app.tray_by_id("main") {
        tray.set_menu(Some(menu))?;
        tray.set_tooltip(Some("Bulk Call Analysis"))?;
    }
    app.on_menu_event(move |app, event| match event.id().as_ref() {
        "open" => {
            let _ = show_main_window(app);
        }
        "pause" => {
            let e = engine.clone();
            tauri::async_runtime::spawn(async move {
                if let Some(job) = e.active_job_id() {
                    let _ = e.call("job/pause", json!({"job": job})).await;
                }
            });
        }
        "quit" => {
            let engine = engine.clone();
            crate::handle_tray_quit(engine, || app.exit(0));
        }
        _ => {}
    });
    Ok(())
}
