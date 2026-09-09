from __future__ import annotations

import math
from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from pdd_data_mcp.contracts.models import (
    DatasetType,
    InventoryRecord,
    ProductCatalogRecord,
    PromotedProductMetricRecord,
    PromotionAccountMetricPayload,
    PromotionConfigurationRecord,
    PromotionOverviewPayload,
    SnapshotDraft,
    StoreOverviewPayload,
    ValidationReport,
    WindowKind,
)
from pdd_data_mcp.errors import ValidationFailure
from pdd_data_mcp.security import assert_no_sensitive_fields
from pdd_data_mcp.utils import canonical_json, scope_key, utc_now


class SnapshotValidator:
    def validate(self, draft: SnapshotDraft, *, synthetic_allowed: bool) -> ValidationReport:
        errors: list[str] = []
        warnings: list[str] = []
        if draft.scope_key != scope_key(draft.scope):
            errors.append("SCOPE_KEY_MISMATCH")
        if draft.source == "SYNTHETIC" and not synthetic_allowed:
            errors.append("SYNTHETIC_NOT_ALLOWED")
        if draft.source == "PDD_BROWSER_CDP":
            adapted = {
                DatasetType.PROMOTION_OVERVIEW,
                DatasetType.PRODUCT_METRICS,
                DatasetType.PROMOTION_CONFIGURATION,
                DatasetType.STORE_OVERVIEW,
                DatasetType.PRODUCT_CATALOG,
                DatasetType.INVENTORY,
            }
            if draft.dataset_type not in adapted:
                errors.append("REAL_DATASET_NOT_ADAPTED")
            valid_scopes = {
                DatasetType.STORE_OVERVIEW: {"TODAY"},
                DatasetType.PRODUCT_CATALOG: {"POINT_IN_TIME"},
                DatasetType.INVENTORY: {"POINT_IN_TIME"},
                DatasetType.PROMOTION_CONFIGURATION: {"POINT_IN_TIME"},
                DatasetType.PROMOTION_OVERVIEW: {
                    "TODAY",
                    "YESTERDAY",
                },
                DatasetType.PRODUCT_METRICS: {"TODAY"},
            }
            if draft.scope.kind.value not in valid_scopes.get(draft.dataset_type, set()):
                errors.append("REAL_SCOPE_NOT_ADAPTED")
            if draft.identity_evidence is None:
                errors.append("REAL_IDENTITY_EVIDENCE_REQUIRED")
            elif (
                draft.identity_evidence.expected_platform_store_id
                != draft.identity_evidence.observed_platform_store_id
            ):
                errors.append("REAL_IDENTITY_EVIDENCE_MISMATCH")
            if draft.dataset_type is DatasetType.PROMOTION_OVERVIEW:
                if draft.metric_window is None:
                    errors.append("REAL_PROMOTION_METRIC_WINDOW_REQUIRED")
                if not draft.field_sources or (
                    draft.capture_method != "MIXED"
                    and any(
                        source != draft.capture_method for source in draft.field_sources.values()
                    )
                ):
                    errors.append("REAL_PROMOTION_FIELD_SOURCE_MISMATCH")
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
            if draft.dataset_type in {
                DatasetType.PRODUCT_CATALOG,
                DatasetType.INVENTORY,
                DatasetType.PROMOTION_CONFIGURATION,
            }:
                if draft.metric_window is not None:
                    errors.append("POINT_IN_TIME_DATASET_MUST_NOT_HAVE_METRIC_WINDOW")
                if not isinstance(draft.payload, list):
                    errors.append("REAL_RECORDS_PAYLOAD_REQUIRED")
            if draft.dataset_type is DatasetType.PRODUCT_METRICS:
                if draft.metric_window is None:
                    errors.append("REAL_PRODUCT_METRIC_WINDOW_REQUIRED")
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
        if draft.promotion_account_time_evidence is not None and not (
            draft.dataset_type is DatasetType.PROMOTION_OVERVIEW
            and draft.scope.version in {"d4-account-v2", "d4-account-v3"}
        ):
            raise ValidationFailure("account time evidence requires d4 account promotion_overview")
        if draft.dataset_type is DatasetType.STORE_OVERVIEW:
            if not isinstance(payload, dict):
                raise ValidationFailure("store_overview requires object payload")
            StoreOverviewPayload.model_validate_json(canonical_json(payload))
        elif draft.dataset_type is DatasetType.PROMOTION_OVERVIEW:
            if not isinstance(payload, dict):
                raise ValidationFailure("promotion_overview requires object payload")
            if draft.scope.version in {"d4-account-v2", "d4-account-v3"}:
                account = PromotionAccountMetricPayload.model_validate_json(canonical_json(payload))
                if draft.parser_version != draft.scope.version:
                    raise ValidationFailure("account metric parser version mismatch")
                if draft.scope.object_type != "ACCOUNT_ALL":
                    raise ValidationFailure("account metrics require ACCOUNT_ALL scope")
                if draft.scope.timezone != "Asia/Shanghai":
                    raise ValidationFailure("account metrics require Asia/Shanghai scope")
                if account.business_date != draft.scope.business_date:
                    raise ValidationFailure("account metric business_date mismatch")
                if account.observed_at != draft.captured_at:
                    raise ValidationFailure("account metric observed_at mismatch")
                if account.source_updated_at != draft.source_updated_at:
                    raise ValidationFailure("account metric source_updated_at mismatch")
                if draft.scope.kind is WindowKind.TODAY and account.source_updated_at is None:
                    raise ValidationFailure("TODAY account metric requires source_updated_at")
                if (
                    draft.scope.kind is WindowKind.YESTERDAY
                    and account.source_updated_at is None
                    and account.source_updated_at_missing_reason != "SOURCE_VALUE_NULL"
                ):
                    raise ValidationFailure("YESTERDAY null update requires SOURCE_VALUE_NULL")
                if draft.metric_window is None:
                    raise ValidationFailure("account metrics require metric_window")
                if (
                    draft.metric_window.kind is not draft.scope.kind
                    or draft.metric_window.timezone != draft.scope.timezone
                    or draft.metric_window.window_complete
                    or draft.metric_window.source_finalized is not False
                ):
                    raise ValidationFailure("account metric window semantics mismatch")
                expected_capture_method = (
                    "NETWORK_RESPONSE" if draft.scope.kind is WindowKind.TODAY else "MIXED"
                )
                expected_end_source = (
                    "NETWORK_RESPONSE" if draft.scope.kind is WindowKind.TODAY else "DOM"
                )
                if (
                    draft.capture_method != expected_capture_method
                    or draft.field_sources.get("metric_window.end") != expected_end_source
                    or any(
                        source != "NETWORK_RESPONSE"
                        for field, source in draft.field_sources.items()
                        if field != "metric_window.end"
                    )
                ):
                    raise ValidationFailure("account metric cutoff source mismatch")
                zone = ZoneInfo("Asia/Shanghai")
                window_start_local = draft.metric_window.start.astimezone(zone)
                window_end_local = draft.metric_window.end.astimezone(zone)
                if (
                    window_start_local.date() != draft.scope.business_date
                    or draft.metric_window.end > draft.captured_at
                    or any(
                        (
                            window_start_local.hour,
                            window_start_local.minute,
                            window_start_local.second,
                            window_start_local.microsecond,
                        )
                    )
                ):
                    raise ValidationFailure("account metric window date mismatch")
                if (
                    draft.scope.version == "d4-account-v2"
                    and window_end_local.date() != draft.scope.business_date
                ):
                    raise ValidationFailure("account metric window date mismatch")
                time_evidence = draft.promotion_account_time_evidence
                if draft.scope.version == "d4-account-v3" and time_evidence is None:
                    raise ValidationFailure("d4-account-v3 requires hourly time evidence")
                if time_evidence is not None:
                    captured_local = draft.captured_at.astimezone(zone)
                    expected_business_date = (
                        captured_local.date()
                        if draft.scope.kind is WindowKind.TODAY
                        else captured_local.date() - timedelta(days=1)
                    )
                    expected_page_cutoff = (
                        time_evidence.page_today_cutoff_hhmm
                        if draft.scope.kind is WindowKind.TODAY
                        else time_evidence.page_yesterday_cutoff_hhmm
                    )
                    today_cutoff_mismatch = False
                    if draft.scope.kind is WindowKind.TODAY:
                        assert account.source_updated_at is not None
                        today_cutoff_mismatch = (
                            time_evidence.page_today_cutoff_hhmm
                            != account.source_updated_at.astimezone(zone).strftime("%H:%M")
                        )
                    selected_cutoff_hour = int(expected_page_cutoff[:2])
                    request_hour_mismatch = (
                        draft.scope.kind is WindowKind.YESTERDAY
                        and time_evidence.request_end_day_hour != selected_cutoff_hour
                    ) or (
                        draft.scope.kind is WindowKind.TODAY
                        and time_evidence.request_end_day_hour < selected_cutoff_hour
                    )
                    if (
                        time_evidence.window_kind is not draft.scope.kind
                        or time_evidence.page_semantics != "SAME_PERIOD_COMPARISON"
                        or time_evidence.business_timezone != draft.scope.timezone
                        or time_evidence.request_source != "NETWORK_RESPONSE"
                        or time_evidence.response_source != "NETWORK_RESPONSE"
                        or time_evidence.page_source != "DOM"
                        or draft.scope.business_date != expected_business_date
                        or time_evidence.request_start_date != draft.scope.business_date
                        or time_evidence.request_end_date != draft.scope.business_date
                        or time_evidence.response_business_date != draft.scope.business_date
                        or today_cutoff_mismatch
                        or request_hour_mismatch
                        or time_evidence.request_end_day_hour > captured_local.hour
                    ):
                        raise ValidationFailure("account time evidence mismatch")
                    if draft.scope.version == "d4-account-v2":
                        if (
                            window_end_local.date() != draft.scope.business_date
                            or window_end_local.strftime("%H:%M") != expected_page_cutoff
                        ):
                            raise ValidationFailure("account time evidence mismatch")
                    else:
                        if (
                            time_evidence.response_hourly_row_count is None
                            or time_evidence.response_hourly_row_count
                            != time_evidence.request_end_day_hour + 1
                            or time_evidence.response_first_hour != 0
                            or time_evidence.response_last_hour
                            != time_evidence.request_end_day_hour
                        ):
                            raise ValidationFailure("account time evidence mismatch")
                        if draft.scope.kind is WindowKind.YESTERDAY:
                            expected_yesterday_cutoff = (
                                f"{time_evidence.request_end_day_hour:02d}:59"
                            )
                            expected_window_end = window_start_local + timedelta(
                                hours=time_evidence.request_end_day_hour + 1
                            )
                            if (
                                time_evidence.page_yesterday_cutoff_hhmm
                                != expected_yesterday_cutoff
                                or draft.metric_window.end != expected_window_end
                            ):
                                raise ValidationFailure(
                                    "YESTERDAY account half-open window mismatch"
                                )
                if draft.scope.kind is WindowKind.TODAY and (
                    draft.metric_window.end != account.source_updated_at
                    or window_end_local.date() != draft.scope.business_date
                ):
                    raise ValidationFailure("TODAY account cutoff must use source update time")
                if (
                    draft.scope.version == "d4-account-v2"
                    and draft.scope.kind is WindowKind.YESTERDAY
                    and (window_end_local.second != 0 or window_end_local.microsecond != 0)
                ):
                    raise ValidationFailure("YESTERDAY account DOM cutoff must be minute precision")
                expected_missing = {
                    f"metrics.{field}:{reason}"
                    for field, reason in account.metrics.missing_reasons.items()
                }
                if account.source_updated_at_missing_reason is not None:
                    expected_missing.add(
                        f"source_updated_at:{account.source_updated_at_missing_reason}"
                    )
                if set(draft.missing_fields) != expected_missing:
                    raise ValidationFailure("account metric missing_fields mismatch")
            elif draft.scope.version == "1":
                if draft.source == "PDD_BROWSER_CDP" and draft.scope.kind is not WindowKind.TODAY:
                    raise ValidationFailure("legacy promotion overview is TODAY-only")
                PromotionOverviewPayload.model_validate_json(canonical_json(payload))
            else:
                raise ValidationFailure("promotion overview scope version is not adapted")
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
        elif draft.dataset_type is DatasetType.PRODUCT_METRICS:
            if not isinstance(payload, list):
                raise ValidationFailure("product_metrics requires records")
            records = [
                PromotedProductMetricRecord.model_validate_json(canonical_json(item))
                for item in payload
            ]
            keys = {(item.ad_id, item.campaign_id, item.platform_product_id) for item in records}
            if len(keys) != len(records):
                raise ValidationFailure("duplicate promoted-product metric record")
            if draft.metric_window is None:
                raise ValidationFailure("product_metrics requires metric_window")
            if (
                draft.scope.kind is not WindowKind.TODAY
                or draft.metric_window.kind is not WindowKind.TODAY
                or draft.scope.timezone != "Asia/Shanghai"
                or draft.metric_window.timezone != draft.scope.timezone
                or draft.metric_window.window_complete
                or draft.metric_window.source_finalized is not False
            ):
                raise ValidationFailure("product metric window semantics mismatch")
            zone = ZoneInfo("Asia/Shanghai")
            window_start_local = draft.metric_window.start.astimezone(zone)
            window_end_local = draft.metric_window.end.astimezone(zone)
            if (
                window_start_local.date() != draft.scope.business_date
                or any(
                    (
                        window_start_local.hour,
                        window_start_local.minute,
                        window_start_local.second,
                        window_start_local.microsecond,
                    )
                )
                or window_end_local.date() != draft.scope.business_date
                or draft.metric_window.end != draft.captured_at
            ):
                raise ValidationFailure("product metric window boundary mismatch")
            if any(item.observed_at != draft.captured_at for item in records):
                raise ValidationFailure("product metric observed_at mismatch")
            if draft.source_updated_at is None or any(
                item.source_updated_at != draft.source_updated_at for item in records
            ):
                raise ValidationFailure("product metric source_updated_at mismatch")
            if not (draft.metric_window.start < draft.source_updated_at <= draft.metric_window.end):
                raise ValidationFailure("product metric source_updated_at outside metric window")
        elif draft.dataset_type is DatasetType.PROMOTION_CONFIGURATION:
            if not isinstance(payload, list):
                raise ValidationFailure("promotion_configuration requires records")
            configurations = [
                PromotionConfigurationRecord.model_validate_json(canonical_json(item))
                for item in payload
            ]
            keys = {
                (item.ad_id, item.campaign_id, item.platform_product_id) for item in configurations
            }
            if len(keys) != len(configurations):
                raise ValidationFailure("duplicate promotion configuration record")
            if draft.metric_window is not None:
                raise ValidationFailure("promotion configuration cannot have metric_window")
            if any(item.configuration_observed_at != draft.captured_at for item in configurations):
                raise ValidationFailure("promotion configuration observed_at mismatch")
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
