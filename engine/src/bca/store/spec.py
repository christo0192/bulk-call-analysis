from __future__ import annotations

import json
import math
import os
import re
import tempfile
import time
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path, PurePath, PureWindowsPath
from typing import Literal, cast

Speaker = Literal["learner", "advisor", "any"]
ConditionOp = Literal["eq", "ne", "in", "not_in", "lt", "le", "gt", "ge", "is_null", "not_null", "contains"]
type Rule = "InScopeRule | ThresholdGroupRule | ReviewFlagRule"


class SpecError(ValueError):
    """Raised when a job specification is malformed."""


def _fail(path: str, message: str) -> SpecError:
    return SpecError(f"{path}: {message}")


def _obj(value: object, path: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise _fail(path, "must be an object")
    return cast(dict[str, object], value)


def _str(data: dict[str, object], key: str, *, nonempty: bool = True, path: str | None = None) -> str:
    value = data.get(key)
    if not isinstance(value, str) or (nonempty and not value):
        raise _fail(path or key, "must be a non-empty string")
    return value


def _int(data: dict[str, object], key: str, *, minimum: int | None = None, path: str | None = None) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or (minimum is not None and value < minimum):
        suffix = f" >= {minimum}" if minimum is not None else ""
        raise _fail(path or key, f"must be an integer{suffix}")
    return value


def _tuple_str(data: dict[str, object], key: str, *, path: str | None = None) -> tuple[str, ...]:
    value = data.get(key)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise _fail(path or key, "must be an array of strings")
    return tuple(value)


def _finite_number(value: object, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise _fail(path, "must be a finite number")
    return float(value)


@dataclass(frozen=True)
class LeadSourceSql:
    kind: Literal["sql"]
    sql: str

    def __init__(self, sql: str, kind: Literal["sql"] = "sql") -> None:
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "sql", sql)

    def to_dict(self) -> dict[str, object]:
        return {"kind": self.kind, "sql": self.sql}

    @classmethod
    def from_dict(cls, value: object) -> LeadSourceSql:
        data = _obj(value, "lead_source")
        if data.get("kind") != "sql":
            raise _fail("lead_source.kind", "must be 'sql'")
        return cls(_str(data, "sql", path="lead_source.sql"))


@dataclass(frozen=True)
class LeadSourceIds:
    kind: Literal["ids"]
    ids_file: str
    count: int

    def __init__(self, ids_file: str, count: int, kind: Literal["ids"] = "ids") -> None:
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "ids_file", ids_file)
        object.__setattr__(self, "count", count)

    def to_dict(self) -> dict[str, object]:
        return {"kind": self.kind, "ids_file": self.ids_file, "count": self.count}

    @classmethod
    def from_dict(cls, value: object) -> LeadSourceIds:
        data = _obj(value, "lead_source")
        if data.get("kind") != "ids":
            raise _fail("lead_source.kind", "must be 'ids'")
        name = _str(data, "ids_file", path="lead_source.ids_file")
        path = PurePath(name)
        if (
            PureWindowsPath(name).drive
            or path.is_absolute()
            or ".." in path.parts
            or ".." in PureWindowsPath(name).parts
        ):
            raise _fail("lead_source.ids_file", "must be a relative path without parent components")
        return cls(
            name,
            _int(data, "count", minimum=0, path="lead_source.count"),
        )


