from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from numbers import Real

from bca.store.spec import Condition, InScopeRule, ReviewFlagRule, Rule, ThresholdGroupRule

type RuleResults = dict[str, bool | str]


class RuleTypeError(TypeError):
    """A rule tried to compare incompatible values."""


def _get(path: str, answer: Mapping[str, object], metadata: Mapping[str, object]) -> object:
    explicit = path.split(".", 1)
    if explicit[0] in ("answer", "meta"):
        source = answer if explicit[0] == "answer" else metadata
        parts = explicit[1].split(".") if len(explicit) == 2 else []
    else:
        in_answer = _walk(answer, path.split("."))
        in_meta = _walk(metadata, path.split("."))
        if in_answer is not _MISSING and in_meta is not _MISSING:
            raise RuleTypeError(f"ambiguous field {path!r}")
        return in_answer if in_answer is not _MISSING else in_meta
    return _walk(source, parts)


class _Missing:
    pass


_MISSING = _Missing()


def _walk(value: object, parts: list[str]) -> object:
    current = value
    for part in parts:
        if not isinstance(current, Mapping) or part not in current:
            return _MISSING
        current = current[part]
    return current


def _same_kind(left: object, right: object) -> None:
    if isinstance(left, bool) or isinstance(right, bool):
        if type(left) is not type(right):
            raise RuleTypeError("boolean comparison requires boolean")
    elif isinstance(left, Real) != isinstance(right, Real):
        raise RuleTypeError("numeric and string values cannot be compared")
    elif isinstance(left, Real) and isinstance(right, Real):
        if not math.isfinite(float(left)) or not math.isfinite(float(right)):
            raise RuleTypeError("non-finite numeric values cannot be compared")
    elif type(left) is not type(right) and not (isinstance(left, str) and isinstance(right, str)):
        raise RuleTypeError("values have incompatible types")


def _condition(condition: Condition, answer: Mapping[str, object], metadata: Mapping[str, object]) -> bool:
    actual = _get(condition.field, answer, metadata)
    if condition.op == "is_null":
        return actual is _MISSING or actual is None
    if condition.op == "not_null":
        return actual is not _MISSING and actual is not None
    expected = condition.value
    if actual is _MISSING or actual is None:
        return condition.op in ("ne", "not_in")
    if condition.op in ("lt", "le", "gt", "ge"):
        if expected is None or isinstance(expected, list):
            raise RuleTypeError("ordered comparison needs a scalar")
        _same_kind(actual, expected)
        if (isinstance(actual, str) and isinstance(expected, str)) or (
            isinstance(actual, Real) and isinstance(expected, Real)
        ):
            comparisons = {
                "lt": actual < expected,
                "le": actual <= expected,
                "gt": actual > expected,
                "ge": actual >= expected,
            }
        else:
            raise RuleTypeError("ordered comparison requires matching scalars")
        return comparisons[condition.op]
    if condition.op in ("in", "not_in"):
        if not isinstance(expected, list):
            raise RuleTypeError("membership comparison needs a list")
        for item in expected:
            _same_kind(actual, item)
        found = actual in expected
        return not found if condition.op == "not_in" else found
    if condition.op == "contains":
        if isinstance(actual, str) and isinstance(expected, str):
            return expected.casefold() in actual.casefold()
        if isinstance(actual, list):
            if expected is None or isinstance(expected, list):
                raise RuleTypeError("list contains needs a scalar")
            for item in actual:
                _same_kind(item, expected)
            return expected in actual
        raise RuleTypeError("contains requires a string or list")
    if expected is None:
        return condition.op == "eq" and actual is None
    _same_kind(actual, expected)
    return actual == expected if condition.op == "eq" else actual != expected


def _match(
    match: str, conditions: Sequence[Condition], answer: Mapping[str, object], metadata: Mapping[str, object]
) -> bool:
    if match == "all":
        return all(_condition(condition, answer, metadata) for condition in conditions)
    return any(_condition(condition, answer, metadata) for condition in conditions)


def evaluate(rules: Sequence[Rule], answer: dict[str, object], metadata: dict[str, object]) -> RuleResults:
    result: RuleResults = {}
    for rule in rules:
        if isinstance(rule, InScopeRule):
            result["in_scope"] = _match(rule.match, rule.conditions, answer, metadata)
        elif isinstance(rule, ReviewFlagRule):
            result[rule.name] = _match(rule.match, rule.conditions, answer, metadata)
        else:
            value = _get(rule.field, answer, metadata)
            if value is _MISSING or value is None:
                result[rule.name] = "unknown"
                continue
            if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(float(value)):
                raise RuleTypeError(f"threshold field {rule.field!r} is not numeric")
            index = 0
            for edge in rule.edges:
                if value <= edge if rule.inclusive_upper else value < edge:
                    break
                index += 1
            result[rule.name] = rule.labels[index]
    return result


def validate_rules(rules: Sequence[Rule], output_schema: Mapping[str, object]) -> None:
    properties = output_schema.get("properties")
    if not isinstance(properties, Mapping):
        raise ValueError("output_schema.properties must be an object")
    output_names = {str(name) for name in properties}
    names: set[str] = set()
    in_scope_seen = False
    for rule in rules:
        if isinstance(rule, InScopeRule):
            if in_scope_seen:
                raise ValueError("only one in_scope rule is allowed")
            in_scope_seen = True
            rule_name = "in_scope"
        elif isinstance(rule, (ThresholdGroupRule, ReviewFlagRule)):
            rule_name = rule.name
        if (
            rule_name in names
            or rule_name in output_names
            or (rule_name == "in_scope" and not isinstance(rule, InScopeRule))
        ):
            raise ValueError(f"rule name {rule_name!r} collides with an output column")
        names.add(rule_name)
        refs: list[tuple[str, bool]] = []
        if isinstance(rule, ThresholdGroupRule):
            refs.append((rule.field, True))
        else:
            refs.extend(
                (condition.field, condition.op in ("lt", "le", "gt", "ge")) for condition in rule.conditions
            )
        for field, numeric in refs:
            name = field.split(".", 1)[1] if field.startswith("answer.") else field
            definition: object
            definition = {"type": "string"} if field.startswith("meta.") else properties.get(name)
            if name not in properties and not field.startswith("meta."):
                raise ValueError(f"rule field {field!r} is not in output schema")
            if not isinstance(definition, Mapping):
                raise ValueError(f"rule field {field!r} has no schema definition")
            schema_type = definition.get("type")
            types = schema_type if isinstance(schema_type, list) else [schema_type]
            types = [item for item in types if item != "null"]
            if numeric and not any(item in ("number", "integer") for item in types):
                raise ValueError(f"rule field {field!r} must have a numeric schema type")
            if not numeric and not field.startswith("meta."):
                for condition in rule.conditions if not isinstance(rule, ThresholdGroupRule) else ():
                    if condition.field != field or condition.op in ("is_null", "not_null"):
                        continue
                    expected_values = (
                        condition.value if isinstance(condition.value, list) else [condition.value]
                    )
                    expected_values = [item for item in expected_values if item is not None]
                    for expected in expected_values:
                        valid = (
                            (isinstance(expected, bool) and "boolean" in types)
                            or (isinstance(expected, str) and "string" in types)
                            or (
                                isinstance(expected, Real)
                                and not isinstance(expected, bool)
                                and any(item in ("number", "integer") for item in types)
                            )
                        )
                        if not valid:
                            raise ValueError(f"condition value for {field!r} does not match schema type")
