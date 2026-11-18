use serde_json::{json, Value};
use std::{collections::HashMap, sync::Arc, time::Duration};
use thiserror::Error;
use tokio::{io::{AsyncWrite, AsyncWriteExt}, sync::{mpsc, oneshot, Mutex}, time::timeout};

#[derive(Debug, Error)]
pub enum RpcError {
    #[error("malformed JSON-RPC line: {0}")]
    Malformed(#[from] serde_json::Error),
    #[error("RPC timed out")]
    Timeout,
    #[error("RPC connection closed")]
    Closed,
    #[error("RPC error {code}: {message}")]
    Remote { code: i64, message: String },
    #[error("I/O error: {0}")]
    Io(#[from] std::io::Error),
}

pub fn encode(message: &Value) -> Result<Vec<u8>, RpcError> {
    let mut bytes = serde_json::to_vec(message).map_err(|e| RpcError::Malformed(e))?;
    bytes.push(b'\n');
    Ok(bytes)
}

pub fn decode(line: &[u8]) -> Result<Value, RpcError> {
    let value: Value = serde_json::from_slice(line)?;
    if !value.is_object() { return Err(RpcError::Malformed(serde_json::from_str("null" ).unwrap_err())); }
    Ok(value)
}

pub type Notification = Value;
type Pending = Arc<Mutex<HashMap<u64, oneshot::Sender<Result<Value, RpcError>>>>>;

pub struct Client<W> {
    writer: Mutex<W>,
    pending: Pending,
    next_id: std::sync::atomic::AtomicU64,
}

impl<W: AsyncWrite + Unpin + Send> Client<W> {
    pub fn new(writer: W) -> Arc<Self> {
        Arc::new(Self { writer: Mutex::new(writer), pending: Arc::new(Mutex::new(HashMap::new())), next_id: std::sync::atomic::AtomicU64::new(1) })
    }

    pub async fn request(self: &Arc<Self>, method: &str, params: Value, wait: Duration) -> Result<Value, RpcError> {
        let id = self.next_id.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        let (tx, rx) = oneshot::channel();
        self.pending.lock().await.insert(id, tx);
        let message = json!({"jsonrpc":"2.0", "id": id, "method": method, "params": params});
        if let Err(error) = self.writer.lock().await.write_all(&encode(&message)?).await {
            self.pending.lock().await.remove(&id);
            return Err(error.into());
        }
        match timeout(wait, rx).await {
            Ok(Ok(result)) => result,
            Ok(Err(_)) => Err(RpcError::Closed),
            Err(_) => { self.pending.lock().await.remove(&id); Err(RpcError::Timeout) }
        }
    }

    pub async fn notify(self: &Arc<Self>, method: &str, params: Value) -> Result<(), RpcError> {
        let message = json!({"jsonrpc":"2.0", "method": method, "params": params});
        self.writer.lock().await.write_all(&encode(&message)?).await?;
        Ok(())
    }

    pub async fn dispatch(&self, message: Value, notifications: &mpsc::Sender<Notification>) {
        let Some(object) = message.as_object() else { return };
        if let Some(id) = object.get("id").and_then(Value::as_u64) {
            if let Some(sender) = self.pending.lock().await.remove(&id) {
                let result = if let Some(error) = object.get("error") {
                    Err(RpcError::Remote { code: error.get("code").and_then(Value::as_i64).unwrap_or(-1), message: error.get("message").and_then(Value::as_str).unwrap_or("unknown error").to_owned() })
                } else { Ok(object.get("result").cloned().unwrap_or(Value::Null)) };
                let _ = sender.send(result);
            }
        } else if object.get("method").is_some() { let _ = notifications.send(message).await; }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test] fn framing_round_trip() { let value = json!({"jsonrpc":"2.0","id":1}); assert_eq!(decode(&encode(&value).unwrap()).unwrap(), value); }
    #[test] fn malformed_line_is_rejected() { assert!(decode(b"not json\n").is_err()); }
}
