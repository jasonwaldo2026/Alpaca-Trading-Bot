"""
Scan rule model — the contract between Studio and Scanner.

Studio *authors* rules; Scanner *executes* them. Both sides import this
module, so neither can invent its own dialect. A rule is plain data and
serializes to JSON, which is what lets Studio save one and Scanner pick it
up without the two apps sharing a process.

A rule matches a symbol when every condition holds on the most recent bar.
Conditions compare a field (an OHLCV column or an indicator column) against
either a literal number or another field:

    {"field": "rsi",      "op": "<",  "value": 30}
    {"field": "volume",   "op": ">",  "field2": "vol_sma"}
    {"field": "sma_fast", "op": "crosses_above", "field2": "sma_slow"}

A condition may also require persistence — "true for the last N bars" —
via `for_bars`, which is how "price has held above VWAP for a while" is
expressed rather than "price crossed VWAP on this exact bar":

    {"field": "close", "op": ">", "field2": "vwap", "for_bars": 6}

Crossover and persistence operators need earlier bars as well, which is why
evaluation takes the whole indicator-enriched frame rather than a single row.
"""

import json
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Sequence

import pandas as pd

from core.alerts import AlertTemplate
from core.indicators import IndicatorParams, add_indicators, indicator_columns
from core.persistence import consecutive_true

# Operators that compare the latest bar only.
_POINT_OPS = {
    ">":  lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "<":  lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
}

# Operators that need the previous bar too.
_CROSS_OPS = ("crosses_above", "crosses_below")

VALID_OPS = tuple(_POINT_OPS) + _CROSS_OPS


class RuleError(ValueError):
    """Raised when a rule is malformed. Studio surfaces this to the user."""


@dataclass
class Condition:
    """One comparison. Exactly one of `value` or `field2` must be set."""

    field: str
    op: str
    value: Optional[float] = None
    field2: Optional[str] = None

    #: Require the comparison to have held for this many consecutive bars,
    #: ending at the latest one. 1 (or None) means "true right now".
    #: Persistence turns a momentary touch into a sustained condition, which
    #: is the difference between "crossed above VWAP" and "has been above
    #: VWAP for half an hour".
    for_bars: Optional[int] = None

    def validate(self) -> None:
        if self.op not in VALID_OPS:
            raise RuleError(
                f"Unknown operator {self.op!r}. Valid: {', '.join(VALID_OPS)}"
            )
        if self.for_bars is not None:
            if self.for_bars < 1:
                raise RuleError(
                    f"for_bars must be >= 1; got {self.for_bars}."
                )
            if self.op in _CROSS_OPS:
                raise RuleError(
                    f"Operator {self.op!r} describes a single-bar event, so "
                    f"'for_bars' is meaningless on it."
                )
        if (self.value is None) == (self.field2 is None):
            raise RuleError(
                f"Condition on {self.field!r} must set exactly one of "
                f"'value' or 'field2' (got value={self.value!r}, "
                f"field2={self.field2!r})"
            )
        if self.op in _CROSS_OPS and self.field2 is None:
            raise RuleError(
                f"Operator {self.op!r} compares two series, so it needs "
                f"'field2' rather than a literal 'value'."
            )

    def _operands(self, row: pd.Series) -> tuple:
        left = row[self.field]
        right = self.value if self.field2 is None else row[self.field2]
        return left, right

    def _series_mask(self, df: pd.DataFrame) -> pd.Series:
        """Row-wise truth of this comparison across the whole frame."""
        left = df[self.field]
        right = self.value if self.field2 is None else df[self.field2]
        mask = _POINT_OPS[self.op](left, right)
        # A NaN comparison is False in pandas already, but an operand that is
        # NaN must break a run rather than silently continue it.
        valid = left.notna()
        if self.field2 is not None:
            valid &= df[self.field2].notna()
        return mask & valid

    def evaluate(self, df: pd.DataFrame) -> bool:
        """Evaluate against the last row of an indicator-enriched frame."""
        self.validate()

        for name in (self.field, self.field2):
            if name is not None and name not in df.columns:
                raise RuleError(
                    f"Unknown field {name!r}. Available: "
                    f"{', '.join(map(str, df.columns))}"
                )

        if self.for_bars and self.for_bars > 1:
            if len(df) < self.for_bars:
                return False
            run = consecutive_true(self._series_mask(df)).iloc[-1]
            return bool(run >= self.for_bars)

        curr = df.iloc[-1]

        if self.op in _CROSS_OPS:
            if len(df) < 2:
                return False
            prev = df.iloc[-2]
            p_left, p_right = self._operands(prev)
            c_left, c_right = self._operands(curr)
            if pd.isna([p_left, p_right, c_left, c_right]).any():
                return False
            if self.op == "crosses_above":
                return bool(p_left <= p_right and c_left > c_right)
            return bool(p_left >= p_right and c_left < c_right)

        left, right = self._operands(curr)
        if pd.isna(left) or pd.isna(right):
            return False
        return bool(_POINT_OPS[self.op](left, right))

    def describe(self) -> str:
        right = self.field2 if self.field2 is not None else self.value
        text = f"{self.field} {self.op} {right}"
        if self.for_bars and self.for_bars > 1:
            text += f" for {self.for_bars} bars"
        return text


