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
    DatasetType,
    LatestSnapshotResult,
    ListSnapshotsResult,
    ReadSnapshotResult,
    Scope,
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
            or item.store_overview_adapter.verified
            or item.product_catalog_adapter.verified
            or item.inventory_adapter.verified
            for item in real_connections
        )
        real_configured = bool(real_connections)
        datasets: dict[str, str] = {}
        for dataset in DatasetType:
            dataset_available = any(
                (dataset is DatasetType.PROMOTION_OVERVIEW and item.promotion_adapter.verified)
                or (dataset is DatasetType.STORE_OVERVIEW and item.store_overview_adapter.verified)
                or (
                    dataset is DatasetType.PRODUCT_CATALOG and item.product_catalog_adapter.verified
                )
                or (dataset is DatasetType.INVENTORY and item.inventory_adapter.verified)
                for item in real_connections
            )
            if dataset is DatasetType.PROMOTION_OVERVIEW and dataset_available:
                datasets[dataset.value] = "REAL_PROMOTION_TODAY"
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
            datasets=datasets,  # type: ignore[arg-type]
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
            if dataset_type is DatasetType.PROMOTION_OVERVIEW and scope.kind.value != "TODAY":
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
