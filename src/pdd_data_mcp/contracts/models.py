from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"
SNAPSHOT_ID_PATTERN = r"^s_[0-9a-f]{32}$"
IDEMPOTENCY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"
SAFE_VERSION_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.+/-]{0,63}$"

InternalId = Annotated[str, Field(pattern=ID_PATTERN)]
SnapshotId = Annotated[str, Field(pattern=SNAPSHOT_ID_PATTERN)]
IdempotencyKey = Annotated[str, Field(pattern=IDEMPOTENCY_PATTERN)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def _aware(value: datetime | None, field_name: str) -> datetime | None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError(f"{field_name} must include a timezone")
    return value


class DatasetType(StrEnum):
    STORE_OVERVIEW = "store_overview"
    PRODUCT_CATALOG = "product_catalog"
    INVENTORY = "inventory"
    PROMOTION_OVERVIEW = "promotion_overview"
    PRODUCT_METRICS = "product_metrics"
    CAMPAIGN_METRICS = "campaign_metrics"
    ACTIVITY_CATALOG = "activity_catalog"


SUPPORTED_SYNTHETIC_DATASETS = frozenset(
    {
        DatasetType.STORE_OVERVIEW,
        DatasetType.PRODUCT_CATALOG,
        DatasetType.INVENTORY,
        DatasetType.PROMOTION_OVERVIEW,
    }
)


class WindowKind(StrEnum):
    TODAY = "TODAY"
    YESTERDAY = "YESTERDAY"
    LAST_7_DAYS = "LAST_7_DAYS"
    LAST_30_DAYS = "LAST_30_DAYS"
    POINT_IN_TIME = "POINT_IN_TIME"
    CUSTOM = "CUSTOM"
    UNKNOWN = "UNKNOWN"


class Scope(StrictModel):
    # MCP transports decode JSON dates/enums to strings before invoking tools.  Keep
    # extra-field and business-rule validation strict while accepting their standard
    # JSON representations at this one protocol boundary.
    model_config = ConfigDict(extra="forbid", strict=False)

    kind: WindowKind
    business_date: date
    timezone: str = "Asia/Shanghai"
    object_type: str = Field(default="ACCOUNT_ALL", pattern=ID_PATTERN)
    filters: dict[str, str] = Field(default_factory=dict)
    currency: str = Field(default="CNY", pattern=r"^[A-Z]{3}$")
    attribution: str = Field(default="PLATFORM_DEFAULT", pattern=ID_PATTERN)
    version: str = Field(default="1", pattern=SAFE_VERSION_PATTERN)
    start: datetime | None = None
    end: datetime | None = None

    @field_validator("start", "end")
    @classmethod
    def validate_aware(cls, value: datetime | None) -> datetime | None:
        return _aware(value, "scope datetime")

    @field_validator("timezone")
    @classmethod
    def validate_timezone_name(cls, value: str) -> str:
        if not value or len(value) > 64 or ".." in value or "\\" in value:
            raise ValueError("invalid timezone name")
        return value

    @field_validator("filters")
    @classmethod
    def validate_filters(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 32:
            raise ValueError("too many filters")
        for key, item in value.items():
            if not key or len(key) > 64 or len(item) > 256:
                raise ValueError("invalid filter")
        return value

    @model_validator(mode="after")
    def validate_custom_window(self) -> Scope:
        if self.kind is WindowKind.CUSTOM:
            if self.start is None or self.end is None:
                raise ValueError("CUSTOM scope requires start and end")
            if self.end <= self.start:
                raise ValueError("scope end must be after start")
        elif self.start is not None or self.end is not None:
            raise ValueError("start/end are accepted only for CUSTOM scope")
        return self


class MetricWindow(StrictModel):
    kind: WindowKind
    timezone: str
    start: datetime
    end: datetime
    window_complete: bool
    source_finalized: bool | None = None

    @field_validator("start", "end")
    @classmethod
    def validate_aware(cls, value: datetime) -> datetime:
        checked = _aware(value, "metric window datetime")
        assert checked is not None
        return checked

    @model_validator(mode="after")
    def validate_order(self) -> MetricWindow:
        if self.end <= self.start:
            raise ValueError("metric window end must be after start")
        return self


class CoverageStatus(StrEnum):
    COMPLETE = "COMPLETE"
    TRUNCATED = "TRUNCATED"
    PARTIAL = "PARTIAL"
    UNKNOWN = "UNKNOWN"


class Coverage(StrictModel):
    total_observed: int | None = Field(default=None, ge=0)
    captured: int = Field(ge=0)
    limit: int = Field(ge=1, le=200)
    pages_read: int = Field(ge=0)
    coverage: CoverageStatus
    truncated: bool
    stop_reason: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def validate_counts(self) -> Coverage:
        if self.captured > self.limit:
            raise ValueError("captured cannot exceed limit")
        if self.total_observed is not None and self.captured > self.total_observed:
            raise ValueError("captured cannot exceed total_observed")
        if self.truncated and self.coverage is not CoverageStatus.TRUNCATED:
            raise ValueError("truncated data must use TRUNCATED coverage")
        if self.coverage is CoverageStatus.TRUNCATED and not self.truncated:
            raise ValueError("TRUNCATED coverage requires truncated=true")
        if (
            self.total_observed is not None
            and self.total_observed > self.captured
            and self.coverage is CoverageStatus.COMPLETE
        ):
            raise ValueError("incomplete known total cannot use COMPLETE coverage")
        if self.coverage is CoverageStatus.PARTIAL and self.truncated:
            raise ValueError("PARTIAL coverage must not be marked truncated")
        return self


class Quality(StrictModel):
    status: Literal["VALID", "INVALID"]
    identity: Literal["MATCHED", "NOT_CHECKED", "MISMATCH"]
    coverage: CoverageStatus
    dom_check: Literal["MATCHED", "NOT_RUN", "MISMATCH"]


class IdentityEvidence(StrictModel):
    expected_platform_store_id: str = Field(min_length=1, max_length=128)
    observed_platform_store_id: str = Field(min_length=1, max_length=128)
    evidence_source: Literal["NETWORK_RESPONSE", "DOM"]
    independent_verification_method: Literal["CONFIG_EXACT_ID", "MERCHANT_PAGE_STATE_SHA256"] = (
        "CONFIG_EXACT_ID"
    )
    independent_verification_reference: str | None = Field(
        default=None, min_length=1, max_length=128
    )
    response_field_path: str | None = Field(default=None, min_length=1, max_length=256)
    dom_selector: str | None = Field(default=None, max_length=512)
    dom_attribute: str | None = Field(default=None, max_length=128)


class MetricValue(StrictModel):
    value: int | str
    unit: Literal["CNY_CENT", "COUNT", "RATIO"]
    observed_at: datetime
    source_updated_at: datetime | None = None
    capture_method: Literal["SYNTHETIC", "NETWORK_RESPONSE", "DOM", "MIXED"]
    precision: Literal["EXACT", "APPROXIMATE"]

    @field_validator("observed_at", "source_updated_at")
    @classmethod
    def validate_observed_at(cls, value: datetime | None) -> datetime | None:
        checked = _aware(value, "observed_at")
        return checked


class StoreOverviewPayload(StrictModel):
    metrics: dict[str, MetricValue | None]


class PromotionOverviewPayload(StrictModel):
    metrics: dict[str, MetricValue | None]


class ProductCatalogRecord(StrictModel):
    product_id: InternalId
    platform_product_id: str | None = Field(default=None, min_length=1, max_length=128)
    sku_id: InternalId | None = None
    name: str | None = Field(default=None, max_length=512)
    price_cents: int | None = Field(default=None, ge=0)
    status: Literal["ON_SALE", "OFF_SALE", "UNKNOWN"] | None = None
    sku_count: int | None = Field(default=None, ge=0)
    created_at: datetime | None = None
    published_at: datetime | None = None
    observed_at: datetime

    @field_validator("created_at", "published_at", "observed_at")
    @classmethod
    def validate_product_times(cls, value: datetime | None) -> datetime | None:
        return _aware(value, "product datetime")


class InventoryRecord(StrictModel):
    product_id: InternalId
    platform_product_id: str | None = Field(default=None, min_length=1, max_length=128)
    sku_id: InternalId | None = None
    inventory: int | None = Field(default=None, ge=0)
    granularity: Literal["PRODUCT", "SKU"]
    observed_at: datetime

    @field_validator("observed_at")
    @classmethod
    def validate_observed_at(cls, value: datetime) -> datetime:
        checked = _aware(value, "observed_at")
        assert checked is not None
        return checked

    @model_validator(mode="after")
    def validate_granularity(self) -> InventoryRecord:
        if self.granularity == "SKU" and self.sku_id is None:
            raise ValueError("SKU inventory requires sku_id")
        if self.granularity == "PRODUCT" and self.sku_id is not None:
            raise ValueError("product inventory must not invent sku_id")
        return self


class ValidationReport(StrictModel):
    valid: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    checked_at: datetime

    @field_validator("checked_at")
    @classmethod
    def validate_checked_at(cls, value: datetime) -> datetime:
        checked = _aware(value, "checked_at")
        assert checked is not None
        return checked


class SnapshotDraft(StrictModel):
    batch_id: InternalId | None = None
    store_id: InternalId
    dataset_type: DatasetType
    scope: Scope
    scope_key: str = Field(pattern=r"^scope_[0-9a-f]{32}$")
    requested_at: datetime
    capture_started_at: datetime
    capture_finished_at: datetime
    captured_at: datetime
    metric_window: MetricWindow | None = None
    source_updated_at: datetime | None = None
    source: Literal["SYNTHETIC", "PDD_BROWSER_CDP"]
    capture_method: Literal["SYNTHETIC", "NETWORK_RESPONSE", "DOM", "MIXED"]
    parser_version: str = Field(pattern=SAFE_VERSION_PATTERN)
    identity_evidence: IdentityEvidence | None = None
    field_sources: dict[str, Literal["NETWORK_RESPONSE", "DOM"]] = Field(default_factory=dict)
    payload: dict[str, Any] | list[dict[str, Any]]
    missing_fields: list[str] = Field(default_factory=list)
    quality: Quality
    coverage: Coverage

    @field_validator(
        "requested_at",
        "capture_started_at",
        "capture_finished_at",
        "captured_at",
        "source_updated_at",
    )
    @classmethod
    def validate_times(cls, value: datetime | None) -> datetime | None:
        return _aware(value, "snapshot datetime")

    @model_validator(mode="after")
    def validate_sequence(self) -> SnapshotDraft:
        if self.capture_finished_at < self.capture_started_at:
            raise ValueError("capture_finished_at precedes capture_started_at")
        if self.captured_at < self.capture_started_at:
            raise ValueError("captured_at precedes capture_started_at")
        if self.source == "SYNTHETIC" and self.capture_method != "SYNTHETIC":
            raise ValueError("synthetic data must use synthetic capture method")
        return self


class FileDescriptor(StrictModel):
    name: Literal["data.json", "records.jsonl", "validation.json"]
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bytes: int = Field(ge=0)
    records: int = Field(ge=0)


class SnapshotManifest(StrictModel):
    snapshot_id: SnapshotId
    request_id: InternalId
    batch_id: InternalId | None = None
    idempotency_key_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    parameter_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    schema_version: Literal["1.0.0"] = "1.0.0"
    layout_version: str
    platform: Literal["pdd"] = "pdd"
    store_id: InternalId
    dataset_type: DatasetType
    scope: Scope
    scope_key: str = Field(pattern=r"^scope_[0-9a-f]{32}$")
    requested_at: datetime
    capture_started_at: datetime
    capture_finished_at: datetime
    captured_at: datetime
    metric_window: MetricWindow | None = None
    source_updated_at: datetime | None = None
    committed_at: datetime
    source: Literal["SYNTHETIC", "PDD_BROWSER_CDP"]
    capture_method: Literal["SYNTHETIC", "NETWORK_RESPONSE", "DOM", "MIXED"]
    parser_version: str
    identity_evidence: IdentityEvidence | None = None
    field_sources: dict[str, Literal["NETWORK_RESPONSE", "DOM"]] = Field(default_factory=dict)
    missing_fields: list[str]
    quality: Quality
    coverage: Coverage
    payload_file: Literal["data.json", "records.jsonl"]
    files: list[FileDescriptor]
    record_count: int = Field(ge=0)

    @field_validator(
        "requested_at",
        "capture_started_at",
        "capture_finished_at",
        "captured_at",
        "source_updated_at",
        "committed_at",
    )
    @classmethod
    def validate_times(cls, value: datetime | None) -> datetime | None:
        return _aware(value, "manifest datetime")


class CommitRecord(StrictModel):
    snapshot_id: SnapshotId
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    committed_at: datetime

    @field_validator("committed_at")
    @classmethod
    def validate_committed_at(cls, value: datetime) -> datetime:
        checked = _aware(value, "committed_at")
        assert checked is not None
        return checked


class CapabilitiesResult(StrictModel):
    service: Literal["pdd-data-mcp"] = "pdd-data-mcp"
    version: str
    transport: Literal["stdio"] = "stdio"
    storage: Literal["local_files"] = "local_files"
    synthetic_test_mode: bool
    real_collection: Literal["DISABLED", "CONFIGURED_NOT_VERIFIED", "AVAILABLE"]
    datasets: dict[
        str,
        Literal[
            "SYNTHETIC_TEST_ONLY",
            "REAL_PROMOTION_TODAY",
            "REAL_STORE_TODAY",
            "REAL_PRODUCT_CATALOG",
            "REAL_INVENTORY",
            "CONFIGURED_NOT_VERIFIED",
            "UNAVAILABLE",
        ],
    ]


class ConnectionStatusResult(StrictModel):
    connection_id: InternalId
    store_id: InternalId
    status: Literal["NOT_CHECKED"] = "NOT_CHECKED"
    real_collection_enabled: bool
    synthetic_test_enabled: bool
    message: str


class CollectResult(StrictModel):
    status: Literal[
        "SUCCEEDED",
        "PARTIAL",
        "FAILED",
        "BUSY",
        "INTERRUPTED",
        "REAL_COLLECTION_DISABLED",
        "DATASET_UNVERIFIED",
        "ADAPTER_UNVERIFIED",
        "CDP_UNAVAILABLE",
        "TARGET_PAGE_NOT_FOUND",
        "AUTH_REQUIRED",
        "IDENTITY_UNVERIFIED",
        "IDENTITY_MISMATCH",
        "PLATFORM_ERROR",
        "TIME_SCOPE_UNVERIFIED",
        "UNIT_UNVERIFIED",
        "DATA_MISMATCH",
        "CAPTURE_TIMEOUT",
    ]
    committed: bool
    snapshot_id: SnapshotId | None = None
    batch_id: InternalId | None = None
    dataset_type: DatasetType
    captured: int = 0
    total_observed: int | None = None
    coverage: CoverageStatus = CoverageStatus.UNKNOWN
    truncated: bool = False
    idempotent_replay: bool = False
    warnings: list[str] = Field(default_factory=list)
    error_code: str | None = None


class SnapshotSummary(StrictModel):
    snapshot_id: SnapshotId
    store_id: InternalId
    dataset_type: DatasetType
    scope_key: str
    captured_at: datetime
    committed_at: datetime
    source: Literal["SYNTHETIC", "PDD_BROWSER_CDP"]
    quality_status: Literal["VALID", "INVALID"]
    coverage: CoverageStatus
    record_count: int


class ListSnapshotsResult(StrictModel):
    items: list[SnapshotSummary]
    next_cursor: str | None = None


class ReadSnapshotResult(StrictModel):
    snapshot_id: SnapshotId
    manifest: dict[str, Any]
    data: dict[str, Any] | None = None
    records: list[dict[str, Any]] = Field(default_factory=list)
    next_cursor: str | None = None


class LatestSnapshotResult(StrictModel):
    status: Literal["FOUND", "NOT_FOUND", "PARTIAL_ONLY"]
    snapshot: SnapshotSummary | None = None
    message: str | None = None
