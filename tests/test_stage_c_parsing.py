from __future__ import annotations

import hashlib
from datetime import date

import pytest

from pdd_data_mcp.browser.parsing import parse_dom_money, parse_promotion_response
from pdd_data_mcp.config import ConnectionSettings, PromotionAdapterSettings
from pdd_data_mcp.errors import CollectionRejected


def adapter(**updates: object) -> PromotionAdapterSettings:
    values: dict[str, object] = {
        "verified": True,
        "response_host": "127.0.0.1",
        "response_path": "/api/promotion/overview",
        "response_method": "GET",
        "response_http_status": 200,
        "response_content_type": "application/json",
        "business_success_path": "result.success",
        "business_success_value": True,
        "platform_store_id_path": "result.store_id",
        "business_date_path": "result.business_date",
        "ad_spend_path": "result.ad_spend_cents",
        "ad_spend_unit": "CNY_CENT",
        "source_updated_at_path": "result.updated_at",
        "parser_version": "local-promotion/1.0.0",
        "trigger": "RELOAD",
        "dom_store_id_selector": "#store",
        "dom_store_id_attribute": "data-store-id",
        "dom_business_date_selector": "#business-date",
        "dom_ad_spend_selector": "#ad-spend",
        "dom_ad_spend_unit": "CNY",
    }
    values.update(updates)
    return PromotionAdapterSettings.model_validate(values)


def response_body(
    *,
    cost: str | int = 12345,
    store_id: str = "platform-store-001",
    day: str = "2026-09-06",
    updated_at: str = "2026-09-06T04:00:00Z",
) -> bytes:
    cost_json = f'"{cost}"' if isinstance(cost, str) else str(cost)
    return (
        '{"result":{"success":true,"store_id":"'
        + store_id
        + '","business_date":"'
        + day
        + '","ad_spend_cents":'
        + cost_json
        + f',"updated_at":"{updated_at}"}}}}'
    ).encode()


def test_verified_response_accepts_real_zero_and_preserves_times() -> None:
    parsed = parse_promotion_response(
        response_body(cost=0),
        adapter(),
        expected_store_id="platform-store-001",
        expected_business_date=date(2026, 9, 6),
    )
    assert parsed.ad_spend_cents == 0
    assert parsed.business_date == date(2026, 9, 6)
    assert parsed.source_updated_at is not None
    assert parsed.source_updated_at.utcoffset() is not None


@pytest.mark.parametrize(
    ("body", "status"),
    [
        (response_body(store_id="other-store"), "IDENTITY_MISMATCH"),
        (response_body(day="2026-09-05"), "TIME_SCOPE_UNVERIFIED"),
        (b'{"result":{"success":false}}', "PLATFORM_ERROR"),
        (b'{"result":{"success":true,"store_id":"x"}}', "IDENTITY_MISMATCH"),
    ],
)
def test_response_rejects_unverified_identity_scope_and_business_status(
    body: bytes, status: str
) -> None:
    with pytest.raises(CollectionRejected) as raised:
        parse_promotion_response(
            body,
            adapter(),
            expected_store_id="platform-store-001",
            expected_business_date=date(2026, 9, 6),
        )
    assert raised.value.status == status


def test_response_rejects_duplicate_keys_nan_and_unverified_adapter() -> None:
    with pytest.raises(CollectionRejected):
        parse_promotion_response(
            b'{"result":{"success":true,"success":true}}',
            adapter(),
            expected_store_id="platform-store-001",
            expected_business_date=date(2026, 9, 6),
        )
    with pytest.raises(CollectionRejected):
        parse_promotion_response(
            b'{"result":{"success":true,"store_id":"platform-store-001",'
            b'"business_date":"2026-09-06","ad_spend_cents":NaN}}',
            adapter(),
            expected_store_id="platform-store-001",
            expected_business_date=date(2026, 9, 6),
        )
    with pytest.raises(CollectionRejected) as raised:
        parse_promotion_response(
            response_body(),
            PromotionAdapterSettings(),
            expected_store_id="platform-store-001",
            expected_business_date=date(2026, 9, 6),
        )
    assert raised.value.status == "ADAPTER_UNVERIFIED"


def test_merchant_page_identity_verification_requires_a_bounded_reference() -> None:
    with pytest.raises(ValueError, match="identity_verification_reference"):
        adapter(
            identity_verification_method="MERCHANT_PAGE_STATE_SHA256",
            identity_verification_reference="",
        )

    configured = adapter(
        identity_verification_method="MERCHANT_PAGE_STATE_SHA256",
        identity_verification_reference="merchant-basic-page-bootstrap-v1",
    )
    assert configured.identity_verification_reference == "merchant-basic-page-bootstrap-v1"


