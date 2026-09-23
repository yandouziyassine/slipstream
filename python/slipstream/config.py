from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass

_LOOPBACK = re.compile(r"^(127\.0\.0\.1|localhost|\[::1\]):(\d{1,5})$")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    engine_address: str


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    source = os.environ if env is None else env
    if source.get("SLIPSTREAM_PAPER_MODE", "true").strip().lower() != "true":
        raise ConfigError("live trading is not implemented; SLIPSTREAM_PAPER_MODE must be 'true'")
    address = source.get("SLIPSTREAM_ENGINE_ADDRESS", "127.0.0.1:50051")
    validate_engine_address(address)
    return Settings(engine_address=address)


def validate_engine_address(address: str) -> None:
    match = _LOOPBACK.match(address)
    if match is None or not 0 < int(match.group(2)) <= 65535:
        raise ConfigError("engine address must be a loopback host:port")