@dataclass(frozen=True)
class UsableRule:
    min_chars: int = 1
    statuses: tuple[str, ...] | None = None
    min_call_seconds: int | None = None
    min_calls_over: tuple[int, int] | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self) | {
            "statuses": list(self.statuses) if self.statuses is not None else None,
            "min_calls_over": list(self.min_calls_over) if self.min_calls_over is not None else None,
        }

    @classmethod
    def from_dict(cls, value: object) -> UsableRule:
        data = _obj(value, "usable")
        min_chars = _int(data, "min_chars", minimum=0, path="usable.min_chars") if "min_chars" in data else 1
        statuses = (
            tuple(_tuple_str(data, "statuses", path="usable.statuses"))
            if data.get("statuses") is not None
            else None
        )
        if statuses is not None and not statuses:
            raise _fail("usable.statuses", "must not be empty")
        min_seconds = data.get("min_call_seconds")
        if min_seconds is not None and (
            isinstance(min_seconds, bool) or not isinstance(min_seconds, int) or min_seconds < 0
        ):
            raise _fail("usable.min_call_seconds", "must be a non-negative integer or null")
        over = data.get("min_calls_over")
        pair: tuple[int, int] | None = None
        if over is not None:
            if (
                not isinstance(over, list)
                or len(over) != 2
                or any(isinstance(x, bool) or not isinstance(x, int) or x < 0 for x in over)
            ):
                raise _fail(
                    "usable.min_calls_over", "must be a two-item array of non-negative integers or null"
                )
            pair = (over[0], over[1])
        return cls(min_chars, statuses, min_seconds, pair)


@dataclass(frozen=True)
class CapRule:
    max_chars: int = 400_000
    policy: Literal["drop_short_then_oldest", "keep_newest"] = "drop_short_then_oldest"
    drop_short_s: int = 180

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: object) -> CapRule:
        data = _obj(value, "cap")
        max_chars = (
            _int(data, "max_chars", minimum=1, path="cap.max_chars") if "max_chars" in data else 400_000
        )
        policy = data.get("policy", "drop_short_then_oldest")
        if policy not in ("drop_short_then_oldest", "keep_newest"):
            raise _fail("cap.policy", "invalid policy")
        drop = (
            _int(data, "drop_short_s", minimum=0, path="cap.drop_short_s") if "drop_short_s" in data else 180
        )
        return cls(max_chars, policy, drop)


@dataclass(frozen=True)
class QuoteField:
    field: str
    speaker: Speaker

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: object) -> QuoteField:
        data = _obj(value, "quote_fields[]")
        field = _str(data, "field", path="quote_fields[].field")
        speaker = data.get("speaker")
        if speaker not in ("learner", "advisor", "any"):
            raise _fail("quote_fields[].speaker", "invalid speaker")
        return cls(field, speaker)


Scalar = str | int | float | bool


@dataclass(frozen=True)
class Condition:
    field: str
    op: ConditionOp
    value: Scalar | list[Scalar] | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: object) -> Condition:
        data = _obj(value, "conditions[]")
        field = _str(data, "field", path="conditions[].field")
        op = data.get("op")
        ops = ("eq", "ne", "in", "not_in", "lt", "le", "gt", "ge", "is_null", "not_null", "contains")
        if op not in ops:
            raise _fail("conditions[].op", "invalid operator")
        item = data.get("value")
        if op in ("is_null", "not_null") and "value" in data:
            raise _fail("conditions[].value", "must not be provided")
        if op in ("in", "not_in") and (not isinstance(item, list) or not item):
            raise _fail("conditions[].value", "must be a non-empty list")
        if op in ("lt", "le", "gt", "ge") and not (
            isinstance(item, (str, int, float)) and not isinstance(item, bool)
        ):
            raise _fail("conditions[].value", "must be a finite number or string")
        if op in ("lt", "le", "gt", "ge") and isinstance(item, float) and not math.isfinite(item):
            raise _fail("conditions[].value", "must be a finite number or string")
        if item is not None and not isinstance(item, (str, int, float, bool, list)):
            raise _fail("conditions[].value", "must be a scalar, array, or null")
        if isinstance(item, float) and not math.isfinite(item):
            raise _fail("conditions[].value", "must be finite")
        if isinstance(item, list) and (
            not item
            or any(
                not isinstance(x, (str, int, float, bool)) or (isinstance(x, float) and not math.isfinite(x))
                for x in item
            )
        ):
            raise _fail("conditions[].value", "array contains invalid value")
        return cls(field, cast(ConditionOp, op), cast(Scalar | list[Scalar] | None, item))


def _conditions(data: dict[str, object], key: str = "conditions") -> tuple[Condition, ...]:
    value = data.get(key)
    if not isinstance(value, list):
        raise _fail(key, "must be an array")
    if not value:
        raise _fail(key, "must contain at least one condition")
    return tuple(Condition.from_dict(item) for item in value)


