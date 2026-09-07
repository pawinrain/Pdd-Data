from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from test_stage_c_parsing import adapter

from pdd_data_mcp.browser.promotion import (
    PromotionOverviewCdpCollector,
    sanitize_discovery_path,
)
from pdd_data_mcp.config import (
    CollectionSettings,
    ConnectionSettings,
    DiscoverySettings,
    PromotionAdapterSettings,
)
from pdd_data_mcp.contracts.models import DatasetType, Scope, WindowKind
from pdd_data_mcp.errors import CollectionRejected
from pdd_data_mcp.validation import SnapshotValidator


class FakeRequest:
    def __init__(self, method: str = "GET") -> None:
        self.method = method


class FakeResponse:
    def __init__(
        self,
        body: bytes,
        *,
        url: str,
        content_type: str = "application/json",
        method: str = "GET",
    ) -> None:
        self.url = url
        self.status = 200
        self.headers = {"content-type": f"{content_type}; charset=utf-8"}
        self.request = FakeRequest(method)
        self._body = body
        self.body_reads = 0

    async def body(self) -> bytes:
        self.body_reads += 1
        await asyncio.sleep(0)
        return self._body


class FakeLocator:
    def __init__(self, value: str | None, attribute: str | None = None) -> None:
        self.value = value
        self.attribute = attribute

    async def count(self) -> int:
        return 1 if self.value is not None or self.attribute is not None else 0

    async def text_content(self) -> str | None:
        return self.value

    async def get_attribute(self, name: str) -> str | None:
        del name
        return self.attribute


class FakePage:
    def __init__(self, url: str, locators: dict[str, FakeLocator]) -> None:
        self.url = url
        self.locators = locators
        self.listeners: list[Any] = []
        self.responses: list[FakeResponse] = []

    def is_closed(self) -> bool:
        return False

    def locator(self, selector: str) -> FakeLocator:
        return self.locators.get(selector, FakeLocator(None))

    def on(self, event: str, callback: Any) -> None:
        assert event == "response"
        self.listeners.append(callback)

    def remove_listener(self, event: str, callback: Any) -> None:
        assert event == "response"
        self.listeners.remove(callback)

    async def reload(self, **kwargs: Any) -> None:
        del kwargs
        for response in self.responses:
            for callback in list(self.listeners):
                callback(response)
        await asyncio.sleep(0)


class FakeContext:
    def __init__(self, pages: list[FakePage]) -> None:
        self.pages = pages


class FakeBrowser:
    def __init__(self, pages: list[FakePage]) -> None:
        self.contexts = [FakeContext(pages)]


class FakeSession:
    def __init__(self, pages: list[FakePage]) -> None:
        self.browser = FakeBrowser(pages)
        self.disconnected = False

    async def disconnect(self) -> None:
        self.disconnected = True


class FakeConnector:
    def __init__(self, session: FakeSession) -> None:
        self.session = session
        self.connect_calls = 0

    async def connect(self, connection: ConnectionSettings, timeout_ms: int) -> FakeSession:
        del connection, timeout_ms
        self.connect_calls += 1
        return self.session


def current_day() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()


def response_body(*, cost: int = 12345, store: str = "platform-store-001") -> bytes:
    return json.dumps(
        {
            "result": {
                "success": True,
                "store_id": store,
                "business_date": current_day(),
                "ad_spend_cents": cost,
                "updated_at": f"{current_day()}T04:00:00+00:00",
            }
        }
    ).encode()


def make_connection(
    promotion_adapter: PromotionAdapterSettings,
    *,
    discovery: DiscoverySettings | None = None,
) -> ConnectionSettings:
    return ConnectionSettings(
        connection_id="conn_real",
        store_id="st_real",
        cdp_endpoint="http://127.0.0.1:9222",
        expected_platform_store_id="platform-store-001",
        target_page_url="http://127.0.0.1:8765/mains/promotionOverview",
        real_collection_enabled=True,
        promotion_adapter=promotion_adapter,
        discovery=discovery or DiscoverySettings(),
    )


def scope() -> Scope:
    return Scope(
        kind=WindowKind.TODAY,
        business_date=datetime.now(ZoneInfo("Asia/Shanghai")).date(),
    )


