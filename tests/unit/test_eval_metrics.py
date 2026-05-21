"""Unit tests for eval/metrics.py — compute_chunk_faithfulness."""
from __future__ import annotations

from unittest.mock import MagicMock

from eval.metrics import compute_chunk_faithfulness

# ---------------------------------------------------------------------------
# Returns None when no scoreable queries
# ---------------------------------------------------------------------------

def test_returns_none_when_no_queries():
    assert compute_chunk_faithfulness([], []) is None


def test_returns_none_when_queries_have_no_substrings():
    queries = [{"query": "What is this?"}, {"query": "Summarize."}]
    assert compute_chunk_faithfulness([{"content": "hello world"}], queries) is None


def test_returns_none_when_substrings_list_is_empty():
    queries = [{"query": "x", "relevant_content_substrings": []}]
    assert compute_chunk_faithfulness([{"content": "hello"}], queries) is None


# ---------------------------------------------------------------------------
# Correct fraction computation
# ---------------------------------------------------------------------------

def test_all_queries_satisfied_returns_one():
    chunks = [{"content": "The sky is blue and the grass is green."}]
    queries = [
        {"query": "sky", "relevant_content_substrings": ["sky is blue"]},
        {"query": "grass", "relevant_content_substrings": ["grass is green"]},
    ]
    assert compute_chunk_faithfulness(chunks, queries) == 1.0


def test_no_queries_satisfied_returns_zero():
    chunks = [{"content": "completely unrelated text"}]
    queries = [
        {"query": "cats", "relevant_content_substrings": ["the cat sat"]},
        {"query": "dogs", "relevant_content_substrings": ["the dog ran"]},
    ]
    assert compute_chunk_faithfulness(chunks, queries) == 0.0


def test_partial_satisfaction_returns_correct_fraction():
    chunks = [{"content": "Paris is the capital of France."}]
    queries = [
        {"query": "capital", "relevant_content_substrings": ["capital of France"]},
        {"query": "berlin", "relevant_content_substrings": ["capital of Germany"]},
        {"query": "london", "relevant_content_substrings": ["capital of England"]},
        {"query": "paris", "relevant_content_substrings": ["Paris is"]},
    ]
    result = compute_chunk_faithfulness(chunks, queries)
    assert result == round(2 / 4, 4)


def test_result_is_rounded_to_4_decimal_places():
    chunks = [{"content": "a b c"}]
    queries = [
        {"relevant_content_substrings": ["a"]},
        {"relevant_content_substrings": ["b"]},
        {"relevant_content_substrings": ["z"]},
    ]
    result = compute_chunk_faithfulness(chunks, queries)
    assert result == round(2 / 3, 4)
    assert isinstance(result, float)


# ---------------------------------------------------------------------------
# Case-insensitive matching
# ---------------------------------------------------------------------------

def test_matching_is_case_insensitive():
    chunks = [{"content": "The QUICK brown FOX"}]
    queries = [{"relevant_content_substrings": ["quick brown fox"]}]
    assert compute_chunk_faithfulness(chunks, queries) == 1.0


def test_substrings_are_case_insensitive():
    chunks = [{"content": "hello world"}]
    queries = [{"relevant_content_substrings": ["HELLO WORLD"]}]
    assert compute_chunk_faithfulness(chunks, queries) == 1.0


# ---------------------------------------------------------------------------
# Multiple chunks — content joined across all chunks
# ---------------------------------------------------------------------------

def test_substring_found_across_multiple_chunks():
    chunks = [
        {"content": "The capital of France"},
        {"content": "is the city of Paris."},
    ]
    queries = [{"relevant_content_substrings": ["city of Paris"]}]
    assert compute_chunk_faithfulness(chunks, queries) == 1.0


def test_empty_chunks_returns_zero():
    queries = [{"relevant_content_substrings": ["something"]}]
    assert compute_chunk_faithfulness([], queries) == 0.0


# ---------------------------------------------------------------------------
# Object-style chunks (with .content attribute)
# ---------------------------------------------------------------------------

def test_works_with_object_chunks():
    chunk = MagicMock()
    chunk.content = "The answer is 42."
    queries = [{"relevant_content_substrings": ["answer is 42"]}]
    assert compute_chunk_faithfulness([chunk], queries) == 1.0


def test_works_with_mixed_object_and_dict_chunks():
    obj_chunk = MagicMock()
    obj_chunk.content = "First part."
    dict_chunk = {"content": "Second part."}
    queries = [
        {"relevant_content_substrings": ["First part"]},
        {"relevant_content_substrings": ["Second part"]},
    ]
    assert compute_chunk_faithfulness([obj_chunk, dict_chunk], queries) == 1.0


# ---------------------------------------------------------------------------
# Any-substring semantics — OR within a query
# ---------------------------------------------------------------------------

def test_query_satisfied_if_any_substring_matches():
    chunks = [{"content": "water boils at 100 degrees Celsius"}]
    queries = [
        {
            "relevant_content_substrings": [
                "boils at 100",       # matches
                "freezes at 0",       # does not match
            ]
        }
    ]
    assert compute_chunk_faithfulness(chunks, queries) == 1.0


def test_query_not_satisfied_if_no_substring_matches():
    chunks = [{"content": "water boils at 100 degrees Celsius"}]
    queries = [
        {
            "relevant_content_substrings": [
                "freezes at 0",
                "melts at 200",
            ]
        }
    ]
    assert compute_chunk_faithfulness(chunks, queries) == 0.0


# ---------------------------------------------------------------------------
# Queries without substrings are skipped (don't count in denominator)
# ---------------------------------------------------------------------------

def test_queries_without_substrings_skipped_in_denominator():
    chunks = [{"content": "relevant text here"}]
    queries = [
        {"query": "no substrings"},                                            # skipped
        {"relevant_content_substrings": ["relevant text"]},                    # hit
        {"query": "also no substrings"},                                       # skipped
    ]
    # Only 1 scoreable query, 1 hit → 1.0
    assert compute_chunk_faithfulness(chunks, queries) == 1.0
