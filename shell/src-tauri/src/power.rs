#![allow(dead_code)]

use std::sync::Mutex;
use thiserror::Error;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PowerStatus {
    pub on_ac: Option<bool>,
    pub percent: Option<u8>,
}

#[derive(Debug, Error)]
pub enum PowerError {
    #[error("power request failed: {0}")]
    Failed(String),
}

pub trait PowerOs: Send + Sync {
    fn hold(&self) -> Result<(), PowerError>;
    fn release(&self) -> Result<(), PowerError>;
    fn status(&self) -> PowerStatus;
}

#[cfg(not(any(windows, target_os = "macos")))]
struct PlatformPower;
#[cfg(not(any(windows, target_os = "macos")))]
impl PowerOs for PlatformPower {
    fn hold(&self) -> Result<(), PowerError> {
        Ok(())
    }
    fn release(&self) -> Result<(), PowerError> {
        Ok(())
    }
    fn status(&self) -> PowerStatus {
        PowerStatus {
            on_ac: None,
            percent: None,
        }
    }
}

#[cfg(windows)]
struct PlatformPower {
    request: Mutex<Option<windows::Win32::Foundation::HANDLE>>,
}

#[cfg(windows)]
unsafe impl Send for PlatformPower {}
#[cfg(windows)]
unsafe impl Sync for PlatformPower {}

#[cfg(windows)]
fn windows_status(raw: windows::Win32::System::Power::SYSTEM_POWER_STATUS) -> PowerStatus {
    PowerStatus {
        on_ac: match raw.ACLineStatus {
            0 => Some(false),
            1 => Some(true),
            _ => None,
        },
        percent: (raw.BatteryLifePercent != 255).then_some(raw.BatteryLifePercent),
    }
}

#[cfg(windows)]
impl PowerOs for PlatformPower {
    fn hold(&self) -> Result<(), PowerError> {
        use windows::Win32::System::Power::{
            PowerCreateRequest, PowerRequestExecutionRequired, PowerRequestSystemRequired,
            PowerSetRequest,
        };
        use windows::Win32::System::Threading::{
            POWER_REQUEST_CONTEXT_SIMPLE_STRING, REASON_CONTEXT, REASON_CONTEXT_0,
        };
        let mut request = self.request.lock().unwrap();
        if request.is_some() {
            return Ok(());
        }
        let mut reason: Vec<u16> = "Bulk processing".encode_utf16().chain([0]).collect();
        let context = REASON_CONTEXT {
            Version: 0,
            Flags: POWER_REQUEST_CONTEXT_SIMPLE_STRING,
            Reason: REASON_CONTEXT_0 {
                SimpleReasonString: windows::core::PWSTR(reason.as_mut_ptr()),
            },
        };
        let handle = unsafe { PowerCreateRequest(&context) }
            .map_err(|e| PowerError::Failed(e.to_string()))?;
        let result = unsafe {
            PowerSetRequest(handle, PowerRequestSystemRequired)
                .and_then(|_| PowerSetRequest(handle, PowerRequestExecutionRequired))
        };
        if let Err(error) = result {
            unsafe {
                let _ = windows::Win32::Foundation::CloseHandle(handle);
            }
            return Err(PowerError::Failed(error.to_string()));
        }
        *request = Some(handle);
        Ok(())
    }
    fn release(&self) -> Result<(), PowerError> {
        use windows::Win32::System::Power::{
            PowerClearRequest, PowerRequestExecutionRequired, PowerRequestSystemRequired,
        };
        let mut request = self.request.lock().unwrap();
        let Some(handle) = request.take() else {
            return Ok(());
        };
        let mut failure = unsafe { PowerClearRequest(handle, PowerRequestSystemRequired).err() };
        if let Some(error) =
            unsafe { PowerClearRequest(handle, PowerRequestExecutionRequired).err() }
        {
            failure.get_or_insert(error);
        }
        if let Err(error) = unsafe { windows::Win32::Foundation::CloseHandle(handle) } {
            failure.get_or_insert(error);
        }
        failure.map_or(Ok(()), |e| Err(PowerError::Failed(e.to_string())))
    }
    fn status(&self) -> PowerStatus {
        let mut raw = windows::Win32::System::Power::SYSTEM_POWER_STATUS::default();
        if unsafe { windows::Win32::System::Power::GetSystemPowerStatus(&mut raw) }.is_ok() {
            windows_status(raw)
        } else {
            PowerStatus {
                on_ac: None,
                percent: None,
            }
        }
    }
}

#[cfg(target_os = "macos")]
struct PlatformPower {
    assertion: Mutex<Option<u32>>,
    app_nap_activity: Mutex<Option<objc2::rc::Retained<objc2_foundation::NSObject>>>,
}

// SAFETY: NSProcessInfo activity tokens are Objective-C objects whose lifetime
// is retained here, and Apple documents that an activity may be ended from any
// thread. Access to the token and IOKit assertion is serialized by the mutexes.
#[cfg(target_os = "macos")]
unsafe impl Send for PlatformPower {}
#[cfg(target_os = "macos")]
unsafe impl Sync for PlatformPower {}