def make_page(cost_text: str = "123.45") -> FakePage:
    return FakePage(
        "http://127.0.0.1:8765/mains/promotionOverview?ignored=1",
        {
            "#store": FakeLocator(None, "platform-store-001"),
            "#business-date": FakeLocator(current_day()),
            "#ad-spend": FakeLocator(cost_text),
        },
    )


def test_collector_selects_target_not_first_and_cleans_listener(tmp_path: Path) -> None:
    unrelated = FakePage("http://127.0.0.1:8765/other", {})
    page = make_page()
    ignored = FakeResponse(response_body(cost=999), url="http://127.0.0.1:8765/api/other")
    selected = FakeResponse(
        response_body(),
        url="http://127.0.0.1:8765/api/promotion/overview?nonce=secret",
    )
    duplicate = FakeResponse(response_body(), url="http://127.0.0.1:8765/api/promotion/overview")
    page.responses = [ignored, selected, duplicate]
    session = FakeSession([unrelated, page])
    collector = PromotionOverviewCdpCollector(
        connection=make_connection(adapter()),
        collection=CollectionSettings(collection_timeout_ms=1000),
        runtime_root=tmp_path,
        connector=FakeConnector(session),  # type: ignore[arg-type]
    )
    draft = asyncio.run(
        collector.collect(
            store_id="st_real",
            dataset_type=DatasetType.PROMOTION_OVERVIEW,
            scope=scope(),
            limit=50,
        )
    )
    assert draft.source == "PDD_BROWSER_CDP"
    assert draft.capture_method == "NETWORK_RESPONSE"
    assert draft.payload["metrics"]["ad_spend"]["value"] == 12345
    assert draft.field_sources == {"metrics.ad_spend": "NETWORK_RESPONSE"}
    assert draft.identity_evidence is not None
    assert draft.identity_evidence.independent_verification_method == "CONFIG_EXACT_ID"
    assert draft.metric_window is not None
    assert draft.metric_window.end == draft.captured_at
    assert page.listeners == []
    assert session.disconnected is True
    assert ignored.body_reads == 0
    assert selected.body_reads + duplicate.body_reads >= 1
    assert SnapshotValidator().validate(draft, synthetic_allowed=False).valid is True
    missing_identity = draft.model_copy(update={"identity_evidence": None})
    invalid = SnapshotValidator().validate(missing_identity, synthetic_allowed=False)
    assert invalid.valid is False
    assert "REAL_IDENTITY_EVIDENCE_REQUIRED" in invalid.errors


def test_dom_conflict_rejects_snapshot_and_disconnects(tmp_path: Path) -> None:
    page = make_page("999.99")
    page.responses = [
        FakeResponse(response_body(), url="http://127.0.0.1:8765/api/promotion/overview")
    ]
    session = FakeSession([page])
    collector = PromotionOverviewCdpCollector(
        connection=make_connection(adapter()),
        collection=CollectionSettings(collection_timeout_ms=1000),
        runtime_root=tmp_path,
        connector=FakeConnector(session),  # type: ignore[arg-type]
    )
    with pytest.raises(CollectionRejected) as raised:
        asyncio.run(
            collector.collect(
                store_id="st_real",
                dataset_type=DatasetType.PROMOTION_OVERVIEW,
                scope=scope(),
                limit=50,
            )
        )
    assert raised.value.status == "DATA_MISMATCH"
    assert page.listeners == []
    assert session.disconnected is True


def test_identity_mismatch_is_rejected_before_dom_commit(tmp_path: Path) -> None:
    page = make_page()
    page.responses = [
        FakeResponse(
            response_body(store="other-store"),
            url="http://127.0.0.1:8765/api/promotion/overview",
        )
    ]
    collector = PromotionOverviewCdpCollector(
        connection=make_connection(adapter()),
        collection=CollectionSettings(collection_timeout_ms=1000),
        runtime_root=tmp_path,
        connector=FakeConnector(FakeSession([page])),  # type: ignore[arg-type]
    )
    with pytest.raises(CollectionRejected) as raised:
        asyncio.run(
            collector.collect(
                store_id="st_real",
                dataset_type=DatasetType.PROMOTION_OVERVIEW,
                scope=scope(),
                limit=50,
            )
        )
    assert raised.value.status == "IDENTITY_MISMATCH"


