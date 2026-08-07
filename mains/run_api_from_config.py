from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from api import AnduinAPIClient
from common.config import load_yaml_config


def _as_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "y"}:
            return True
        if lowered in {"0", "false", "no", "n"}:
            return False
    return bool(value)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run API adapter queries from YAML config.")
    parser.add_argument(
        "--config",
        default="configs/api/lincs_signatures.yaml",
        help="Path to YAML config file.",
    )
    args = parser.parse_args()

    cfg = load_yaml_config(args.config)
    adapter = cfg.get("adapter", {})
    if not isinstance(adapter, dict):
        raise ValueError("adapter must be a mapping")

    adapter_name = str(adapter.get("name", "")).strip().lower()
    operation = str(adapter.get("operation", "")).strip()
    if not operation:
        # Backward compatibility with earlier configs.
        operation = str(adapter.get("workflow", "")).strip()
    if not adapter_name:
        raise ValueError("adapter.name is required")
    if not operation:
        raise ValueError("adapter.operation is required")

    params = cfg.get("params", {})
    if not isinstance(params, dict):
        raise ValueError("params must be a mapping")

    client = AnduinAPIClient()
    out = client.execute(source=adapter_name, operation=operation, **params)

    output_cfg = cfg.get("output", {})
    if not isinstance(output_cfg, dict):
        output_cfg = {}
    pretty = _as_bool(output_cfg.get("pretty", True), default=True)
    output_path = output_cfg.get("path")
    payload = json.dumps(out, indent=2 if pretty else None, default=str)

    if output_path:
        target = Path(str(output_path))
        if not target.is_absolute():
            target = PROJECT_ROOT / target
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(payload + "\n", encoding="utf-8")

    try:
        print(payload)
    except BrokenPipeError:
        try:
            sys.stdout.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