@dataclass(frozen=True)
class InScopeRule:
    kind: Literal["in_scope"]
    match: Literal["all", "any"]
    conditions: tuple[Condition, ...]

    def __init__(
        self,
        match: Literal["all", "any"],
        conditions: tuple[Condition, ...],
        kind: Literal["in_scope"] = "in_scope",
    ) -> None:
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "match", match)
        object.__setattr__(self, "conditions", conditions)

    def to_dict(self) -> dict[str, object]:
        return {"kind": self.kind, "match": self.match, "conditions": [x.to_dict() for x in self.conditions]}

    @classmethod
    def from_dict(cls, value: object) -> InScopeRule:
        data = _obj(value, "rules[]")
        if data.get("kind") != "in_scope":
            raise _fail("rules[].kind", "must be 'in_scope'")
        match = data.get("match")
        if match not in ("all", "any"):
            raise _fail("rules[].match", "must be all or any")
        return cls(match, _conditions(data))


@dataclass(frozen=True)
class ThresholdGroupRule:
    kind: Literal["threshold_group"]
    name: str
    field: str
    edges: tuple[float, ...]
    labels: tuple[str, ...]
    inclusive_upper: bool = True

    def __init__(
        self,
        name: str,
        field: str,
        edges: tuple[float, ...],
        labels: tuple[str, ...],
        inclusive_upper: bool = True,
        kind: Literal["threshold_group"] = "threshold_group",
    ) -> None:
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "field", field)
        object.__setattr__(self, "edges", edges)
        object.__setattr__(self, "labels", labels)
        object.__setattr__(self, "inclusive_upper", inclusive_upper)

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "name": self.name,
            "field": self.field,
            "edges": list(self.edges),
            "labels": list(self.labels),
            "inclusive_upper": self.inclusive_upper,
        }

    @classmethod
    def from_dict(cls, value: object) -> ThresholdGroupRule:
        data = _obj(value, "rules[]")
        if data.get("kind") != "threshold_group":
            raise _fail("rules[].kind", "must be 'threshold_group'")
        edges = data.get("edges")
        labels = data.get("labels")
        if not isinstance(edges, list) or any(
            isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item)
            for item in edges
        ):
            raise _fail("rules[].edges", "must be an array of numbers")
        if (
            not isinstance(labels, list)
            or any(not isinstance(item, str) for item in labels)
            or len(labels) != len(edges) + 1
        ):
            raise _fail("rules[].labels", "must have one more item than edges")
        if any(not label for label in labels) or len(set(labels)) != len(labels):
            raise _fail("rules[].labels", "must be non-empty and unique")
        if any(float(edges[index]) >= float(edges[index + 1]) for index in range(len(edges) - 1)):
            raise _fail("rules[].edges", "must be strictly increasing")
        inclusive = data.get("inclusive_upper", True)
        if not isinstance(inclusive, bool):
            raise _fail("rules[].inclusive_upper", "must be boolean")
        return cls(
            _str(data, "name", path="rules[].name"),
            _str(data, "field", path="rules[].field"),
            tuple(edges),
            tuple(labels),
            inclusive,
        )


@dataclass(frozen=True)
class ReviewFlagRule:
    kind: Literal["review_flag"]
    name: str
    match: Literal["all", "any"]
    conditions: tuple[Condition, ...]

    def __init__(
        self,
        name: str,
        match: Literal["all", "any"],
        conditions: tuple[Condition, ...],
        kind: Literal["review_flag"] = "review_flag",
    ) -> None:
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "match", match)
        object.__setattr__(self, "conditions", conditions)

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "name": self.name,
            "match": self.match,
            "conditions": [x.to_dict() for x in self.conditions],
        }

    @classmethod
    def from_dict(cls, value: object) -> ReviewFlagRule:
        data = _obj(value, "rules[]")
        if data.get("kind") != "review_flag":
            raise _fail("rules[].kind", "must be 'review_flag'")
        match = data.get("match")
        if match not in ("all", "any"):
            raise _fail("rules[].match", "must be all or any")
        return cls(_str(data, "name", path="rules[].name"), match, _conditions(data))


