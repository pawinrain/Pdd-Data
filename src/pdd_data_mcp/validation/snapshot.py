from __future__ import annotations

import math
from typing import Any

from pydantic import ValidationError

from pdd_data_mcp.contracts.models import (
    DatasetType,
    InventoryRecord,
    ProductCatalogRecord,
    PromotionOverviewPayload,
    SnapshotDraft,
    StoreOverviewPayload,
    ValidationReport,
)
from pdd_data_mcp.errors import ValidationFailure
from pdd_data_mcp.security import assert_no_sensitive_fields
from pdd_data_mcp.utils import canonical_json, utc_now


class SnapshotValidator:
    def validate(self, draft: SnapshotDraft, *, synthetic_allowed: bool) -> ValidationReport:
        errors: list[str] = []
        warnings: list[str] = []
        if draft.source == "SYNTHETIC" and not synthetic_allowed:
            errors.append("SYNTHETIC_NOT_ALLOWED")
        if draft.source == "PDD_BROWSER_CDP":
            adapted = {
                DatasetType.PROMOTION_OVERVIEW,
                DatasetType.STORE_OVERVIEW,
                DatasetType.PRODUCT_CATALOG,
                DatasetType.INVENTORY,
            }
            if draft.dataset_type not in adapted:
                errors.append("REAL_DATASET_NOT_ADAPTED")
            expected_scope = (
                "TODAY"
                if draft.dataset_type
                in {DatasetType.PROMOTION_OVERVIEW, DatasetType.STORE_OVERVIEW}
                else "POINT_IN_TIME"
            )
            if draft.scope.kind.value != expected_scope:
                errors.append("REAL_SCOPE_NOT_ADAPTED")
            if draft.identity_evidence is None:
                errors.append("REAL_IDENTITY_EVIDENCE_REQUIRED")
            elif (
                draft.identity_evidence.expected_platform_store_id
                != draft.identity_evidence.observed_platform_store_id
            ):
                errors.append("REAL_IDENTITY_EVIDENCE_MISMATCH")
            if (
                draft.dataset_type is DatasetType.PROMOTION_OVERVIEW
                and draft.field_sources.get("metrics.ad_spend") != draft.capture_method
            ):
                errors.append("REAL_AD_SPEND_FIELD_SOURCE_MISMATCH")
            if draft.dataset_type is DatasetType.STORE_OVERVIEW:
                if not isinstance(draft.payload, dict):
                    errors.append("REAL_STORE_PAYLOAD_REQUIRED")
                else:
                    metrics = draft.payload.get("metrics")
                    if (
                        not isinstance(metrics, dict)
                        or sum(value is not None for value in metrics.values()) < 2
                    ):
                        errors.append("REAL_STORE_REQUIRES_TWO_METRICS")
                if draft.metric_window is None:
                    errors.append("REAL_STORE_METRIC_WINDOW_REQUIRED")
            if draft.dataset_type in {DatasetType.PRODUCT_CATALOG, DatasetType.INVENTORY}:
                if draft.metric_window is not None:
                    errors.append("POINT_IN_TIME_DATASET_MUST_NOT_HAVE_METRIC_WINDOW")
                if not isinstance(draft.payload, list):
                    errors.append("REAL_RECORDS_PAYLOAD_REQUIRED")
            if draft.capture_method not in {"NETWORK_RESPONSE", "DOM", "MIXED"}:
                errors.append("REAL_CAPTURE_METHOD_UNSUPPORTED")
            if draft.quality.identity != "MATCHED" or draft.quality.dom_check != "MATCHED":
                errors.append("REAL_IDENTITY_AND_DOM_CHECK_REQUIRED")
        try:
            assert_no_sensitive_fields(draft.payload)
            self._assert_finite(draft.payload)
            self._validate_payload(draft)
        except (ValidationFailure, ValidationError) as exc:
            errors.append(str(exc))
        if draft.coverage.truncated:
            warnings.append("CAPTURE_LIMIT_REACHED")
        return ValidationReport(
            valid=not errors,
            errors=errors,
            warnings=warnings,
            missing_fields=draft.missing_fields,
            checked_at=utc_now(),
        )

    def _validate_payload(self, draft: SnapshotDraft) -> None:
        payload = draft.payload
        if draft.dataset_type is DatasetType.STORE_OVERVIEW:
            if not isinstance(payload, dict):
                raise ValidationFailure("store_overview requires object payload")
            StoreOverviewPayload.model_validate_json(canonical_json(payload))
        elif draft.dataset_type is DatasetType.PROMOTION_OVERVIEW:
            if not isinstance(payload, dict):
                raise ValidationFailure("promotion_overview requires object payload")
            PromotionOverviewPayload.model_validate_json(canonical_json(payload))
        elif draft.dataset_type is DatasetType.PRODUCT_CATALOG:
            if not isinstance(payload, list):
                raise ValidationFailure("product_catalog requires records")
            product_records = [
                ProductCatalogRecord.model_validate_json(canonical_json(item)) for item in payload
            ]
            product_keys = {(item.product_id, item.sku_id) for item in product_records}
            if len(product_keys) != len(product_records):
                raise ValidationFailure("duplicate product record")
            if draft.source == "PDD_BROWSER_CDP" and any(
                item.platform_product_id is None or item.platform_product_id != item.product_id
                for item in product_records
            ):
                raise ValidationFailure("real product requires matching platform_product_id")
        elif draft.dataset_type is DatasetType.INVENTORY:
            if not isinstance(payload, list):
                raise ValidationFailure("inventory requires records")
            inventory_records = [
                InventoryRecord.model_validate_json(canonical_json(item)) for item in payload
            ]
            inventory_keys = {
                (item.product_id, item.sku_id, item.granularity) for item in inventory_records
            }
            if len(inventory_keys) != len(inventory_records):
                raise ValidationFailure("duplicate inventory record")
            if draft.source == "PDD_BROWSER_CDP" and any(
                item.platform_product_id is None or item.platform_product_id != item.product_id
                for item in inventory_records
            ):
                raise ValidationFailure("real inventory requires matching platform_product_id")
        else:
            raise ValidationFailure("DATASET_UNVERIFIED")
        actual_count = len(payload) if isinstance(payload, list) else 1
        if actual_count != draft.coverage.captured:
            raise ValidationFailure("coverage captured count does not match payload")

    def _assert_finite(self, value: Any) -> None:
        if isinstance(value, float) and not math.isfinite(value):
            raise ValidationFailure("NaN and Infinity are forbidden")
        if isinstance(value, dict):
            for item in value.values():
                self._assert_finite(item)
        elif isinstance(value, list):
            for item in value:
                self._assert_finite(item)
