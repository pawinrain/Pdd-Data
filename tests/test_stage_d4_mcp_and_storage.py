from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from conftest import make_repository

from pdd_data_mcp.application import PddDataService
from pdd_data_mcp.collectors import SyntheticCollector
from pdd_data_mcp.config import (
    AppConfig,
    CollectionSettings,
    ConnectionSettings,
    PromotionMetricsAdapterSettings,
    ServiceSettings,
    StorageSettings,
)
from pdd_data_mcp.contracts.models import (
    Coverage,
    CoverageStatus,
    DatasetType,
    IdentityEvidence,
    MetricWindow,
    PromotedProductEffectMetrics,
    PromotedProductMetricRecord,
    PromotionConfigurationRecord,
    Quality,
    Scope,
    SnapshotDraft,
    WindowKind,
)
from pdd_data_mcp.utils import scope_key
from pdd_data_mcp.validation import SnapshotValidator


def promotion_adapter() -> PromotionMetricsAdapterSettings:
    return PromotionMetricsAdapterSettings(
        verified=True,
        data_source="NETWORK_RESPONSE",
        request_contract_version="PROMOTED_PRODUCT_LIST_UNFILTERED_V1",
        request_crawler_info_max_length=4096,
        target_page_url="https://yingxiao.pinduoduo.com/goods/promotion/list",
        response_host="yingxiao.pinduoduo.com",
        response_path="/mms-gateway/venus/api/goods/promotion/v3/list",
        response_method="POST",
        business_success_path="success",
        identity_source="IDENTITY_RESPONSE",
        identity_response_host="yingxiao.pinduoduo.com",
        identity_response_path="/mms-gateway/venus/api/user/userInfo",
        identity_response_method="POST",
        identity_business_success_path="success",
        identity_platform_store_id_path="result.mall.mallId",
        identity_verification_reference="test-independent-binding",
        parser_version="pdd-promoted-product-v3/1.0.0",
        trigger="RELOAD",
        supported_windows=["TODAY"],
        quick_option_testids={"TODAY": "DateAreaQuickOption_0"},
        list_path="result.adInfos",
        summary_path="result.sumReportInfo",
        source_updated_at_path="result.reportLastUpdateTime",
        row_platform_store_id_path="mallId",
        promotion_id_path="adId",
        campaign_id_path="planId",
        platform_product_id_path="goodsId",
        product_name_path="goodsInfo.goodsName",
        report_path="reportInfo",
        max_cost_path="maxCost",
        target_roi_path="targetRoi",
        agent_bid_path="agentBid",
        ad_status_path="adStatus",
        request_begin_date_field="beginDate",
        request_end_date_field="endDate",
        request_page_number_field="pageNumber",
        request_page_size_field="pageSize",
        dom_row_selector="tbody tr",
        reload_resets_to_today=True,
    )


def real_config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        service=ServiceSettings(),
        storage=StorageSettings(
            data_root=tmp_path / "data",
            runtime_root=tmp_path / "runtime",
            min_free_bytes=0,
        ),
        collection=CollectionSettings(min_interval_seconds=0),
        connections=[
            ConnectionSettings(
                connection_id="conn_d4",
                store_id="st_d4",
                cdp_endpoint="http://127.0.0.1:9222",
                expected_platform_store_id="9001",
                real_collection_enabled=True,
                promotion_metrics_adapter=promotion_adapter(),
            )
        ],
        config_path=tmp_path / "config.toml",
    )


class StaticD4Collector:
    async def collect(
        self,
        *,
        store_id: str,
        dataset_type: DatasetType,
        scope: Scope,
        limit: int,
        batch_id: str | None = None,
    ) -> SnapshotDraft:
        del limit
        observed = datetime(2026, 9, 8, 3, 1, tzinfo=UTC)
        updated = observed - timedelta(minutes=1)
        metric_window = None
        if dataset_type is DatasetType.PRODUCT_METRICS:
            record = PromotedProductMetricRecord(
                ad_id="101",
                campaign_id="201",
                platform_product_id="301",
                metrics=PromotedProductEffectMetrics(
                    spend_cents=100,
                    order_spend_cents=100,
                    order_spend_roi="1",
                    order_spend_net_roi="1",
                    net_order_count=1,
                    order_count=1,
                    gmv_cents=100,
                    net_gmv_cents=100,
                    impression_count=1,
                    click_count=1,
                    settlement_roi="1",
                    settlement_order_count=1,
                ),
                observed_at=observed,
                source_updated_at=updated,
            )
            payload = [record.model_dump(mode="json")]
            metric_window = MetricWindow(
                kind=scope.kind,
                timezone=scope.timezone,
                start=datetime(2026, 9, 7, 16, tzinfo=UTC),
                end=observed,
                window_complete=False,
                source_finalized=False,
            )
            source_updated_at = updated
            field_sources = {"records.metrics.*": "NETWORK_RESPONSE"}
        elif dataset_type is DatasetType.PROMOTION_CONFIGURATION:
            configuration = PromotionConfigurationRecord(
                ad_id="101",
                campaign_id="201",
                platform_product_id="301",
                max_cost_cents=2_000,
                target_roi="2",
                agent_bid=None,
                ad_status=1,
                configuration_observed_at=observed,
                missing_reasons={"agent_bid": "SOURCE_VALUE_NULL"},
            )
            payload = [configuration.model_dump(mode="json")]
            source_updated_at = None
            field_sources = {"records.*": "NETWORK_RESPONSE"}
        else:
            raise AssertionError("unexpected dataset")
        return SnapshotDraft(
            batch_id=batch_id,
            store_id=store_id,
            dataset_type=dataset_type,
            scope=scope,
            scope_key=scope_key(scope),
            requested_at=observed,
            capture_started_at=observed,
            capture_finished_at=observed,
            captured_at=observed,
            metric_window=metric_window,
            source_updated_at=source_updated_at,
            source="PDD_BROWSER_CDP",
            capture_method="NETWORK_RESPONSE",
            parser_version="test/1.0.0",
            identity_evidence=IdentityEvidence(
                expected_platform_store_id="9001",
                observed_platform_store_id="9001",
                evidence_source="NETWORK_RESPONSE",
                independent_verification_reference="test-independent-binding",
                response_field_path="result.mall.mallId",
            ),
            field_sources=field_sources,
            payload=payload,
            missing_fields=(
                ["records[0].agent_bid:SOURCE_VALUE_NULL"]
                if dataset_type is DatasetType.PROMOTION_CONFIGURATION
                else []
            ),
            quality=Quality(
                status="VALID",
                identity="MATCHED",
                coverage=CoverageStatus.COMPLETE,
                dom_check="MATCHED",
            ),
            coverage=Coverage(
                total_observed=1,
                captured=1,
                limit=50,
                pages_read=1,
                coverage=CoverageStatus.COMPLETE,
                truncated=False,
            ),
        )


