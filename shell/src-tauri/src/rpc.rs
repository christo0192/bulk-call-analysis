use serde_json::{json, Value};
use std::{
    collections::HashMap,
    marker::PhantomData,
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
    Remote {
        code: i64,
        message: String,
        data: Option<Value>,
    },
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

struct PendingGuard {
    pending: Pending,
    id: u64,
}
impl Drop for PendingGuard {
    fn drop(&mut self) {
        let pending = self.pending.clone();
        let id = self.id;
        tokio::spawn(async move {
            pending.lock().await.remove(&id);
        });
    }
}

const DEFAULT_WRITE_TIMEOUT: Duration = Duration::from_secs(10);

struct Frame {
    bytes: Vec<u8>,
    written: oneshot::Sender<Result<(), RpcError>>,
    write_timeout: Duration,
}

pub struct Client<W> {
    frames: mpsc::Sender<Frame>,
    _writer: PhantomData<W>,
    pending: Pending,
    next_id: std::sync::atomic::AtomicU64,
    closed: Arc<AtomicBool>,
    write_timeout: Duration,
}

impl<W: AsyncWrite + Unpin + Send + 'static> Client<W> {
    #[cfg(test)]
    pub(crate) async fn pending_len(&self) -> usize {
        self.pending.lock().await.len()
    }

    pub fn new(writer: W) -> Arc<Self> {
        Self::with_write_timeout(writer, DEFAULT_WRITE_TIMEOUT)
    }

    pub fn with_write_timeout(writer: W, write_timeout: Duration) -> Arc<Self> {
        let (frames, mut frame_rx) = mpsc::channel::<Frame>(64);
        let pending = Arc::new(Mutex::new(HashMap::new()));
        let closed = Arc::new(AtomicBool::new(false));
        let writer_closed = closed.clone();
        let writer_pending = pending.clone();
        tokio::spawn(async move {
            let mut writer = writer;
            while let Some(frame) = frame_rx.recv().await {
                if writer_closed.load(Ordering::Acquire) {
                    let _ = frame.written.send(Err(RpcError::Closed));
                    continue;
                }
                match timeout(frame.write_timeout, writer.write_all(&frame.bytes)).await {
                    Ok(Ok(())) => {
                        let _ = frame.written.send(Ok(()));
                    }
                    Ok(Err(error)) => {
                        writer_closed.store(true, Ordering::Release);
                        let _ = frame.written.send(Err(RpcError::Io(error)));
                        fail_pending(&writer_pending).await;
                        break;
                    }
                    Err(_) => {
                        writer_closed.store(true, Ordering::Release);
                        let _ = frame.written.send(Err(RpcError::WriteTimeout));
                        fail_pending(&writer_pending).await;
                        break;
                    }
                }
            }
        });
        Arc::new(Self {
            frames,
            _writer: PhantomData,
            pending,
            next_id: std::sync::atomic::AtomicU64::new(1),
            closed,
            write_timeout,
        })
    }

    pub async fn request(
        self: &Arc<Self>,
        method: &str,
        params: Value,
        wait: Duration,
    ) -> Result<Value, RpcError> {
        let deadline = tokio::time::Instant::now() + wait;
        let id = self
            .next_id
            .fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        let (tx, rx) = oneshot::channel();
        self.pending.lock().await.insert(id, tx);
        let _pending_guard = PendingGuard {
            pending: self.pending.clone(),
            id,
        };
        let message = json!({"jsonrpc":"2.0", "id": id, "method": method, "params": params});
        let bytes = encode(&message)?;
        if self.closed.load(Ordering::Acquire) {
            self.pending.lock().await.remove(&id);
            return Err(RpcError::Closed);
        }
        let (written, written_rx) = oneshot::channel();
        let frame = Frame {
            bytes,
            written,
            write_timeout: self.write_timeout,
        };
        match timeout(remaining(deadline), self.frames.send(frame)).await {
            Ok(Ok(())) => {}
            Ok(Err(_)) => {
                self.pending.lock().await.remove(&id);
                return Err(RpcError::Closed);
            }
            Err(_) => {
                self.pending.lock().await.remove(&id);
                return Err(RpcError::Timeout);
            }
        }
        match timeout(remaining(deadline), written_rx).await {
            Ok(Ok(Ok(()))) => {}
            Ok(Ok(Err(error))) => {
                self.pending.lock().await.remove(&id);
                return Err(error);
            }
            Ok(Err(_)) | Err(_) => {
                self.pending.lock().await.remove(&id);
                return Err(if self.closed.load(Ordering::Acquire) {
                    RpcError::WriteTimeout
                } else {
                    RpcError::Timeout
                });
            }
        }
        if self.closed.load(Ordering::Acquire) {
            self.pending.lock().await.remove(&id);
            return Err(RpcError::Closed);
        }
        match timeout(remaining(deadline), rx).await {
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

    pub async fn notify(
        self: &Arc<Self>,
        method: &str,
        params: Value,
        wait: Duration,
    ) -> Result<(), RpcError> {
        let message = json!({"jsonrpc":"2.0", "method": method, "params": params});
        if self.closed.load(Ordering::Acquire) {
            return Err(RpcError::Closed);
        }
        let bytes = encode(&message)?;
        self.enqueue(bytes, wait).await
    }

    async fn enqueue(&self, bytes: Vec<u8>, wait: Duration) -> Result<(), RpcError> {
        let deadline = tokio::time::Instant::now() + wait;
        let (written, written_rx) = oneshot::channel();
        let frame = Frame {
            bytes,
            written,
            write_timeout: self.write_timeout,
        };
        match timeout(remaining(deadline), self.frames.send(frame)).await {
            Ok(Ok(())) => {}
            Ok(Err(_)) => return Err(RpcError::Closed),
            Err(_) => return Err(RpcError::Timeout),
        }
        match timeout(remaining(deadline), written_rx).await {
            Ok(Ok(result)) => result,
            Ok(Err(_)) => Err(RpcError::Closed),
            Err(_) => Err(RpcError::Timeout),
        }
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
                    let _ = self.enqueue(bytes, Duration::from_secs(10)).await;
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
                        data: error.get("data").cloned(),
                    })
                } else {
                    Ok(object.get("result").cloned().unwrap_or(Value::Null))
                };
                let _ = sender.send(result);
            }
        }
    }
}

