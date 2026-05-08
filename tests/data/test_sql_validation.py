"""Tests for src.data.sql_validation — the shared user-SQL policy."""

from __future__ import annotations

import pytest

from src.data.sql_validation import (
    DANGEROUS_PATTERNS,
    DangerousSQLError,
    assert_safe_where_clause,
    check_dangerous_patterns,
)

SAFE_CLAUSES = [
    "is_military = 1",
    "registration LIKE 'B-%'",
    "altitude_baro > 10000 AND ground_speed > 400",
    "aircraft_type IN ('A380', 'B747')",
    "latitude BETWEEN -90 AND 90",
]

# Each tuple is (clause, expected_first_matching_pattern). The expected
# pattern is the FIRST entry in DANGEROUS_PATTERNS that matches — order
# matters because check_dangerous_patterns returns the first hit.
DANGEROUS_CLAUSES = [
    ("drop table users", r"\bDROP\b"),
    ("DELETE FROM aircraft_snapshots", r"\bDELETE\b"),
    # ; would match, but DROP is earlier in the list so DROP wins:
    ("hex = 'x'; DROP TABLE users", r"\bDROP\b"),
    ("hex = 'x' -- comment", r"--"),
    ("1=1 UNION SELECT * FROM users", r"\bUNION\b.*\bSELECT\b"),
    ("exec something", r"\bEXEC\b"),
    ("TRUNCATE aircraft_snapshots", r"\bTRUNCATE\b"),
    ("GRANT ALL TO bob", r"\bGRANT\b"),
    ("hex = 'x'; select * from y", r";"),  # isolated semicolon match
]


class TestCheckDangerousPatterns:
    @pytest.mark.parametrize("clause", SAFE_CLAUSES)
    def test_safe_clauses_pass(self, clause: str) -> None:
        assert check_dangerous_patterns(clause) is None

    @pytest.mark.parametrize("clause,pattern", DANGEROUS_CLAUSES)
    def test_dangerous_clauses_match(self, clause: str, pattern: str) -> None:
        assert check_dangerous_patterns(clause) == pattern

    def test_case_insensitive(self) -> None:
        # Upper, lower, and mixed case should all match.
        assert check_dangerous_patterns("DROP table x") is not None
        assert check_dangerous_patterns("drop table x") is not None
        assert check_dangerous_patterns("DrOp table x") is not None

    def test_word_boundary_avoids_false_positives(self) -> None:
        # 'Executive' contains 'EXEC' but should not match \bEXEC\b.
        assert check_dangerous_patterns("role = 'Executive'") is None
        # 'updated_at' contains 'UPDATE' as a substring but not as a word.
        assert check_dangerous_patterns("updated_at > '2024-01-01'") is None


class TestAssertSafeWhereClause:
    @pytest.mark.parametrize("clause", SAFE_CLAUSES)
    def test_safe_returns_input(self, clause: str) -> None:
        assert assert_safe_where_clause(clause) == clause

    def test_dangerous_raises(self) -> None:
        with pytest.raises(DangerousSQLError) as exc:
            assert_safe_where_clause("DROP TABLE users")
        assert "DROP" in str(exc.value)

    def test_dangerous_error_is_value_error_subclass(self) -> None:
        # Historical callers catch ValueError; preserve that contract.
        with pytest.raises(ValueError):
            assert_safe_where_clause("; DELETE")


class TestPatternList:
    def test_patterns_list_is_non_empty(self) -> None:
        assert len(DANGEROUS_PATTERNS) > 0

    def test_each_pattern_is_a_string(self) -> None:
        assert all(isinstance(p, str) for p in DANGEROUS_PATTERNS)
