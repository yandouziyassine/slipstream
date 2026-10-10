from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

_ENV_FILE_VAR = "SLIPSTREAM_ENV_FILE"
_HOST_KEY = "SLIPSTREAM_SUPABASE_POOLER_HOST"
_REF_KEY = "SLIPSTREAM_SUPABASE_PROJECT_REF"
_PW_KEY = "SLIPSTREAM_SUPABASE_COLLECTOR_PASSWORD"
_HF_KEY = "SLIPSTREAM_HF_TOKEN"

# The password can only ever be sent to a Supabase session pooler.
_HOST = re.compile(r"aws-[0-9]+-[a-z0-9-]+\.pooler\.supabase\.com")
_PROJECT_REF = re.compile(r"[a-z0-9]{20}")
_PASSWORD = re.compile(r"[\x21-\x7e]{24,}")
_TOKEN = re.compile(r"hf_[A-Za-z0-9]{30,}")


class CloudConfigError(RuntimeError):
    """A cloud setting is missing or invalid. The message names the key, never the value."""


@dataclass(frozen=True)
class SupabaseSettings:
    pooler_host: str
    project_ref: str
    password: str = field(repr=False)

    @property
    def user(self) -> str:
        return f"slipstream_collector.{self.project_ref}"


@dataclass(frozen=True)
class HubSettings:
    token: str = field(repr=False)
    repo_id: str = "yandouziyassine/slipstream-recordings"


def env_file_path() -> Path:
    override = os.environ.get(_ENV_FILE_VAR)
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[3] / ".env"


def ca_path(data_dir: Path) -> Path:
    return data_dir / "cloud" / "supabase-ca.crt"


def load_supabase(env_file: Path) -> SupabaseSettings:
    values = _read(env_file, (_HOST_KEY, _REF_KEY, _PW_KEY))
    return SupabaseSettings(
        pooler_host=_checked(values, _HOST_KEY, _HOST),
        project_ref=_checked(values, _REF_KEY, _PROJECT_REF),
        password=_checked(values, _PW_KEY, _PASSWORD),
    )


def load_hub(env_file: Path) -> HubSettings:
    values = _read(env_file, (_HF_KEY,))
    return HubSettings(token=_checked(values, _HF_KEY, _TOKEN))


def _checked(values: dict[str, str], key: str, pattern: re.Pattern[str]) -> str:
    value = values.get(key)
    if value is None:
        raise CloudConfigError(f"{key} is missing")
    if pattern.fullmatch(value) is None:
        raise CloudConfigError(f"{key} is invalid")
    return value


def _read(env_file: Path, wanted: Iterable[str]) -> dict[str, str]:
    """Parse `KEY=VALUE` lines for the wanted keys only; every other line is ignored."""
    keys = frozenset(wanted)
    try:
        text = env_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise CloudConfigError("cloud settings: .env not found") from None
    except (OSError, UnicodeDecodeError):
        raise CloudConfigError("cloud settings: cannot read .env") from None
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        exported = line.startswith("export") and line[6:7].isspace()
        if exported:
            line = line[6:].lstrip()
        name, separator, value = line.partition("=")
        key = name.strip()
        if not separator or key not in keys:
            continue
        if exported:
            raise CloudConfigError(f"{key}: 'export' is not supported in .env, use KEY=VALUE")
        if key in values:
            raise CloudConfigError(f"{key} is defined more than once")
        values[key] = _unquote(value.strip())
    return values


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value
