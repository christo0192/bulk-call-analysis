from __future__ import annotations

import asyncio
import logging
import os
import sys

from .production import ProductionBackend
from .server import run_stdio


def main() -> None:
    backend = ProductionBackend()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    exit_code = 0
    try:
        result = loop.run_until_complete(run_stdio(backend))
        if isinstance(result, int) and not isinstance(result, bool):
            exit_code = result
    except BaseException:
        logging.getLogger("bca.api").exception("stdio server failed")
        exit_code = 1
    finally:
        loop.run_until_complete(loop.shutdown_asyncgens())
        executor = getattr(loop, "_default_executor", None)
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)
        loop.close()
        asyncio.set_event_loop(None)
        store = getattr(backend, "store", None)
        close = getattr(store, "close", None)
        if callable(close) and not getattr(backend, "_closed", False):
            close()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(exit_code)


if __name__ == "__main__":
    main()
