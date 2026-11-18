use std::io::{BufRead, BufReader, Write};
use std::process::{Command, Stdio};

#[test]
fn tiny_fake_engine_round_trip_and_shutdown() {
    let python = ["python", "python3"].into_iter().find(|candidate| {
        Command::new(candidate)
            .arg("--version")
            .output()
            .is_ok_and(|output| output.status.success())
    });
    let Some(python) = python else { return };
    let script = r#"import json,sys
for line in sys.stdin:
 m=json.loads(line)
 print(json.dumps({'jsonrpc':'2.0','id':m['id'],'result':{'method':m['method']}}),flush=True)
 if m['method']=='shutdown': break
"#;
    let mut child = Command::new(python)
        .args(["-c", script])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    let mut stdin = child.stdin.take().unwrap();
    let mut stdout = BufReader::new(child.stdout.take().unwrap());
    writeln!(
        stdin,
        r#"{{"jsonrpc":"2.0","id":1,"method":"health/ping","params":{{}}}}"#
    )
    .unwrap();
    let mut line = String::new();
    stdout.read_line(&mut line).unwrap();
    assert!(line.contains(r#""id": 1"#));
    assert!(line.contains("health/ping"));
    writeln!(
        stdin,
        r#"{{"jsonrpc":"2.0","id":2,"method":"shutdown","params":{{}}}}"#
    )
    .unwrap();
    line.clear();
    stdout.read_line(&mut line).unwrap();
    assert!(line.contains(r#""id": 2"#));
    drop(stdin);
    assert!(child.wait().unwrap().success());
}