def test_response_rejects_source_update_outside_business_day() -> None:
    with pytest.raises(CollectionRejected) as raised:
        parse_promotion_response(
            response_body(updated_at="2026-09-05T04:00:00Z"),
            adapter(),
            expected_store_id="platform-store-001",
            expected_business_date=date(2026, 9, 6),
        )
    assert raised.value.status == "TIME_SCOPE_UNVERIFIED"


def test_dated_list_and_separate_identity_response_are_strictly_joined() -> None:
    dated_adapter = adapter(
        business_success_path="success",
        platform_store_id_path="",
        identity_response_host="127.0.0.1",
        identity_response_path="/api/user/info",
        identity_response_method="POST",
        identity_business_success_path="success",
        identity_business_success_value=True,
        identity_platform_store_id_path="result.mallId",
        metric_list_path="result",
        metric_item_business_date_path="date",
        metric_item_date_format="ISO_DATETIME_SECONDS",
        business_date_path="",
        ad_spend_path="dailyCostForHttp.value",
        ad_spend_unit="CNY",
        ad_spend_unit_path="dailyCostForHttp.unit",
        ad_spend_expected_unit_value="YUAN",
        source_updated_at_path="",
    )
    metric = (
        b'{"success":true,"result":['
        b'{"date":"2026-09-05 00:00:00","dailyCostForHttp":{"unit":"YUAN","value":"1.00"}},'
        b'{"date":"2026-09-06 00:00:00","dailyCostForHttp":{"unit":"YUAN","value":"123.45"}}]}'
    )
    identity = b'{"success":true,"result":{"mallId":123456}}'
    parsed = parse_promotion_response(
        metric,
        dated_adapter,
        expected_store_id="123456",
        expected_business_date=date(2026, 9, 6),
        identity_raw=identity,
    )
    assert parsed.ad_spend_cents == 12345
    assert parsed.platform_store_id == "123456"

    fingerprinted = parse_promotion_response(
        metric,
        dated_adapter,
        expected_store_id="",
        expected_store_id_sha256=hashlib.sha256(b"123456").hexdigest(),
        expected_business_date=date(2026, 9, 6),
        identity_raw=identity,
    )
    assert fingerprinted.platform_store_id.startswith("sha256:")

    with pytest.raises(CollectionRejected) as wrong_unit:
        parse_promotion_response(
            metric.replace(b'"YUAN"', b'"UNKNOWN"'),
            dated_adapter,
            expected_store_id="123456",
            expected_business_date=date(2026, 9, 6),
            identity_raw=identity,
        )
    assert wrong_unit.value.status == "UNIT_UNVERIFIED"

    with pytest.raises(CollectionRejected) as missing_today:
        parse_promotion_response(
            metric,
            dated_adapter,
            expected_store_id="123456",
            expected_business_date=date(2026, 9, 7),
            identity_raw=identity,
        )
    assert missing_today.value.error_code == "TODAY_METRIC_RECORD_MISSING"


def test_dom_money_exact_approximate_missing_and_zero() -> None:
    assert parse_dom_money("¥123.45元", "CNY").displayed_cents == 12345
    assert parse_dom_money("0.00", "CNY").matches(0)
    approximate = parse_dom_money("1.2万", "CNY")
    assert approximate.precision == "APPROXIMATE"
    assert approximate.matches(1_200_000)
    assert approximate.matches(1_249_999)
    assert not approximate.matches(1_260_000)
    with pytest.raises(CollectionRejected) as raised:
        parse_dom_money("--", "CNY")
    assert raised.value.status == "DATA_MISMATCH"


def test_connection_rejects_non_loopback_cdp_and_requires_evidence() -> None:
    with pytest.raises(ValueError):
        adapter(dom_fallback_enabled=True, dom_store_id_selector="")
    with pytest.raises(ValueError):
        ConnectionSettings(
            connection_id="conn_real",
            store_id="st_real",
            cdp_endpoint="http://0.0.0.0:9222",
        )
    with pytest.raises(ValueError):
        ConnectionSettings(
            connection_id="conn_real",
            store_id="st_real",
            cdp_endpoint="http://127.0.0.1:9222",
            expected_platform_store_id="platform-store-001",
            real_collection_enabled=True,
        )
