use serde_json::{json, Value};
use std::{
    collections::HashMap,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
    time::Duration,
};
use thiserror::Error;
use tokio::{
    io::{AsyncWrite, AsyncWriteExt},
    sync::{mpsc, oneshot, Mutex},
    time::timeout,
};

#[derive(Debug, Error)]
pub enum RpcError {
    #[error("malformed JSON-RPC line: {0}")]
    Malformed(#[from] serde_json::Error),
    #[error("RPC timed out")]
    Timeout,
    #[error("RPC write timed out")]
    WriteTimeout,
    #[error("RPC connection closed")]
    Closed,
    #[error("RPC error {code}: {message}")]
    Remote { code: i64, message: String },
    #[error("I/O error: {0}")]
    Io(#[from] std::io::Error),
}

pub fn encode(message: &Value) -> Result<Vec<u8>, RpcError> {
    let mut bytes = serde_json::to_vec(message).map_err(RpcError::Malformed)?;
    bytes.push(b'\n');
    Ok(bytes)
}

pub fn decode(line: &[u8]) -> Result<Value, RpcError> {
    let value: Value = serde_json::from_slice(line)?;
    if !value.is_object() {
        return Err(RpcError::Malformed(
            serde_json::from_str::<Value>("not-json").unwrap_err(),
        ));
    }
    Ok(value)
}

pub type Notification = Value;
type Pending = Arc<Mutex<HashMap<u64, oneshot::Sender<Result<Value, RpcError>>>>>;

pub struct Client<W> {
    writer: Mutex<W>,
    pending: Pending,
    next_id: std::sync::atomic::AtomicU64,
    closed: AtomicBool,
}

impl<W: AsyncWrite + Unpin + Send> Client<W> {
    pub fn new(writer: W) -> Arc<Self> {
        Arc::new(Self {
            writer: Mutex::new(writer),
            pending: Arc::new(Mutex::new(HashMap::new())),
            next_id: std::sync::atomic::AtomicU64::new(1),
            closed: AtomicBool::new(false),
        })
    }

    pub async fn request(
        self: &Arc<Self>,
        method: &str,
        params: Value,
        wait: Duration,
    ) -> Result<Value, RpcError> {
        let id = self
            .next_id
            .fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        let (tx, rx) = oneshot::channel();
        self.pending.lock().await.insert(id, tx);
        let message = json!({"jsonrpc":"2.0", "id": id, "method": method, "params": params});
        let bytes = encode(&message)?;
        if self.closed.load(Ordering::Acquire) {
            self.pending.lock().await.remove(&id);
            return Err(RpcError::Closed);
        }
        let mut writer = self.writer.lock().await;
        if self.closed.load(Ordering::Acquire) {
            drop(writer);
            self.pending.lock().await.remove(&id);
            return Err(RpcError::Closed);
        }
        let write_result = timeout(wait, writer.write_all(&bytes))
            .await
            .map_err(|_| RpcError::WriteTimeout)
            .and_then(|result| result.map_err(RpcError::from));
        drop(writer);
        if let Err(error) = write_result {
            self.close().await;
            self.pending.lock().await.remove(&id);
            return Err(error);
        }
        if self.closed.load(Ordering::Acquire) {
            self.pending.lock().await.remove(&id);
            return Err(RpcError::Closed);
        }
        match timeout(wait, rx).await {
            Ok(Ok(result)) => result,
            Ok(Err(_)) => Err(RpcError::Closed),
            Err(_) => {
                self.pending.lock().await.remove(&id);
                Err(RpcError::Timeout)
            }
        }
    }

    pub async fn close(&self) {
        self.closed.store(true, Ordering::Release);
        let mut pending = self.pending.lock().await;
        for (_, sender) in pending.drain() {
            let _ = sender.send(Err(RpcError::Closed));
        }
    }

    #[allow(dead_code)]
    pub async fn notify(self: &Arc<Self>, method: &str, params: Value) -> Result<(), RpcError> {
        let message = json!({"jsonrpc":"2.0", "method": method, "params": params});
        if self.closed.load(Ordering::Acquire) {
            return Err(RpcError::Closed);
        }
        let bytes = encode(&message)?;
        let mut writer = self.writer.lock().await;
        if self.closed.load(Ordering::Acquire) {
            return Err(RpcError::Closed);
        }
        if let Err(error) = writer.write_all(&bytes).await {
            drop(writer);
            self.close().await;
            return Err(RpcError::Io(error));
        }
        Ok(())
    }

    pub async fn dispatch(&self, message: Value, notifications: &mpsc::Sender<Notification>) {
        let Some(object) = message.as_object() else {
            return;
        };
        if object.get("method").is_some() {
            if let Some(id) = object.get("id") {
                let response = json!({
                    "jsonrpc": "2.0",
                    "id": id,
                    "error": {"code": -32601, "message": "Method not found"}
                });
                if let Ok(bytes) = encode(&response) {
                    if self.closed.load(Ordering::Acquire) {
                        return;
                    }
                    let mut writer = self.writer.lock().await;
                    if self.closed.load(Ordering::Acquire) {
                        return;
                    }
                    if writer.write_all(&bytes).await.is_err() {
                        drop(writer);
                        self.close().await;
                    }
                }
            } else {
                let _ = notifications.send(message).await;
            }
        } else if let Some(id) = object.get("id").and_then(Value::as_u64) {
            if let Some(sender) = self.pending.lock().await.remove(&id) {
                let result = if let Some(error) = object.get("error") {
                    Err(RpcError::Remote {
                        code: error.get("code").and_then(Value::as_i64).unwrap_or(-1),
                        message: error
                            .get("message")
                            .and_then(Value::as_str)
                            .unwrap_or("unknown error")
                            .to_owned(),
                    })
                } else {
                    Ok(object.get("result").cloned().unwrap_or(Value::Null))
                };
                let _ = sender.send(result);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::io::{AsyncBufReadExt, BufReader};
    #[test]
    fn framing_round_trip() {
        let value = json!({"jsonrpc":"2.0","id":1});
        assert_eq!(decode(&encode(&value).unwrap()).unwrap(), value);
    }
    #[test]
    fn malformed_line_is_rejected() {
        assert!(decode(b"not json\n").is_err());
    }

    #[test]
    fn non_object_line_is_rejected() {
        assert!(decode(b"[1, 2]\n").is_err());
    }

    #[test]
    fn request_correlates_response_by_id() {
        let runtime = tokio::runtime::Runtime::new().unwrap();
        runtime.block_on(async {
            let (client_io, server_io) = tokio::io::duplex(1024);
            let client = Client::new(client_io);
            let request = {
                let client = client.clone();
                tokio::spawn(async move {
                    client
                        .request("echo", json!({}), Duration::from_secs(1))
                        .await
                })
            };
            let mut server = BufReader::new(server_io);
            let mut line = String::new();
            server.read_line(&mut line).await.unwrap();
            let sent: Value = serde_json::from_str(&line).unwrap();
            let (notifications, _) = mpsc::channel(1);
            client
                .dispatch(json!({"id":sent["id"],"result":42}), &notifications)
                .await;
            assert_eq!(request.await.unwrap().unwrap(), json!(42));
        });
    }

    #[test]
    fn request_times_out_and_removes_pending_entry() {
        let runtime = tokio::runtime::Runtime::new().unwrap();
        runtime.block_on(async {
            let (client_io, _server_io) = tokio::io::duplex(1024);
            let client = Client::new(client_io);
            assert!(matches!(
                client
                    .request("slow", json!({}), Duration::from_millis(5))
                    .await,
                Err(RpcError::Timeout)
            ));
            assert!(client.pending.lock().await.is_empty());
        });
    }

    #[test]
    fn slow_reply_timeout_does_not_close_the_client() {
        let runtime = tokio::runtime::Runtime::new().unwrap();
        runtime.block_on(async {
            let (client_io, server_io) = tokio::io::duplex(1024);
            let client = Client::new(client_io);
            let request_client = client.clone();
            let request = tokio::spawn(async move {
                request_client
                    .request("slow", json!({}), Duration::from_millis(5))
                    .await
            });
            let mut server = BufReader::new(server_io);
            let mut line = String::new();
            server.read_line(&mut line).await.unwrap();
            assert!(matches!(request.await.unwrap(), Err(RpcError::Timeout)));
            assert!(!client.closed.load(Ordering::Acquire));
            let sent: Value = serde_json::from_str(&line).unwrap();
            let (notifications, _) = mpsc::channel(1);
            client
                .dispatch(json!({"id": sent["id"], "result": 42}), &notifications)
                .await;
            assert!(!client.closed.load(Ordering::Acquire));
        });
    }

    #[test]
    fn remote_error_is_returned() {
        let runtime = tokio::runtime::Runtime::new().unwrap();
        runtime.block_on(async {
            let (client_io, server_io) = tokio::io::duplex(1024);
            let client = Client::new(client_io);
            let request = {
                let client = client.clone();
                tokio::spawn(async move {
                    client
                        .request("fail", json!({}), Duration::from_secs(1))
                        .await
                })
            };
            let mut server = BufReader::new(server_io);
            let mut line = String::new();
            server.read_line(&mut line).await.unwrap();
            let sent: Value = serde_json::from_str(&line).unwrap();
            let (notifications, _) = mpsc::channel(1);
            client
                .dispatch(
                    json!({"id":sent["id"],"error":{"code":7,"message":"bad"}}),
                    &notifications,
                )
                .await;
            assert!(matches!(
                request.await.unwrap(),
                Err(RpcError::Remote { code: 7, .. })
            ));
        });
    }

    #[test]
    fn closing_connection_fails_all_pending_requests() {
        let runtime = tokio::runtime::Runtime::new().unwrap();
        runtime.block_on(async {
            let (client_io, _server_io) = tokio::io::duplex(1024);
            let client = Client::new(client_io);
            let request = {
                let client = client.clone();
                tokio::spawn(async move {
                    client
                        .request("wait", json!({}), Duration::from_secs(10))
                        .await
                })
            };
            for _ in 0..10 {
                if !client.pending.lock().await.is_empty() {
                    break;
                }
                tokio::task::yield_now().await;
            }
            client.close().await;
            assert!(matches!(request.await.unwrap(), Err(RpcError::Closed)));
        });
    }

    #[test]
    fn request_with_id_and_method_gets_method_not_found_reply() {
        let runtime = tokio::runtime::Runtime::new().unwrap();
        runtime.block_on(async {
            let (client_io, server_io) = tokio::io::duplex(1024);
            let client = Client::new(client_io);
            let (notifications, _) = mpsc::channel(1);
            client
                .dispatch(json!({"id": 1, "method": "callback"}), &notifications)
                .await;
            let mut server = BufReader::new(server_io);
            let mut line = String::new();
            server.read_line(&mut line).await.unwrap();
            assert_eq!(
                serde_json::from_str::<Value>(&line).unwrap(),
                json!({
                    "jsonrpc":"2.0","id":1,
                    "error":{"code":-32601,"message":"Method not found"}
                })
            );
        });
    }

    #[test]
    fn timed_out_write_breaks_connection() {
        let runtime = tokio::runtime::Runtime::new().unwrap();
        runtime.block_on(async {
            let (client_io, _server_io) = tokio::io::duplex(1);
            let client = Client::new(client_io);
            let result = client
                .request(
                    "large",
                    json!({"data": "x".repeat(4096)}),
                    Duration::from_millis(1),
                )
                .await;
            assert!(matches!(result, Err(RpcError::WriteTimeout)));
            assert!(client.closed.load(Ordering::Acquire));
        });
    }
}
