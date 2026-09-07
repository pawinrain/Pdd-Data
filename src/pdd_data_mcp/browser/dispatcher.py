from __future__ import annotations

from pdd_data_mcp.browser.core import CoreDataCdpCollector
from pdd_data_mcp.browser.promotion import PromotionOverviewCdpCollector
from pdd_data_mcp.contracts.models import DatasetType, Scope, SnapshotDraft
from pdd_data_mcp.errors import CollectionRejected


class RealDatasetCollector:
    def __init__(
        self,
        *,
        promotion: PromotionOverviewCdpCollector,
        core: CoreDataCdpCollector,
    ) -> None:
        self.promotion = promotion
        self.core = core

    async def collect(
        self,
        *,
        store_id: str,
        dataset_type: DatasetType,
        scope: Scope,
        limit: int,
        batch_id: str | None = None,
    ) -> SnapshotDraft:
        if dataset_type is DatasetType.PROMOTION_OVERVIEW:
            return await self.promotion.collect(
                store_id=store_id,
                dataset_type=dataset_type,
                scope=scope,
                limit=limit,
                batch_id=batch_id,
            )
        if dataset_type in {
            DatasetType.STORE_OVERVIEW,
            DatasetType.PRODUCT_CATALOG,
            DatasetType.INVENTORY,
        }:
            return await self.core.collect(
                store_id=store_id,
                dataset_type=dataset_type,
                scope=scope,
                limit=limit,
                batch_id=batch_id,
            )
        raise CollectionRejected("DATASET_UNVERIFIED", "REAL_DATASET_NOT_ADAPTED")
