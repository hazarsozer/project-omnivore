from __future__ import annotations

from omnivore.pipeline.routing import DEFAULT_POLICY, evaluate_policy, validate_policy


class TestEvaluatePolicy:
    def test_text_routes_to_vector_by_default(self):
        sinks = evaluate_policy("text", None, {})
        assert "vector" in sinks

    def test_table_row_routes_to_relational_by_default(self):
        sinks = evaluate_policy("table_row", None, {})
        assert "relational" in sinks
        assert "vector" not in sinks

    def test_transcript_routes_to_vector_by_default(self):
        sinks = evaluate_policy("transcript", None, {})
        assert "vector" in sinks

    def test_unknown_kind_uses_default_sinks(self):
        sinks = evaluate_policy("unknown_kind", None, {})
        assert sinks == frozenset(DEFAULT_POLICY["default_sinks"])

    def test_custom_policy_overrides_default(self):
        policy = {
            "default_sinks": ["relational", "vector"],
            "rules": [
                {"match": {"kind": "text"}, "sinks": ["relational"]},
            ],
        }
        sinks = evaluate_policy("text", None, {}, policy=policy)
        assert sinks == frozenset({"relational"})

    def test_empty_sinks_means_drop(self):
        policy = {
            "default_sinks": ["vector"],
            "rules": [
                {"match": {"kind": "text", "confidence_lt": 0.5}, "sinks": []},
            ],
        }
        sinks = evaluate_policy("text", 0.3, {}, policy=policy)
        assert sinks == frozenset()

    def test_confidence_lt_not_triggered_when_high(self):
        policy = {
            "default_sinks": ["vector"],
            "rules": [
                {"match": {"kind": "text", "confidence_lt": 0.5}, "sinks": []},
            ],
        }
        sinks = evaluate_policy("text", 0.9, {}, policy=policy)
        assert "vector" in sinks

    def test_confidence_gte_matches(self):
        policy = {
            "default_sinks": ["relational"],
            "rules": [
                {"match": {"kind": "text", "confidence_gte": 0.8}, "sinks": ["vector"]},
            ],
        }
        sinks = evaluate_policy("text", 0.9, {}, policy=policy)
        assert sinks == frozenset({"vector"})

    def test_none_confidence_skips_confidence_conditions(self):
        policy = {
            "default_sinks": ["vector"],
            "rules": [
                {"match": {"kind": "text", "confidence_lt": 0.5}, "sinks": []},
            ],
        }
        # None confidence → condition fails → falls through to default
        sinks = evaluate_policy("text", None, {}, policy=policy)
        assert "vector" in sinks

    def test_first_matching_rule_wins(self):
        policy = {
            "default_sinks": ["vector"],
            "rules": [
                {"match": {"kind": "text"}, "sinks": ["relational"]},
                {"match": {"kind": "text"}, "sinks": ["vector"]},
            ],
        }
        sinks = evaluate_policy("text", None, {}, policy=policy)
        assert sinks == frozenset({"relational"})

    def test_returns_frozenset(self):
        sinks = evaluate_policy("text", None, {})
        assert isinstance(sinks, frozenset)

    def test_mime_condition(self):
        policy = {
            "default_sinks": ["vector"],
            "rules": [
                {"match": {"kind": "text", "mime": "application/json"}, "sinks": ["relational"]},
            ],
        }
        sinks = evaluate_policy("text", None, {"mime_type": "application/json"}, policy=policy)
        assert sinks == frozenset({"relational"})

    def test_mime_condition_not_matched(self):
        policy = {
            "default_sinks": ["vector"],
            "rules": [
                {"match": {"kind": "text", "mime": "application/json"}, "sinks": ["relational"]},
            ],
        }
        sinks = evaluate_policy("text", None, {"mime_type": "text/plain"}, policy=policy)
        assert "vector" in sinks

    def test_none_policy_uses_default(self):
        sinks = evaluate_policy("text", None, {}, policy=None)
        assert isinstance(sinks, frozenset)


class TestValidatePolicy:
    def test_valid_policy(self):
        errors = validate_policy(DEFAULT_POLICY)
        assert errors == []

    def test_missing_default_sinks(self):
        errors = validate_policy({"rules": []})
        assert any("default_sinks" in e for e in errors)

    def test_invalid_sink_name(self):
        policy = {"default_sinks": ["unknown_sink"], "rules": []}
        errors = validate_policy(policy)
        assert any("unknown_sink" in e for e in errors)

    def test_rule_missing_match(self):
        policy = {"default_sinks": ["vector"], "rules": [{"sinks": ["vector"]}]}
        errors = validate_policy(policy)
        assert any("match" in e for e in errors)

    def test_rule_missing_sinks(self):
        policy = {"default_sinks": ["vector"], "rules": [{"match": {"kind": "text"}}]}
        errors = validate_policy(policy)
        assert any("sinks" in e for e in errors)

    def test_non_dict_policy(self):
        errors = validate_policy("not a dict")  # type: ignore[arg-type]
        assert errors

    def test_non_list_rules(self):
        errors = validate_policy({"default_sinks": ["vector"], "rules": "not a list"})
        assert any("list" in e for e in errors)

    def test_empty_rules_is_valid(self):
        errors = validate_policy({"default_sinks": ["vector"], "rules": []})
        assert errors == []
