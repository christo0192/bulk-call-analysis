import json
from pathlib import Path

import pytest
from jsonschema import Draft7Validator

from tests.fakes import fake_codex


def test_fake_protocol_samples_match_pinned_v2_schemas() -> None:
    schema_path = (
        Path(__file__).parents[1]
        / "fixtures"
        / "codex_schema_0_165_0"
        / "codex_app_server_protocol.v2.schemas.json"
    )
    document = json.loads(schema_path.read_text(encoding="utf-8"))
    definitions = document["definitions"]

    def validate(name: str, value: object) -> None:
        schema = {"$schema": document["$schema"], "$ref": f"#/definitions/{name}", "definitions": definitions}
        Draft7Validator(schema).validate(value)

    validate(
        "ModelListResponse",
        {
            "data": [
                {
                    "id": "codex",
                    "model": "codex",
                    "defaultReasoningEffort": "low",
                    "description": "synthetic model",
                    "displayName": "codex",
                    "hidden": False,
                    "isDefault": True,
                    "supportedReasoningEfforts": [{"reasoningEffort": "low", "description": "low"}],
                }
            ],
            "nextCursor": None,
        },
    )
    validate("AccountRateLimitsUpdatedNotification", {"rateLimits": {"limitId": "codex"}})
    validate(
        "TurnCompletedNotification",
        {"threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed", "items": []}},
    )
    approval_schema = json.loads(
        (schema_path.parent / "CommandExecutionRequestApprovalResponse.json").read_text(encoding="utf-8")
    )
    Draft7Validator(approval_schema).validate({"decision": "cancel"})


def test_fake_rejects_unknown_scenario_keys(tmp_path: Path) -> None:
    scenario_path = tmp_path / "scenario.json"
    scenario_path.write_text(json.dumps({"unknown": True}), encoding="utf-8")

    with pytest.raises(ValueError, match="unknown fake Codex scenario keys"):
        fake_codex._scenario(scenario_path)
