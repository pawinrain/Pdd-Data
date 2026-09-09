from __future__ import annotations

import asyncio
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from conftest import make_repository
from pydantic import ValidationError

from pdd_data_mcp.application import PddDataService
from pdd_data_mcp.browser.dispatcher import RealDatasetCollector
from pdd_data_mcp.collectors import SyntheticCollector
from pdd_data_mcp.config import (
    AppConfig,
    CollectionSettings,
    ConnectionSettings,
    PromotionAccountAdapterSettings,
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
    PromotionAccountMetricPayload,
    PromotionAccountTimeEvidence,
    Quality,
    Scope,
    SnapshotDraft,
    SnapshotManifest,
    WindowKind,
)
from pdd_data_mcp.errors import AuthorizationError, SnapshotCorruptError, StorageError
from pdd_data_mcp.storage import LocalFileSnapshotRepository
from pdd_data_mcp.utils import canonical_json, scope_key, sha256_bytes
from pdd_data_mcp.validation import SnapshotValidator


def account_adapter() -> PromotionAccountAdapterSettings:
    return PromotionAccountAdapterSettings(
        verified=True,
        data_source="NETWORK_RESPONSE",
        request_contract_version="PROMOTION_ACCOUNT_HOURLY_DUAL_V1",
        target_page_url="https://yingxiao.pinduoduo.com/mains/promotionOverview",
        response_host="yingxiao.pinduoduo.com",
        response_path="/mms-gateway/poseidon/api/report/queryHourlyRangeReport",
        response_method="POST",
        response_http_status=200,
        business_success_path="success",
        business_success_value=True,
        identity_source="IDENTITY_RESPONSE",
        identity_response_host="yingxiao.pinduoduo.com",
        identity_response_path="/mms-gateway/venus/api/user/info",
        identity_response_method="POST",
        identity_response_http_status=200,
        identity_business_success_path="success",
        identity_business_success_value=True,
        identity_platform_store_id_path="result.mallId",
        identity_verification_reference="offline-account-binding",
        parser_version="d4-account-v3",
        trigger="RELOAD",
        supported_windows=["TODAY", "YESTERDAY"],
        daily_report_list_path="result.dailyReportList",
        summary_path="result.sumReport",
        daily_business_date_path="date",
        request_date_format="PDD_MIDNIGHT_SECONDS",
        response_date_format="PDD_MIDNIGHT_SECONDS",
        result_source_updated_at_path="result.reportLastUpdateTime",
        secondary_source_updated_at_path="result.lastUpdateTime",
        request_entity_id_field="entityId",
        request_start_date_field="startDate",
        request_end_date_field="endDate",
        request_query_dimension_type_field="queryDimensionType",
        request_report_promotion_type_field="reportPromotionType",
        request_client_type_field="clientType",
        request_end_day_hour_field="endDayHour",
        request_return_last_update_time_field="returnLastUpdateTime",
        request_block_types_field="blockTypes",
        request_crawler_info_field="crawlerInfo",
        dom_today_spend_selector="[data-testid='today-total-spend']",
        dom_yesterday_spend_selector="[data-testid='yesterday-total-spend']",
        dom_report_date_explanation_selector=("div[class*='ReportDateExplain_content__']"),
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
                connection_id="conn_account",
                store_id="st_account",
                cdp_endpoint="http://127.0.0.1:9222",
                expected_platform_store_id="9001",
                real_collection_enabled=True,
                promotion_account_adapter=account_adapter(),
            )
        ],
        config_path=tmp_path / "config.toml",
    )


def test_account_capability_is_independent_from_product_and_campaign(tmp_path: Path) -> None:
    config = real_config(tmp_path)
    capabilities = PddDataService(
        config=config,
        repository=make_repository(config),
        synthetic_collector=SyntheticCollector(),
        validator=SnapshotValidator(),
    ).capabilities()

    assert capabilities.datasets["promotion_overview"] == "REAL_PROMOTION_WINDOWS"
    overview = capabilities.dataset_details["promotion_overview"]
    assert overview.verified is True
    assert overview.supported_window_kinds == [WindowKind.TODAY, WindowKind.YESTERDAY]
    assert overview.entity_granularity == "ACCOUNT"
    assert overview.limitation is not None and "d4-account-v3" in overview.limitation
    assert capabilities.datasets["product_metrics"] == "CONFIGURED_NOT_VERIFIED"
    assert capabilities.datasets["campaign_metrics"] == "UNAVAILABLE"


