"""Visual strategy builder rule evaluation (PRD section 32).

A rule tree is either a leaf condition ({field, operator, value} -- the
same shape the scanner uses, and the same evaluate_condition underneath) or
a branch ({"all": [...]} / {"any": [...]}) combining child nodes. Reusing
the scanner's field/operator vocabulary means "IF EMA(20) > EMA(50) AND
RSI(14) > 55" is expressed and evaluated identically whether it came from a
scan or a strategy's entry rules.
"""

from app.models.market_data import OhlcvCandle
from app.schemas.scanner import ScanCondition
from app.services.scanner import evaluate_condition

MAX_RULE_DEPTH = 6


def evaluate_rule_node(candles: list[OhlcvCandle], node: dict, depth: int = 0) -> bool:
    if depth > MAX_RULE_DEPTH:
        raise ValueError("Rule tree too deeply nested")

    if "all" in node:
        return all(evaluate_rule_node(candles, child, depth + 1) for child in node["all"])
    if "any" in node:
        return any(evaluate_rule_node(candles, child, depth + 1) for child in node["any"])
    if {"field", "operator", "value"} <= node.keys():
        passed, _ = evaluate_condition(candles, ScanCondition(**node))
        return passed
    raise ValueError(f"Invalid rule node: {node}")
