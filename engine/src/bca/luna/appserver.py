from __future__ import annotations

import asyncio
import os
import signal
from dataclasses import dataclass
from pathlib import Path

from bca import childenv

from .rpc import AppServerClient

LOCKDOWN_OVERRIDES: tuple[tuple[str, str], ...] = (
    ("approval_policy", "never"),
    ("sandbox_mode", "read-only"),
    ("history.persistence", "none"),
    ("check_for_update_on_startup", "false"),
    ("forced_login_method", "chatgpt"),
    ("cli_auth_credentials_store", "keyring"),
    ("web_search", "disabled"),
    ("model_reasoning_summary", "concise"),
)
DISABLED_FEATURES: tuple[str, ...] = (
    "shell_tool",
    "unified_exec",
    "apps",
    "browser_use",
    "browser_use_external",
    "computer_use",
    "view_image",
    "hooks",
    "daemon_auto_start",
    "code_mode_host",
    "skill_mcp_dependency_install",
    "tool_suggest",
    "goals",
    "sleep_tool",
    "workspace_dependencies",
    "fast_mode",
    "ultrafast_mode",
    "multi_agent",
    "memories",
)


@dataclass(frozen=True)
class CodexLaunch:
    exe: Path | tuple[str, ...] | list[str]
    codex_home: Path
    work_dir: Path


def build_command(launch: CodexLaunch) -> list[str]:
    prefix = (
        [str(part) for part in launch.exe] if isinstance(launch.exe, (tuple, list)) else [str(launch.exe)]
    )
    command = [*prefix, "app-server"]
    for key, value in (*LOCKDOWN_OVERRIDES, ("sandbox.readableRoots", str(launch.work_dir))):
        command.extend(["-c", f"{key}={value}"])
    for feature in DISABLED_FEATURES:
        command.extend(["--disable", feature])
    return command


@dataclass
class AppServer:
    client: AppServerClient
    process: asyncio.subprocess.Process

    @property
    def pid(self) -> int | None:
        return self.process.pid

    @property
    def returncode(self) -> int | None:
        return self.process.returncode

    async def stop(self, kill_tree: bool = True) -> None:
        await self.client.close()
        if self.process.returncode is not None:
            return
        if kill_tree and self.process.pid is not None:
            if os.name == "nt":
                killer = await asyncio.create_subprocess_exec(
                    "taskkill", "/PID", str(self.process.pid), "/T", "/F", stderr=asyncio.subprocess.DEVNULL
                )
                await killer.wait()
            else:
                os.killpg(self.process.pid, signal.SIGTERM)  # type: ignore[attr-defined]
        else:
            self.process.terminate()
        try:
            await asyncio.wait_for(self.process.wait(), 5)
        except TimeoutError:
            self.process.kill()
            await self.process.wait()


async def start(
    launch: CodexLaunch, client_name: str = "bulk_call_analysis", version: str = "0.1.0"
) -> AppServer:
    command = build_command(launch)
    stderr_path = launch.work_dir / "codex-app-server.log"
    stderr = stderr_path.open("ab")
    kwargs: dict[str, object] = {
        "stdin": asyncio.subprocess.PIPE,
        "stdout": asyncio.subprocess.PIPE,
        "stderr": stderr,
        "cwd": str(launch.work_dir),
        "env": childenv.build_child_env({"CODEX_HOME": str(launch.codex_home)}),
    }
    if os.name != "nt":
        kwargs["start_new_session"] = True
    try:
        process = await asyncio.create_subprocess_exec(*command, **kwargs)  # type: ignore[arg-type]
    except BaseException:
        stderr.close()
        raise
    stderr.close()
    if process.stdout is None or process.stdin is None:
        raise RuntimeError("app-server pipes were not created")
    client = AppServerClient(process.stdout, process.stdin)
    try:
        await client.request(
            "initialize",
            {"clientInfo": {"name": client_name, "title": "Bulk Call Analysis", "version": version}},
        )
    except ConnectionError as exc:
        if process.returncode is None:
            await process.wait()
        await client.close()
        try:
            stderr_tail = stderr_path.read_text(encoding="utf-8", errors="replace")[-2000:]
        except OSError as read_error:
            raise ConnectionError(f"{exc}; unable to read app-server stderr: {read_error}") from exc
        detail = stderr_tail.strip() or "<no stderr>"
        raise ConnectionError(f"{exc}; app-server stderr tail: {detail}") from exc
    client._write({"method": "initialized", "params": {}})
    return AppServer(client, process)
