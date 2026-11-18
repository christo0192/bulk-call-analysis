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
struct PlatformPower;
#[cfg(target_os = "macos")]
impl PowerOs for PlatformPower {
    fn hold(&self) -> Result<(), PowerError> {
        /* IOPMAssertionCreateWithName and NSProcessInfo activity belong here. */
        Ok(())
    }
    fn release(&self) -> Result<(), PowerError> {
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
pub fn platform() -> KeepAwake<impl PowerOs> {
    KeepAwake::new(PlatformPower)
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
}
