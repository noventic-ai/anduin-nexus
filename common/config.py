from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import yaml


def load_yaml_config(path: str | Path) -> dict[str, Any]:
    """Load a YAML file into a dictionary."""
    config_path = Path(path)
    with config_path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(
            f"Expected a mapping at the root of {config_path}, got {type(data).__name__}."
        )
    return data


def save_yaml_config(path: str | Path, config: Mapping[str, Any]) -> None:
    """Persist a dictionary as YAML."""
    config_path = Path(path)
    with config_path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(dict(config), handle, sort_keys=False)
