"""Test-only stdio entry point for the in-memory API backend."""

from __future__ import annotations

import asyncio

from bca.api.server import MemoryBackend, run_stdio


def main() -> None:
    asyncio.run(run_stdio(MemoryBackend()))


if __name__ == "__main__":
    main()
