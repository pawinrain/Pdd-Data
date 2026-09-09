from __future__ import annotations

from pdd_data_mcp.browser.core import CoreDataCdpCollector
from pdd_data_mcp.browser.promotion import PromotionOverviewCdpCollector
from pdd_data_mcp.browser.promotion_account import PromotionAccountCdpCollector
from pdd_data_mcp.browser.promotion_metrics import PromotionMetricsCdpCollector
from pdd_data_mcp.contracts.models import DatasetType, Scope, SnapshotDraft
from pdd_data_mcp.errors import CollectionRejected


class RealDatasetCollector:
    def __init__(
        self,
        *,
        promotion: PromotionOverviewCdpCollector,
        promotion_account: PromotionAccountCdpCollector,
        promotion_metrics: PromotionMetricsCdpCollector,
        core: CoreDataCdpCollector,
    ) -> None:
        self.promotion = promotion
        self.promotion_account = promotion_account
        self.promotion_metrics = promotion_metrics
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
            if scope.version == "d4-account-v3":
                return await self.promotion_account.collect(
                    store_id=store_id,
                    dataset_type=dataset_type,
                    scope=scope,
                    limit=limit,
                    batch_id=batch_id,
                )
            if scope.version != "1":
                raise CollectionRejected(
                    "TIME_SCOPE_UNVERIFIED", "PROMOTION_ACCOUNT_SCOPE_VERSION_NOT_ADAPTED"
                )
            return await self.promotion.collect(
                store_id=store_id,
                dataset_type=dataset_type,
                scope=scope,
                limit=limit,
                batch_id=batch_id,
            )
        if dataset_type in {
            DatasetType.PRODUCT_METRICS,
            DatasetType.PROMOTION_CONFIGURATION,
        }:
            return await self.promotion_metrics.collect(
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