def test_d4_capabilities_are_precise_and_campaign_stays_unavailable(tmp_path: Path) -> None:
    config = real_config(tmp_path)
    service = PddDataService(
        config=config,
        repository=make_repository(config),
        synthetic_collector=SyntheticCollector(),
        validator=SnapshotValidator(),
    )
    capabilities = service.capabilities()
    assert capabilities.datasets["promotion_overview"] == "CONFIGURED_NOT_VERIFIED"
    assert capabilities.datasets["product_metrics"] == "REAL_PROMOTED_PRODUCT_METRICS"
    assert (
        capabilities.datasets["promotion_configuration"] == "REAL_PROMOTION_CONFIGURATION_CURRENT"
    )
    assert capabilities.datasets["campaign_metrics"] == "UNAVAILABLE"
    assert capabilities.dataset_details["product_metrics"].supported_window_kinds == [
        WindowKind.TODAY,
    ]
    overview = capabilities.dataset_details["promotion_overview"]
    assert overview.verified is False
    assert overview.supported_window_kinds == []
    campaign = capabilities.dataset_details["campaign_metrics"]
    assert campaign.verified is False
    assert campaign.limitation is not None and "association" in campaign.limitation


def test_d4_file_roundtrip_latest_restart_and_idempotency(tmp_path: Path) -> None:
    config = real_config(tmp_path)
    repository = make_repository(config)
    service = PddDataService(
        config=config,
        repository=repository,
        synthetic_collector=SyntheticCollector(),
        validator=SnapshotValidator(),
        real_collectors={"conn_d4": StaticD4Collector()},
    )
    scopes = {
        DatasetType.PRODUCT_METRICS: Scope(
            kind=WindowKind.TODAY,
            business_date=date(2026, 9, 8),
            object_type="PROMOTED_PRODUCT_ALL",
        ),
        DatasetType.PROMOTION_CONFIGURATION: Scope(
            kind=WindowKind.POINT_IN_TIME,
            business_date=date(2026, 9, 8),
            object_type="PROMOTED_PRODUCT_ALL",
        ),
    }
    with repository.service_lock():
        repository.initialize()
        snapshot_ids: list[str] = []
        for dataset, current_scope in scopes.items():
            call = dict(
                connection_id="conn_d4",
                dataset_type=dataset,
                scope=current_scope,
                limit=50,
                idempotency_key=f"d4-{dataset.value}",
            )
            first = asyncio.run(service.collect_snapshot(**call))
            replay = asyncio.run(service.collect_snapshot(**call))
            assert first.committed is True
            assert replay.snapshot_id == first.snapshot_id
            assert replay.idempotent_replay is True
            assert first.snapshot_id is not None
            snapshot_ids.append(first.snapshot_id)
            latest = service.latest_snapshot(
                store_id="st_d4",
                dataset_type=dataset,
                scope=current_scope,
                require_complete=True,
            )
            assert latest.status == "FOUND"
            assert latest.snapshot is not None
            assert latest.snapshot.snapshot_id == first.snapshot_id

    restarted_repository = make_repository(config)
    with restarted_repository.service_lock():
        restarted_repository.initialize()
        restarted_repository.recover()
        for snapshot_id in snapshot_ids:
            read = restarted_repository.read_snapshot(
                snapshot_id=snapshot_id, cursor=None, page_size=200
            )
            assert read.snapshot_id == snapshot_id
            assert len(read.records) == 1


def test_product_metric_validator_rejects_source_update_outside_window() -> None:
    current_scope = Scope(
        kind=WindowKind.TODAY,
        business_date=date(2026, 9, 8),
        object_type="PROMOTED_PRODUCT_ALL",
    )
    draft = asyncio.run(
        StaticD4Collector().collect(
            store_id="st_d4",
            dataset_type=DatasetType.PRODUCT_METRICS,
            scope=current_scope,
            limit=50,
        )
    )
    assert SnapshotValidator().validate(draft, synthetic_allowed=False).valid is True
    assert draft.metric_window is not None

    for invalid_source in (
        draft.metric_window.start,
        draft.metric_window.end + timedelta(seconds=1),
    ):
        row = dict(draft.payload[0])
        row["source_updated_at"] = invalid_source.isoformat()
        tampered = draft.model_copy(update={"source_updated_at": invalid_source, "payload": [row]})
        report = SnapshotValidator().validate(tampered, synthetic_allowed=False)
        assert report.valid is False
        assert "product metric source_updated_at outside metric window" in report.errors
