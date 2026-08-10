from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
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
            raise RuntimeError("Neo4j authentication failed (401). Provide password via config or env var.") from exc
        raise RuntimeError(f"Neo4j HTTP error {exc.code} from {endpoint}: {exc.reason}") from exc
    except URLError as exc:
        raise RuntimeError(f"Cannot connect to Neo4j endpoint {endpoint}: {exc.reason}") from exc

    return payload if isinstance(payload, dict) else {}


def _validate_no_errors(payload: dict[str, Any], statement_name: str) -> None:
    errors = payload.get("errors", []) if isinstance(payload, dict) else []
    if not isinstance(errors, list):
        errors = []
    if errors:
        raise RuntimeError(f"Cypher statement failed ({statement_name}): {json.dumps(errors, default=str)}")


def _edge_id(subject: str, predicate: str, obj: str, hint: str) -> str:
    digest = hashlib.sha1(f"{subject}|{predicate}|{obj}|{hint}".encode("utf-8")).hexdigest()
    return f"NEXUS_EDGE:{digest}"


def _batch(items: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    out: list[list[dict[str, Any]]] = []
    for i in range(0, len(items), size):
        out.append(items[i : i + size])
    return out


def _open_sql(path: Path):
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return path.open("r", encoding="utf-8", errors="replace")


def _parse_string_schema(paths: list[Path], table_limit: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    table_nodes: dict[str, dict[str, Any]] = {}
    column_nodes: dict[str, dict[str, Any]] = {}
    edges: dict[str, dict[str, Any]] = {}

    table_start_re = re.compile(r"CREATE\s+TABLE\s+`?([A-Za-z0-9_]+)`?\s*\(", re.IGNORECASE)
    col_re = re.compile(r"^\s*`?([A-Za-z0-9_]+)`?\s+([A-Za-z0-9_()]+)", re.IGNORECASE)
    table_count = 0

    for path in paths:
        with _open_sql(path) as handle:
            in_table = False
            current_table = ""
            body_lines: list[str] = []

            for raw_line in handle:
                line = raw_line.rstrip("\n")
                if not in_table:
                    m_start = table_start_re.search(line)
                    if not m_start:
                        continue
                    in_table = True
                    current_table = m_start.group(1)
                    body_lines = []
                    continue

                if line.strip().startswith(")"):
                    table_name = current_table
                    body = "\n".join(body_lines)

                    table_id = f"STRING.TABLE:{table_name}"
                    table_nodes[table_id] = {
                        "id": table_id,
                        "name": table_name,
                        "category": "biolink:InformationContentEntity",
                        "provided_by": "string",
                        "source_payload": json.dumps({"sql_file": str(path)}, default=str),
                    }

                    for raw_col_line in body.splitlines():
                        col_line = raw_col_line.strip().rstrip(",")
                        if not col_line or col_line.upper().startswith(("PRIMARY KEY", "KEY ", "UNIQUE ", "CONSTRAINT ")):
                            continue
                        m_col = col_re.match(col_line)
                        if not m_col:
                            continue
                        col_name = m_col.group(1)
                        col_type = m_col.group(2)
                        col_id = f"STRING.COLUMN:{table_name}.{col_name}"
                        column_nodes[col_id] = {
                            "id": col_id,
                            "name": f"{table_name}.{col_name}",
                            "category": "biolink:Attribute",
                            "provided_by": "string",
                            "source_payload": json.dumps({"type": col_type, "sql_file": str(path)}, default=str),
                        }
                        e_id = _edge_id(table_id, "biolink:has_part", col_id, "table_column")
                        edges[e_id] = {
                            "id": e_id,
                            "subject": table_id,
                            "object": col_id,
                            "predicate": "biolink:has_part",
                            "association": "biolink:Association",
                            "provided_by": "string",
                            "source_payload": json.dumps({"rel": "table_column"}, default=str),
                        }

                    table_count += 1
                    if table_limit > 0 and table_count >= table_limit:
                        nodes = list(table_nodes.values()) + list(column_nodes.values())
                        return nodes, list(edges.values())

                    in_table = False
                    current_table = ""
                    body_lines = []
                    continue

                body_lines.append(line)

    nodes = list(table_nodes.values()) + list(column_nodes.values())
    return nodes, list(edges.values())


def _upsert_nodes(connection: dict[str, Any], nodes: list[dict[str, Any]], batch_size: int) -> int:
    if not nodes:
        return 0
    cypher = """
    UNWIND $rows AS row
    MERGE (n:NexusNode {id: row.id})
    SET n.name = row.name,
        n.category = row.category,
        n.provided_by = row.provided_by,
        n.source_payload = row.source_payload,
        n.updated_at = datetime()
    ON CREATE SET n.created_at = datetime()
    RETURN count(n) AS n_count
    """
    total = 0
    for chunk in _batch(nodes, batch_size):
        payload = _cypher_http_query(
            uri=str(connection["uri"]),
            database=str(connection["database"]),
            user=str(connection["user"]),
            password=str(connection["password"]),
            use_auth=bool(connection["require_auth"]),
            statement=cypher,
            params={"rows": chunk},
        )
        _validate_no_errors(payload, "upsert_string_nodes")
        total += len(chunk)
    return total


def _upsert_edges(connection: dict[str, Any], edges: list[dict[str, Any]], batch_size: int) -> int:
    if not edges:
        return 0
    cypher = """
    UNWIND $rows AS row
    MATCH (s:NexusNode {id: row.subject})
    MATCH (o:NexusNode {id: row.object})
    MERGE (s)-[r:NEXUS_EDGE {id: row.id}]->(o)
    SET r.predicate = row.predicate,
        r.association = row.association,
        r.provided_by = row.provided_by,
        r.source_payload = row.source_payload,
        r.updated_at = datetime()
    ON CREATE SET r.created_at = datetime()
    RETURN count(r) AS r_count
    """
    total = 0
    for chunk in _batch(edges, batch_size):
        payload = _cypher_http_query(
            uri=str(connection["uri"]),
            database=str(connection["database"]),
            user=str(connection["user"]),
            password=str(connection["password"]),
            use_auth=bool(connection["require_auth"]),
            statement=cypher,
            params={"rows": chunk},
        )
        _validate_no_errors(payload, "upsert_string_edges")
        total += len(chunk)
    return total


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert STRING SQL schema into Nexus KG metadata nodes/edges.")
    parser.add_argument("--config", default="configs/kg_build/string_schema_to_nexus.yaml", help="YAML config path")
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = PROJECT_ROOT / config_path
    cfg = load_yaml_config(config_path)

    source_cfg = cfg.get("source_string", {}) if isinstance(cfg, dict) else {}
    nexus_cfg = cfg.get("nexus_connection", {}) if isinstance(cfg, dict) else {}
    output_cfg = cfg.get("output", {}) if isinstance(cfg, dict) else {}

    if not isinstance(source_cfg, dict) or not isinstance(nexus_cfg, dict):
        raise ValueError("source_string and nexus_connection sections are required and must be mappings.")

    sql_files = source_cfg.get("sql_files", [])
    if not isinstance(sql_files, list) or not sql_files:
        raise ValueError("source_string.sql_files must be a non-empty list.")

    paths: list[Path] = []
    for entry in sql_files:
        p = Path(str(entry).strip())
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        if not p.exists():
            raise FileNotFoundError(f"STRING SQL schema file not found: {p}")
        paths.append(p)

    batch_size = int(source_cfg.get("batch_size", 1000))
    table_limit = int(source_cfg.get("table_limit", 0))
    dry_run = _as_bool(source_cfg.get("dry_run", True), default=True)
    sample_size = int(source_cfg.get("sample_size", 5))

    uri = str(nexus_cfg.get("uri", "")).strip()
    database = str(nexus_cfg.get("database", "neo4j")).strip() or "neo4j"
    user = str(nexus_cfg.get("user", "neo4j")).strip() or "neo4j"
    require_auth = _as_bool(nexus_cfg.get("require_auth", True), default=True)
    password_env = str(nexus_cfg.get("password_env", "NEO4J_PASSWORD")).strip() or "NEO4J_PASSWORD"
    password = str(nexus_cfg.get("password", "")).strip() or str(os.environ.get(password_env, "")).strip()

    if not dry_run:
        if not uri:
            raise ValueError("nexus_connection.uri is required when dry_run is false.")
        if require_auth and not password:
            raise ValueError(f"Password required via nexus_connection.password or env var {password_env}.")

    nodes, edges = _parse_string_schema(paths, table_limit=table_limit)

    nexus_conn = {
        "uri": uri,
        "database": database,
        "user": user,
        "password": password,
        "require_auth": require_auth,
    }

    upserted_nodes = 0
    upserted_edges = 0
    if not dry_run:
        upserted_nodes = _upsert_nodes(nexus_conn, nodes, batch_size=batch_size)
        upserted_edges = _upsert_edges(nexus_conn, edges, batch_size=batch_size)

    summary = {
        "source": {
            "sql_files": [str(p) for p in paths],
            "table_limit": table_limit,
        },
        "counts": {
            "normalized_nodes": len(nodes),
            "normalized_edges": len(edges),
            "upserted_nodes": upserted_nodes,
            "upserted_edges": upserted_edges,
        },
        "dry_run": dry_run,
        "sample": {
            "nodes": nodes[:sample_size],
            "edges": edges[:sample_size],
        },
    }

    pretty = True
    output_path = ""
    if isinstance(output_cfg, dict):
        pretty = _as_bool(output_cfg.get("pretty", True), default=True)
        output_path = str(output_cfg.get("path", "")).strip()

    rendered = json.dumps(summary, indent=2 if pretty else None, default=str)
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
