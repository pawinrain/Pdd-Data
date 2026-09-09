from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import suppress
from datetime import datetime

from pdd_data_mcp import __version__
from pdd_data_mcp.collectors import DatasetCollector
from pdd_data_mcp.config import AppConfig
from pdd_data_mcp.contracts.models import (
    SUPPORTED_SYNTHETIC_DATASETS,
    CapabilitiesResult,
    CollectResult,
    ConnectionStatusResult,
    CoverageStatus,
    DatasetCapabilityDetail,
    DatasetCapabilityStatus,
    DatasetType,
    LatestSnapshotResult,
    ListSnapshotsResult,
    ReadSnapshotResult,
    Scope,
    WindowKind,
)
from pdd_data_mcp.errors import AuthorizationError, CollectionRejected, ValidationFailure
from pdd_data_mcp.storage import SnapshotRepository
from pdd_data_mcp.utils import scope_key
from pdd_data_mcp.validation import SnapshotValidator


class PddDataService:
    def __init__(
        self,
        *,
        config: AppConfig,
        repository: SnapshotRepository,
        synthetic_collector: DatasetCollector,
        validator: SnapshotValidator,
        real_collectors: Mapping[str, DatasetCollector] | None = None,
    ) -> None:
        self.config = config
        self.repository = repository
        self.synthetic_collector = synthetic_collector
        self.validator = validator
        self.real_collectors = dict(real_collectors or {})
        self._global_collection_lock = asyncio.Lock()
        self._connection_locks = {
            connection.connection_id: asyncio.Lock() for connection in config.connections
        }

    def capabilities(self) -> CapabilitiesResult:
        synthetic_enabled = self.config.service.test_mode and any(
            item.synthetic_enabled for item in self.config.connections
        )
        real_connections = [
            item for item in self.config.connections if item.real_collection_enabled
        ]
        real_available = any(
            item.promotion_adapter.verified
            or item.promotion_account_adapter.verified
            or item.promotion_metrics_adapter.verified
            or item.store_overview_adapter.verified
            or item.product_catalog_adapter.verified
            or item.inventory_adapter.verified
            for item in real_connections
        )
        real_configured = bool(real_connections)
        datasets: dict[str, DatasetCapabilityStatus] = {}
        details: dict[str, DatasetCapabilityDetail] = {}
        promoted_product_windows = [
            window
            for window in WindowKind
            if any(
                item.promotion_metrics_adapter.verified
                and window.value in item.promotion_metrics_adapter.supported_windows
                for item in real_connections
            )
        ]
        promotion_account_windows = [
            window
            for window in (WindowKind.TODAY, WindowKind.YESTERDAY)
            if any(
                item.promotion_account_adapter.verified
                and window.value in item.promotion_account_adapter.supported_windows
                for item in real_connections
            )
        ]
        for dataset in DatasetType:
            dataset_available = any(
                (
                    dataset is DatasetType.PROMOTION_OVERVIEW
                    and (item.promotion_account_adapter.verified or item.promotion_adapter.verified)
                )
                or (
                    dataset in {DatasetType.PRODUCT_METRICS, DatasetType.PROMOTION_CONFIGURATION}
                    and item.promotion_metrics_adapter.verified
                )
                or (dataset is DatasetType.STORE_OVERVIEW and item.store_overview_adapter.verified)
                or (
                    dataset is DatasetType.PRODUCT_CATALOG and item.product_catalog_adapter.verified
                )
                or (dataset is DatasetType.INVENTORY and item.inventory_adapter.verified)
                for item in real_connections
            )
            if dataset is DatasetType.PROMOTION_OVERVIEW and dataset_available:
                datasets[dataset.value] = (
                    "REAL_PROMOTION_WINDOWS"
                    if promotion_account_windows
                    else "REAL_PROMOTION_TODAY"
                )
            elif dataset is DatasetType.PRODUCT_METRICS and dataset_available:
                datasets[dataset.value] = "REAL_PROMOTED_PRODUCT_METRICS"
            elif dataset is DatasetType.PROMOTION_CONFIGURATION and dataset_available:
                datasets[dataset.value] = "REAL_PROMOTION_CONFIGURATION_CURRENT"
            elif dataset is DatasetType.STORE_OVERVIEW and dataset_available:
                datasets[dataset.value] = "REAL_STORE_TODAY"
            elif dataset is DatasetType.PRODUCT_CATALOG and dataset_available:
                datasets[dataset.value] = "REAL_PRODUCT_CATALOG"
            elif dataset is DatasetType.INVENTORY and dataset_available:
                datasets[dataset.value] = "REAL_INVENTORY"
            elif (
                dataset
                in {
                    DatasetType.PROMOTION_OVERVIEW,
                    DatasetType.PRODUCT_METRICS,
                    DatasetType.PROMOTION_CONFIGURATION,
                    DatasetType.STORE_OVERVIEW,
                    DatasetType.PRODUCT_CATALOG,
                    DatasetType.INVENTORY,
                }
                and real_configured
            ):
                datasets[dataset.value] = "CONFIGURED_NOT_VERIFIED"
            elif synthetic_enabled and dataset in SUPPORTED_SYNTHETIC_DATASETS:
                datasets[dataset.value] = "SYNTHETIC_TEST_ONLY"
            else:
                datasets[dataset.value] = "UNAVAILABLE"
            granularity = {
                DatasetType.STORE_OVERVIEW: "STORE",
                DatasetType.PROMOTION_OVERVIEW: "ACCOUNT",
                DatasetType.CAMPAIGN_METRICS: "CAMPAIGN",
                DatasetType.PRODUCT_METRICS: "PROMOTED_PRODUCT",
                DatasetType.PRODUCT_BUSINESS_METRICS: "PRODUCT",
                DatasetType.PROMOTION_CONFIGURATION: "PROMOTED_PRODUCT",
                DatasetType.PRODUCT_CATALOG: "PRODUCT",
                DatasetType.INVENTORY: "PRODUCT",
                DatasetType.ACTIVITY_CATALOG: "UNKNOWN",
            }[dataset]
            supported_windows: list[WindowKind] = []
            current_only = False
            limitation: str | None = None
            if dataset is DatasetType.PRODUCT_METRICS:
                supported_windows = promoted_product_windows
            elif dataset is DatasetType.PROMOTION_OVERVIEW:
                supported_windows = (
                    promotion_account_windows
                    if promotion_account_windows
                    else [WindowKind.TODAY]
                    if dataset_available
                    else []
                )
                if promotion_account_windows:
                    limitation = (
                        "D4 account windows require scope.version=d4-account-v3; "
                        "legacy Stage C remains version=1 and TODAY-only."
                    )
            elif dataset is DatasetType.PROMOTION_CONFIGURATION:
                supported_windows = [WindowKind.POINT_IN_TIME] if dataset_available else []
                current_only = True
                limitation = "Current observation only; never represented as historical settings."
            elif dataset is DatasetType.STORE_OVERVIEW:
                supported_windows = [WindowKind.TODAY] if dataset_available else []
            elif dataset in {DatasetType.PRODUCT_CATALOG, DatasetType.INVENTORY}:
                supported_windows = [WindowKind.POINT_IN_TIME] if dataset_available else []
                current_only = True
            elif dataset is DatasetType.CAMPAIGN_METRICS:
                limitation = (
                    "A planId association exists, but no campaign-grain metric source is verified."
                )
            elif dataset is DatasetType.PRODUCT_BUSINESS_METRICS:
                limitation = (
                    "Contract/parser only; collection remains unavailable. The initial design is "
                    "PRODUCT-grain full YESTERDAY daily aggregates. Metric string formats, units, "
                    "pagination semantics, source classification, and independent identity are "
                    "not yet verified; 7-day and interval summaries are unavailable."
                )
            details[dataset.value] = DatasetCapabilityDetail(
                status=datasets[dataset.value],
                supported_window_kinds=supported_windows,
                entity_granularity=granularity,  # type: ignore[arg-type]
                current_only=current_only,
                verified=dataset_available,
                limitation=limitation,
            )
        return CapabilitiesResult(
            version=__version__,
            synthetic_test_mode=synthetic_enabled,
            real_collection=(
                "AVAILABLE"
                if real_available
                else "CONFIGURED_NOT_VERIFIED"
                if real_configured
                else "DISABLED"
            ),
            datasets=datasets,
            dataset_details=details,
        )

    def connection_status(self, connection_id: str) -> ConnectionStatusResult:
        try:
            connection = self.config.connection(connection_id)
        except KeyError as exc:
            raise AuthorizationError("UNKNOWN_CONNECTION") from exc
        return ConnectionStatusResult(
            connection_id=connection.connection_id,
            store_id=connection.store_id,
            real_collection_enabled=connection.real_collection_enabled,
            # This status endpoint never performs an implicit CDP connection.
            synthetic_test_enabled=(self.config.service.test_mode and connection.synthetic_enabled),
            message=(
                "Real collection is configured but CDP is not contacted by the status tool."
                if connection.real_collection_enabled
                else "Real collection is disabled; CDP was not contacted."
            ),
        )

    async def collect_snapshot(
        self,
        *,
        connection_id: str,
        dataset_type: DatasetType,
        scope: Scope,
        limit: int,
        idempotency_key: str,
        batch_id: str | None = None,
    ) -> CollectResult:
        try:
            connection = self.config.connection(connection_id)
        except KeyError as exc:
            raise AuthorizationError("UNKNOWN_CONNECTION") from exc
        synthetic_allowed = self.config.service.test_mode and connection.synthetic_enabled
        collector: DatasetCollector
        if synthetic_allowed:
            if dataset_type not in SUPPORTED_SYNTHETIC_DATASETS:
                return CollectResult(
                    status="DATASET_UNVERIFIED",
                    committed=False,
                    dataset_type=dataset_type,
                    error_code="DATASET_UNVERIFIED",
                )
            collector = self.synthetic_collector
        elif connection.real_collection_enabled:
            if dataset_type is DatasetType.PRODUCT_BUSINESS_METRICS:
                return CollectResult(
                    status="DATASET_UNVERIFIED",
                    committed=False,
                    dataset_type=dataset_type,
                    error_code="PRODUCT_BUSINESS_COLLECTION_NOT_ADAPTED",
                )
            if dataset_type is DatasetType.PROMOTION_OVERVIEW:
                account_adapter = connection.promotion_account_adapter
                account_v3_allowed = (
                    scope.version == "d4-account-v3"
                    and account_adapter.verified
                    and scope.kind.value in account_adapter.supported_windows
                )
                stage_c_allowed = (
                    scope.version == "1"
                    and connection.promotion_adapter.verified
                    and scope.kind is WindowKind.TODAY
                )
                if not account_v3_allowed and not stage_c_allowed:
                    return CollectResult(
                        status="DATASET_UNVERIFIED",
                        committed=False,
                        dataset_type=dataset_type,
                        error_code="REAL_DATASET_OR_SCOPE_NOT_ADAPTED",
                    )
            elif dataset_type is DatasetType.PRODUCT_METRICS:
                promotion_metrics = connection.promotion_metrics_adapter
                if (
                    not promotion_metrics.verified
                    or scope.kind.value not in promotion_metrics.supported_windows
                ):
                    return CollectResult(
                        status="DATASET_UNVERIFIED",
                        committed=False,
                        dataset_type=dataset_type,
                        error_code="REAL_DATASET_OR_SCOPE_NOT_ADAPTED",
                    )
            elif (
                dataset_type is DatasetType.PROMOTION_CONFIGURATION
                and scope.kind is not WindowKind.POINT_IN_TIME
            ):
                return CollectResult(
                    status="DATASET_UNVERIFIED",
                    committed=False,
                    dataset_type=dataset_type,
                    error_code="REAL_DATASET_OR_SCOPE_NOT_ADAPTED",
                )
            real_collector = self.real_collectors.get(connection_id)
            if real_collector is None:
                return CollectResult(
                    status="REAL_COLLECTION_DISABLED",
                    committed=False,
                    dataset_type=dataset_type,
                    error_code="REAL_COLLECTOR_NOT_CONFIGURED",
                )
            collector = real_collector
        else:
            return CollectResult(
                status="REAL_COLLECTION_DISABLED",
                committed=False,
                dataset_type=dataset_type,
                error_code="REAL_COLLECTION_DISABLED",
            )
        connection_lock = self._connection_locks[connection_id]
        if self._global_collection_lock.locked() or connection_lock.locked():
            return CollectResult(
                status="BUSY",
                committed=False,
                dataset_type=dataset_type,
                error_code="LOCK_BUSY",
            )
        async with self._global_collection_lock, connection_lock:
            canonical_parameters: dict[str, object] = {
                "store_id": connection.store_id,
                "dataset_type": dataset_type.value,
                "scope": scope.model_dump(mode="json"),
                "limit": limit,
            }
            if batch_id is not None:
                canonical_parameters["batch_id"] = batch_id
            key = scope_key(scope)
            reservation = self.repository.begin_request(
                store_id=connection.store_id,
                dataset_type=dataset_type,
                idempotency_key=idempotency_key,
                parameters=canonical_parameters,
                scope_key=key,
            )
            if reservation.state == "COMMITTED":
                effective_status = self.repository.get_snapshot_effective_status(
                    reservation.snapshot_id
                )
                if effective_status != "ACTIVE":
                    return CollectResult(
                        status="DATA_MISMATCH",
                        committed=False,
                        snapshot_id=None,
                        dataset_type=dataset_type,
                        idempotent_replay=True,
                        error_code=(
                            "IDEMPOTENT_SNAPSHOT_SEMANTICALLY_INVALIDATED"
                            if effective_status == "SEMANTICALLY_INVALIDATED"
                            else "IDEMPOTENT_SNAPSHOT_INVALIDATION_STATE_UNKNOWN"
                        ),
                    )
                manifest = self.repository.get_manifest(reservation.snapshot_id)
                return self._collect_result(manifest, idempotent_replay=True)
            if reservation.state == "RUNNING":
                return CollectResult(
                    status="BUSY",
                    committed=False,
                    snapshot_id=None,
                    dataset_type=dataset_type,
                    error_code="IDEMPOTENCY_REQUEST_RUNNING",
                )
            if reservation.state in {"FAILED", "INTERRUPTED"}:
                return CollectResult(
                    status="FAILED" if reservation.state == "FAILED" else "INTERRUPTED",
                    committed=False,
                    dataset_type=dataset_type,
                    error_code=(reservation.existing or {}).get("error_code", reservation.state),
                )
            try:
                draft = await collector.collect(
                    store_id=connection.store_id,
                    dataset_type=dataset_type,
                    scope=scope,
                    limit=limit,
                    batch_id=batch_id,
                )
                validation = self.validator.validate(draft, synthetic_allowed=synthetic_allowed)
                if not validation.valid:
                    self.repository.fail_request(reservation, "VALIDATION_FAILED")
                    raise ValidationFailure("; ".join(validation.errors))
                manifest, warnings = self.repository.commit_reserved(reservation, draft, validation)
                return self._collect_result(manifest, warnings=warnings)
            except CollectionRejected as exc:
                with suppress(Exception):
                    self.repository.fail_request(reservation, exc.error_code)
                return CollectResult(
                    status=exc.status,  # type: ignore[arg-type]
                    committed=False,
                    dataset_type=dataset_type,
                    warnings=exc.warnings,
                    error_code=exc.error_code,
                )
            except Exception:
                with suppress(Exception):
                    self.repository.fail_request(reservation, "COLLECTION_OR_STORAGE_FAILED")
                raise

    def _collect_result(
        self,
        manifest: object,
        *,
        warnings: list[str] | None = None,
        idempotent_replay: bool = False,
    ) -> CollectResult:
        from pdd_data_mcp.contracts.models import SnapshotManifest

        if not isinstance(manifest, SnapshotManifest):
            raise TypeError("invalid manifest")
        partial = manifest.coverage.coverage is not CoverageStatus.COMPLETE
        combined_warnings = list(warnings or [])
        if manifest.coverage.truncated and "CAPTURE_LIMIT_REACHED" not in combined_warnings:
            combined_warnings.append("CAPTURE_LIMIT_REACHED")
        return CollectResult(
            status="PARTIAL" if partial else "SUCCEEDED",
            committed=True,
            snapshot_id=manifest.snapshot_id,
            batch_id=manifest.batch_id,
            dataset_type=manifest.dataset_type,
            captured=manifest.coverage.captured,
            total_observed=manifest.coverage.total_observed,
            coverage=manifest.coverage.coverage,
            truncated=manifest.coverage.truncated,
            idempotent_replay=idempotent_replay,
            warnings=combined_warnings,
        )

    def list_snapshots(
        self,
        *,
        store_id: str,
        dataset_type: DatasetType,
        captured_from: datetime | None,
        captured_to: datetime | None,
        cursor: str | None,
        limit: int,
    ) -> ListSnapshotsResult:
        return self.repository.list_snapshots(
            store_id=store_id,
            dataset_type=dataset_type,
            captured_from=captured_from,
            captured_to=captured_to,
            cursor=cursor,
            limit=limit,
        )

    def read_snapshot(
        self, *, snapshot_id: str, cursor: str | None, page_size: int
    ) -> ReadSnapshotResult:
        return self.repository.read_snapshot(
            snapshot_id=snapshot_id, cursor=cursor, page_size=page_size
        )

    def latest_snapshot(
        self,
        *,
        store_id: str,
        dataset_type: DatasetType,
        scope: Scope,
        require_complete: bool,
    ) -> LatestSnapshotResult:
        return self.repository.latest_snapshot(
            store_id=store_id,
            dataset_type=dataset_type,
            scope=scope,
            require_complete=require_complete,
        )
