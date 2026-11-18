from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from bca.store.spec import QuoteField

Speaker = Literal["learner", "advisor", "any"]


@dataclass(frozen=True)
class QuoteCheck:
    ok: bool
    reason: str
    diag: str = ""


def _normalise(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def check_quote(
    quote: str,
    call_text: str,
    speaker: Speaker,
    speaker_labels: Mapping[str, list[str] | tuple[str, ...]],
) -> QuoteCheck:
    expected = _normalise(quote)
    lines: list[tuple[str, str]] = []
    for raw in call_text.splitlines():
        match = re.match(r"\s*([^:]+):\s*(.*)", raw)
        if match:
            lines.append((match.group(1).strip(), _normalise(match.group(2))))
    allowed = (
        set(speaker_labels.get(speaker, ()))
        if speaker != "any"
        else set(label for labels in speaker_labels.values() for label in labels)
    )
    speaker_text = " ".join(text for label, text in lines if label in allowed)
    if expected and expected in speaker_text:
        return QuoteCheck(True, "exact match")
    all_text = " ".join(text for _, text in lines)
    if expected and expected in all_text and speaker != "any":
        return QuoteCheck(False, "wrong speaker", "quote found under another speaker")
    if expected:
        from difflib import SequenceMatcher

        candidates = [text for _, text in lines]
        ratio = max((SequenceMatcher(None, expected, text).ratio() for text in candidates), default=0.0)
        if ratio >= 0.85:
            return QuoteCheck(False, "near miss", f"fuzzy ratio={ratio:.3f}")
    return QuoteCheck(False, "not found", "no exact normalised substring")


def _call_section(bundle: str, number: int) -> str:
    marker = re.compile(r"^=== CALL (\d+) of (\d+)\s*$", re.MULTILINE)
    matches = list(marker.finditer(bundle))
    for index, match in enumerate(matches):
        if int(match.group(1)) == number:
            end = matches[index + 1].start() if index + 1 < len(matches) else len(bundle)
            return bundle[match.end() : end]
    return ""


def check_answer(
    answer: Mapping[str, object],
    quote_fields: tuple[QuoteField, ...],
    bundle_text: str,
    speaker_labels: Mapping[str, list[str] | tuple[str, ...]] | None = None,
) -> list[QuoteCheck]:
    labels = speaker_labels or {
        "learner": ["Customer", "Lead", "Learner"],
        "advisor": ["Agent", "Advisor", "PA"],
    }
    call = answer.get("call_number")
    section = (
        _call_section(bundle_text, call)
        if isinstance(call, int) and not isinstance(call, bool)
        else bundle_text
    )
    checks: list[QuoteCheck] = []
    for field in quote_fields:
        value = answer.get(field.field)
        checks.append(
            check_quote(value, section, field.speaker, labels)
            if isinstance(value, str)
            else QuoteCheck(False, "not found", f"{field.field} is not a string")
        )
    return checks


def repair_prompt(answer: Mapping[str, object], failures: list[QuoteCheck], instructions: str) -> str:
    details = "; ".join(f"{item.reason}: {item.diag}" for item in failures)
    return (
        f"{instructions}\nRepair only the failed quote fields. Return the complete answer JSON, "
        f"preserving all valid values. Diagnostics: {details}"
    )
