"""The explicit, deadline-guarded BCA1 IPC wire protocol.

The D-PIPE design uses a standard local multiprocessing endpoint, but never
uses multiprocessing's authenticated pickled protocol.  Each connection
starts with a nonce challenge, followed by a JSON request and response sent as
UTF-8 bytes.  Deadlines and byte limits keep unauthenticated or silent peers
from blocking the engine.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
from collections.abc import Mapping

HANDSHAKE_TIMEOUT = 5.0
REQUEST_TIMEOUT = 30.0
CALL_TIMEOUT = 120.0
MAX_REQUEST_BYTES = 1_000_000
MAX_REPLY_BYTES = 2_000_000
HELLO_PREFIX = b"BCA1"
OK = b"OK"


def ipc_family() -> str:
    return "AF_PIPE" if sys.platform == "win32" else "AF_UNIX"


def make_hello(authkey: bytes) -> tuple[bytes, bytes]:
    nonce = os.urandom(32)
    return HELLO_PREFIX + nonce, hmac.new(authkey, nonce, hashlib.sha256).digest()


def expected_mac(authkey: bytes, hello: bytes) -> bytes:
    return hmac.new(authkey, hello[4:], hashlib.sha256).digest()


def encode_json(value: Mapping[str, object]) -> bytes:
    return json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")


def decode_json(payload: bytes) -> object:
    return json.loads(payload.decode("utf-8"))
