#[cfg(windows)]
pub struct Guard(#[allow(dead_code)] windows::Win32::Foundation::HANDLE);
#[cfg(not(windows))]
pub struct Guard;

pub fn attach(pid: u32) -> std::io::Result<Guard> {
    #[cfg(windows)] { attach_windows(pid) }
    #[cfg(not(windows))] { let _ = pid; Ok(Guard) }
}

#[cfg(windows)]
fn attach_windows(pid: u32) -> std::io::Result<Guard> {
    use windows::Win32::{Foundation::HANDLE, System::{JobObjects::*, Threading::OpenProcess}};
    // The assignment follows spawn immediately; the engine places its own children in nested jobs.
    unsafe {
        let job = CreateJobObjectW(None, None).map_err(|e| std::io::Error::other(e.to_string()))?;
        let mut limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        SetInformationJobObject(job, JobObjectExtendedLimitInformation, &limits as *const _ as *const _, std::mem::size_of_val(&limits) as u32).map_err(|e| std::io::Error::other(e.to_string()))?;
        let process = OpenProcess(windows::Win32::System::Threading::PROCESS_SET_QUOTA | windows::Win32::System::Threading::PROCESS_TERMINATE, false, pid).map_err(|e| std::io::Error::other(e.to_string()))?;
        AssignProcessToJobObject(job, process).map_err(|e| std::io::Error::other(e.to_string()))?;
        Ok(Guard(HANDLE(job.0)))
    }
}

#[cfg(target_os = "macos")]
pub fn terminate_group(_pid: u32) { /* TODO: send SIGTERM and SIGKILL after five seconds. */ }
