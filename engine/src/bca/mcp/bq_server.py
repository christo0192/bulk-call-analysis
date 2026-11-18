from __future__ import annotations

from .server_common import TOOLS_BQ, run_shim

if __name__ == "__main__":
    run_shim("bq", TOOLS_BQ)
