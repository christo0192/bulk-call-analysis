from __future__ import annotations

from .server_common import TOOLS_APP, run_shim

if __name__ == "__main__":
    run_shim("app", TOOLS_APP)
