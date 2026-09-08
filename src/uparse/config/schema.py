from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

OutputFormat = Literal["json", "jsonl", "csv", "sqlite"]
ExtractionMode = Literal["auto", "manual"]
WaitUntil = Literal["load", "domcontentloaded", "networkidle", "commit"]
AcquirerName = Literal["auto", "browser", "http", "file"]


class Base(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class SourcesConfig(Base):
    type: Literal["url", "urls", "file", "stdin"] = "url"
    url: str | None = None
    urls: list[str] = Field(default_factory=list)
    path: Path | None = None

    @model_validator(mode="after")
    def _check(self) -> SourcesConfig:
        if self.type == "file" and not self.path:
            raise ValueError("sources.type='file' requires 'path'")
        return self


class BrowserConfig(Base):
    enabled: bool = True
    headless: bool = True
    timeout: int = Field(default=30_000, ge=1_000, le=600_000)
    concurrency: int = Field(default=4, ge=1, le=32)
    wait_until: WaitUntil = "domcontentloaded"
    wait_for: str | None = None
    wait_ms: int = Field(default=0, ge=0, le=120_000)
    user_agent: str | None = None
    viewport: tuple[int, int] = (1366, 900)
    locale: str | None = None
    timezone: str | None = None
    block_resources: list[str] = Field(default_factory=lambda: ["font", "media"])
    block_images: bool = False
    persist_cookies: bool = True
    storage_state: Path | None = None
    executable_path: Path | None = None
    extra_headers: dict[str, str] = Field(default_factory=dict)

    @field_validator("block_resources")
    @classmethod
    def _known(cls, v: list[str]) -> list[str]:
        known = {
            "font",
            "media",
            "image",
            "stylesheet",
            "script",
            "xhr",
            "fetch",
            "websocket",
            "other",
        }
        bad = sorted(set(v) - known)
        if bad:
            raise ValueError(f"unknown resource types: {', '.join(bad)}")
        return v


class FieldSpec(Base):
    """`"auto"` in JSONC widens to FieldSpec(mode='auto'); an object pins the extraction."""

    mode: Literal["auto", "selector", "attribute", "regex", "constant"] = "auto"
    selector: str | None = None
    attribute: str | None = None
    regex: str | None = None
    value: Any = None
    type: Literal["auto", "string", "number", "integer", "boolean", "url", "date", "datetime"] = (
        "auto"
    )
    required: bool = False
    many: bool = False
    default: Any = None

    @model_validator(mode="before")
    @classmethod
    def _widen(cls, data: Any) -> Any:
        if data == "auto":
            return {"mode": "auto"}
        if isinstance(data, str):
            return {"mode": "selector", "selector": data}
        if isinstance(data, dict) and "mode" not in data:
            data = dict(data)
            if data.get("regex"):
                data["mode"] = "regex"
            elif data.get("attribute"):
                data["mode"] = "attribute"
            elif data.get("selector"):
                data["mode"] = "selector"
            elif data.get("value") is not None:
                data["mode"] = "constant"
        return data

    @model_validator(mode="after")
    def _check(self) -> FieldSpec:
        if self.mode == "selector" and not self.selector:
            raise ValueError("selector mode requires 'selector'")
        if self.mode == "attribute" and not (self.selector and self.attribute):
            raise ValueError("attribute mode requires 'selector' and 'attribute'")
        if self.mode == "regex" and not self.regex:
            raise ValueError("regex mode requires 'regex'")
        return self


class ExtractionConfig(Base):
    mode: ExtractionMode = "auto"
    collection: str | None = None
    fields: dict[str, FieldSpec] = Field(default_factory=dict)
    min_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    min_records: int = Field(default=2, ge=1)
    max_records_per_page: int = Field(default=5_000, ge=1)
    include_structured_data: bool = True
    include_tables: bool = True
    provenance: bool = False


class PaginationConfig(Base):
    enabled: bool = True
    selector: str | None = None
    url_template: str | None = None
    max_pages: int = Field(default=25, ge=1, le=10_000)
    infinite_scroll: bool = False
    max_scrolls: int = Field(default=20, ge=0, le=500)
    max_items: int = Field(default=10_000, ge=1)
    max_duration_s: int = Field(default=300, ge=1)
    stop_on_duplicate_page: bool = True


class DedupeConfig(Base):
    enabled: bool = True
    keys: list[str] = Field(default_factory=list)
    scope: Literal["job", "source", "page"] = "job"


class RetryConfig(Base):
    attempts: int = Field(default=3, ge=1, le=10)
    base_delay_s: float = Field(default=1.0, ge=0.0)
    max_delay_s: float = Field(default=30.0, ge=0.0)
    jitter: float = Field(default=0.25, ge=0.0, le=1.0)


class OutputConfig(Base):
    format: OutputFormat | None = None
    path: Path | None = None
    provenance: bool = False
    pretty: bool = True
    csv_delimiter: str = ","
    columns: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _infer_format(self) -> OutputConfig:
        if self.format is None and self.path is not None:
            suffix = self.path.suffix.lower()
            guess: dict[str, OutputFormat] = {
                ".json": "json",
                ".jsonl": "jsonl",
                ".ndjson": "jsonl",
                ".csv": "csv",
                ".db": "sqlite",
                ".sqlite": "sqlite",
                ".sqlite3": "sqlite",
            }
            if suffix in guess:
                object.__setattr__(self, "format", guess[suffix])
        return self


class PolitenessConfig(Base):
    delay_s: float = Field(default=0.0, ge=0.0, le=60.0)
    respect_robots: bool = True
    obey_crawl_delay: bool = True
    per_host: int = Field(default=2, ge=1, le=32)
    user_agent_note: str | None = None


class FollowConfig(Base):
    """Visit the page each record links to and merge what it says back into the record."""

    enabled: bool = False
    field: str = "url"
    max_pages: int = Field(default=200, ge=1, le=100_000)
    prefer: Literal["detail", "listing"] = "detail"
    same_host: bool = True


class Config(Base):
    sources: SourcesConfig = Field(default_factory=SourcesConfig)
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    extraction: ExtractionConfig = Field(default_factory=ExtractionConfig)
    pagination: PaginationConfig = Field(default_factory=PaginationConfig)
    dedupe: DedupeConfig = Field(default_factory=DedupeConfig)
    retry: RetryConfig = Field(default_factory=RetryConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)
    politeness: PolitenessConfig = Field(default_factory=PolitenessConfig)
    follow: FollowConfig = Field(default_factory=FollowConfig)
    # Sources processed at once. Per-host limits still apply, so this is a job-wide
    # budget rather than permission to point every thread at one site.
    concurrency: int = Field(default=4, ge=1, le=64)
    acquirer: AcquirerName = "auto"
    db: Path | None = None
    name: str | None = None

    def merged(self, **overrides: Any) -> Config:
        """Deep-merge partial overrides (CLI flags) onto this config."""
        data = self.model_dump(mode="python")
        _deep_update(data, {k: v for k, v in overrides.items() if v is not None})
        return Config.model_validate(data)


def _deep_update(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_update(base[key], value)
        else:
            base[key] = value
    return base