def _rule(value: object) -> InScopeRule | ThresholdGroupRule | ReviewFlagRule:
    data = _obj(value, "rules[]")
    kind = data.get("kind")
    if kind in ("in_scope", "review_flag"):
        match = data.get("match")
        if match not in ("all", "any"):
            raise _fail("rules[].match", "must be all or any")
        if kind == "in_scope":
            return InScopeRule.from_dict(data)
        return ReviewFlagRule.from_dict(data)
    if kind == "threshold_group":
        edges = data.get("edges")
        labels = data.get("labels")
        if not isinstance(edges, list) or any(
            isinstance(x, bool) or not isinstance(x, (int, float)) for x in edges
        ):
            raise _fail("rules[].edges", "must be an array of numbers")
        if (
            not isinstance(labels, list)
            or any(not isinstance(x, str) for x in labels)
            or len(labels) != len(edges) + 1
        ):
            raise _fail("rules[].labels", "must have one more item than edges")
        inclusive = data.get("inclusive_upper", True)
        if not isinstance(inclusive, bool):
            raise _fail("rules[].inclusive_upper", "must be boolean")
        return ThresholdGroupRule.from_dict(data)
    raise _fail("rules[].kind", "invalid rule kind")


@dataclass(frozen=True)
class JobSpec:
    spec_version: int
    job_id: str
    project_id: str
    title: str
    question: str
    lead_source: LeadSourceSql | LeadSourceIds
    calls_scope: Literal["all", "window"] = "all"
    window_start: str | None = None
    window_end: str | None = None
    usable: UsableRule = UsableRule()
    cap: CapRule = CapRule()
    instructions: str = ""
    output_schema: dict[str, object] = None  # type: ignore[assignment]
    quote_fields: tuple[QuoteField, ...] = ()
    rules: tuple[InScopeRule | ThresholdGroupRule | ReviewFlagRule, ...] = ()
    luna_model: str = ""
    luna_effort: str = ""
    sample_size: int = 20

    def to_dict(self) -> dict[str, object]:
        return {
            "spec_version": self.spec_version,
            "job_id": self.job_id,
            "project_id": self.project_id,
            "title": self.title,
            "question": self.question,
            "lead_source": self.lead_source.to_dict(),
            "calls_scope": self.calls_scope,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "usable": self.usable.to_dict(),
            "cap": self.cap.to_dict(),
            "instructions": self.instructions,
            "output_schema": self.output_schema,
            "quote_fields": [x.to_dict() for x in self.quote_fields],
            "rules": [x.to_dict() for x in self.rules],
            "luna_model": self.luna_model,
            "luna_effort": self.luna_effort,
            "sample_size": self.sample_size,
        }

    @classmethod
    def from_dict(cls, value: object) -> JobSpec:
        data = _obj(value, "spec")
        allowed = {
            "spec_version",
            "job_id",
            "project_id",
            "title",
            "question",
            "lead_source",
            "calls_scope",
            "window_start",
            "window_end",
            "usable",
            "cap",
            "instructions",
            "output_schema",
            "quote_fields",
            "rules",
            "luna_model",
            "luna_effort",
            "sample_size",
        }
        unknown = set(data) - allowed
        if unknown:
            raise _fail(next(iter(sorted(unknown))), "unknown field")
        version = _int(data, "spec_version", minimum=1) if "spec_version" in data else 1
        scope = data.get("calls_scope", "all")
        if scope not in ("all", "window"):
            raise _fail("calls_scope", "must be all or window")
        start, end = data.get("window_start"), data.get("window_end")
        if scope == "window":
            if not isinstance(start, str) or not isinstance(end, str):
                raise _fail("window_start", "window scope requires start and end")
            try:
                from datetime import date

                start_date, end_date = date.fromisoformat(start), date.fromisoformat(end)
            except ValueError as exc:
                raise _fail("window_start", "must be an ISO date") from exc
            if start_date > end_date:
                raise _fail("window_start", "must be on or before window_end")
        elif start is not None or end is not None:
            raise _fail("window_start", "must be null when calls_scope is all")
        schema = data.get("output_schema")
        if (
            not isinstance(schema, dict)
            or schema.get("type") != "object"
            or not isinstance(schema.get("properties"), dict)
        ):
            raise _fail("output_schema", "must be an object schema with type object and properties")
        quote = data.get("quote_fields", [])
        rules = data.get("rules", [])
        if not isinstance(quote, list):
            raise _fail("quote_fields", "must be an array")
        if not isinstance(rules, list):
            raise _fail("rules", "must be an array")
        quote_fields = tuple(QuoteField.from_dict(x) for x in quote)
        properties = cast(dict[str, object], schema["properties"])
        if any(field.field not in properties for field in quote_fields):
            raise _fail("quote_fields", "every quote field must exist in output_schema.properties")
        parsed_rules = tuple(_rule(x) for x in rules)
        names = [rule.name for rule in parsed_rules if hasattr(rule, "name")]
        if len(names) != len(set(names)):
            raise _fail("rules", "rule names must be unique")
        return cls(
            version,
            _str(data, "job_id"),
            _str(data, "project_id"),
            _str(data, "title"),
            _str(data, "question"),
            LeadSourceSql.from_dict(data["lead_source"])
            if isinstance(data.get("lead_source"), dict)
            and cast(dict[str, object], data["lead_source"]).get("kind") == "sql"
            else LeadSourceIds.from_dict(data.get("lead_source")),
            scope,
            start,
            end,
            UsableRule.from_dict(data.get("usable", {})),
            CapRule.from_dict(data.get("cap", {})),
            _str(data, "instructions", nonempty=False),
            cast(dict[str, object], schema),
            quote_fields,
            parsed_rules,
            _str(data, "luna_model"),
            _str(data, "luna_effort"),
            _sample_size(data),
        )