def test_discovery_records_only_sanitized_metadata_and_never_reads_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = make_page()
    response = FakeResponse(
        b'{"token":"must-not-be-read"}',
        url="http://127.0.0.1:8765/api/unknown?token=must-not-persist",
    )
    page.responses = [response]
    unverified = PromotionAdapterSettings(trigger="RELOAD")
    connection = make_connection(
        unverified,
        discovery=DiscoverySettings(enabled=True, observe_seconds=5, max_metadata_entries=10),
    )

    async def no_wait(seconds: float) -> None:
        del seconds

    monkeypatch.setattr("pdd_data_mcp.browser.promotion.asyncio.sleep", no_wait)
    collector = PromotionOverviewCdpCollector(
        connection=connection,
        collection=CollectionSettings(collection_timeout_ms=1000),
        runtime_root=tmp_path,
        connector=FakeConnector(FakeSession([page])),  # type: ignore[arg-type]
    )
    with pytest.raises(CollectionRejected) as raised:
        asyncio.run(
            collector.collect(
                store_id="st_real",
                dataset_type=DatasetType.PROMOTION_OVERVIEW,
                scope=scope(),
                limit=50,
            )
        )
    assert raised.value.status == "ADAPTER_UNVERIFIED"
    report = next((tmp_path / "discovery").rglob("*.json"))
    content = report.read_text(encoding="utf-8")
    assert '"path":"/api/unknown"' in content
    assert "must-not-persist" not in content
    assert "must-not-be-read" not in content
    assert response.body_reads == 0
    assert page.listeners == []


def test_discovery_path_redacts_likely_identifiers() -> None:
    assert sanitize_discovery_path("/api/store/123456/overview") == "/api/store/{id}/overview"
    assert sanitize_discovery_path("/api/a/550e8400-e29b-41d4-a716-446655440000") == "/api/a/{id}"
    assert sanitize_discovery_path("/api/a/%E5%BA%97%E9%93%BA") == "/api/a/{id}"


def test_candidate_probe_reads_only_exact_body_and_persists_no_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = make_page()
    ignored = FakeResponse(b'{"secret":"ignored-value"}', url="http://127.0.0.1:8765/api/other")
    selected = FakeResponse(
        b'{"result":{"store_id":"actual-store-value","cost":12345}}',
        url="http://127.0.0.1:8765/api/candidate?token=not-persisted",
    )
    page.responses = [ignored, selected]
    connection = make_connection(
        PromotionAdapterSettings(trigger="RELOAD"),
        discovery=DiscoverySettings(
            enabled=True,
            observe_seconds=5,
            candidate_body_probe_enabled=True,
            candidate_response_host="127.0.0.1",
            candidate_response_paths=["/api/candidate"],
            candidate_response_method="GET",
        ),
    )

    async def no_wait(seconds: float) -> None:
        del seconds

    monkeypatch.setattr("pdd_data_mcp.browser.promotion.asyncio.sleep", no_wait)
    collector = PromotionOverviewCdpCollector(
        connection=connection,
        collection=CollectionSettings(collection_timeout_ms=1000),
        runtime_root=tmp_path,
        connector=FakeConnector(FakeSession([page])),  # type: ignore[arg-type]
    )
    with pytest.raises(CollectionRejected):
        asyncio.run(
            collector.collect(
                store_id="st_real",
                dataset_type=DatasetType.PROMOTION_OVERVIEW,
                scope=scope(),
                limit=50,
            )
        )
    report = next((tmp_path / "discovery").rglob("*.json"))
    content = report.read_text(encoding="utf-8")
    assert '"path":"result.store_id","type":"string"' in content
    assert '"path":"result.cost","type":"number"' in content
    assert "actual-store-value" not in content
    assert "not-persisted" not in content
    assert "ignored-value" not in content
    assert ignored.body_reads == 0
    assert selected.body_reads == 1


