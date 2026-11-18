from __future__ import annotations

import pytest

from bca.outputs.rules import RuleTypeError, evaluate, validate_rules
from bca.store import Condition, InScopeRule, ReviewFlagRule, ThresholdGroupRule


@pytest.mark.parametrize(
    ("op", "expected", "result"),
    [
        ("eq", 3, True),
        ("ne", 4, True),
        ("in", [2, 3], True),
        ("not_in", [1], True),
        ("lt", 4, True),
        ("le", 3, True),
        ("gt", 2, True),
        ("ge", 3, True),
        ("contains", "BC", True),
        ("is_null", None, True),
        ("not_null", None, False),
    ],
)
def test_all_condition_operators(op: str, expected: object, result: bool) -> None:
    condition = Condition("score" if op not in ("is_null", "not_null", "contains") else "text", op, expected)  # type: ignore[arg-type]
    answer = {"score": 3, "text": "abc"}
    if op == "is_null":
        answer["text"] = None
    if op == "not_null":
        answer["text"] = None
    assert evaluate((ReviewFlagRule("flag", "all", (condition,)),), answer, {})["flag"] is result


def test_rule_type_error_and_ambiguous_paths() -> None:
    condition = Condition("score", "eq", "3")
    with pytest.raises(RuleTypeError):
        evaluate((ReviewFlagRule("flag", "all", (condition,)),), {"score": 3}, {})
    with pytest.raises(RuleTypeError, match="ambiguous"):
        evaluate((ReviewFlagRule("flag", "all", (Condition("x", "eq", 1),)),), {"x": 1}, {"x": 1})


@pytest.mark.parametrize("inclusive", [True, False])
def test_threshold_edges(inclusive: bool) -> None:
    rule = ThresholdGroupRule("band", "score", (10,), ("low", "high"), inclusive)
    assert evaluate((rule,), {"score": 10}, {})["band"] == ("low" if inclusive else "high")
    assert evaluate((rule,), {"score": 11}, {})["band"] == "high"


@pytest.mark.parametrize(
    ("condition", "answer"),
    [
        (Condition("score", "lt", 4), {"score": "3"}),
        (Condition("score", "in", [3, "4"]), {"score": 3}),
        (Condition("text", "contains", 3), {"text": "abc"}),
    ],
)
def test_rule_type_errors(condition: Condition, answer: dict[str, object]) -> None:
    with pytest.raises(RuleTypeError):
        evaluate((ReviewFlagRule("flag", "all", (condition,)),), answer, {})


def test_numeric_float_null_and_guarded_conditions() -> None:
    rules = (
        ReviewFlagRule("greater", "all", (Condition("score", "gt", 2),)),
        ReviewFlagRule("equal", "all", (Condition("score", "eq", 3),)),
        ReviewFlagRule("null", "all", (Condition("stage", "eq", "a"),)),
        ReviewFlagRule("guarded", "all", (Condition("score", "not_null"), Condition("score", "gt", 5))),
    )
    result = evaluate(rules, {"score": 2.5, "stage": None}, {})
    assert result == {"greater": True, "equal": False, "null": False, "guarded": False}
    assert (
        evaluate((ReviewFlagRule("missing", "all", (Condition("x", "ne", None),)),), {}, {})["missing"]
        is True
    )


def test_match_short_circuits_and_nonfinite_thresholds_are_rejected() -> None:
    rule = ReviewFlagRule("guarded", "any", (Condition("missing", "is_null"), Condition("bad", "gt", "x")))
    assert evaluate((rule,), {}, {})["guarded"] is True
    with pytest.raises(RuleTypeError):
        evaluate((ThresholdGroupRule("band", "score", (10,), ("low", "high")),), {"score": float("nan")}, {})


def test_validate_rules_checks_values_nullable_numbers_and_collisions() -> None:
    schema = {"properties": {"score": {"type": ["number", "null"]}, "stage": {"type": "string"}}}
    validate_rules((ReviewFlagRule("ok", "all", (Condition("score", "eq", 3),)),), schema)
    with pytest.raises(ValueError, match="condition value"):
        validate_rules((ReviewFlagRule("bad", "all", (Condition("stage", "eq", 3),)),), schema)
    with pytest.raises(ValueError, match="only one"):
        validate_rules(
            (
                InScopeRule("all", (Condition("score", "gt", 1),)),
                InScopeRule("all", (Condition("score", "gt", 2),)),
            ),
            schema,
        )


def test_validate_rules_schema() -> None:
    rule = InScopeRule("all", (Condition("missing", "eq", 1),))
    with pytest.raises(ValueError, match="schema"):
        validate_rules((rule,), {"type": "object", "properties": {}})
    numeric = ReviewFlagRule("f", "all", (Condition("score", "gt", 2),))
    with pytest.raises(ValueError, match="numeric"):
        validate_rules((numeric,), {"type": "object", "properties": {"score": {"type": "string"}}})

    validate_rules(
        (ReviewFlagRule("meta", "all", (Condition("meta.source", "eq", "x"),)),),
        {"properties": {}},
    )