@dataclass
class Rule:
    """A named set of conditions, evaluated against a symbol universe."""

    name: str
    conditions: List[Condition] = field(default_factory=list)
    universe: Any = "default_stocks"       # named universe or explicit list
    params: IndicatorParams = field(default_factory=IndicatorParams)
    description: str = ""

    #: What to send when this rule matches. None means "detect but do not
    #: notify" — useful while a scenario is still being tuned.
    alert: Optional[AlertTemplate] = None

    def validate(self) -> None:
        if not self.name.strip():
            raise RuleError("Rule needs a non-empty name.")
        if not self.conditions:
            raise RuleError(
                f"Rule {self.name!r} has no conditions — it would match "
                f"every symbol."
            )
        for cond in self.conditions:
            cond.validate()
        if self.alert is not None:
            self.alert.validate(self.available_fields())

    def available_fields(self) -> set:
        """Every column a match on this rule can reference, for templates."""
        from core.fundamentals import COL_FLOAT_MILLIONS, COL_FLOAT_SHARES
        return (
            {"open", "high", "low", "close", "volume",
             COL_FLOAT_SHARES, COL_FLOAT_MILLIONS}
            | set(indicator_columns(self.params))
        )

    def matches(self, bars: pd.DataFrame) -> bool:
        """
        True when every condition holds on the latest bar.

        `bars` may be raw OHLCV; indicators are added here if absent, so
        callers scanning many symbols can pre-compute once and pass the
        enriched frame to avoid recomputing per rule.
        """
        self.validate()
        if bars is None or bars.empty or len(bars) < self.params.min_bars():
            return False
        enriched = (
            bars if "rsi" in bars.columns else add_indicators(bars, self.params)
        )
        return all(cond.evaluate(enriched) for cond in self.conditions)

    def describe(self) -> str:
        return " AND ".join(c.describe() for c in self.conditions)

    # ── Serialization — how Studio hands a rule to Scanner ────────────────

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        # asdict() turns the alert into a plain dict already; drop it entirely
        # when unset so a rule without notifications stays a clean file.
        if self.alert is None:
            data.pop("alert", None)
        return data

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Rule":
        try:
            conditions = [Condition(**c) for c in data.get("conditions", [])]
        except TypeError as exc:
            raise RuleError(f"Malformed condition in rule: {exc}") from exc
        params = IndicatorParams(**data.get("params", {}))
        alert_block = data.get("alert")
        rule = cls(
            name=data.get("name", ""),
            conditions=conditions,
            universe=data.get("universe", "default_stocks"),
            params=params,
            description=data.get("description", ""),
            alert=AlertTemplate.from_dict(alert_block) if alert_block else None,
        )
        rule.validate()
        return rule

    @classmethod
    def from_json(cls, text: str) -> "Rule":
        try:
            return cls.from_dict(json.loads(text))
        except json.JSONDecodeError as exc:
            raise RuleError(f"Rule is not valid JSON: {exc}") from exc


def load_rules(paths: Sequence[str]) -> List[Rule]:
    """Load rule JSON files saved by Studio."""
    rules: List[Rule] = []
    for path in paths:
        with open(path) as fh:
            rules.append(Rule.from_json(fh.read()))
    return rules