def test_service_requires_v3_scope_for_new_account_adapter(tmp_path: Path) -> None:
    config = real_config(tmp_path)
    service = PddDataService(
        config=config,
        repository=make_repository(config),
        synthetic_collector=SyntheticCollector(),
        validator=SnapshotValidator(),
    )
    legacy = Scope(
        kind=WindowKind.TODAY,
        business_date=date.today(),
        object_type="ACCOUNT_ALL",
    )
    rejected = asyncio.run(
        service.collect_snapshot(
            connection_id="conn_account",
            dataset_type=DatasetType.PROMOTION_OVERVIEW,
            scope=legacy,
            limit=1,
            idempotency_key="account-legacy-scope",
        )
    )
    assert rejected.committed is False
    assert rejected.error_code == "REAL_DATASET_OR_SCOPE_NOT_ADAPTED"

    account_v2 = legacy.model_copy(update={"version": "d4-account-v2"})
    rejected_v2 = asyncio.run(
        service.collect_snapshot(
            connection_id="conn_account",
            dataset_type=DatasetType.PROMOTION_OVERVIEW,
            scope=account_v2,
            limit=1,
            idempotency_key="account-v2-retired-scope",
        )
    )
    assert rejected_v2.committed is False
    assert rejected_v2.error_code == "REAL_DATASET_OR_SCOPE_NOT_ADAPTED"

    account_v3 = legacy.model_copy(update={"version": "d4-account-v3"})
    enabled = asyncio.run(
        service.collect_snapshot(
            connection_id="conn_account",
            dataset_type=DatasetType.PROMOTION_OVERVIEW,
            scope=account_v3,
            limit=1,
            idempotency_key="account-v3-scope",
        )
    )
    assert enabled.committed is False
    assert enabled.error_code == "REAL_COLLECTOR_NOT_CONFIGURED"


