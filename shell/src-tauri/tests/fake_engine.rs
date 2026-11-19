use bulk_call_analysis_lib::Engine;
use serde_json::json;
use std::{fs, process::Command, time::Duration};

#[cfg(unix)]
fn process_exists(pid: u32) -> bool {
    unsafe { libc::kill(pid as libc::pid_t, 0) == 0 }
}

#[cfg(windows)]
fn process_exists(pid: u32) -> bool {
    Command::new("tasklist")
        .args(["/FI", &format!("PID eq {pid}")])
        .output()
        .is_ok_and(|output| String::from_utf8_lossy(&output.stdout).contains(&pid.to_string()))
}

#[test]
fn real_engine_supervises_restarts_and_kills_process_tree() {
    let python = ["python", "python3"].into_iter().find(|candidate| {
        Command::new(candidate)
            .arg("--version")
            .output()
            .is_ok_and(|output| output.status.success())
    });
    let Some(python) = python else {
        if std::env::var_os("CI").is_some() {
            panic!("Python is required for the fake engine integration test");
        }
        return;
    };
    let marker = std::env::temp_dir().join(format!(
        "bca-engine-test-{}-{}.txt",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    let script = r#"import json, os, subprocess, sys
marker = sys.argv[1]
count = 0
for line in sys.stdin:
    message = json.loads(line)
    if message.get('method') == 'health/ping':
        count += 1
        grandchild = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with open(marker, 'a', encoding='utf-8') as output:
            output.write(f'{os.getpid()} {grandchild.pid}\n')
        print(json.dumps({'jsonrpc':'2.0','id':message['id'],'result':{'ok':True}}), flush=True)
        if count == 1:
            break
    elif message.get('method') == 'shutdown':
        break
"#;
    let args = json!(vec!["-u", "-c", script, marker.to_string_lossy().as_ref()]).to_string();
    std::env::set_var("BCA_ENGINE_PROGRAM", python);
    std::env::set_var("BCA_ENGINE_ARGS", args);

    let runtime = tokio::runtime::Runtime::new().unwrap();
    let engine = Engine::<tauri::Wry>::new_without_app();
    runtime.block_on(async {
        engine.start();
        let deadline = tokio::time::Instant::now() + Duration::from_secs(8);
        loop {
            let lines = fs::read_to_string(&marker).unwrap_or_default();
            if lines.lines().count() >= 2 {
                break;
            }
            assert!(
                tokio::time::Instant::now() < deadline,
                "engine did not restart"
            );
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
        let pids: Vec<u32> = fs::read_to_string(&marker)
            .unwrap()
            .lines()
            .flat_map(|line| line.split_whitespace())
            .map(|pid| pid.parse().unwrap())
            .collect();
        engine.shutdown().await;
        let deadline = tokio::time::Instant::now() + Duration::from_secs(10);
        while pids.iter().any(|pid| process_exists(*pid)) && tokio::time::Instant::now() < deadline
        {
            tokio::time::sleep(Duration::from_millis(25)).await;
        }
        assert!(pids.iter().all(|pid| !process_exists(*pid)));
    });
    let _ = fs::remove_file(marker);
    std::env::remove_var("BCA_ENGINE_PROGRAM");
    std::env::remove_var("BCA_ENGINE_ARGS");
}
