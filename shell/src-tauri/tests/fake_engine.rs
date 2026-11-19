use bulk_call_analysis_lib::spawn_for_test;
use serde_json::json;
use std::process::Command;
use std::time::Duration;

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
fn real_child_rpc_round_trip_and_shutdown() {
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
    let script = r#"import json, os, subprocess, sys
grandchild = None
for line in sys.stdin:
    message = json.loads(line)
    if message.get('method') == 'health/ping' and grandchild is None:
        grandchild = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    print(json.dumps({'jsonrpc':'2.0','id':message['id'],'result':{'method':message['method'],'engine':os.getpid(),'grandchild':grandchild.pid}}), flush=True)
    if message.get('method') == 'shutdown':
        break
"#;
    let runtime = tokio::runtime::Runtime::new().unwrap();
    runtime.block_on(async {
        let engine = spawn_for_test(
            python.to_owned(),
            vec!["-u".into(), "-c".into(), script.into()],
        )
        .await
        .unwrap();
        let response = engine.call("health/ping", json!({})).await.unwrap();
        assert_eq!(response["method"], "health/ping");
        let engine_pid = engine.pid();
        let grandchild = response["grandchild"].as_u64().unwrap() as u32;
        engine.shutdown().await;
        let deadline = tokio::time::Instant::now() + Duration::from_secs(10);
        while (process_exists(engine_pid) || process_exists(grandchild))
            && tokio::time::Instant::now() < deadline
        {
            tokio::time::sleep(Duration::from_millis(25)).await;
        }
        assert!(!process_exists(engine_pid));
        assert!(!process_exists(grandchild));
    });
}
