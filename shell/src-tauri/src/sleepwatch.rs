use tokio::sync::mpsc;
use serde_json::json;

pub async fn start(notifications: mpsc::Sender<serde_json::Value>) {
    // Platform notification integration is intentionally isolated here. The engine heartbeat
    // gap remains authoritative until the native notification adapters are enabled.
    let _ = notifications.send(json!({"jsonrpc":"2.0","method":"os/ready","params":{}})).await;
}
