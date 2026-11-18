from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any


def _scenario(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


async def run(path: Path) -> None:
    scenario = _scenario(path)
    log_path = Path(str(path) + ".methods.json")
    methods: list[str] = []
    turns = 0
    quota = int(scenario.get("used_percent", 0))
    threads: dict[str, str] = {}

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
                    "credits": {"balance": str(scenario.get("credit_balance", "0")), "hasCredits": False},
                    "spendControlReached": scenario.get("spend_control_reached"),
                    "rateLimitReachedType": scenario.get("reached_type"),
                    "normalModelSlug": "codex",
                },
                "ordinaryUsageAllowed": True,
                "rateLimitsByLimitId": {},
            }
        elif method == "model/list":
            result = {"models": scenario.get("models", [])}
        elif method == "thread/start":
            thread_id = f"thread-{len(threads) + 1}"
            threads[thread_id] = thread_id
            result = {"thread": {"id": thread_id}}
            await send({"method": "thread/started", "params": {"thread": {"id": thread_id}}})
        elif method == "turn/start":
            turns += 1
            thread_id = str(params.get("threadId", "thread-1"))
            turn_id = f"turn-{turns}"
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
                return
            await asyncio.sleep(float(scenario.get("latency_ms", 0)) / 1000)
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
                answer = scenario.get("answers", {}).get(lead, scenario.get("default_answer", "{}"))
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
                await send(
                    {
                        "method": "turn/completed",
                        "params": {
                            "threadId": thread_id,
                            "turn": {"id": turn_id, "status": "completed", "items": [], "usage": {}},
                        },
                    }
                )
                await send(
                    {
                        "method": "account/rateLimits/updated",
                        "params": {
                            "rateLimits": {
                                "primary": {
                                    "usedPercent": quota,
                                    "windowDurationMins": 300,
                                    "resetsAt": 9999999999,
                                }
                            }
                        },
                    }
                )
            result = {"turn": {"id": turn_id}}
        elif method in {"turn/interrupt", "thread/unsubscribe"}:
            result = {}
        elif method == "initialized":
            return
        else:
            result = {}
        if request_id is not None:
            await send({"id": request_id, "result": result})
        if scenario.get("crash_after_turns") and turns >= int(scenario["crash_after_turns"]):
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
    asyncio.run(run(parser.parse_args().scenario))


if __name__ == "__main__":
    main()
