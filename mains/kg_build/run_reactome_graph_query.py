from __future__ import annotations

import argparse
import base64
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

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


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.strip().lower())
    return slug.strip("_") or "query"


def _archive_query_run(*, config_path: Path, payload: str, stem: str) -> tuple[Path, Path]:
    queries_dir = PROJECT_ROOT / "queries"
    queries_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    basename = f"{timestamp}_{_slugify(stem)}"

    result_path = queries_dir / f"{basename}.json"
    config_copy_path = queries_dir / f"{basename}.config.yaml"
    result_path.write_text(payload + "\n", encoding="utf-8")
    config_copy_path.write_text(config_path.read_text(encoding="utf-8"), encoding="utf-8")
    return result_path, config_copy_path


def _cypher_http_query(
    *,
    uri: str,
    database: str,
    user: str,
    password: str,
    use_auth: bool,
    statement: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    endpoint = f"{uri.rstrip('/')}/db/{database}/tx/commit"
    body = {
        "statements": [
            {
                "statement": statement,
                "parameters": params,
                "resultDataContents": ["row"],
            }
        ]
    }
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
    }
    if use_auth:
        auth_raw = f"{user}:{password}".encode("utf-8")
        auth_header = base64.b64encode(auth_raw).decode("ascii")
        headers["authorization"] = f"Basic {auth_header}"

    req = Request(
        endpoint,
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )

    try:
        with urlopen(req, timeout=120) as response:  # noqa: S310
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise RuntimeError(f"Neo4j HTTP error {exc.code} from {endpoint}: {exc.reason}") from exc
    except URLError as exc:
        raise RuntimeError(f"Cannot connect to Neo4j endpoint {endpoint}: {exc.reason}") from exc

    return payload if isinstance(payload, dict) else {}


def _normalize_result(payload: dict[str, Any]) -> dict[str, Any]:
    errors = payload.get("errors", []) if isinstance(payload, dict) else []
    if not isinstance(errors, list):
        errors = []
    if errors:
        raise RuntimeError(f"Cypher query failed: {json.dumps(errors, default=str)}")

    results = payload.get("results", []) if isinstance(payload, dict) else []
    if not isinstance(results, list) or not results:
        return {"columns": [], "rows": []}

    first = results[0]
    if not isinstance(first, dict):
        return {"columns": [], "rows": []}

    columns = first.get("columns", [])
    data = first.get("data", [])
    if not isinstance(columns, list):
        columns = []
    if not isinstance(data, list):
        data = []

    rows: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        row_values = item.get("row", [])
        if not isinstance(row_values, list):
            continue
        row_obj: dict[str, Any] = {}
        for idx, col in enumerate(columns):
            key = str(col)
            row_obj[key] = row_values[idx] if idx < len(row_values) else None
        rows.append(row_obj)

    return {"columns": columns, "rows": rows}


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Cypher query against a Reactome/Neo4j graph database from YAML config.")
    parser.add_argument("--config", default="configs/kg_build/reactome_graph_fluoxetine.yaml", help="Path to YAML config file.")
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = PROJECT_ROOT / config_path

    cfg = load_yaml_config(config_path)

    connection_cfg = cfg.get("connection", {}) if isinstance(cfg, dict) else {}
    query_cfg = cfg.get("query", {}) if isinstance(cfg, dict) else {}
    output_cfg = cfg.get("output", {}) if isinstance(cfg, dict) else {}

    if not isinstance(connection_cfg, dict) or not isinstance(query_cfg, dict):
        raise ValueError("connection and query sections are required and must be mappings.")

    uri = str(connection_cfg.get("uri", "")).strip()
    database = str(connection_cfg.get("database", "neo4j")).strip() or "neo4j"
    user = str(connection_cfg.get("user", "neo4j")).strip() or "neo4j"
    require_auth = _as_bool(connection_cfg.get("require_auth", False), default=False)
    password_env = str(connection_cfg.get("password_env", "NEO4J_PASSWORD")).strip() or "NEO4J_PASSWORD"
    password = str(connection_cfg.get("password", "")).strip() or str(__import__("os").environ.get(password_env, "")).strip()

    if not uri:
        raise ValueError("connection.uri is required.")
    if require_auth and not password:
        raise ValueError(f"Neo4j password is required via connection.password or env var {password_env}.")

    cypher = str(query_cfg.get("cypher", "")).strip()
    if not cypher:
        raise ValueError("query.cypher is required.")
    params = query_cfg.get("params", {})
    if not isinstance(params, dict):
        raise ValueError("query.params must be a mapping.")

    raw_payload = _cypher_http_query(
        uri=uri,
        database=database,
        user=user,
        password=password,
        use_auth=require_auth,
        statement=cypher,
        params=params,
    )
    normalized = _normalize_result(raw_payload)

    output: dict[str, Any] = {
        "connection": {
            "uri": uri,
            "database": database,
            "user": user,
        },
        "query": {
            "cypher": cypher,
            "params": params,
        },
        "result": normalized,
        "row_count": len(normalized.get("rows", [])),
    }

    pretty = True
    output_path = ""
    if isinstance(output_cfg, dict):
        pretty = _as_bool(output_cfg.get("pretty", True), default=True)
        output_path = str(output_cfg.get("path", "")).strip()

    rendered = json.dumps(output, indent=2 if pretty else None, default=str)

    if output_path:
        target = Path(output_path)
        if not target.is_absolute():
            target = PROJECT_ROOT / target
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rendered + "\n", encoding="utf-8")

    _archive_query_run(config_path=config_path, payload=rendered, stem=config_path.stem)

    print(rendered)


if __name__ == "__main__":
    main()
