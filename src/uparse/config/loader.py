from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from uparse.config import jsonc
from uparse.config.schema import Config
from uparse.core.errors import ConfigError

CONFIG_SUFFIXES = {".json", ".jsonc", ".json5"}


def is_config_path(target: str) -> bool:
    p = Path(target)
    return p.suffix.lower() in CONFIG_SUFFIXES and p.exists()


def load_config(path: str | Path) -> Config:
    data = jsonc.load(path)
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be an object")
    return build_config(data, origin=str(path))


def build_config(data: dict[str, Any], *, origin: str = "<config>") -> Config:
    try:
        return Config.model_validate(data)
    except PydanticValidationError as exc:
        raise ConfigError(f"{origin}: {_format_errors(exc)}") from exc


def _format_errors(exc: PydanticValidationError) -> str:
    parts = []
    for err in exc.errors():
        loc = ".".join(str(x) for x in err["loc"]) or "<root>"
        parts.append(f"{loc}: {err['msg']}")
    return "; ".join(parts)
