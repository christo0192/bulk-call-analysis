from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Any


def _scenario(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


async def run(path: Path, argv_overrides: list[str], argv_disabled: list[str]) -> None:
    scenario = _scenario(path)
    env_report = scenario.get("env_report")
    if isinstance(env_report, str):
        Path(env_report).write_text(
            json.dumps(
                {key: value for key, value in os.environ.items() if "API_KEY" in key or key == "CODEX_HOME"}
            ),
            encoding="utf-8",
        )
    log_path = Path(str(path) + ".methods.json")
    methods: list[str | dict[str, list[str]]] = [
        {"argv_overrides": argv_overrides, "argv_disabled": argv_disabled}
    ]
    turns = 0
    quota = int(scenario.get("used_percent", 0))
    threads: dict[str, str] = {}
    turn_threads: dict[str, str] = {}

    async def send(message: dict[str, Any]) -> None:
        sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
        sys.stdout.flush()

    async def handle(message: dict[str, Any]) -> None:
        nonlocal turns, quota
        method, request_id, params = message.get("method"), message.get("id"), message.get("params") or {}
        if not isinstance(method, str):
            return
        methods.append(method)
        if method == "initialize":
            result: dict[str, Any] = {}
        elif method == "account/read":
            result = {"account": {"type": "chatgpt"}}
        elif method == "account/rateLimits/read":
            result = {
                "rateLimits": {
                    "limitId": "codex",
                    "primary": {"usedPercent": quota, "windowDurationMins": 300, "resetsAt": 9999999999},
                    "secondary": None,
                    "credits": {"balance": str(scenario.get("credit_balance", "5")), "hasCredits": False},
                    "spendControlReached": scenario.get("spend_control_reached"),
                    "rateLimitReachedType": scenario.get("reached_type"),
                    "normalModelSlug": "codex",
                },
                "ordinaryUsageAllowed": True,
                "rateLimitsByLimitId": {},
            }
        elif method == "model/list":
            result = {"models": scenario.get("models", ["codex"])}
        elif method == "thread/start":
            thread_id = f"thread-{len(threads) + 1}"
            threads[thread_id] = thread_id
            result = {"thread": {"id": thread_id}}
            await send({"method": "thread/started", "params": {"thread": {"id": thread_id}}})
        elif method == "turn/start":
            turns += 1
            thread_id = str(params.get("threadId", "thread-1"))
            turn_id = f"turn-{turns}"
            turn_threads[turn_id] = thread_id
            await send(
                {
                    "method": "turn/started",
                    "params": {
                        "threadId": thread_id,
                        "turn": {"id": turn_id, "status": "inProgress", "items": []},
                    },
                }
            )
            if scenario.get("approval_request"):
                await send(
                    {
                        "id": 9000 + turns,
                        "method": "item/commandExecution/requestApproval",
                        "params": {
                            "itemId": "item",
                            "startedAtMs": 1,
                            "threadId": thread_id,
                            "turnId": turn_id,
                        },
                    }
                )
            lead_match = re.search(r"LEAD_ID=([^\s]+)", json.dumps(params))
            lead = lead_match.group(1) if lead_match else "default"
            errors = scenario.get("errors", {})
            if lead in scenario.get("hangs", []) or scenario.get("hang") == lead:
                result = {"turn": {"id": turn_id}}
                if request_id is not None:
                    await send({"id": request_id, "result": result})
                return
            await asyncio.sleep(float(scenario.get("latency_ms", 0)) / 1000)
            if scenario.get("tool_call") == lead:
                await send(
                    {
                        "method": "item/completed",
                        "params": {
                            "threadId": thread_id,
                            "turnId": turn_id,
                            "item": {"type": "commandExecution"},
                        },
                    }
                )
                await send(
                    {"method": "turn/completed", "params": {"threadId": thread_id, "turn": {"id": turn_id}}}
                )
                result = {"turn": {"id": turn_id}}
                if request_id is not None:
                    await send({"id": request_id, "result": result})
                return
            if scenario.get("reroute") == lead:
                await send(
                    {
                        "method": "model/rerouted",
                        "params": {
                            "threadId": thread_id,
                            "turnId": turn_id,
                            "fromModel": "codex",
                            "toModel": "codex-mini",
                            "reason": "fake",
                        },
                    }
                )
            if lead in errors:
                await send(
                    {
                        "method": "error",
                        "params": {
                            "threadId": thread_id,
                            "turnId": turn_id,
                            "willRetry": False,
                            "error": {"message": "fake error", "codexErrorInfo": errors[lead]},
                        },
                    }
                )
            else:
                answer = scenario.get("answers", {}).get(lead, scenario.get("default_answer", {"ok": True}))
                text = answer if isinstance(answer, str) else json.dumps(answer)
                await send(
                    {
                        "method": "item/agentMessage/delta",
                        "params": {"threadId": thread_id, "turnId": turn_id, "itemId": "item", "delta": text},
                    }
                )
                await send(
                    {
                        "method": "item/completed",
                        "params": {
                            "threadId": thread_id,
                            "turnId": turn_id,
                            "completedAtMs": 1,
                            "item": {"type": "agentMessage", "id": "item", "text": text},
                        },
                    }
                )
                quota += int(scenario.get("quota_increment", 0))
                if scenario.get("credit_drop"):
                    await send(
                        {
                            "method": "account/rateLimits/updated",
                            "params": {
                                "rateLimits": {
                                    "primary": {
                                        "usedPercent": quota,
                                        "windowDurationMins": 300,
                                        "resetsAt": 9999999999,
                                    },
                                    "credits": {"balance": "0", "hasCredits": True},
                                }
                            },
                        }
                    )
                await send(
                    {
                        "method": "turn/completed",
                        "params": {
                            "threadId": thread_id,
                            "turn": {"id": turn_id, "status": "completed", "items": [], "usage": {}},
                        },
                    }
                )
                update: dict[str, Any] = {
                    "primary": {
                        "usedPercent": quota,
                        "windowDurationMins": 300,
                        "resetsAt": 9999999999,
                    }
                }
                if scenario.get("credit_drop"):
                    update["credits"] = {"balance": "0", "hasCredits": True}
                await send({"method": "account/rateLimits/updated", "params": {"rateLimits": update}})
            result = {"turn": {"id": turn_id}}
        elif method == "turn/interrupt":
            turn_id = str(params.get("turnId", ""))
            if not scenario.get("ignore_interrupt"):
                await send(
                    {
                        "method": "turn/completed",
                        "params": {
                            "threadId": turn_threads.get(turn_id),
                            "turn": {"id": turn_id, "status": "interrupted"},
                        },
                    }
                )
            result = {}
        elif method == "thread/unsubscribe":
            result = {}
        elif method == "initialized":
            return
        else:
            result = {}
        if request_id is not None:
            await send({"id": request_id, "result": result})
            crash_once = scenario.get("crash_once_file")
            should_crash = scenario.get("crash_after_turns") and turns >= int(scenario["crash_after_turns"])
            if should_crash and isinstance(crash_once, str) and Path(crash_once).exists():
                should_crash = False
            if should_crash and isinstance(crash_once, str):
                Path(crash_once).write_text("crashed", encoding="utf-8")
            if should_crash:
                raise SystemExit(2)

    for line in sys.stdin:
        try:
            await handle(json.loads(line))
        except SystemExit:
            raise
        except (json.JSONDecodeError, TypeError):
            continue
    log_path.write_text(json.dumps(methods), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", type=Path, required=True)
    parser.add_argument("app_server", nargs="?")
    parser.add_argument("-c", dest="argv_overrides", action="append", default=[])
    parser.add_argument("--disable", dest="argv_disabled", action="append", default=[])
    args, _ = parser.parse_known_args()
    asyncio.run(run(args.scenario, args.argv_overrides, args.argv_disabled))


if __name__ == "__main__":
    main()
