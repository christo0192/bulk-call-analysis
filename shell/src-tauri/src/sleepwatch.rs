use crate::engine::Engine;
use serde_json::json;
use std::time::{Duration, SystemTime};

const SLEEP_GAP: Duration = Duration::from_secs(2);

#[cfg(windows)]
pub fn power_message_hint(message: u32) -> Option<&'static str> {
    use windows::Win32::UI::WindowsAndMessaging::{PBT_APMRESUMEAUTOMATIC, PBT_APMSUSPEND};
    match message {
        PBT_APMSUSPEND => Some("os/suspendHint"),
        PBT_APMRESUMEAUTOMATIC => Some("os/resumeHint"),
        _ => None,
    }
}

pub fn gap_hints(previous: SystemTime, now: SystemTime, threshold: Duration) -> Vec<&'static str> {
    match now.duration_since(previous) {
        Ok(gap) if gap > threshold => vec!["os/suspendHint", "os/resumeHint"],
        _ => Vec::new(),
    }
}

pub fn start(engine: Engine) {
    let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
    #[cfg(windows)]
    start_windows(tx.clone());
    #[cfg(target_os = "macos")]
    start_macos(tx.clone());
    tauri::async_runtime::spawn(async move {
        let mut previous = SystemTime::now();
        loop {
            tokio::time::sleep(Duration::from_secs(1)).await;
            let now = SystemTime::now();
            for method in gap_hints(previous, now, SLEEP_GAP) {
                let _ = engine.notify(method, json!({})).await;
            }
            previous = now;
            while let Ok(method) = rx.try_recv() {
                let _ = engine.notify(method, json!({})).await;
            }
        }
    });
}

#[cfg(windows)]
fn start_windows(sender: tokio::sync::mpsc::UnboundedSender<&'static str>) {
    std::thread::spawn(move || {
        use windows::core::w;
        use windows::Win32::Foundation::{HINSTANCE, HWND, LPARAM, LRESULT, WPARAM};
        use windows::Win32::UI::WindowsAndMessaging::*;
        static SENDER: std::sync::OnceLock<tokio::sync::mpsc::UnboundedSender<&'static str>> =
            std::sync::OnceLock::new();
        unsafe extern "system" fn window_proc(
            hwnd: HWND,
            message: u32,
            wparam: WPARAM,
            lparam: LPARAM,
        ) -> LRESULT {
            if message == WM_POWERBROADCAST {
                if let Some(method) = power_message_hint(wparam.0 as u32) {
                    if let Some(sender) = SENDER.get() {
                        let _ = sender.send(method);
                    }
                }
            }
            unsafe { DefWindowProcW(hwnd, message, wparam, lparam) }
        }
        let _ = SENDER.set(sender);
        unsafe {
            use windows::Win32::System::LibraryLoader::GetModuleHandleW;
            let instance = GetModuleHandleW(None).unwrap_or_default();
            let class_name = w!("BcaPowerMessageWindow");
            let class = WNDCLASSEXW {
                cbSize: std::mem::size_of::<WNDCLASSEXW>() as u32,
                lpfnWndProc: Some(window_proc),
                hInstance: HINSTANCE(instance.0),
                lpszClassName: class_name,
                ..Default::default()
            };
            RegisterClassExW(&class);
            let _window = CreateWindowExW(
                Default::default(),
                class_name,
                w!(""),
                Default::default(),
                0,
                0,
                0,
                0,
                HWND_MESSAGE,
                None,
                instance,
                None,
            );
            let mut message = MSG::default();
            while GetMessageW(&mut message, None, 0, 0).as_bool() {
                let _ = TranslateMessage(&message);
                DispatchMessageW(&message);
            }
        }
    });
}

#[cfg(target_os = "macos")]
fn start_macos(sender: tokio::sync::mpsc::UnboundedSender<&'static str>) {
    std::thread::spawn(move || {
        use block2::RcBlock;
        use objc2_app_kit::NSWorkspace;
        use objc2_foundation::{NSNotification, NSNotificationCenter, NSString};
        use std::ptr::NonNull;
        let workspace = unsafe { NSWorkspace::sharedWorkspace() };
        let center: objc2::rc::Retained<NSNotificationCenter> =
            unsafe { workspace.notificationCenter() };
        let will_sleep = NSString::from_str("NSWorkspaceWillSleepNotification");
        let did_wake = NSString::from_str("NSWorkspaceDidWakeNotification");
        let sleep_sender = sender.clone();
        let sleep_block = RcBlock::new(move |_notification: NonNull<NSNotification>| {
            let _ = sleep_sender.send("os/suspendHint");
        });
        let wake_sender = sender;
        let wake_block = RcBlock::new(move |_notification: NonNull<NSNotification>| {
            let _ = wake_sender.send("os/resumeHint");
        });
        unsafe {
            let _sleep_observer = center.addObserverForName_object_queue_usingBlock(
                Some(&will_sleep),
                None,
                None,
                &sleep_block,
            );
            let _wake_observer = center.addObserverForName_object_queue_usingBlock(
                Some(&did_wake),
                None,
                None,
                &wake_block,
            );
            objc2_foundation::NSRunLoop::currentRunLoop().run();
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn gap_detector_emits_suspend_then_resume() {
        let start = SystemTime::UNIX_EPOCH;
        assert_eq!(
            gap_hints(
                start,
                start + Duration::from_secs(3),
                Duration::from_secs(2)
            ),
            vec!["os/suspendHint", "os/resumeHint"]
        );
        assert!(gap_hints(
            start,
            start + Duration::from_secs(2),
            Duration::from_secs(2)
        )
        .is_empty());
    }
    #[cfg(windows)]
    #[test]
    fn power_messages_map_to_engine_hints() {
        use windows::Win32::UI::WindowsAndMessaging::{PBT_APMRESUMEAUTOMATIC, PBT_APMSUSPEND};
        assert_eq!(power_message_hint(PBT_APMSUSPEND), Some("os/suspendHint"));
        assert_eq!(
            power_message_hint(PBT_APMRESUMEAUTOMATIC),
            Some("os/resumeHint")
        );
        assert_eq!(power_message_hint(0), None);
    }
}
