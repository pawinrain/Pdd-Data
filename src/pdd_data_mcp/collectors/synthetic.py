from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from math import ceil
from zoneinfo import ZoneInfo

from pdd_data_mcp.contracts.models import (
    SUPPORTED_SYNTHETIC_DATASETS,
    Coverage,
    CoverageStatus,
    DatasetType,
    InventoryRecord,
    MetricValue,
    MetricWindow,
    ProductCatalogRecord,
    PromotionOverviewPayload,
    Quality,
    Scope,
    SnapshotDraft,
    StoreOverviewPayload,
    WindowKind,
)
from pdd_data_mcp.errors import ValidationFailure
from pdd_data_mcp.utils import scope_key, utc_now


class SyntheticCollector:
    """Offline-only deterministic collector for stage A/B tests."""

    async def collect(
        self,
        *,
        store_id: str,
        dataset_type: DatasetType,
        scope: Scope,
        limit: int,
        batch_id: str | None = None,
    ) -> SnapshotDraft:
        if dataset_type not in SUPPORTED_SYNTHETIC_DATASETS:
            raise ValidationFailure("DATASET_UNVERIFIED")
        requested_at = utc_now()
        started_at = utc_now()
        observed_at = utc_now()
        payload, total, missing = self._payload(dataset_type, observed_at)
        if isinstance(payload, list):
            selected = payload[:limit]
            captured = len(selected)
            truncated = total > captured
            payload_value: dict[str, object] | list[dict[str, object]] = selected
        else:
            captured = 1
            truncated = False
            payload_value = payload
        coverage_status = CoverageStatus.TRUNCATED if truncated else CoverageStatus.COMPLETE
        finished_at = utc_now()
        return SnapshotDraft(
            batch_id=batch_id,
            store_id=store_id,
            dataset_type=dataset_type,
            scope=scope,
            scope_key=scope_key(scope),
            requested_at=requested_at,
            capture_started_at=started_at,
            capture_finished_at=finished_at,
            captured_at=observed_at,
            metric_window=self._metric_window(scope, observed_at, dataset_type),
            source_updated_at=None,
            source="SYNTHETIC",
            capture_method="SYNTHETIC",
            parser_version=f"synthetic-{dataset_type.value}/1.0.0",
            payload=payload_value,
            missing_fields=missing,
            quality=Quality(
                status="VALID",
                identity="NOT_CHECKED",
                coverage=coverage_status,
                dom_check="NOT_RUN",
            ),
            coverage=Coverage(
                total_observed=total,
                captured=captured,
                limit=limit,
                pages_read=(
                    ceil(total / 10)
                    if dataset_type is DatasetType.PRODUCT_CATALOG and captured
                    else 1
                    if captured
                    else 0
                ),
                coverage=coverage_status,
                truncated=truncated,
                stop_reason="CAPTURE_LIMIT_REACHED" if truncated else None,
            ),
        )

    def _payload(
        self, dataset_type: DatasetType, observed_at: datetime
    ) -> tuple[dict[str, object] | list[dict[str, object]], int, list[str]]:
        if dataset_type is DatasetType.STORE_OVERVIEW:
            store_payload = StoreOverviewPayload(
                metrics={
                    "visitor_count": MetricValue(
                        value=42,
                        unit="COUNT",
                        observed_at=observed_at,
                        capture_method="SYNTHETIC",
                        precision="EXACT",
                    ),
                    "order_count": None,
                }
            )
            return store_payload.model_dump(mode="json"), 1, ["order_count"]
        if dataset_type is DatasetType.PROMOTION_OVERVIEW:
            promotion_payload = PromotionOverviewPayload(
                metrics={
                    "ad_spend": MetricValue(
                        value=12345,
                        unit="CNY_CENT",
                        observed_at=observed_at,
                        capture_method="SYNTHETIC",
                        precision="EXACT",
                    ),
                    "ad_gmv": None,
                    "roi": None,
                }
            )
            return promotion_payload.model_dump(mode="json"), 1, ["ad_gmv", "roi"]
        if dataset_type is DatasetType.PRODUCT_CATALOG:
            records = [
                ProductCatalogRecord(
                    product_id=f"p_demo_{index:03d}",
                    platform_product_id=f"p_demo_{index:03d}",
                    sku_id=f"sku_demo_{index:03d}" if index % 5 else None,
                    name=f"Synthetic product {index}",
                    price_cents=1000 + index,
                    status="ON_SALE",
                    observed_at=observed_at,
                ).model_dump(mode="json")
                for index in range(1, 56)
            ]
            return records, 55, []
        records = [
            InventoryRecord(
                product_id=f"p_demo_{index:03d}",
                platform_product_id=f"p_demo_{index:03d}",
                sku_id=f"sku_demo_{index:03d}" if index % 4 else None,
                inventory=None if index == 7 else 100 - index,
                granularity="SKU" if index % 4 else "PRODUCT",
                observed_at=observed_at,
            ).model_dump(mode="json")
            for index in range(1, 56)
        ]
        return records, 55, ["records[6].inventory"]

    def _metric_window(
        self, scope: Scope, observed_at: datetime, dataset_type: DatasetType
    ) -> MetricWindow | None:
        if dataset_type in {DatasetType.PRODUCT_CATALOG, DatasetType.INVENTORY}:
            return None
        zone = ZoneInfo(scope.timezone)
        if scope.kind is WindowKind.POINT_IN_TIME:
            return None
        if scope.kind is WindowKind.CUSTOM:
            assert scope.start is not None and scope.end is not None
            return MetricWindow(
                kind=scope.kind,
                timezone=scope.timezone,
                start=scope.start.astimezone(UTC),
                end=scope.end.astimezone(UTC),
                window_complete=True,
                source_finalized=None,
            )
        local_midnight = datetime.combine(scope.business_date, time.min, tzinfo=zone)
        if scope.kind is WindowKind.YESTERDAY:
            start = local_midnight
            end = local_midnight + timedelta(days=1)
            complete = True
        elif scope.kind in {
            WindowKind.LAST_7_DAYS,
            WindowKind.LAST_30_DAYS,
            WindowKind.LAST_90_DAYS,
        }:
            days = {
                WindowKind.LAST_7_DAYS: 7,
                WindowKind.LAST_30_DAYS: 30,
                WindowKind.LAST_90_DAYS: 90,
            }[scope.kind]
            end = local_midnight + timedelta(days=1)
            start = end - timedelta(days=days)
            complete = True
        elif scope.kind is WindowKind.UNKNOWN:
            return None
        else:
            start = local_midnight
            end = observed_at.astimezone(zone)
            if end <= start:
                raise ValidationFailure("TODAY scope business_date is after capture time")
            complete = False
        return MetricWindow(
            kind=scope.kind,
            timezone=scope.timezone,
            start=start.astimezone(UTC),
            end=end.astimezone(UTC),
            window_complete=complete,
            source_finalized=None,
        )
