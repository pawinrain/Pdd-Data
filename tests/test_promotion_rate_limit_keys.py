from __future__ import annotations

from pathlib import Path

import pytest

from pdd_data_mcp.browser.promotion import PromotionOverviewCdpCollector
from pdd_data_mcp.browser.promotion_account import PromotionAccountCdpCollector
from pdd_data_mcp.browser.promotion_metrics import PromotionMetricsCdpCollector
from pdd_data_mcp.config import CollectionSettings, ConnectionSettings
from pdd_data_mcp.contracts.models import DatasetType
from pdd_data_mcp.errors import CollectionRejected


def _connection() -> ConnectionSettings:
    return ConnectionSettings(connection_id="conn_1", store_id="st_1")


def _settings() -> CollectionSettings:
    return CollectionSettings(min_interval_seconds=60)


def test_promotion_metrics_and_configuration_can_run_back_to_back(
    tmp_path: Path,
) -> None:
    """One user sync collects product_metrics then promotion_configuration via the same
    collector; the throttle key must distinguish the two datasets."""
    collector = PromotionMetricsCdpCollector(
        connection=_connection(),
        collection=_settings(),
        runtime_root=tmp_path,
    )

    collector._enforce_min_interval(DatasetType.PRODUCT_METRICS, "1")
    collector._enforce_min_interval(DatasetType.PROMOTION_CONFIGURATION, "1")

    with pytest.raises(CollectionRejected) as raised:
        collector._enforce_min_interval(DatasetType.PRODUCT_METRICS, "1")
    assert raised.value.status == "BUSY"
    assert raised.value.error_code == "MIN_COLLECTION_INTERVAL_NOT_ELAPSED"


def test_stage_c_and_v3_overview_have_distinct_throttle_keys(tmp_path: Path) -> None:
    stage_c = PromotionOverviewCdpCollector(
        connection=_connection(),
        collection=_settings(),
        runtime_root=tmp_path,
    )
    v3 = PromotionAccountCdpCollector(
        connection=_connection(),
        collection=_settings(),
        runtime_root=tmp_path,
    )

    stage_c._enforce_min_interval(DatasetType.PROMOTION_OVERVIEW, "1")
    # Different route/version for the same dataset type must not be throttled together.
    v3._enforce_min_interval(DatasetType.PROMOTION_OVERVIEW, "d4-account-v3")

    with pytest.raises(CollectionRejected) as raised:
        stage_c._enforce_min_interval(DatasetType.PROMOTION_OVERVIEW, "1")
    assert raised.value.error_code == "MIN_COLLECTION_INTERVAL_NOT_ELAPSED"


def test_v3_same_scope_repeat_is_still_throttled(tmp_path: Path) -> None:
    collector = PromotionAccountCdpCollector(
        connection=_connection(),
        collection=_settings(),
        runtime_root=tmp_path,
    )

    collector._enforce_min_interval(DatasetType.PROMOTION_OVERVIEW, "d4-account-v3")
    with pytest.raises(CollectionRejected) as raised:
        collector._enforce_min_interval(DatasetType.PROMOTION_OVERVIEW, "d4-account-v3")
    assert raised.value.status == "BUSY"
    assert raised.value.error_code == "MIN_COLLECTION_INTERVAL_NOT_ELAPSED"
