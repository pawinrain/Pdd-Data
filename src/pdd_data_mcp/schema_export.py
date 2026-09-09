from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel

from pdd_data_mcp.contracts.models import (
    CapabilitiesResult,
    CollectResult,
    CommitRecord,
    InventoryRecord,
    LatestSnapshotResult,
    ListSnapshotsResult,
    ProductBusinessMetricCandidateRecord,
    ProductCatalogRecord,
    PromotedProductMetricRecord,
    PromotionAccountMetricPayload,
    PromotionAccountTimeEvidence,
    PromotionConfigurationRecord,
    PromotionOverviewPayload,
    ReadSnapshotResult,
    Scope,
    SnapshotInvalidationRecord,
    SnapshotManifest,
    StoreOverviewPayload,
    ValidationReport,
)

SCHEMAS: dict[str, type[BaseModel]] = {
    "scope.schema.json": Scope,
    "snapshot-manifest.schema.json": SnapshotManifest,
    "snapshot-invalidation.schema.json": SnapshotInvalidationRecord,
    "commit.schema.json": CommitRecord,
    "validation.schema.json": ValidationReport,
    "store-overview.schema.json": StoreOverviewPayload,
    "promotion-overview.schema.json": PromotionOverviewPayload,
    "product-catalog-record.schema.json": ProductCatalogRecord,
    "inventory-record.schema.json": InventoryRecord,
    "product-business-metric-candidate.schema.json": ProductBusinessMetricCandidateRecord,
    "promoted-product-metric-record.schema.json": PromotedProductMetricRecord,
    "promotion-account-metric.schema.json": PromotionAccountMetricPayload,
    "promotion-account-time-evidence.schema.json": PromotionAccountTimeEvidence,
    "promotion-configuration-record.schema.json": PromotionConfigurationRecord,
    "capabilities-result.schema.json": CapabilitiesResult,
    "collect-result.schema.json": CollectResult,
    "list-snapshots-result.schema.json": ListSnapshotsResult,
    "read-snapshot-result.schema.json": ReadSnapshotResult,
    "latest-snapshot-result.schema.json": LatestSnapshotResult,
}


def export_schemas(output_dir: Path) -> list[str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for name, model in SCHEMAS.items():
        content = json.dumps(
            model.model_json_schema(), ensure_ascii=False, indent=2, sort_keys=True
        )
        (output_dir / name).write_text(content + "\n", encoding="utf-8")
        written.append(name)
    return written
