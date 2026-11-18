use serde_json::json;
use tokio::sync::mpsc;

#[allow(dead_code)]
pub async fn start(notifications: mpsc::Sender<serde_json::Value>) {
    let _ = notifications
        .send(json!({"jsonrpc":"2.0","method":"os/ready","params":{}}))
        .await;
    let mut last_tick = tokio::time::Instant::now();
    loop {
        tokio::time::sleep(std::time::Duration::from_secs(1)).await;
        let now = tokio::time::Instant::now();
        if now.duration_since(last_tick) > std::time::Duration::from_secs(2) {
            let _ = notifications
                .send(json!({"jsonrpc":"2.0","method":"os/sleep","params":{}}))
                .await;
            let _ = notifications
                .send(json!({"jsonrpc":"2.0","method":"os/wake","params":{}}))
                .await;
        }
        last_tick = now;
    }
}
