from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from pydantic import ValidationError

from pdd_data_mcp.contracts.models import Coverage, CoverageStatus, Scope, WindowKind
from pdd_data_mcp.errors import ValidationFailure
from pdd_data_mcp.parsers import money_to_cents, ratio_to_string
from pdd_data_mcp.utils import decode_cursor, encode_cursor


def test_scope_requires_timezone_aware_custom_dates() -> None:
    with pytest.raises(ValidationError):
        Scope(
            kind=WindowKind.CUSTOM,
            business_date=date(2026, 9, 6),
            start=datetime(2026, 9, 6),
            end=datetime(2026, 9, 7),
        )
    scope = Scope(
        kind=WindowKind.CUSTOM,
        business_date=date(2026, 9, 6),
        start=datetime(2026, 9, 6, tzinfo=UTC),
        end=datetime(2026, 9, 7, tzinfo=UTC),
    )
    assert scope.end - scope.start == timedelta(days=1)


def test_coverage_enforces_truncation_for_55_of_50() -> None:
    with pytest.raises(ValidationError):
        Coverage(
            total_observed=55,
            captured=50,
            limit=50,
            pages_read=1,
            coverage=CoverageStatus.COMPLETE,
            truncated=False,
        )
    valid = Coverage(
        total_observed=55,
        captured=50,
        limit=50,
        pages_read=1,
        coverage=CoverageStatus.TRUNCATED,
        truncated=True,
    )
    assert valid.coverage is CoverageStatus.TRUNCATED


def test_unknown_total_does_not_become_zero() -> None:
    value = Coverage(
        total_observed=None,
        captured=3,
        limit=50,
        pages_read=1,
        coverage=CoverageStatus.UNKNOWN,
        truncated=False,
    )
    assert value.total_observed is None


def test_parsers_do_not_use_float_for_money_and_ratio() -> None:
    assert money_to_cents("1.23").value == 123
    approximate = money_to_cents("1.2万")
    assert (approximate.value, approximate.precision) == (1_200_000, "APPROXIMATE")
    assert ratio_to_string("12.50", percent=True).value == "0.125"


def test_cursor_is_query_bound_and_tamper_evident() -> None:
    cursor = encode_cursor("snapshot-list", "abc", 10)
    assert decode_cursor(cursor, kind="snapshot-list", query_hash="abc") == 10
    with pytest.raises(ValidationFailure):
        decode_cursor(cursor, kind="snapshot-records", query_hash="abc")
    with pytest.raises(ValidationFailure):
        decode_cursor(
            cursor[:-1] + ("A" if cursor[-1] != "A" else "B"),
            kind="snapshot-list",
            query_hash="abc",
        )
