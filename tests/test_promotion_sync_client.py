from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from examples.promotion_sync_client import (
    _EFFECT_FIELDS,
    _account_payload_is_strict,
    _association_rows,
    _manifest_contract_is_valid,
    _record_payload_is_strict,
    scope,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
BUSINESS_DATE = datetime(2026, 9, 8, tzinfo=SHANGHAI).date()


def _metrics(*, all_null: bool = False) -> dict[str, Any]:
    values: dict[str, Any] = {field: None for field in _EFFECT_FIELDS}
    if not all_null:
        values["spend_cents"] = 123
    return {
        **values,
        "missing_reasons": {
            field: "SOURCE_VALUE_NULL" for field, value in values.items() if value is None
        },
    }


def _quality() -> dict[str, str]:
    return {"status": "VALID", "identity": "MATCHED", "coverage": "COMPLETE"}


def _account_read(*, yesterday: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    requested_scope = scope(
        "promotion_overview", run_business_date=BUSINESS_DATE, yesterday=yesterday
    )
    if yesterday:
        start = "2026-09-07T00:00:00+08:00"
        end = "2026-09-07T16:42:00+08:00"
        captured_at = "2026-09-08T16:45:00+08:00"
        source_updated_at = None
        source_updated_at_missing_reason = "SOURCE_VALUE_NULL"
        capture_method = "MIXED"
        end_source = "DOM"
    else:
        start = "2026-09-08T00:00:00+08:00"
        end = "2026-09-08T16:42:31+08:00"
        captured_at = "2026-09-08T16:45:00+08:00"
        source_updated_at = end
        source_updated_at_missing_reason = None
        capture_method = "NETWORK_RESPONSE"
        end_source = "NETWORK_RESPONSE"
    read = {
        "manifest": {
            "dataset_type": "promotion_overview",
            "source": "PDD_BROWSER_CDP",
            "capture_method": capture_method,
            "quality": _quality(),
            "scope": requested_scope,
            "captured_at": captured_at,
            "source_updated_at": source_updated_at,
            "metric_window": {
                "kind": requested_scope["kind"],
                "start": start,
                "end": end,
                "window_complete": False,
                "source_finalized": False,
            },
            "field_sources": {"metric_window.end": end_source},
        },
        "data": {
            "entity_granularity": "ACCOUNT",
            "business_date": requested_scope["business_date"],
            "metrics": _metrics(),
            "observed_at": captured_at,
            "source_updated_at": source_updated_at,
            "source_updated_at_missing_reason": source_updated_at_missing_reason,
        },
        "records": [],
    }
    return read, requested_scope


def _product_read() -> tuple[dict[str, Any], dict[str, Any]]:
    requested_scope = scope("product_metrics", run_business_date=BUSINESS_DATE)
    captured_at = "2026-09-08T16:45:00+08:00"
    source_updated_at = "2026-09-08T16:42:31+08:00"
    read = {
        "manifest": {
            "dataset_type": "product_metrics",
            "source": "PDD_BROWSER_CDP",
            "capture_method": "NETWORK_RESPONSE",
            "quality": _quality(),
            "scope": requested_scope,
            "captured_at": captured_at,
            "source_updated_at": source_updated_at,
            "metric_window": {
                "kind": "TODAY",
                "start": "2026-09-08T00:00:00+08:00",
                "end": captured_at,
                "window_complete": False,
                "source_finalized": False,
            },
        },
        "data": None,
        "records": [
            {
                "entity_granularity": "PROMOTED_PRODUCT",
                "ad_id": "ad-1",
                "campaign_id": "campaign-1",
                "platform_product_id": "product-1",
                "metrics": _metrics(),
                "observed_at": captured_at,
                "source_updated_at": source_updated_at,
            }
        ],
    }
    return read, requested_scope


def _configuration_read() -> tuple[dict[str, Any], dict[str, Any]]:
    requested_scope = scope("promotion_configuration", run_business_date=BUSINESS_DATE)
    captured_at = "2026-09-08T16:45:00+08:00"
    read = {
        "manifest": {
            "dataset_type": "promotion_configuration",
            "source": "PDD_BROWSER_CDP",
            "capture_method": "NETWORK_RESPONSE",
            "quality": _quality(),
            "scope": requested_scope,
            "captured_at": captured_at,
            "source_updated_at": None,
            "metric_window": None,
        },
        "data": None,
        "records": [
            {
                "entity_granularity": "PROMOTED_PRODUCT",
                "ad_id": "ad-1",
                "campaign_id": "campaign-1",
                "platform_product_id": "product-1",
                "configuration_observed_at": captured_at,
                "max_cost_cents": 5000,
                "target_roi": None,
                "agent_bid": None,
                "ad_status": None,
                "missing_reasons": {
                    "target_roi": "SOURCE_VALUE_NULL",
                    "agent_bid": "SOURCE_VALUE_NULL",
                    "ad_status": "SOURCE_VALUE_NULL",
                },
            }
        ],
    }
    return read, requested_scope


@pytest.mark.parametrize("yesterday", [False, True])
def test_account_manifest_and_payload_accept_evidenced_windows(yesterday: bool) -> None:
    read, requested_scope = _account_read(yesterday=yesterday)

    assert _account_payload_is_strict(
        read,
        expected_business_date=requested_scope["business_date"],
        yesterday=yesterday,
    )
    assert _manifest_contract_is_valid(
        read,
        dataset="promotion_overview",
        requested_scope=requested_scope,
        captured=1,
    )


@pytest.mark.parametrize("yesterday", [False, True])
@pytest.mark.parametrize("drift", ["capture_method", "field_source"])
def test_account_manifest_rejects_capture_provenance_drift(yesterday: bool, drift: str) -> None:
    read, requested_scope = _account_read(yesterday=yesterday)
    manifest = read["manifest"]
    if drift == "capture_method":
        manifest["capture_method"] = "NETWORK_RESPONSE" if yesterday else "MIXED"
    else:
        manifest["field_sources"]["metric_window.end"] = "NETWORK_RESPONSE" if yesterday else "DOM"

    assert not _manifest_contract_is_valid(
        read,
        dataset="promotion_overview",
        requested_scope=requested_scope,
        captured=1,
    )


@pytest.mark.parametrize(
    "source_updated_at",
    ["2026-09-07T23:59:59+08:00", "2026-09-08T16:45:01+08:00"],
)
def test_product_metrics_rejects_source_update_outside_window(
    source_updated_at: str,
) -> None:
    read, requested_scope = _product_read()
    read["manifest"]["source_updated_at"] = source_updated_at
    read["records"][0]["source_updated_at"] = source_updated_at

    assert not _manifest_contract_is_valid(
        read,
        dataset="product_metrics",
        requested_scope=requested_scope,
        captured=1,
    )


def test_product_metrics_rejects_all_null_business_fields() -> None:
    read, requested_scope = _product_read()
    read["records"][0]["metrics"] = _metrics(all_null=True)

    assert not _record_payload_is_strict(read, dataset="product_metrics")
    assert not _manifest_contract_is_valid(
        read,
        dataset="product_metrics",
        requested_scope=requested_scope,
        captured=1,
    )


def test_product_metrics_requires_exact_missing_reason_pairing() -> None:
    read, requested_scope = _product_read()
    assert _record_payload_is_strict(read, dataset="product_metrics")
    assert _manifest_contract_is_valid(
        read,
        dataset="product_metrics",
        requested_scope=requested_scope,
        captured=1,
    )

    extra_reason = deepcopy(read)
    extra_reason["records"][0]["metrics"]["missing_reasons"]["spend_cents"] = "SOURCE_VALUE_NULL"
    missing_reason = deepcopy(read)
    missing_reason["records"][0]["metrics"]["missing_reasons"].pop("click_count")

    assert not _record_payload_is_strict(extra_reason, dataset="product_metrics")
    assert not _record_payload_is_strict(missing_reason, dataset="product_metrics")


def test_configuration_accepts_valid_product_association() -> None:
    product, _ = _product_read()
    configuration, requested_scope = _configuration_read()

    assert _record_payload_is_strict(configuration, dataset="promotion_configuration")
    assert _manifest_contract_is_valid(
        configuration,
        dataset="promotion_configuration",
        requested_scope=requested_scope,
        captured=1,
    )
    assert (
        _association_rows(product)
        == _association_rows(configuration)
        == {("ad-1", "campaign-1", "product-1")}
    )


def test_configuration_rejects_all_null_business_fields() -> None:
    read, requested_scope = _configuration_read()
    record = read["records"][0]
    for field in ("max_cost_cents", "target_roi", "agent_bid", "ad_status"):
        record[field] = None
        record["missing_reasons"][field] = "SOURCE_VALUE_NULL"

    assert not _record_payload_is_strict(read, dataset="promotion_configuration")
    assert not _manifest_contract_is_valid(
        read,
        dataset="promotion_configuration",
        requested_scope=requested_scope,
        captured=1,
    )
