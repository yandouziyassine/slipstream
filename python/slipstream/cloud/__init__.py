from __future__ import annotations

from pathlib import Path


def is_enabled(data_dir: Path) -> bool:
    return (data_dir / "cloud.enabled").exists()
