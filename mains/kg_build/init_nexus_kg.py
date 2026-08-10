from __future__ import annotations

import argparse
import base64
import json
import os
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
        if exc.code == 401:
            raise RuntimeError(
                "Neo4j authentication failed (401). Set connection.require_auth=true and provide "
                "connection.password or the configured password_env."
            ) from exc
        raise RuntimeError(f"Neo4j HTTP error {exc.code} from {endpoint}: {exc.reason}") from exc
    except URLError as exc:
        raise RuntimeError(f"Cannot connect to Neo4j endpoint {endpoint}: {exc.reason}") from exc

    return payload if isinstance(payload, dict) else {}


def _validate_no_errors(payload: dict[str, Any], statement_name: str) -> dict[str, Any]:
    errors = payload.get("errors", []) if isinstance(payload, dict) else []
    if not isinstance(errors, list):
        errors = []
    if errors:
        raise RuntimeError(f"Cypher statement failed ({statement_name}): {json.dumps(errors, default=str)}")
    return payload


def _statement_rows(payload: dict[str, Any]) -> tuple[list[str], list[dict[str, Any]]]:
    results = payload.get("results", []) if isinstance(payload, dict) else []
    if not isinstance(results, list) or not results:
        return [], []

    first = results[0]
    if not isinstance(first, dict):
        return [], []

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

    return [str(c) for c in columns], rows


def _run_statement(
    *,
    connection: dict[str, Any],
    name: str,
    cypher: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    payload = _cypher_http_query(
        uri=str(connection["uri"]),
        database=str(connection["database"]),
        user=str(connection["user"]),
        password=str(connection["password"]),
        use_auth=bool(connection["require_auth"]),
        statement=cypher,
        params=params,
    )
    _validate_no_errors(payload, statement_name=name)
    columns, rows = _statement_rows(payload)
    return {
        "name": name,
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
    }


def _build_statements(graph_cfg: dict[str, Any], create_constraints: bool) -> list[tuple[str, str, dict[str, Any]]]:
    graph_name = str(graph_cfg.get("name", "nexus_kg")).strip() or "nexus_kg"
    schema_url = str(graph_cfg.get("biolink_schema", "https://w3id.org/biolink/biolink-model.yaml")).strip()

    statements: list[tuple[str, str, dict[str, Any]]] = []
    if create_constraints:
        statements.extend(
            [
                (
                    "constraint_nexus_node_id",
                    "CREATE CONSTRAINT nexus_node_id IF NOT EXISTS FOR (n:NexusNode) REQUIRE n.id IS UNIQUE",
                    {},
                ),
                (
                    "constraint_nexus_edge_id",
                    "CREATE CONSTRAINT nexus_edge_id IF NOT EXISTS FOR ()-[r:NEXUS_EDGE]-() REQUIRE r.id IS UNIQUE",
                    {},
                ),
                (
                    "index_nexus_node_category",
                    "CREATE INDEX nexus_node_category IF NOT EXISTS FOR (n:NexusNode) ON (n.category)",
                    {},
                ),
                (
                    "index_nexus_edge_predicate",
                    "CREATE INDEX nexus_edge_predicate IF NOT EXISTS FOR ()-[r:NEXUS_EDGE]-() ON (r.predicate)",
                    {},
                ),
            ]
        )

    statements.append(
        (
            "upsert_nexus_graph_metadata",
            """
            MERGE (m:NexusGraphMeta {name: $name})
                        ON CREATE SET m.created_at = datetime($updated_at)
            SET
              m.kind = 'knowledge_graph',
              m.schema = 'biolink',
              m.schema_url = $schema_url,
              m.updated_at = datetime($updated_at),
              m.status = 'initialized'
            RETURN m.name AS name, m.schema_url AS schema_url, m.status AS status
            """,
            {
                "name": graph_name,
                "schema_url": schema_url,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )
    )
    return statements


def main() -> None:
    parser = argparse.ArgumentParser(description="Initialize Nexus KG scaffolding in Neo4j.")
    parser.add_argument("--config", default="configs/kg_build/nexus_kg_init.yaml", help="Path to YAML config file.")
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = PROJECT_ROOT / config_path

    cfg = load_yaml_config(config_path)

    connection_cfg = cfg.get("connection", {}) if isinstance(cfg, dict) else {}
    graph_cfg = cfg.get("graph", {}) if isinstance(cfg, dict) else {}
    output_cfg = cfg.get("output", {}) if isinstance(cfg, dict) else {}

    if not isinstance(connection_cfg, dict):
        raise ValueError("connection section is required and must be a mapping.")
    if not isinstance(graph_cfg, dict):
        graph_cfg = {}

    uri = str(connection_cfg.get("uri", "")).strip()
    database = str(connection_cfg.get("database", "neo4j")).strip() or "neo4j"
    user = str(connection_cfg.get("user", "neo4j")).strip() or "neo4j"
    require_auth = _as_bool(connection_cfg.get("require_auth", False), default=False)
    password_env = str(connection_cfg.get("password_env", "NEO4J_PASSWORD")).strip() or "NEO4J_PASSWORD"
    password = str(connection_cfg.get("password", "")).strip() or str(os.environ.get(password_env, "")).strip()

    if not uri:
        raise ValueError("connection.uri is required.")
    if require_auth and not password:
        raise ValueError(f"Neo4j password is required via connection.password or env var {password_env}.")

    create_constraints = _as_bool(graph_cfg.get("create_constraints", True), default=True)

    conn = {
        "uri": uri,
        "database": database,
        "user": user,
        "password": password,
        "require_auth": require_auth,
    }

    statement_defs = _build_statements(graph_cfg=graph_cfg, create_constraints=create_constraints)
    statement_results = []
    for name, cypher, params in statement_defs:
        result = _run_statement(connection=conn, name=name, cypher=cypher, params=params)
        statement_results.append(result)

    output: dict[str, Any] = {
        "connection": {
            "uri": uri,
            "database": database,
            "user": user,
        },
        "graph": {
            "name": str(graph_cfg.get("name", "nexus_kg")),
            "biolink_schema": str(graph_cfg.get("biolink_schema", "https://w3id.org/biolink/biolink-model.yaml")),
            "create_constraints": create_constraints,
        },
        "statements": statement_results,
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