fn remaining(deadline: tokio::time::Instant) -> Duration {
    deadline.saturating_duration_since(tokio::time::Instant::now())
}

async fn fail_pending(pending: &Pending) {
    let mut pending = pending.lock().await;
    for (_, sender) in pending.drain() {
        let _ = sender.send(Err(RpcError::Closed));
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
                    .request("slow", json!({}), Duration::from_millis(50))
                    .await,
                Err(RpcError::Timeout)
            ));
            assert!(client.pending.lock().await.is_empty());
        });
    }

    #[test]
    fn dropped_request_frees_its_pending_id() {
        let runtime = tokio::runtime::Runtime::new().unwrap();
        runtime.block_on(async {
            let (client_io, mut server_io) = tokio::io::duplex(1024);
            let client = Client::new(client_io);
            let server = tokio::spawn(async move {
                let mut line = String::new();
                BufReader::new(&mut server_io)
                    .read_line(&mut line)
                    .await
                    .unwrap();
            });
            let _ = tokio::time::timeout(
                Duration::from_millis(50),
                client.request("slow", json!({}), Duration::from_secs(30)),
            )
            .await;
            server.await.unwrap();
            tokio::time::timeout(Duration::from_secs(1), async {
                while client.pending_len().await != 0 {
                    tokio::task::yield_now().await;
                }
            })
            .await
            .expect("dropped request remained pending");
            assert_eq!(client.pending_len().await, 0);
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
                    .request("slow", json!({}), Duration::from_millis(50))
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
                    json!({"id":sent["id"],"error":{"code":7,"message":"bad","data":{"reason":"x"}}}),
                    &notifications,
                )
                .await;
            assert!(matches!(
                request.await.unwrap(),
                Err(RpcError::Remote { code: 7, data: Some(data), .. }) if data == json!({"reason":"x"})
            ));
        });
    }

    #[test]
    fn overlapping_requests_can_complete_out_of_order() {
        let runtime = tokio::runtime::Runtime::new().unwrap();
        runtime.block_on(async {
            let (client_io, server_io) = tokio::io::duplex(4096);
            let client = Client::new(client_io);
            let first = {
                let c = client.clone();
                tokio::spawn(
                    async move { c.request("slow", json!({}), Duration::from_secs(1)).await },
                )
            };
            let second = {
                let c = client.clone();
                tokio::spawn(
                    async move { c.request("fast", json!({}), Duration::from_secs(1)).await },
                )
            };
            let mut server = BufReader::new(server_io);
            let mut ids = HashMap::new();
            for _ in 0..2 {
                let mut line = String::new();
                server.read_line(&mut line).await.unwrap();
                let request: Value = serde_json::from_str(&line).unwrap();
                ids.insert(
                    request["method"].as_str().unwrap().to_owned(),
                    request["id"].clone(),
                );
            }
            let (notifications, _) = mpsc::channel(1);
            client
                .dispatch(json!({"id": ids["fast"], "result": "fast"}), &notifications)
                .await;
            client
                .dispatch(json!({"id": ids["slow"], "result": "slow"}), &notifications)
                .await;
            assert_eq!(second.await.unwrap().unwrap(), json!("fast"));
            assert_eq!(first.await.unwrap().unwrap(), json!("slow"));
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
            let client = Client::with_write_timeout(client_io, Duration::from_millis(20));
            let result = client
                .request(
                    "large",
                    json!({"data": "x".repeat(4096)}),
                    Duration::from_secs(5),
                )
                .await;
            assert!(matches!(result, Err(RpcError::WriteTimeout)));
            assert!(client.closed.load(Ordering::Acquire));
        });
    }

    #[test]
    fn notify_to_a_stuck_sink_is_bounded() {
        let runtime = tokio::runtime::Runtime::new().unwrap();
        runtime.block_on(async {
            let (client_io, _server_io) = tokio::io::duplex(1);
            let client = Client::with_write_timeout(client_io, Duration::from_millis(20));
            let result = timeout(
                Duration::from_millis(100),
                client.notify(
                    "large",
                    json!({"data": "x".repeat(4096)}),
                    Duration::from_secs(5),
                ),
            )
            .await
            .expect("notify must not block forever");
            assert!(matches!(result, Err(RpcError::WriteTimeout)));
            assert!(client.closed.load(Ordering::Acquire));
        });
    }
}
