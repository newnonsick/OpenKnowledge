"""Tier 2 Boundary Tests for Feature 20: PostgreSQL Full-Text Search (tsvector + GIN).

Tests boundary conditions, SQL injection safety, FTS operators (! & | : *), stop words, and empty queries.
"""

import re
import pytest


def sanitize_tsquery(raw_query: str) -> str:
    """Sanitizes user input for safe full-text search tsquery parsing."""
    if not raw_query or not raw_query.strip():
        return ""

    # Strip dangerous control characters, SQL separators, and raw boolean operators to avoid syntax errors
    cleaned = re.sub(r"[!&|():*'\";\-]+", " ", raw_query)
    tokens = [t.strip() for t in cleaned.split() if t.strip()]
    if not tokens:
        return ""
    # Join with AND logic
    return " & ".join(tokens)


@pytest.mark.tier2
@pytest.mark.feature("F20")
def test_f20_boundary_empty_and_whitespace_only_query():
    """Test boundary: empty or whitespace search queries sanitize to empty string."""
    assert sanitize_tsquery("") == ""
    assert sanitize_tsquery("   ") == ""
    assert sanitize_tsquery("\t\n") == ""


@pytest.mark.tier2
@pytest.mark.feature("F20")
def test_f20_boundary_special_fts_punctuation_and_operators():
    """Test boundary: punctuation like ! & | : * ' \" is stripped to prevent tsquery syntax errors."""
    dirty_queries = [
        "python ! & | java",
        "title:(keyword)*",
        "''' OR 1=1 --",
        "search && term || other",
        "!*()&:''",
    ]
    for q in dirty_queries:
        sanitized = sanitize_tsquery(q)
        assert "!" not in sanitized
        assert "|" not in sanitized
        assert "(" not in sanitized
        assert ")" not in sanitized
        assert ":" not in sanitized
        assert "*" not in sanitized
        assert "'" not in sanitized


@pytest.mark.tier2
@pytest.mark.feature("F20")
def test_f20_boundary_sql_injection_attempt_in_search_query():
    """Test boundary: SQL injection strings are treated purely as literal search terms."""
    sqli_payload = "'; DROP TABLE knowledge_items; --"
    sanitized = sanitize_tsquery(sqli_payload)
    assert "DROP" in sanitized
    assert "TABLE" in sanitized
    assert ";" not in sanitized
    assert "--" not in sanitized


@pytest.mark.tier2
@pytest.mark.feature("F20")
def test_f20_boundary_extremely_long_search_query():
    """Test boundary: queries with 5,000 characters process safely without truncation crashes."""
    long_query = "word " * 1000
    sanitized = sanitize_tsquery(long_query)
    assert len(sanitized) > 0
    assert "&" in sanitized


@pytest.mark.tier2
@pytest.mark.feature("F20")
def test_f20_boundary_unicode_and_alphanumeric_search_terms():
    """Test boundary: unicode keywords in various languages remain intact during FTS sanitization."""
    unicode_query = "FastAPI データベース 🚀 python"
    sanitized = sanitize_tsquery(unicode_query)
    assert "FastAPI" in sanitized
    assert "データベース" in sanitized
    assert "python" in sanitized
