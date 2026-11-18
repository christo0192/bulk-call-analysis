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
        let write = async {
            self.writer
                .lock()
                .await
                .write_all(&encode(&message)?)
                .await
                .map_err(RpcError::from)
        };
        if let Err(error) = timeout(wait, write).await.unwrap_or(Err(RpcError::Timeout)) {
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
        self.writer
            .lock()
            .await
            .write_all(&encode(&message)?)
            .await?;
        Ok(())
    }

    pub async fn dispatch(&self, message: Value, notifications: &mpsc::Sender<Notification>) {
        let Some(object) = message.as_object() else {
            return;
        };
        if object.get("method").is_some() {
            let _ = notifications.send(message).await;
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
    fn request_with_id_and_method_is_not_response() {
        let runtime = tokio::runtime::Runtime::new().unwrap();
        runtime.block_on(async {
            let (client_io, _server_io) = tokio::io::duplex(1024);
            let client = Client::new(client_io);
            let (notifications, mut received) = mpsc::channel(1);
            client
                .dispatch(json!({"id": 1, "method": "callback"}), &notifications)
                .await;
            assert_eq!(received.recv().await.unwrap()["method"], "callback");
        });
    }
}
