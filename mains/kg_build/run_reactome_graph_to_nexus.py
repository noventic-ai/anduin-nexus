from __future__ import annotations

import argparse
import base64
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
            raise RuntimeError(
                "Neo4j authentication failed (401). Provide password via config field or configured env var."
            ) from exc
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


def _rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    results = payload.get("results", []) if isinstance(payload, dict) else []
    if not isinstance(results, list) or not results:
        return []
    first = results[0]
    if not isinstance(first, dict):
        return []
    columns = first.get("columns", [])
    data = first.get("data", [])
    if not isinstance(columns, list) or not isinstance(data, list):
        return []

    rows: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        values = item.get("row", [])
        if not isinstance(values, list):
            continue
        row = {}
        for i, col in enumerate(columns):
            row[str(col)] = values[i] if i < len(values) else None
        rows.append(row)
    return rows


def _batch(items: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    chunks: list[list[dict[str, Any]]] = []
    for i in range(0, len(items), size):
        chunks.append(items[i : i + size])
    return chunks


def _map_category(labels: list[str], category_map: dict[str, str], default_category: str) -> str:
    for label in labels:
        if label in category_map:
            category = str(category_map[label]).strip()
            if category:
                return category
    return default_category


def _map_predicate(raw_type: str, predicate_map: dict[str, str], default_predicate: str) -> str:
    mapped = str(predicate_map.get(raw_type, raw_type)).strip()
    if not mapped:
        return default_predicate
    if ":" not in mapped:
        return f"biolink:{mapped}"
    return mapped


def _curie_from_reactome_id(value: Any, default_prefix: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if ":" in text:
        return text
    return f"{default_prefix}:{text}"


def _edge_id(subject: str, predicate: str, obj: str, source: str) -> str:
    key = f"{source}|{subject}|{predicate}|{obj}"
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
    return f"NEXUS_EDGE:{digest}"


def _fetch_source_nodes(connection: dict[str, Any], limit: int, offset: int) -> list[dict[str, Any]]:
    cypher = """
    MATCH (n)
    WHERE NOT n:NexusNode
      AND NOT n:NexusGraphMeta
    RETURN
      coalesce(n.stId, toString(n.dbId), toString(id(n))) AS reactome_id,
      coalesce(n.displayName, n.name, toString(id(n))) AS name,
      labels(n) AS labels,
      properties(n) AS props
    SKIP $offset
    LIMIT $limit
    """
    payload = _cypher_http_query(
        uri=str(connection["uri"]),
        database=str(connection["database"]),
        user=str(connection["user"]),
        password=str(connection["password"]),
        use_auth=bool(connection["require_auth"]),
        statement=cypher,
        params={"limit": limit, "offset": offset},
    )
    _validate_no_errors(payload, "fetch_source_nodes")
    return _rows(payload)


def _fetch_source_edges(connection: dict[str, Any], limit: int, offset: int) -> list[dict[str, Any]]:
    cypher = """
    MATCH (s)-[r]->(o)
    WHERE type(r) <> 'NEXUS_EDGE'
      AND NOT s:NexusNode
      AND NOT o:NexusNode
      AND NOT s:NexusGraphMeta
      AND NOT o:NexusGraphMeta
    RETURN
      coalesce(s.stId, toString(s.dbId), toString(id(s))) AS s_id,
      coalesce(o.stId, toString(o.dbId), toString(id(o))) AS o_id,
      type(r) AS rel_type,
      properties(r) AS props
    SKIP $offset
    LIMIT $limit
    """
    payload = _cypher_http_query(
        uri=str(connection["uri"]),
        database=str(connection["database"]),
        user=str(connection["user"]),
        password=str(connection["password"]),
        use_auth=bool(connection["require_auth"]),
        statement=cypher,
        params={"limit": limit, "offset": offset},
    )
    _validate_no_errors(payload, "fetch_source_edges")
    return _rows(payload)


def _upsert_nodes(connection: dict[str, Any], nodes: list[dict[str, Any]], batch_size: int) -> int:
    if not nodes:
        return 0
    cypher = """
    UNWIND $rows AS row
    MERGE (n:NexusNode {id: row.id})
    SET n.name = row.name,
        n.category = row.category,
        n.provided_by = row.provided_by,
        n.source_labels = row.source_labels,
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
        _validate_no_errors(payload, "upsert_nodes")
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
        r.source_type = row.source_type,
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
        _validate_no_errors(payload, "upsert_edges")
        total += len(chunk)
    return total


def _load_conn(cfg: dict[str, Any], prefix: str) -> dict[str, Any]:
    uri = str(cfg.get("uri", "")).strip()
    database = str(cfg.get("database", "neo4j")).strip() or "neo4j"
    user = str(cfg.get("user", "neo4j")).strip() or "neo4j"
    require_auth = _as_bool(cfg.get("require_auth", True), default=True)
    password_env = str(cfg.get("password_env", "NEO4J_PASSWORD")).strip() or "NEO4J_PASSWORD"
    password = str(cfg.get("password", "")).strip() or str(os.environ.get(password_env, "")).strip()
    if not uri:
        raise ValueError(f"{prefix}.uri is required.")
    if require_auth and not password:
        raise ValueError(f"Password required via {prefix}.password or env var {password_env}.")
    return {
        "uri": uri,
        "database": database,
        "user": user,
        "password": password,
        "require_auth": require_auth,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read Reactome from Neo4j graph DB and upsert Biolink-mapped records into Nexus KG."
    )
    parser.add_argument("--config", default="configs/kg_build/reactome_graph_to_nexus.yaml", help="YAML config path")
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = PROJECT_ROOT / config_path

    cfg = load_yaml_config(config_path)
    source_cfg = cfg.get("source_connection", {}) if isinstance(cfg, dict) else {}
    target_cfg = cfg.get("nexus_connection", {}) if isinstance(cfg, dict) else {}
    import_cfg = cfg.get("import", {}) if isinstance(cfg, dict) else {}
    mapping_cfg = cfg.get("mapping", {}) if isinstance(cfg, dict) else {}
    output_cfg = cfg.get("output", {}) if isinstance(cfg, dict) else {}

    if not isinstance(source_cfg, dict) or not isinstance(target_cfg, dict):
        raise ValueError("source_connection and nexus_connection are required mappings.")
    if not isinstance(import_cfg, dict):
        import_cfg = {}
    if not isinstance(mapping_cfg, dict):
        mapping_cfg = {}

    source_conn = _load_conn(source_cfg, "source_connection")
    target_conn = _load_conn(target_cfg, "nexus_connection")

    source_name = str(import_cfg.get("source_name", "reactome_graph")).strip() or "reactome_graph"
    default_prefix = str(import_cfg.get("default_prefix", "REACT")).strip() or "REACT"
    batch_size = int(import_cfg.get("batch_size", 1000))
    node_limit = int(import_cfg.get("node_limit", 2000))
    edge_limit = int(import_cfg.get("edge_limit", 5000))
    node_offset = int(import_cfg.get("node_offset", 0))
    edge_offset = int(import_cfg.get("edge_offset", 0))
    dry_run = _as_bool(import_cfg.get("dry_run", True), default=True)
    sample_size = int(import_cfg.get("sample_size", 5))

    if batch_size < 1:
        batch_size = 1000
    if sample_size < 1:
        sample_size = 5

    nodes_raw = _fetch_source_nodes(source_conn, limit=node_limit, offset=node_offset)
    edges_raw = _fetch_source_edges(source_conn, limit=edge_limit, offset=edge_offset)

    node_map = mapping_cfg.get("node_category_map", {}) if isinstance(mapping_cfg, dict) else {}
    if not isinstance(node_map, dict):
        node_map = {}

    default_category = str(mapping_cfg.get("default_node_category", "biolink:NamedThing")).strip() or "biolink:NamedThing"
    edge_map = mapping_cfg.get("predicate_map", {}) if isinstance(mapping_cfg, dict) else {}
    if not isinstance(edge_map, dict):
        edge_map = {}
    default_predicate = str(mapping_cfg.get("default_predicate", "biolink:related_to")).strip() or "biolink:related_to"
    default_association = str(mapping_cfg.get("default_association", "biolink:Association")).strip() or "biolink:Association"

    nodes: list[dict[str, Any]] = []
    for row in nodes_raw:
        labels = row.get("labels", [])
        if not isinstance(labels, list):
            labels = []
        nid = _curie_from_reactome_id(row.get("reactome_id"), default_prefix)
        if not nid:
            continue
        category = _map_category([str(l) for l in labels], node_map, default_category)
        if ":" not in category:
            category = f"biolink:{category}"
        nodes.append(
            {
                "id": nid,
                "name": str(row.get("name", "")).strip() or nid,
                "category": category,
                "provided_by": source_name,
                "source_labels": [str(l) for l in labels],
                "source_payload": json.dumps(row.get("props", {}), default=str),
            }
        )

    edges: list[dict[str, Any]] = []
    for row in edges_raw:
        subject = _curie_from_reactome_id(row.get("s_id"), default_prefix)
        obj = _curie_from_reactome_id(row.get("o_id"), default_prefix)
        if not subject or not obj:
            continue
        raw_type = str(row.get("rel_type", "")).strip()
        predicate = _map_predicate(raw_type, edge_map, default_predicate)
        eid = _edge_id(subject, predicate, obj, source_name)
        edges.append(
            {
                "id": eid,
                "subject": subject,
                "object": obj,
                "predicate": predicate,
                "association": default_association,
                "provided_by": source_name,
                "source_type": raw_type,
                "source_payload": json.dumps(row.get("props", {}), default=str),
            }
        )

    upserted_nodes = 0
    upserted_edges = 0
    if not dry_run:
        upserted_nodes = _upsert_nodes(target_conn, nodes, batch_size=batch_size)
        upserted_edges = _upsert_edges(target_conn, edges, batch_size=batch_size)

    out = {
        "source": {
            "uri": source_conn["uri"],
            "database": source_conn["database"],
            "source_name": source_name,
        },
        "target": {
            "uri": target_conn["uri"],
            "database": target_conn["database"],
        },
        "counts": {
            "source_nodes": len(nodes_raw),
            "source_edges": len(edges_raw),
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

    rendered = json.dumps(out, indent=2 if pretty else None, default=str)
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
