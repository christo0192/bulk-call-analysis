#[cfg(windows)]
pub struct Guard(#[allow(dead_code)] windows::Win32::Foundation::HANDLE);
#[cfg(unix)]
pub struct Guard(u32);

pub fn attach(pid: u32) -> std::io::Result<Guard> {
    #[cfg(windows)]
    {
        attach_windows(pid)
    }
    #[cfg(not(windows))]
    {
        Ok(Guard(pid))
    }
}

#[cfg(windows)]
fn attach_windows(pid: u32) -> std::io::Result<Guard> {
    use windows::Win32::{
        Foundation::HANDLE,
        System::{JobObjects::*, Threading::OpenProcess},
    };
    // The assignment follows spawn immediately; the engine places its own children in nested jobs.
    unsafe {
        let job = CreateJobObjectW(None, None).map_err(|e| std::io::Error::other(e.to_string()))?;
        let mut limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        SetInformationJobObject(
            job,
            JobObjectExtendedLimitInformation,
            &limits as *const _ as *const _,
            std::mem::size_of_val(&limits) as u32,
        )
        .map_err(|e| std::io::Error::other(e.to_string()))?;
        let process = OpenProcess(
            windows::Win32::System::Threading::PROCESS_SET_QUOTA
                | windows::Win32::System::Threading::PROCESS_TERMINATE,
            false,
            pid,
        )
        .map_err(|e| std::io::Error::other(e.to_string()))?;
        AssignProcessToJobObject(job, process).map_err(|e| std::io::Error::other(e.to_string()))?;
        Ok(Guard(HANDLE(job.0)))
    }
}

#[cfg(unix)]
pub fn terminate_group(pid: u32) {
    if pid == 0 {
        return;
    }
    let group = pid as libc::pid_t;
    // The child is the process-group leader, so a negative group id targets all descendants.
    let term_result = unsafe { libc::killpg(group, libc::SIGTERM) };
    if term_result == -1 && std::io::Error::last_os_error().raw_os_error() == Some(libc::ESRCH) {
        return;
    }
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
    while std::time::Instant::now() < deadline {
        let exists = unsafe { libc::killpg(group, 0) } == 0;
        if !exists && std::io::Error::last_os_error().raw_os_error() == Some(libc::ESRCH) {
            return;
        }
        std::thread::sleep(std::time::Duration::from_millis(50));
    }
    let result = unsafe { libc::killpg(group, libc::SIGKILL) };
    if result == -1 && std::io::Error::last_os_error().raw_os_error() != Some(libc::ESRCH) {
        log::warn!(
            "failed to kill process group {pid}: {}",
            std::io::Error::last_os_error()
        );
    }
}

#[cfg(unix)]
impl Drop for Guard {
    fn drop(&mut self) {
        terminate_group(self.0);
    }
}
