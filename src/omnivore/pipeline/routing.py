"""Declarative routing policy engine.

A policy is a dict (typically stored in tenant config) with this structure:

    {
        "default_sinks": ["relational", "vector"],
        "rules": [
            {"match": {"kind": "table_row"}, "sinks": ["relational"]},
            {"match": {"kind": "text"}, "sinks": ["vector"]},
            {"match": {"kind": "transcript", "confidence_lt": 0.6}, "sinks": []}
        ]
    }

evaluate_policy() is a pure function — it has no side effects and requires no I/O.
Rules are evaluated in order; the first match wins. If no rule matches, default_sinks is used.
"""
from __future__ import annotations

from typing import Any

DEFAULT_POLICY: dict[str, Any] = {
    "default_sinks": ["relational", "vector"],
    "rules": [
        {"match": {"kind": "table_row"}, "sinks": ["relational"]},
        {"match": {"kind": "text"}, "sinks": ["vector"]},
        {"match": {"kind": "transcript"}, "sinks": ["vector"]},
    ],
}


def evaluate_policy(
    chunk_kind: str,
    confidence: float | None,
    doc_meta: dict[str, Any],
    policy: dict[str, Any] | None = None,
) -> frozenset[str]:
    """Return the set of sinks for a chunk given the active policy.

    Args:
        chunk_kind: e.g. "text", "table_row", "transcript"
        confidence: extraction confidence (0.0-1.0) from the handler, or None
        doc_meta: document-level metadata dict
        policy: routing policy dict; falls back to DEFAULT_POLICY if None

    Returns:
        frozenset of sink names, e.g. frozenset({"vector"})
    """
    resolved = policy if policy is not None else DEFAULT_POLICY
    rules = resolved.get("rules", [])
    default_sinks = resolved.get("default_sinks", ["relational", "vector"])

    for rule in rules:
        if _matches(rule.get("match", {}), chunk_kind, confidence, doc_meta):
            return frozenset(rule.get("sinks", []))

    return frozenset(default_sinks)


def validate_policy(policy: dict[str, Any]) -> list[str]:
    """Return a list of validation error strings. Empty list means the policy is valid."""
    errors: list[str] = []
    if not isinstance(policy, dict):
        return ["policy must be a JSON object"]

    default_sinks = policy.get("default_sinks")
    if default_sinks is None:
        errors.append("missing required key: default_sinks")
    elif not isinstance(default_sinks, list):
        errors.append("default_sinks must be a list")
    else:
        for s in default_sinks:
            if s not in ("relational", "vector"):
                errors.append(f"unknown sink: {s!r} (allowed: relational, vector)")

    rules = policy.get("rules", [])
    if not isinstance(rules, list):
        errors.append("rules must be a list")
    else:
        for i, rule in enumerate(rules):
            if not isinstance(rule, dict):
                errors.append(f"rule[{i}] must be an object")
                continue
            if "match" not in rule:
                errors.append(f"rule[{i}] missing required key: match")
            if "sinks" not in rule:
                errors.append(f"rule[{i}] missing required key: sinks")
            elif not isinstance(rule["sinks"], list):
                errors.append(f"rule[{i}].sinks must be a list")

    return errors


def _matches(
    conditions: dict[str, Any],
    chunk_kind: str,
    confidence: float | None,
    doc_meta: dict[str, Any],
) -> bool:
    for key, expected in conditions.items():
        if key == "kind":
            if chunk_kind != expected:
                return False
        elif key == "confidence_lt":
            if confidence is None or confidence >= float(expected):
                return False
        elif key == "confidence_gte":
            if confidence is None or confidence < float(expected):
                return False
        elif key == "mime":
            if doc_meta.get("mime_type") != expected:
                return False
        # Unknown condition keys are ignored for forward-compatibility.
    return True