def test_minimum_interval_rejects_before_a_second_cdp_connection(tmp_path: Path) -> None:
    page = make_page()
    page.responses = [
        FakeResponse(response_body(), url="http://127.0.0.1:8765/api/promotion/overview")
    ]
    connector = FakeConnector(FakeSession([page]))
    collector = PromotionOverviewCdpCollector(
        connection=make_connection(adapter()),
        collection=CollectionSettings(collection_timeout_ms=1000, min_interval_seconds=60),
        runtime_root=tmp_path,
        connector=connector,  # type: ignore[arg-type]
    )
    asyncio.run(
        collector.collect(
            store_id="st_real",
            dataset_type=DatasetType.PROMOTION_OVERVIEW,
            scope=scope(),
            limit=50,
        )
    )
    with pytest.raises(CollectionRejected) as raised:
        asyncio.run(
            collector.collect(
                store_id="st_real",
                dataset_type=DatasetType.PROMOTION_OVERVIEW,
                scope=scope(),
                limit=50,
            )
        )
    assert raised.value.status == "BUSY"
    assert raised.value.error_code == "MIN_COLLECTION_INTERVAL_NOT_ELAPSED"
    assert connector.connect_calls == 1


def test_explicit_dom_fallback_is_labeled_dom_after_network_timeout(tmp_path: Path) -> None:
    page = make_page()
    fallback_adapter = adapter().model_copy(update={"dom_fallback_enabled": True})
    collector = PromotionOverviewCdpCollector(
        connection=make_connection(fallback_adapter),
        collection=CollectionSettings(collection_timeout_ms=100, min_interval_seconds=0),
        runtime_root=tmp_path,
        connector=FakeConnector(FakeSession([page])),  # type: ignore[arg-type]
    )
    draft = asyncio.run(
        collector.collect(
            store_id="st_real",
            dataset_type=DatasetType.PROMOTION_OVERVIEW,
            scope=scope(),
            limit=50,
        )
    )
    assert draft.capture_method == "DOM"
    assert draft.field_sources == {"metrics.ad_spend": "DOM"}
    assert draft.payload["metrics"]["ad_spend"]["capture_method"] == "DOM"
    assert draft.identity_evidence is not None
    assert draft.identity_evidence.evidence_source == "DOM"
    assert SnapshotValidator().validate(draft, synthetic_allowed=False).valid is True


def test_collector_joins_exact_metric_and_identity_responses(tmp_path: Path) -> None:
    day = current_day()
    page = FakePage(
        "http://127.0.0.1:8765/mains/promotionOverview",
        {
            "#store": FakeLocator(None, "1001"),
            "#business-date": FakeLocator(day),
            "#ad-spend": FakeLocator("123.45"),
        },
    )
    identity = FakeResponse(
        b'{"success":true,"result":{"mallId":1001}}',
        url="http://127.0.0.1:8765/api/user/info",
        method="POST",
    )
    metric = FakeResponse(
        (
            '{"success":true,"result":['
            f'{{"date":"{day} 00:00:00",'
            '"dailyCostForHttp":{"unit":"YUAN","value":"123.45"}}]}'
        ).encode(),
        url="http://127.0.0.1:8765/api/daily-costs",
        method="POST",
    )
    page.responses = [identity, metric]
    joined = adapter(
        response_path="/api/daily-costs",
        response_method="POST",
        business_success_path="success",
        platform_store_id_path="",
        identity_response_host="127.0.0.1",
        identity_response_path="/api/user/info",
        identity_response_method="POST",
        identity_business_success_path="success",
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
    connection = make_connection(joined).model_copy(update={"expected_platform_store_id": "1001"})
    draft = asyncio.run(
        PromotionOverviewCdpCollector(
            connection=connection,
            collection=CollectionSettings(collection_timeout_ms=1000, min_interval_seconds=0),
            runtime_root=tmp_path,
            connector=FakeConnector(FakeSession([page])),  # type: ignore[arg-type]
        ).collect(
            store_id="st_real",
            dataset_type=DatasetType.PROMOTION_OVERVIEW,
            scope=scope(),
            limit=50,
        )
    )
    assert draft.payload["metrics"]["ad_spend"]["value"] == 12345
    assert draft.identity_evidence is not None
    assert draft.identity_evidence.response_field_path == "result.mallId"
    assert identity.body_reads == 1
    assert metric.body_reads == 1