def _sample_size(data: dict[str, object]) -> int:
    value = _int(data, "sample_size", minimum=1) if "sample_size" in data else 20
    if value > 200:
        raise _fail("sample_size", "must be <= 200")
    return value


def save_spec(spec: JobSpec, job_dir: Path) -> Path:
    job_dir.mkdir(parents=True, exist_ok=True)
    # The lock is deliberately a file created exclusively so separate processes share it.
    lock_path = job_dir / ".spec.lock"
    while True:
        try:
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(lock_fd)
            break
        except FileExistsError:
            time.sleep(0.001)
    try:
        version = 1
        pattern = re.compile(r"^spec\.v(\d+)\.json$")
        for path in job_dir.iterdir():
            match = pattern.fullmatch(path.name)
            if match:
                version = max(version, int(match.group(1)) + 1)
        while True:
            target = job_dir / f"spec.v{version}.json"
            fd, name = tempfile.mkstemp(prefix=".spec.", suffix=".tmp", dir=job_dir)
            try:
                candidate = JobSpec.from_dict(spec.to_dict() | {"spec_version": version})
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                    json.dump(candidate.to_dict(), handle, indent=2)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    with target.open("x", encoding="utf-8", newline="\n") as handle:
                        handle.write(Path(name).read_text(encoding="utf-8"))
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.unlink(name)
                    return target
                except FileExistsError:
                    version += 1
            finally:
                with suppress(OSError):
                    os.unlink(name)
    finally:
        with suppress(OSError):
            lock_path.unlink()


def load_latest_spec(job_dir: Path) -> tuple[JobSpec, int]:
    pattern = re.compile(r"^spec\.v(\d+)\.json$")
    paths = [path for path in job_dir.iterdir() if pattern.fullmatch(path.name)]
    paths.sort(key=lambda p: int(pattern.fullmatch(p.name).group(1)))  # type: ignore[union-attr]
    if not paths:
        raise SpecError("spec: no versioned spec found")
    path = paths[-1]
    match = pattern.fullmatch(path.name)
    assert match is not None
    version = int(match.group(1))
    try:
        with path.open(encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise SpecError(f"spec: cannot read {path.name}: {exc}") from exc
    spec = JobSpec.from_dict(raw)
    if spec.spec_version != version:
        raise SpecError("spec_version: does not match filename")
    return spec, version
