from __future__ import annotations

import json
from collections import Counter

from bca.store import JobSpec, Store

from .rules import evaluate


def _cap(counter: Counter[str]) -> dict[str, int]:
    common = counter.most_common(50)
    result = dict(common)
    other = sum(counter.values()) - sum(value for _, value in common)
    if other:
        key = "other"
        while key in result:
            key = f"other ({key.count('other') + 1})"
        result[key] = other
    return result


def aggregates(
    store: Store, job_id: str, spec: JobSpec, group_by: list[str] | None = None
) -> dict[str, object]:
    answers: list[dict[str, object]] = []
    for row in store.rows("SELECT answer_json FROM results WHERE job_id=?", (job_id,)):
        value = json.loads(str(row[0]))
        if isinstance(value, dict):
            answers.append(value)
    raw_fields = spec.output_schema.get("properties", {})
    fields = group_by or (list(raw_fields) if isinstance(raw_fields, dict) else [])
    answer_counts = {field: _cap(Counter(str(answer.get(field)) for answer in answers)) for field in fields}
    rules = spec.rules
    rebuilt = store.rows(
        "SELECT data_json FROM job_events WHERE job_id=? AND kind='rules_rebuilt' ORDER BY id DESC LIMIT 1",
        (job_id,),
    )
    if rebuilt:
        from bca.store.spec import _rule

        raw = json.loads(str(rebuilt[0][0]))["rules"]
        rules = tuple(_rule(item) for item in raw)
    rule_counts: dict[str, Counter[str]] = {}
    for answer in answers:
        try:
            evaluated = evaluate(rules, answer, {"lead_id": str(answer.get("lead_id", ""))})
        except TypeError as exc:
            evaluated = {str(getattr(rule, "name", "in_scope")): "rule error" for rule in rules}
            evaluated["rule error"] = str(exc)
        for name, value in evaluated.items():
            rule_counts.setdefault(name, Counter())[str(value)] += 1
    result: dict[str, object] = {
        "answer_fields": answer_counts,
        "rule_results": {name: _cap(counts) for name, counts in rule_counts.items()},
    }
    if len(fields) >= 2:
        cross = Counter((str(answer.get(fields[0])), str(answer.get(fields[1]))) for answer in answers)
        capped = _cap(Counter({f"{left} | {right}": count for (left, right), count in cross.items()}))
        result["crosstab"] = capped
    return result
