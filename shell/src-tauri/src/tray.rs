use crate::engine::Engine;
use serde_json::json;
use tauri::{menu::{Menu, MenuItem}, tray::TrayIconBuilder, AppHandle, Manager};

pub fn setup(app:&AppHandle, engine:Engine)->tauri::Result<()> { let open=MenuItem::with_id(app,"open","Open window",true,None::<&str>)?; let pause=MenuItem::with_id(app,"pause","Pause job",true,None::<&str>)?; let quit=MenuItem::with_id(app,"quit","Quit",true,None::<&str>)?; let menu=Menu::with_items(app,&[&open,&pause,&quit])?; let _tray=TrayIconBuilder::new().menu(&menu).tooltip("Bulk Call Analysis").on_menu_event(move |app,event|{match event.id().as_ref(){"open"=>{if let Some(w)=app.get_webview_window("main"){let _=w.show();let _=w.set_focus();}},"pause"=>{let e=engine.clone();tauri::async_runtime::spawn(async move{let _=e.call("job/pause",json!({})).await;});},"quit"=>app.exit(0),_=>{}}}).build(app)?; Ok(()) }