class _RoutingStub:
    def __init__(self, marker: str) -> None:
        self.marker = marker
        self.calls: list[dict[str, Any]] = []

    async def collect(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self.marker


def test_dispatcher_separates_v3_account_from_stage_c_and_product_routes() -> None:
    stage_c = _RoutingStub("stage-c")
    account = _RoutingStub("account-v3")
    product = _RoutingStub("product")
    core = _RoutingStub("core")
    dispatcher = RealDatasetCollector(
        promotion=stage_c,  # type: ignore[arg-type]
        promotion_account=account,  # type: ignore[arg-type]
        promotion_metrics=product,  # type: ignore[arg-type]
        core=core,  # type: ignore[arg-type]
    )
    base_scope = Scope(
        kind=WindowKind.TODAY,
        business_date=date(2026, 9, 8),
        object_type="ACCOUNT_ALL",
    )

    legacy_result = asyncio.run(
        dispatcher.collect(
            store_id="st_account",
            dataset_type=DatasetType.PROMOTION_OVERVIEW,
            scope=base_scope,
            limit=1,
        )
    )
    v3_result = asyncio.run(
        dispatcher.collect(
            store_id="st_account",
            dataset_type=DatasetType.PROMOTION_OVERVIEW,
            scope=base_scope.model_copy(update={"version": "d4-account-v3"}),
            limit=1,
        )
    )

    assert legacy_result == "stage-c"
    assert v3_result == "account-v3"
    assert len(stage_c.calls) == 1
    assert len(account.calls) == 1
    assert product.calls == []
    assert core.calls == []


def yesterday_account_draft(*, version: str = "d4-account-v3") -> SnapshotDraft:
    zone = ZoneInfo("Asia/Shanghai")
    observed = datetime(2026, 9, 8, 1, 20, tzinfo=zone)
    scope = Scope(
        kind=WindowKind.YESTERDAY,
        business_date=date(2026, 9, 7),
        object_type="ACCOUNT_ALL",
        version=version,
    )
    metrics = PromotedProductEffectMetrics(
        spend_cents=100,
        order_spend_cents=200,
        order_spend_roi="2",
        order_spend_net_roi="1.5",
        net_order_count=1,
        order_count=2,
        gmv_cents=300,
        net_gmv_cents=250,
        impression_count=10,
        click_count=5,
        settlement_roi="1.2",
        settlement_order_count=1,
    )
    payload = PromotionAccountMetricPayload(
        business_date=scope.business_date,
        metrics=metrics,
        observed_at=observed,
        source_updated_at=None,
        source_updated_at_missing_reason="SOURCE_VALUE_NULL",
    )
    return SnapshotDraft(
        store_id="st_account",
        dataset_type=DatasetType.PROMOTION_OVERVIEW,
        scope=scope,
        scope_key=scope_key(scope),
        requested_at=observed,
        capture_started_at=observed,
        capture_finished_at=observed,
        captured_at=observed,
        metric_window=MetricWindow(
            kind=WindowKind.YESTERDAY,
            timezone="Asia/Shanghai",
            start=datetime(2026, 9, 7, 0, 0, tzinfo=zone),
            end=datetime(2026, 9, 7, 2, 0, tzinfo=zone),
            window_complete=False,
            source_finalized=False,
        ),
        source_updated_at=None,
        promotion_account_time_evidence=PromotionAccountTimeEvidence(
            window_kind=WindowKind.YESTERDAY,
            request_start_date=date(2026, 9, 7),
            request_end_date=date(2026, 9, 7),
            request_end_day_hour=1,
            response_business_date=date(2026, 9, 7),
            page_today_cutoff_hhmm="01:15",
            page_yesterday_cutoff_hhmm="01:59",
            response_hourly_row_count=2,
            response_first_hour=0,
            response_last_hour=1,
        ),
        source="PDD_BROWSER_CDP",
        capture_method="MIXED",
        parser_version=version,
        identity_evidence=IdentityEvidence(
            expected_platform_store_id="9001",
            observed_platform_store_id="9001",
            evidence_source="NETWORK_RESPONSE",
            independent_verification_method="CONFIG_EXACT_ID",
            independent_verification_reference="offline-account-binding",
            response_field_path="result.mallId",
        ),
        field_sources={
            "business_date": "NETWORK_RESPONSE",
            "metric_window.end": "DOM",
            **{f"metrics.{field}": "NETWORK_RESPONSE" for field in metrics.model_fields_set},
        },
        payload=payload.model_dump(mode="json"),
        missing_fields=["source_updated_at:SOURCE_VALUE_NULL"],
        quality=Quality(
            status="VALID",
            identity="MATCHED",
            coverage=CoverageStatus.COMPLETE,
            dom_check="MATCHED",
        ),
        coverage=Coverage(
            total_observed=1,
            captured=1,
            limit=1,
            pages_read=1,
            coverage=CoverageStatus.COMPLETE,
            truncated=False,
        ),
    )


def legacy_v2_yesterday_draft() -> SnapshotDraft:
    zone = ZoneInfo("Asia/Shanghai")
    return yesterday_account_draft(version="d4-account-v2").model_copy(
        update={
            "metric_window": MetricWindow(
                kind=WindowKind.YESTERDAY,
                timezone="Asia/Shanghai",
                start=datetime(2026, 9, 7, 0, 0, tzinfo=zone),
                end=datetime(2026, 9, 7, 1, 15, tzinfo=zone),
                window_complete=False,
                source_finalized=False,
            ),
            "promotion_account_time_evidence": None,
        }
    )


def today_account_draft() -> SnapshotDraft:
    zone = ZoneInfo("Asia/Shanghai")
    observed = datetime(2026, 9, 8, 16, 45, tzinfo=zone)
    updated = datetime(2026, 9, 8, 16, 42, 31, tzinfo=zone)
    base = yesterday_account_draft()
    scope = Scope(
        kind=WindowKind.TODAY,
        business_date=date(2026, 9, 8),
        object_type="ACCOUNT_ALL",
        version="d4-account-v3",
    )
    payload = PromotionAccountMetricPayload.model_validate_json(canonical_json(base.payload))
    payload = payload.model_copy(
        update={
            "business_date": scope.business_date,
            "observed_at": observed,
            "source_updated_at": updated,
            "source_updated_at_missing_reason": None,
        }
    )
    evidence = PromotionAccountTimeEvidence(
        window_kind=WindowKind.TODAY,
        request_start_date=scope.business_date,
        request_end_date=scope.business_date,
        request_end_day_hour=16,
        response_business_date=scope.business_date,
        page_today_cutoff_hhmm="16:42",
        page_yesterday_cutoff_hhmm="16:59",
        response_hourly_row_count=17,
        response_first_hour=0,
        response_last_hour=16,
    )
    return base.model_copy(
        update={
            "scope": scope,
            "scope_key": scope_key(scope),
            "requested_at": observed,
            "capture_started_at": observed,
            "capture_finished_at": observed,
            "captured_at": observed,
            "metric_window": MetricWindow(
                kind=WindowKind.TODAY,
                timezone="Asia/Shanghai",
                start=datetime(2026, 9, 8, 0, 0, tzinfo=zone),
                end=updated,
                window_complete=False,
                source_finalized=False,
            ),
            "source_updated_at": updated,
            "promotion_account_time_evidence": evidence,
            "capture_method": "NETWORK_RESPONSE",
            "field_sources": {field: "NETWORK_RESPONSE" for field in base.field_sources},
            "payload": payload.model_dump(mode="json"),
            "missing_fields": [],
        }
    )


class _StaticAccountCollector:
    async def collect(
        self,
        *,
        store_id: str,
        dataset_type: DatasetType,
        scope: Scope,
        limit: int,
        batch_id: str | None = None,
    ) -> SnapshotDraft:
        draft = yesterday_account_draft()
        assert store_id == draft.store_id
        assert dataset_type is DatasetType.PROMOTION_OVERVIEW
        assert scope == draft.scope
        return draft.model_copy(
            update={
                "batch_id": batch_id,
                "coverage": draft.coverage.model_copy(update={"limit": limit}),
            }
        )


class _ExplodingCollector:
    async def collect(self, **_: Any) -> SnapshotDraft:
        raise AssertionError("an effective-state replay must not start collection")


def replay_service(
    config: AppConfig,
    repository: LocalFileSnapshotRepository,
) -> PddDataService:
    connection = config.connections[0].model_copy(
        update={"real_collection_enabled": False, "synthetic_enabled": True}
    )
    replay_config = config.model_copy(
        update={
            "service": ServiceSettings(test_mode=True),
            "connections": [connection],
        }
    )
    return PddDataService(
        config=replay_config,
        repository=repository,
        synthetic_collector=_ExplodingCollector(),  # type: ignore[arg-type]
        validator=SnapshotValidator(),
    )


def commit_legacy_v2_snapshot(
    repository: LocalFileSnapshotRepository,
) -> tuple[SnapshotManifest, Path]:
    draft = legacy_v2_yesterday_draft()
    validation = SnapshotValidator().validate(draft, synthetic_allowed=False)
    assert validation.valid is True
    reservation = repository.begin_request(
        store_id=draft.store_id,
        dataset_type=draft.dataset_type,
        idempotency_key="legacy-v2-semantic-window",
        parameters={
            "store_id": draft.store_id,
            "dataset_type": draft.dataset_type.value,
            "scope": draft.scope.model_dump(mode="json"),
            "limit": draft.coverage.limit,
        },
        scope_key=draft.scope_key,
    )
    manifest, _ = repository.commit_reserved(reservation, draft, validation)
    snapshot_dir = next(repository.data_root.rglob(f"*{manifest.snapshot_id}"))
    return manifest, snapshot_dir


def commit_v3_snapshot(
    repository: LocalFileSnapshotRepository,
    *,
    idempotency_key: str = "account-v3-persistent-contract",
    draft: SnapshotDraft | None = None,
) -> tuple[SnapshotManifest, Path]:
    selected = draft or yesterday_account_draft()
    validation = SnapshotValidator().validate(selected, synthetic_allowed=False)
    assert validation.valid is True
    reservation = repository.begin_request(
        store_id=selected.store_id,
        dataset_type=selected.dataset_type,
        idempotency_key=idempotency_key,
        parameters={"scope": selected.scope.model_dump(mode="json")},
        scope_key=selected.scope_key,
    )
    manifest, _ = repository.commit_reserved(reservation, selected, validation)
    snapshot_dir = next(repository.data_root.rglob(f"*{manifest.snapshot_id}"))
    return manifest, snapshot_dir


def rewrite_manifest_and_commit(snapshot_dir: Path, value: dict[str, Any]) -> None:
    manifest_path = snapshot_dir / "manifest.json"
    commit_path = snapshot_dir / "COMMIT.json"
    manifest_path.write_bytes(canonical_json(value) + b"\n")
    commit = json.loads(commit_path.read_text(encoding="utf-8"))
    commit["manifest_sha256"] = sha256_bytes(manifest_path.read_bytes())
    commit_path.write_bytes(canonical_json(commit) + b"\n")


def test_validator_accepts_strict_v3_payload_and_rejects_legacy_masquerade() -> None:
    draft = yesterday_account_draft()
    assert SnapshotValidator().validate(draft, synthetic_allowed=False).valid is True

    legacy_scope = draft.scope.model_copy(update={"version": "1"})
    masquerade = draft.model_copy(
        update={"scope": legacy_scope, "scope_key": scope_key(legacy_scope)}
    )
    report = SnapshotValidator().validate(masquerade, synthetic_allowed=False)
    assert report.valid is False


def test_validator_keeps_old_v2_draft_without_optional_time_evidence_valid() -> None:
    legacy_draft = legacy_v2_yesterday_draft()

    report = SnapshotValidator().validate(legacy_draft, synthetic_allowed=False)

    assert report.valid is True


def test_validator_requires_v3_hourly_evidence_and_next_hour_exclusive_end() -> None:
    draft = yesterday_account_draft()

    missing = SnapshotValidator().validate(
        draft.model_copy(update={"promotion_account_time_evidence": None}),
        synthetic_allowed=False,
    )
    assert missing.valid is False
    assert "d4-account-v3 requires hourly time evidence" in missing.errors

    assert draft.metric_window is not None
    wrong_end = draft.metric_window.model_copy(
        update={"end": datetime(2026, 9, 7, 1, 59, tzinfo=ZoneInfo("Asia/Shanghai"))}
    )
    mislabeled = SnapshotValidator().validate(
        draft.model_copy(update={"metric_window": wrong_end}),
        synthetic_allowed=False,
    )
    assert mislabeled.valid is False
    assert "YESTERDAY account half-open window mismatch" in mislabeled.errors


@pytest.mark.parametrize(
    "evidence_update",
    [
        {"window_kind": WindowKind.TODAY},
        {"request_start_date": date(2026, 9, 6)},
        {"request_end_date": date(2026, 9, 6)},
        {"response_business_date": date(2026, 9, 6)},
        {"request_end_day_hour": 2},
        {"page_yesterday_cutoff_hhmm": "02:15"},
        {"business_timezone": "UTC"},
        {"page_semantics": "FULL_NATURAL_DAY"},
        {"request_source": "DOM"},
        {"response_hourly_row_count": 1},
        {"response_first_hour": 1},
        {"response_last_hour": 0},
    ],
)
def test_validator_rejects_inconsistent_account_time_evidence(
    evidence_update: dict[str, object],
) -> None:
    draft = yesterday_account_draft()
    evidence = draft.promotion_account_time_evidence
    assert evidence is not None
    tampered = draft.model_copy(
        update={
            "promotion_account_time_evidence": evidence.model_copy(update=evidence_update),
        }
    )

    report = SnapshotValidator().validate(tampered, synthetic_allowed=False)

    assert report.valid is False
    assert "account time evidence mismatch" in report.errors


@pytest.mark.parametrize(
    ("capture_method", "end_source"),
    [
        ("NETWORK_RESPONSE", "DOM"),
        ("MIXED", "NETWORK_RESPONSE"),
    ],
)
def test_validator_rejects_wrong_yesterday_cutoff_provenance(
    capture_method: str, end_source: str
) -> None:
    draft = yesterday_account_draft()
    tampered_sources = {**draft.field_sources, "metric_window.end": end_source}
    tampered = draft.model_copy(
        update={
            "capture_method": capture_method,
            "field_sources": tampered_sources,
        }
    )

    report = SnapshotValidator().validate(tampered, synthetic_allowed=False)

    assert report.valid is False
    assert "account metric cutoff source mismatch" in report.errors


def test_v3_scope_key_cannot_resolve_legacy_or_v2_snapshot_scopes() -> None:
    draft = yesterday_account_draft()
    legacy = draft.scope.model_copy(update={"version": "1"})
    account_v2 = draft.scope.model_copy(update={"version": "d4-account-v2"})
    assert scope_key(draft.scope) != scope_key(legacy)
    assert scope_key(draft.scope) != scope_key(account_v2)

    mislabeled = draft.model_copy(update={"scope_key": scope_key(account_v2)})
    report = SnapshotValidator().validate(mislabeled, synthetic_allowed=False)
    assert report.valid is False
    assert "SCOPE_KEY_MISMATCH" in report.errors


def test_account_v3_file_roundtrip_latest_restart_and_idempotency(tmp_path: Path) -> None:
    config = real_config(tmp_path)
    repository = make_repository(config)
    service = PddDataService(
        config=config,
        repository=repository,
        synthetic_collector=SyntheticCollector(),
        validator=SnapshotValidator(),
        real_collectors={"conn_account": _StaticAccountCollector()},
    )
    current_scope = yesterday_account_draft().scope
    call = dict(
        connection_id="conn_account",
        dataset_type=DatasetType.PROMOTION_OVERVIEW,
        scope=current_scope,
        limit=50,
        idempotency_key="account-v3-idempotency",
    )

    with repository.service_lock():
        repository.initialize()
        first = asyncio.run(service.collect_snapshot(**call))
        replay = asyncio.run(service.collect_snapshot(**call))
        assert first.committed is True and first.snapshot_id is not None
        assert replay.snapshot_id == first.snapshot_id
        assert replay.idempotent_replay is True
        latest = service.latest_snapshot(
            store_id="st_account",
            dataset_type=DatasetType.PROMOTION_OVERVIEW,
            scope=current_scope,
            require_complete=True,
        )
        assert latest.snapshot is not None
        assert latest.snapshot.snapshot_id == first.snapshot_id
        legacy_scope = current_scope.model_copy(update={"version": "1"})
        legacy_latest = service.latest_snapshot(
            store_id="st_account",
            dataset_type=DatasetType.PROMOTION_OVERVIEW,
            scope=legacy_scope,
            require_complete=True,
        )
        assert legacy_latest.status == "NOT_FOUND"
        assert legacy_latest.snapshot is None

    restarted = make_repository(config)
    with restarted.service_lock():
        restarted.initialize()
        restarted.recover()
        read = restarted.read_snapshot(
            snapshot_id=first.snapshot_id,
            cursor=None,
            page_size=50,
        )
        assert read.snapshot_id == first.snapshot_id
        assert read.records == []
        assert read.data is not None
        assert read.data["entity_granularity"] == "ACCOUNT"
        assert read.manifest["schema_version"] == "1.1.0"
        assert read.manifest["promotion_account_time_evidence"] == {
            "window_kind": "YESTERDAY",
            "request_start_date": "2026-09-07",
            "request_end_date": "2026-09-07",
            "request_end_day_hour": 1,
            "response_business_date": "2026-09-07",
            "page_today_cutoff_hhmm": "01:15",
            "page_yesterday_cutoff_hhmm": "01:59",
            "page_semantics": "SAME_PERIOD_COMPARISON",
            "business_timezone": "Asia/Shanghai",
            "request_source": "NETWORK_RESPONSE",
            "response_source": "NETWORK_RESPONSE",
            "page_source": "DOM",
            "response_hourly_row_count": 2,
            "response_first_hour": 0,
            "response_last_hour": 1,
        }
        v3_without_version = json.loads(json.dumps(read.manifest))
        v3_without_version.pop("schema_version")
        with pytest.raises(ValidationError, match="requires manifest schema 1.1.0"):
            SnapshotManifest.model_validate_json(json.dumps(v3_without_version))

        old_manifest = json.loads(json.dumps(read.manifest))
        old_manifest.pop("promotion_account_time_evidence")
        old_manifest["scope"]["version"] = "d4-account-v2"
        old_manifest["parser_version"] = "d4-account-v2"
        old_manifest["schema_version"] = "1.0.0"
        parsed_v1 = SnapshotManifest.model_validate_json(json.dumps(old_manifest))
        assert parsed_v1.schema_version == "1.0.0"
        assert parsed_v1.promotion_account_time_evidence is None
        assert "promotion_account_time_evidence" not in parsed_v1.model_dump(mode="json")
        assert (
            SnapshotManifest.model_validate_json(parsed_v1.model_dump_json()).schema_version
            == "1.0.0"
        )
        legacy_without_version = {**old_manifest}
        legacy_without_version.pop("schema_version")
        assert (
            SnapshotManifest.model_validate_json(json.dumps(legacy_without_version)).schema_version
            == "1.0.0"
        )

        v11_manifest = {**old_manifest, "schema_version": "1.1.0"}
        parsed_v11 = SnapshotManifest.model_validate_json(json.dumps(v11_manifest))
        assert parsed_v11.schema_version == "1.1.0"
        assert (
            SnapshotManifest.model_validate_json(parsed_v11.model_dump_json()).schema_version
            == "1.1.0"
        )

        v1_with_evidence = {
            **old_manifest,
            "promotion_account_time_evidence": read.manifest["promotion_account_time_evidence"],
        }
        with pytest.raises(ValidationError, match="cannot contain account time evidence"):
            SnapshotManifest.model_validate_json(json.dumps(v1_with_evidence))

        v1_v3 = {
            **old_manifest,
            "scope": {**old_manifest["scope"], "version": "d4-account-v3"},
            "parser_version": "d4-account-v3",
        }
        with pytest.raises(ValidationError, match="requires manifest schema 1.1.0"):
            SnapshotManifest.model_validate_json(json.dumps(v1_v3))


@pytest.mark.parametrize("tamper", ["delete-evidence", "change-hourly-evidence"])
def test_v3_manifest_time_evidence_tamper_is_not_indexed_or_selected(
    tmp_path: Path,
    tamper: str,
) -> None:
    config = real_config(tmp_path)
    repository = make_repository(config)
    scope = yesterday_account_draft().scope
    with repository.service_lock():
        repository.initialize()
        manifest, snapshot_dir = commit_v3_snapshot(repository)
        value = json.loads((snapshot_dir / "manifest.json").read_text(encoding="utf-8"))
        if tamper == "delete-evidence":
            value.pop("promotion_account_time_evidence")
        else:
            value["promotion_account_time_evidence"]["response_last_hour"] = 0
        rewrite_manifest_and_commit(snapshot_dir, value)

        with pytest.raises(SnapshotCorruptError):
            repository._verify_snapshot_metadata(snapshot_dir)
        rebuilt = repository.rebuild_index()
        assert snapshot_dir.name in rebuilt["invalid"]
        listed = repository.list_snapshots(
            store_id=manifest.store_id,
            dataset_type=manifest.dataset_type,
            captured_from=None,
            captured_to=None,
            cursor=None,
            limit=20,
        )
        latest = repository.latest_snapshot(
            store_id=manifest.store_id,
            dataset_type=manifest.dataset_type,
            scope=scope,
            require_complete=False,
        )
        assert listed.items == []
        assert latest.status == "NOT_FOUND"


@pytest.mark.parametrize(
    ("target", "value"),
    [
        ("dataset_type", "store_overview"),
        ("object_type", "STORE_ALL"),
        ("kind", "LAST_7_DAYS"),
        ("source", "SYNTHETIC"),
        ("window_complete", True),
        ("source_finalized", True),
        ("parser_version", "d4-account-v2"),
        ("yesterday_cutoff", "01:58"),
    ],
)
def test_v3_manifest_hard_locks_account_contract(
    tmp_path: Path,
    target: str,
    value: object,
) -> None:
    config = real_config(tmp_path)
    repository = make_repository(config)
    with repository.service_lock():
        repository.initialize()
        manifest, _ = commit_v3_snapshot(repository)
    encoded = json.loads(manifest.model_dump_json())
    if target in {"dataset_type", "source", "parser_version"}:
        encoded[target] = value
    elif target in {"object_type", "kind"}:
        encoded["scope"][target] = value
    elif target in {"window_complete", "source_finalized"}:
        encoded["metric_window"][target] = value
    else:
        encoded["promotion_account_time_evidence"]["page_yesterday_cutoff_hhmm"] = value

    with pytest.raises(ValidationError):
        SnapshotManifest.model_validate_json(json.dumps(encoded))


@pytest.mark.parametrize("tamper", ["cutoff-minute", "window-end"])
def test_v3_today_manifest_hard_locks_source_update_cutoff(
    tmp_path: Path,
    tamper: str,
) -> None:
    config = real_config(tmp_path)
    repository = make_repository(config)
    draft = today_account_draft()
    with repository.service_lock():
        repository.initialize()
        manifest, _ = commit_v3_snapshot(
            repository,
            idempotency_key="account-v3-today-contract",
            draft=draft,
        )
    encoded = json.loads(manifest.model_dump_json())
    if tamper == "cutoff-minute":
        encoded["promotion_account_time_evidence"]["page_today_cutoff_hhmm"] = "16:41"
    else:
        encoded["metric_window"]["end"] = "2026-09-08T16:43:00+08:00"

    with pytest.raises(ValidationError):
        SnapshotManifest.model_validate_json(json.dumps(encoded))


def test_append_only_invalidation_preserves_history_and_excludes_v2_latest(
    tmp_path: Path,
) -> None:
    config = real_config(tmp_path)
    repository = make_repository(config)
    v2_scope = legacy_v2_yesterday_draft().scope
    with repository.service_lock():
        repository.initialize()
        manifest, snapshot_dir = commit_legacy_v2_snapshot(repository)
        manifest_before = (snapshot_dir / "manifest.json").read_bytes()
        commit_before = (snapshot_dir / "COMMIT.json").read_bytes()

        before = repository.latest_snapshot(
            store_id=manifest.store_id,
            dataset_type=manifest.dataset_type,
            scope=v2_scope,
            require_complete=True,
        )
        assert before.status == "FOUND"

        invalidation = repository.invalidate_snapshot(
            snapshot_id=manifest.snapshot_id,
            reason_code="METRIC_WINDOW_END_MISLABELED",
            replacement_scope_version="d4-account-v3",
        )
        replay = repository.invalidate_snapshot(
            snapshot_id=manifest.snapshot_id,
            reason_code="METRIC_WINDOW_END_MISLABELED",
            replacement_scope_version="d4-account-v3",
        )
        assert replay.invalidation_id == invalidation.invalidation_id
        assert len(list((config.storage.data_root / "_invalidations").rglob("*.json"))) == 1
        assert (snapshot_dir / "manifest.json").read_bytes() == manifest_before
        assert (snapshot_dir / "COMMIT.json").read_bytes() == commit_before

        after = repository.latest_snapshot(
            store_id=manifest.store_id,
            dataset_type=manifest.dataset_type,
            scope=v2_scope,
            require_complete=True,
        )
        assert after.status == "NOT_FOUND"
        listed = repository.list_snapshots(
            store_id=manifest.store_id,
            dataset_type=manifest.dataset_type,
            captured_from=None,
            captured_to=None,
            cursor=None,
            limit=20,
        )
        assert listed.items[0].quality_status == "VALID"
        assert listed.items[0].effective_status == "SEMANTICALLY_INVALIDATED"
        assert listed.items[0].invalidation == invalidation
        read = repository.read_snapshot(
            snapshot_id=manifest.snapshot_id,
            cursor=None,
            page_size=20,
        )
        assert read.manifest["quality"]["status"] == "VALID"
        assert read.effective_status == "SEMANTICALLY_INVALIDATED"
        assert read.invalidation == invalidation

    restarted = make_repository(config)
    with restarted.service_lock():
        restarted.initialize()
        restarted.recover()
        latest = restarted.latest_snapshot(
            store_id=manifest.store_id,
            dataset_type=manifest.dataset_type,
            scope=v2_scope,
            require_complete=False,
        )
        assert latest.status == "NOT_FOUND"
        assert restarted.verify_storage()["semantically_invalidated_snapshots"] == 1


def test_invalidation_retry_rebuilds_until_existing_record_is_effective(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = real_config(tmp_path)
    repository = make_repository(config)
    with repository.service_lock():
        repository.initialize()
        manifest, _ = commit_legacy_v2_snapshot(repository)
        original_rebuild = repository.rebuild_index

        def fail_rebuild() -> dict[str, Any]:
            raise StorageError("injected index failure")

        monkeypatch.setattr(repository, "rebuild_index", fail_rebuild)
        for _ in range(2):
            with pytest.raises(StorageError, match="injected index failure"):
                repository.invalidate_snapshot(
                    snapshot_id=manifest.snapshot_id,
                    reason_code="METRIC_WINDOW_END_MISLABELED",
                    replacement_scope_version="d4-account-v3",
                )
        paths = list((config.storage.data_root / "_invalidations").rglob("*.json"))
        assert len(paths) == 1
        persisted_id = json.loads(paths[0].read_text(encoding="utf-8"))["invalidation_id"]

        monkeypatch.setattr(repository, "rebuild_index", original_rebuild)
        recovered = repository.invalidate_snapshot(
            snapshot_id=manifest.snapshot_id,
            reason_code="METRIC_WINDOW_END_MISLABELED",
            replacement_scope_version="d4-account-v3",
        )

        assert recovered.invalidation_id == persisted_id
        assert len(list((config.storage.data_root / "_invalidations").rglob("*.json"))) == 1
        assert repository.get_snapshot_effective_status(manifest.snapshot_id) == (
            "SEMANTICALLY_INVALIDATED"
        )


def test_corrupt_invalidation_is_fail_closed_for_latest_and_explicit_on_read(
    tmp_path: Path,
) -> None:
    config = real_config(tmp_path)
    repository = make_repository(config)
    v2_scope = legacy_v2_yesterday_draft().scope
    with repository.service_lock():
        repository.initialize()
        manifest, _ = commit_legacy_v2_snapshot(repository)
        invalidation = repository.invalidate_snapshot(
            snapshot_id=manifest.snapshot_id,
            reason_code="METRIC_WINDOW_END_MISLABELED",
            replacement_scope_version="d4-account-v3",
        )
        invalidation_path = (
            config.storage.data_root
            / "_invalidations"
            / manifest.snapshot_id
            / f"{invalidation.invalidation_id}.json"
        )
        invalidation_path.write_text("{broken", encoding="utf-8")

        latest = repository.latest_snapshot(
            store_id=manifest.store_id,
            dataset_type=manifest.dataset_type,
            scope=v2_scope,
            require_complete=False,
        )
        assert latest.status == "NOT_FOUND"
        read = repository.read_snapshot(
            snapshot_id=manifest.snapshot_id,
            cursor=None,
            page_size=20,
        )
        assert read.effective_status == "INVALIDATION_STATE_UNKNOWN"
        assert read.invalidation is None
        verified = repository.verify_storage()
        assert verified["status"] == "FAIL"
        assert verified["invalidation_state_unknown_snapshots"] == 1
        assert verified["invalid_invalidation_records"]


def test_semantically_valid_invalidation_with_wrong_prior_parser_is_unknown(
    tmp_path: Path,
) -> None:
    config = real_config(tmp_path)
    repository = make_repository(config)
    v2_scope = legacy_v2_yesterday_draft().scope
    with repository.service_lock():
        repository.initialize()
        manifest, _ = commit_legacy_v2_snapshot(repository)
        invalidation = repository.invalidate_snapshot(
            snapshot_id=manifest.snapshot_id,
            reason_code="METRIC_WINDOW_END_MISLABELED",
            replacement_scope_version="d4-account-v3",
        )
        invalidation_path = (
            config.storage.data_root
            / "_invalidations"
            / manifest.snapshot_id
            / f"{invalidation.invalidation_id}.json"
        )
        value = json.loads(invalidation_path.read_text(encoding="utf-8"))
        value["prior_parser_version"] = "d4-account-v1"
        invalidation_path.write_text(json.dumps(value), encoding="utf-8")

        listed = repository.list_snapshots(
            store_id=manifest.store_id,
            dataset_type=manifest.dataset_type,
            captured_from=None,
            captured_to=None,
            cursor=None,
            limit=20,
        )
        assert listed.items[0].effective_status == "INVALIDATION_STATE_UNKNOWN"
        assert listed.items[0].invalidation is None
        latest = repository.latest_snapshot(
            store_id=manifest.store_id,
            dataset_type=manifest.dataset_type,
            scope=v2_scope,
            require_complete=False,
        )
        assert latest.status == "NOT_FOUND"
        read = repository.read_snapshot(
            snapshot_id=manifest.snapshot_id,
            cursor=None,
            page_size=20,
        )
        assert read.effective_status == "INVALIDATION_STATE_UNKNOWN"
        assert read.invalidation is None


def test_semantic_invalidation_reauthorizes_the_snapshot_store(tmp_path: Path) -> None:
    config = real_config(tmp_path)
    writer = make_repository(config)
    with writer.service_lock():
        writer.initialize()
        manifest, _ = commit_legacy_v2_snapshot(writer)

    unauthorized = make_repository(config, allowed=frozenset({"st_other"}))
    with unauthorized.service_lock():
        unauthorized.initialize()
        with pytest.raises(AuthorizationError):
            unauthorized.invalidate_snapshot(
                snapshot_id=manifest.snapshot_id,
                reason_code="METRIC_WINDOW_END_MISLABELED",
                replacement_scope_version="d4-account-v3",
            )


@pytest.mark.parametrize(
    ("corrupt", "expected_error"),
    [
        (False, "IDEMPOTENT_SNAPSHOT_SEMANTICALLY_INVALIDATED"),
        (True, "IDEMPOTENT_SNAPSHOT_INVALIDATION_STATE_UNKNOWN"),
    ],
)
def test_committed_idempotency_replay_never_claims_invalidated_snapshot_success(
    tmp_path: Path,
    corrupt: bool,
    expected_error: str,
) -> None:
    config = real_config(tmp_path)
    repository = make_repository(config)
    scope = legacy_v2_yesterday_draft().scope
    with repository.service_lock():
        repository.initialize()
        manifest, _ = commit_legacy_v2_snapshot(repository)
        invalidation = repository.invalidate_snapshot(
            snapshot_id=manifest.snapshot_id,
            reason_code="METRIC_WINDOW_END_MISLABELED",
            replacement_scope_version="d4-account-v3",
        )
        if corrupt:
            invalidation_path = (
                config.storage.data_root
                / "_invalidations"
                / manifest.snapshot_id
                / f"{invalidation.invalidation_id}.json"
            )
            invalidation_path.write_text("{broken", encoding="utf-8")
        committed_before = list(config.storage.data_root.rglob("COMMIT.json"))

        replay = asyncio.run(
            replay_service(config, repository).collect_snapshot(
                connection_id="conn_account",
                dataset_type=DatasetType.PROMOTION_OVERVIEW,
                scope=scope,
                limit=1,
                idempotency_key="legacy-v2-semantic-window",
            )
        )

        assert replay.status == "DATA_MISMATCH"
        assert replay.committed is False
        assert replay.snapshot_id is None
        assert replay.idempotent_replay is True
        assert replay.error_code == expected_error
        assert list(config.storage.data_root.rglob("COMMIT.json")) == committed_before
