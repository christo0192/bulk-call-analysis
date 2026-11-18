#![allow(dead_code)]

use std::sync::Mutex;
use thiserror::Error;

#[derive(Debug, Error)]
pub enum PowerError {
    #[error("power request failed: {0}")]
    Failed(String),
}
pub trait PowerOs: Send + Sync {
    fn hold(&self) -> Result<(), PowerError>;
    fn release(&self) -> Result<(), PowerError>;
    fn status(&self) -> (bool, u8);
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
    fn status(&self) -> (bool, u8) {
        (true, 100)
    }
}

#[cfg(windows)]
struct PlatformPower;
#[cfg(windows)]
impl PowerOs for PlatformPower {
    fn hold(&self) -> Result<(), PowerError> {
        use windows::Win32::System::Power::{
            PowerCreateRequest, PowerSetRequest, POWER_REQUEST_TYPE,
        };
        use windows::Win32::System::Threading::{
            POWER_REQUEST_CONTEXT_SIMPLE_STRING, REASON_CONTEXT, REASON_CONTEXT_0,
        };
        unsafe {
            let context = REASON_CONTEXT {
                Version: 0,
                Flags: POWER_REQUEST_CONTEXT_SIMPLE_STRING,
                Reason: REASON_CONTEXT_0 {
                    SimpleReasonString: windows::core::PWSTR::null(),
                },
            };
            let h = PowerCreateRequest(&context).map_err(|e| PowerError::Failed(e.to_string()))?;
            PowerSetRequest(h, POWER_REQUEST_TYPE(0))
                .map_err(|e| PowerError::Failed(e.to_string()))?;
            PowerSetRequest(h, POWER_REQUEST_TYPE(1))
                .map_err(|e| PowerError::Failed(e.to_string()))?;
        }
        Ok(())
    }
    fn release(&self) -> Result<(), PowerError> {
        Ok(())
    }
    fn status(&self) -> (bool, u8) {
        (true, 100)
    }
}
#[cfg(target_os = "macos")]
struct PlatformPower {
    assertion: Mutex<Option<u32>>,
    app_nap_guard: Mutex<Option<std::process::Child>>,
}
#[cfg(target_os = "macos")]
impl PowerOs for PlatformPower {
    fn hold(&self) -> Result<(), PowerError> {
        let mut assertion = self.assertion.lock().unwrap();
        if assertion.is_some() {
            return Ok(());
        }
        let assertion_type = cf_string("PreventUserIdleSystemSleep")?;
        let reason = cf_string("Processing activity")?;
        let mut id = 0;
        let status = unsafe { IOPMAssertionCreateWithName(assertion_type, 255, reason, &mut id) };
        unsafe {
            CFRelease(assertion_type as *const _);
            CFRelease(reason as *const _);
        }
        if status != 0 {
            return Err(PowerError::Failed(format!(
                "IOPM assertion failed ({status})"
            )));
        }
        let nap_guard = match std::process::Command::new("caffeinate")
            .args(["-dimsu", "-w", &std::process::id().to_string()])
            .spawn()
        {
            Ok(child) => child,
            Err(error) => {
                let _ = unsafe { IOPMAssertionRelease(id) };
                return Err(PowerError::Failed(format!("caffeinate failed: {error}")));
            }
        };
        *assertion = Some(id);
        *self.app_nap_guard.lock().unwrap() = Some(nap_guard);
        Ok(())
    }
    fn release(&self) -> Result<(), PowerError> {
        let mut assertion = self.assertion.lock().unwrap();
        if let Some(id) = assertion.take() {
            let status = unsafe { IOPMAssertionRelease(id) };
            if status != 0 {
                return Err(PowerError::Failed(format!(
                    "IOPM release failed ({status})"
                )));
            }
        }
        if let Some(mut guard) = self.app_nap_guard.lock().unwrap().take() {
            guard
                .kill()
                .map_err(|error| PowerError::Failed(format!("caffeinate stop failed: {error}")))?;
            guard
                .wait()
                .map_err(|error| PowerError::Failed(format!("caffeinate wait failed: {error}")))?;
        }
        Ok(())
    }
    fn status(&self) -> (bool, u8) {
        (true, 100)
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
        app_nap_guard: Mutex::new(None),
    });
    #[cfg(not(target_os = "macos"))]
    KeepAwake::new(PlatformPower)
}

#[cfg(target_os = "macos")]
type CFStringRef = *const std::ffi::c_void;
#[cfg(target_os = "macos")]
unsafe extern "C" {
    fn IOPMAssertionCreateWithName(
        assertion_type: CFStringRef,
        assertion_level: u32,
        reason: CFStringRef,
        assertion_id: *mut u32,
    ) -> i32;
    fn IOPMAssertionRelease(assertion_id: u32) -> i32;
    fn CFRelease(value: *const std::ffi::c_void);
    fn CFStringCreateWithCString(
        allocator: *const std::ffi::c_void,
        string: *const std::ffi::c_char,
        encoding: u32,
    ) -> CFStringRef;
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
        fn status(&self) -> (bool, u8) {
            (true, 100)
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

    #[test]
    fn failed_hold_does_not_enter_held_state() {
        struct Failing;
        impl PowerOs for Failing {
            fn hold(&self) -> Result<(), PowerError> {
                Err(PowerError::Failed("no".into()))
            }
            fn release(&self) -> Result<(), PowerError> {
                Ok(())
            }
            fn status(&self) -> (bool, u8) {
                (false, 0)
            }
        }
        let k = KeepAwake::new(Failing);
        assert!(k.hold().is_err());
        assert!(k.release().is_ok());
    }

    #[test]
    fn status_is_available_from_power_backend() {
        let m = Mock {
            holds: Mutex::new(0),
            releases: Mutex::new(0),
        };
        assert_eq!(m.status(), (true, 100));
    }
}
