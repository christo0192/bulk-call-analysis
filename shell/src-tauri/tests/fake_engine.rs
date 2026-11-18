use bulk_call_analysis_lib::rpc::{Client, RpcError};
use serde_json::{json, Value};
use std::process::{Command, Stdio};
use std::time::Duration;
use tokio::io::{AsyncBufReadExt, BufReader};

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
    let script = r#"import json, subprocess, sys
grandchild = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
for line in sys.stdin:
    message = json.loads(line)
    if message.get('method') == 'shutdown':
        grandchild.terminate()
    print(json.dumps({'jsonrpc':'2.0','id':message['id'],'result':{'method':message['method'],'grandchild':grandchild.pid}}), flush=True)
    if message.get('method') == 'shutdown':
        grandchild.wait(timeout=5)
        break
"#;
    let runtime = tokio::runtime::Runtime::new().unwrap();
    runtime.block_on(async {
        let mut child = tokio::process::Command::new(python)
            .args(["-c", script])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .kill_on_drop(true)
            .spawn()
            .unwrap();
        let stdout = BufReader::new(child.stdout.take().unwrap());
        let mut lines = stdout.lines();
        let client = Client::new(child.stdin.take().unwrap());
        let reader_client = client.clone();
        let reader = tokio::spawn(async move {
            let notifications = tokio::sync::mpsc::channel(1).0;
            while let Ok(Some(line)) = lines.next_line().await {
                reader_client
                    .dispatch(
                        serde_json::from_str::<Value>(&line).unwrap(),
                        &notifications,
                    )
                    .await;
            }
            reader_client.close().await;
        });
        let response = client
            .request("health/ping", json!({}), Duration::from_secs(2))
            .await
            .unwrap();
        assert_eq!(response["method"], "health/ping");
        let shutdown = client
            .request("shutdown", json!({}), Duration::from_secs(2))
            .await
            .unwrap();
        let grandchild = shutdown["grandchild"].as_u64().unwrap() as u32;
        assert!(child.wait().await.unwrap().success());
        reader.await.unwrap();
        assert!(!process_exists(grandchild));
        assert!(matches!(
            client
                .request("after", json!({}), Duration::from_millis(20))
                .await,
            Err(RpcError::Closed)
        ));
    });
}
