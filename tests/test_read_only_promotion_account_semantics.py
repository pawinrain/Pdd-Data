from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest

from examples.read_only_promotion_account_semantics import (
    _hourly_temporal_evidence,
    _parse_page_date_explanation,
    _request_time_summary,
    _response_time_summary,
)


def request(window_kind: str) -> dict[str, object]:
    business_date = "2026-09-08" if window_kind == "TODAY" else "2026-09-07"
    value: dict[str, object] = {
        "blockTypes": [1],
        "clientType": 1,
        "crawlerInfo": "opaque-and-never-returned",
        "endDate": f"{business_date} 00:00:00",
        "endDayHour": 14,
        "entityId": 987654321,
        "queryDimensionType": 0,
        "reportPromotionType": 9,
        "startDate": f"{business_date} 00:00:00",
    }
    if window_kind == "TODAY":
        value["returnLastUpdateTime"] = True
    return value


def response(window_kind: str) -> dict[str, object]:
    business_date = "2026-09-08" if window_kind == "TODAY" else "2026-09-07"
    update = (
        int(datetime(2026, 9, 8, 6, 30, 17, 123000, tzinfo=UTC).timestamp() * 1000)
        if window_kind == "TODAY"
        else None
    )
    return {
        "success": True,
        "result": {
            "dailyReportList": [{"date": f"{business_date} 00:00:00"}],
            "hourlyReportList": [
                {
                    "date": f"{business_date} 00:00:00",
                    "hour": hour,
                    "spend": {"value": f"hidden-{hour}"},
                    "click": 1_000 + hour,
                }
                for hour in range(11)
            ],
            "reportLastUpdateTime": update,
            "lastUpdateTime": update,
        },
    }


def test_page_date_explanation_is_normalized_without_retaining_raw_text() -> None:
    parsed = _parse_page_date_explanation(
        "*\u00a0今日截至 14:30 的数据\uff1b 昨日截至 14:16 的数据"
    )

    assert parsed == {
        "today_cutoff_hhmm": "14:30",
        "yesterday_cutoff_hhmm": "14:16",
        "page_semantics": "SAME_PERIOD_COMPARISON",
        "business_timezone": "Asia/Shanghai",
    }
    assert "数据" not in json.dumps(parsed, ensure_ascii=False)


@pytest.mark.parametrize(
    "value",
    [
        "今日截至14:30的数据\uff1b昨日截至14:16的数据",
        "*今日截至14:30的数据;昨日截至14:16的数据",
        "*今日截至24:00的数据\uff1b昨日截至14:16的数据",
        "*今日截至14:30的数据\uff1b昨日截至14:16的数据\uff08预计\uff09",
    ],
)
def test_page_date_explanation_rejects_noncanonical_semantics(value: str) -> None:
    with pytest.raises(RuntimeError, match="not canonical"):
        _parse_page_date_explanation(value)


@pytest.mark.parametrize("window_kind", ["TODAY", "YESTERDAY"])
def test_request_time_summary_is_exact_and_sanitized(window_kind: str) -> None:
    parsed = _request_time_summary(request(window_kind), current_date=date(2026, 9, 8))

    expected_date = "2026-09-08" if window_kind == "TODAY" else "2026-09-07"
    assert parsed == {
        "window_kind": window_kind,
        "business_date": expected_date,
        "start_date": expected_date,
        "end_date": expected_date,
        "end_day_hour": 14,
    }
    encoded = json.dumps(parsed)
    assert "entityId" not in encoded
    assert "987654321" not in encoded
    assert "opaque" not in encoded


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("startDate", "2026-09-08 00:00:01"),
        ("endDate", "2026-09-07 00:00:00"),
        ("endDayHour", True),
        ("clientType", "1"),
        ("crawlerInfo", ""),
    ],
)
def test_request_time_summary_rejects_contract_drift(field: str, changed: object) -> None:
    value = request("TODAY")
    value[field] = changed

    with pytest.raises(RuntimeError):
        _request_time_summary(value, current_date=date(2026, 9, 8))


def test_response_time_summary_reports_today_timestamp_and_shape_only() -> None:
    parsed = _response_time_summary(
        response("TODAY"),
        window_kind="TODAY",
        expected_business_date=date(2026, 9, 8),
    )

    assert parsed == {
        "daily_business_date": "2026-09-08",
        "daily_row_count": 1,
        "hourly_row_count": 11,
        "hourly_row_shape": [
            {"path": "$", "type": "object"},
            {"path": "click", "type": "number"},
            {"path": "date", "type": "string"},
            {"path": "hour", "type": "number"},
            {"path": "spend", "type": "object"},
            {"path": "spend.value", "type": "string"},
        ],
        "hourly_temporal_evidence": {
            "found": True,
            "first": {"date": "2026-09-08", "hour": 0},
            "last": {"date": "2026-09-08", "hour": 10},
        },
        "source_update_timestamp": "2026-09-08T14:30:17.123000+08:00",
        "source_update_missing_status": None,
    }
    encoded = json.dumps(parsed)
    assert "hidden-" not in encoded
    assert "1000" not in encoded


def test_response_time_summary_preserves_yesterday_null_update_status() -> None:
    parsed = _response_time_summary(
        response("YESTERDAY"),
        window_kind="YESTERDAY",
        expected_business_date=date(2026, 9, 7),
    )

    assert parsed["daily_business_date"] == "2026-09-07"
    assert parsed["hourly_row_count"] == 11
    assert parsed["hourly_temporal_evidence"] == {
        "found": True,
        "first": {"date": "2026-09-07", "hour": 0},
        "last": {"date": "2026-09-07", "hour": 10},
    }
    assert parsed["source_update_timestamp"] is None
    assert parsed["source_update_missing_status"] == "SOURCE_VALUE_NULL"


@pytest.mark.parametrize(
    "hourly",
    [
        [{"hour": 0}, {"hour": 24}],
        [{"time": "00:00"}, {"time": "10:60"}],
        [{"spend": 1}, {"spend": 2}],
        [{"hour": 0}, {"time": "10:00"}],
    ],
)
def test_hourly_temporal_evidence_rejects_unsafe_or_inconsistent_values(
    hourly: list[object],
) -> None:
    assert _hourly_temporal_evidence(hourly) == {"found": False}


def test_response_time_summary_rejects_date_and_update_drift() -> None:
    wrong_date = response("YESTERDAY")
    with pytest.raises(RuntimeError, match="daily business date mismatch"):
        _response_time_summary(
            wrong_date,
            window_kind="YESTERDAY",
            expected_business_date=date(2026, 9, 6),
        )

    wrong_update = response("YESTERDAY")
    result = wrong_update["result"]
    assert isinstance(result, dict)
    result["lastUpdateTime"] = 1788849017123
    with pytest.raises(RuntimeError, match="not both null"):
        _response_time_summary(
            wrong_update,
            window_kind="YESTERDAY",
            expected_business_date=date(2026, 9, 7),
        )