#[cfg(target_os = "macos")]
fn mac_status(kind: Option<&str>, percent: Option<u8>) -> PowerStatus {
    PowerStatus {
        on_ac: kind.map(|value| value != "Battery Power"),
        percent,
    }
}

#[cfg(target_os = "macos")]
impl PowerOs for PlatformPower {
    fn hold(&self) -> Result<(), PowerError> {
        use objc2_foundation::{NSActivityOptions, NSProcessInfo, NSString};
        let mut assertion = self.assertion.lock().unwrap();
        if assertion.is_some() {
            return Ok(());
        }
        let assertion_type = cf_string("PreventUserIdleSystemSleep")?;
        let reason = cf_string("Processing activity")?;
        let mut id = 0;
        let status = unsafe { IOPMAssertionCreateWithName(assertion_type, 255, reason, &mut id) };
        unsafe {
            CFRelease(assertion_type);
            CFRelease(reason);
        }
        if status != 0 {
            return Err(PowerError::Failed(format!(
                "IOPM assertion failed ({status})"
            )));
        }
        let nap = unsafe {
            NSProcessInfo::processInfo().beginActivityWithOptions_reason(
                NSActivityOptions::NSActivityUserInitiatedAllowingIdleSystemSleep,
                &NSString::from_str("Processing activity"),
            )
        };
        *assertion = Some(id);
        *self.app_nap_activity.lock().unwrap() = Some(nap);
        Ok(())
    }
    fn release(&self) -> Result<(), PowerError> {
        let mut failure = None;
        if let Some(id) = self.assertion.lock().unwrap().take() {
            let status = unsafe { IOPMAssertionRelease(id) };
            if status != 0 {
                failure = Some(format!("IOPM release failed ({status})"));
            }
        }
        if let Some(activity) = self.app_nap_activity.lock().unwrap().take() {
            unsafe {
                objc2_foundation::NSProcessInfo::processInfo().endActivity(&activity);
            }
        }
        failure.map_or(Ok(()), |e| Err(PowerError::Failed(e)))
    }
    fn status(&self) -> PowerStatus {
        mac_power_status().unwrap_or(PowerStatus {
            on_ac: None,
            percent: None,
        })
    }
}

pub struct KeepAwake<P: PowerOs> {
    power: P,
    held: Mutex<bool>,
}
impl<P: PowerOs> KeepAwake<P> {
    pub fn new(power: P) -> Self {
        Self {
            power,
            held: Mutex::new(false),
        }
    }
    pub fn hold(&self) -> Result<(), PowerError> {
        let mut held = self.held.lock().unwrap();
        if !*held {
            self.power.hold()?;
            *held = true;
        }
        Ok(())
    }
    pub fn release(&self) -> Result<(), PowerError> {
        let mut held = self.held.lock().unwrap();
        if *held {
            self.power.release()?;
            *held = false;
        }
        Ok(())
    }
}
impl<P: PowerOs> Drop for KeepAwake<P> {
    fn drop(&mut self) {
        if self.held.get_mut().is_ok_and(|held| *held) {
            if let Err(error) = self.power.release() {
                log::warn!("failed to release power hold during shutdown: {error}");
            }
        }
    }
}

pub fn platform() -> KeepAwake<impl PowerOs> {
    #[cfg(target_os = "macos")]
    return KeepAwake::new(PlatformPower {
        assertion: Mutex::new(None),
        app_nap_activity: Mutex::new(None),
    });
    #[cfg(windows)]
    return KeepAwake::new(PlatformPower {
        request: Mutex::new(None),
    });
    #[cfg(not(any(windows, target_os = "macos")))]
    KeepAwake::new(PlatformPower)
}

#[cfg(target_os = "macos")]
type CFTypeRef = *const std::ffi::c_void;
#[cfg(target_os = "macos")]
type CFStringRef = CFTypeRef;
#[cfg(target_os = "macos")]
#[link(name = "IOKit", kind = "framework")]
unsafe extern "C" {
    fn IOPMAssertionCreateWithName(
        assertion_type: CFStringRef,
        level: u32,
        reason: CFStringRef,
        id: *mut u32,
    ) -> i32;
    fn IOPMAssertionRelease(id: u32) -> i32;
    fn IOPSCopyPowerSourcesInfo() -> CFTypeRef;
    fn IOPSGetProvidingPowerSourceType(info: CFTypeRef) -> CFStringRef;
    fn IOPSCopyPowerSourcesList(info: CFTypeRef, count: *mut i64) -> *const CFTypeRef;
    fn IOPSGetPowerSourceDescription(info: CFTypeRef, source: CFTypeRef) -> CFTypeRef;
}
#[cfg(target_os = "macos")]
#[link(name = "CoreFoundation", kind = "framework")]
unsafe extern "C" {
    fn CFRelease(value: CFTypeRef);
    fn CFStringCreateWithCString(
        allocator: *const std::ffi::c_void,
        value: *const std::ffi::c_char,
        encoding: u32,
    ) -> CFStringRef;
    fn CFStringGetCString(
        value: CFStringRef,
        buffer: *mut std::ffi::c_char,
        size: i64,
        encoding: u32,
    ) -> bool;
    fn CFDictionaryGetValue(dict: CFTypeRef, key: CFTypeRef) -> CFTypeRef;
    fn CFNumberGetValue(number: CFTypeRef, type_code: i32, value: *mut std::ffi::c_void) -> bool;
}
#[cfg(target_os = "macos")]
fn cf_string(value: &str) -> Result<CFStringRef, PowerError> {
    let c = std::ffi::CString::new(value).map_err(|e| PowerError::Failed(e.to_string()))?;
    let result = unsafe { CFStringCreateWithCString(std::ptr::null(), c.as_ptr(), 0x0800_0100) };
    if result.is_null() {
        Err(PowerError::Failed(
            "could not create CoreFoundation string".into(),
        ))
    } else {
        Ok(result)
    }
}
#[cfg(target_os = "macos")]
fn mac_power_status() -> Result<PowerStatus, PowerError> {
    let info = unsafe { IOPSCopyPowerSourcesInfo() };
    if info.is_null() {
        return Ok(PowerStatus {
            on_ac: None,
            percent: None,
        });
    }
    let result = (|| {
        let kind_ref = unsafe { IOPSGetProvidingPowerSourceType(info) };
        let kind = if kind_ref.is_null() {
            None
        } else {
            let mut buf = [0i8; 64];
            unsafe {
                CFStringGetCString(kind_ref, buf.as_mut_ptr(), buf.len() as i64, 0x0800_0100);
            }
            Some(
                unsafe { std::ffi::CStr::from_ptr(buf.as_ptr()) }
                    .to_string_lossy()
                    .into_owned(),
            )
        };
        let key = cf_string("Current Capacity")?;
        let max_key = cf_string("Max Capacity")?;
        let mut count = 0;
        let list = unsafe { IOPSCopyPowerSourcesList(info, &mut count) };
        let percent = if count > 0 && !list.is_null() {
            let desc = unsafe { IOPSGetPowerSourceDescription(info, *list) };
            let mut current = 0i32;
            let mut maximum = 0i32;
            let ok = unsafe {
                CFNumberGetValue(
                    CFDictionaryGetValue(desc, key),
                    3,
                    &mut current as *mut _ as _,
                ) && CFNumberGetValue(
                    CFDictionaryGetValue(desc, max_key),
                    3,
                    &mut maximum as *mut _ as _,
                )
            };
            (ok && maximum > 0).then(|| (current * 100 / maximum).clamp(0, 100) as u8)
        } else {
            None
        };
        unsafe {
            CFRelease(key);
            CFRelease(max_key);
            if !list.is_null() {
                CFRelease(list as _);
            }
        }
        Ok(mac_status(kind.as_deref(), percent))
    })();
    unsafe {
        CFRelease(info);
    }
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    struct Mock {
        holds: Mutex<u8>,
        releases: Mutex<u8>,
    }
    impl PowerOs for Mock {
        fn hold(&self) -> Result<(), PowerError> {
            *self.holds.lock().unwrap() += 1;
            Ok(())
        }
        fn release(&self) -> Result<(), PowerError> {
            *self.releases.lock().unwrap() += 1;
            Ok(())
        }
        fn status(&self) -> PowerStatus {
            PowerStatus {
                on_ac: Some(true),
                percent: Some(100),
            }
        }
    }
    #[test]
    fn idempotent() {
        let m = Mock {
            holds: Mutex::new(0),
            releases: Mutex::new(0),
        };
        let k = KeepAwake::new(m);
        k.hold().unwrap();
        k.hold().unwrap();
        k.release().unwrap();
        k.release().unwrap();
        assert_eq!(*k.power.holds.lock().unwrap(), 1);
        assert_eq!(*k.power.releases.lock().unwrap(), 1);
    }
    #[cfg(windows)]
    #[test]
    fn windows_raw_status_maps_unknown_values() {
        assert_eq!(
            windows_status(windows::Win32::System::Power::SYSTEM_POWER_STATUS {
                ACLineStatus: 2,
                BatteryLifePercent: 255,
                ..Default::default()
            }),
            PowerStatus {
                on_ac: None,
                percent: None
            }
        );
    }
    #[cfg(windows)]
    #[test]
    fn real_backend_holds_and_releases() {
        let backend = platform();
        backend.hold().unwrap();
        backend.hold().unwrap();
        backend.release().unwrap();
    }
    #[cfg(target_os = "macos")]
    #[test]
    fn macos_platform_links_and_maps_status() {
        let _ = platform();
        assert_eq!(
            mac_status(Some("AC Power"), None),
            PowerStatus {
                on_ac: Some(true),
                percent: None
            }
        );
        assert_eq!(
            mac_status(Some("Battery Power"), Some(42)),
            PowerStatus {
                on_ac: Some(false),
                percent: Some(42)
            }
        );
    }
}
