from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sqlite3
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
                "Neo4j authentication failed (401). Provide password via config or env var."
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


def _fetchall_dict(cursor: sqlite3.Cursor, query: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    cursor.execute(query, params)
    columns = [desc[0] for desc in cursor.description or []]
    rows = cursor.fetchall()
    return [dict(zip(columns, row, strict=False)) for row in rows]


def _find_compounds(cursor: sqlite3.Cursor, query: str, limit: int) -> list[dict[str, Any]]:
    sql = """
    SELECT
        molregno,
        chembl_id,
        pref_name,
        max_phase,
        therapeutic_flag,
        molecule_type
    FROM molecule_dictionary
    WHERE UPPER(pref_name) LIKE UPPER(?)
    ORDER BY
        CASE WHEN UPPER(pref_name) = UPPER(?) THEN 0 ELSE 1 END,
        max_phase DESC,
        chembl_id
    LIMIT ?
    """
    return _fetchall_dict(cursor, sql, (f"{query}%", query, limit))


def _indications(cursor: sqlite3.Cursor, molregno: int, limit: int) -> list[dict[str, Any]]:
    sql = """
    SELECT
        efo_id,
        efo_term,
        mesh_id,
        mesh_heading,
        max_phase_for_ind
    FROM drug_indication
    WHERE molregno = ?
    ORDER BY max_phase_for_ind DESC, efo_term
    LIMIT ?
    """
    return _fetchall_dict(cursor, sql, (molregno, limit))


def _mechanisms(cursor: sqlite3.Cursor, molregno: int, limit: int) -> list[dict[str, Any]]:
    sql = """
    SELECT
        dm.mechanism_of_action,
        dm.action_type,
        dm.direct_interaction,
        dm.molecular_mechanism,
        dm.disease_efficacy,
        td.chembl_id AS target_chembl_id,
        td.pref_name AS target_name,
        td.organism AS target_organism
    FROM drug_mechanism dm
    LEFT JOIN target_dictionary td ON td.tid = dm.tid
    WHERE dm.molregno = ?
    ORDER BY dm.mec_id
    LIMIT ?
    """
    return _fetchall_dict(cursor, sql, (molregno, limit))


def _edge_id(subject: str, predicate: str, obj: str, payload_hint: str) -> str:
    key = f"{subject}|{predicate}|{obj}|{payload_hint}"
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
    return f"NEXUS_EDGE:{digest}"


def _split_curie(value: str) -> tuple[str, str]:
    text = str(value or "").strip()
    if not text or ":" not in text:
        return "", ""
    prefix, local_id = text.split(":", 1)
    return prefix.strip().upper(), local_id.strip()


def _choose_canonical_disease_id(
    *,
    efo_id: str,
    mesh_id: str,
    unified_ns: str,
    fallback_ns_list: list[str],
    phenotype_prefixes: list[str],
) -> tuple[str, str, list[str]]:
    # Returns: (canonical_id, category, xrefs)
    candidates: list[str] = []
    efo_text = str(efo_id or "").strip()
    mesh_text = str(mesh_id or "").strip()

    if efo_text:
        candidates.append(efo_text)
    if mesh_text:
        candidates.append(mesh_text if ":" in mesh_text else f"MESH:{mesh_text}")

    if not candidates:
        return "", "", []

    normalized: list[str] = []
    for item in candidates:
        item_text = str(item).strip()
        if not item_text:
            continue
        if ":" not in item_text:
            item_text = f"MESH:{item_text}"
        normalized.append(item_text)

    phenotype_prefix_set = {str(p).strip().upper() for p in phenotype_prefixes if str(p).strip()}

    # If one candidate is phenotype (for example HP), keep it as phenotype and do not force-disease remap.
    for curie in normalized:
        prefix, _ = _split_curie(curie)
        if prefix in phenotype_prefix_set:
            return curie, "biolink:PhenotypicFeature", sorted(set(normalized))

    target_ns = str(unified_ns or "").strip().upper()
    if target_ns:
        for curie in normalized:
            prefix, _ = _split_curie(curie)
            if prefix == target_ns:
                return curie, "biolink:Disease", sorted(set(normalized))

    for backup_ns in fallback_ns_list:
        backup = str(backup_ns or "").strip().upper()
        if not backup:
            continue
        for curie in normalized:
            prefix, _ = _split_curie(curie)
            if prefix == backup:
                return curie, "biolink:Disease", sorted(set(normalized))

    # Fallback when no term exists in selected unified namespace.
    return normalized[0], "biolink:Disease", sorted(set(normalized))


def _batch(items: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    chunks: list[list[dict[str, Any]]] = []
    for i in range(0, len(items), size):
        chunks.append(items[i : i + size])
    return chunks


def _normalize(
    compounds: list[dict[str, Any]],
    *,
    indication_limit: int,
    mechanism_limit: int,
    cursor: sqlite3.Cursor,
    compound_prefix: str,
    target_prefix: str,
    unified_disease_namespace: str,
    fallback_disease_namespaces: list[str],
    phenotype_prefixes: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    nodes_by_id: dict[str, dict[str, Any]] = {}
    edges_by_id: dict[str, dict[str, Any]] = {}

    def upsert_node(node_id: str, name: str, category: str, payload: dict[str, Any]) -> None:
        if node_id not in nodes_by_id:
            nodes_by_id[node_id] = {
                "id": node_id,
                "name": name,
                "category": category,
                "provided_by": "chembl",
                "source_payload": json.dumps(payload, default=str),
            }

    for compound in compounds:
        chembl_id = str(compound.get("chembl_id", "")).strip()
        if not chembl_id:
            continue
        compound_id = f"{compound_prefix}:{chembl_id}"
        compound_name = str(compound.get("pref_name", "")).strip() or chembl_id

        upsert_node(
            compound_id,
            compound_name,
            "biolink:ChemicalEntity",
            {
                "chembl_id": chembl_id,
                "max_phase": compound.get("max_phase"),
                "therapeutic_flag": compound.get("therapeutic_flag"),
                "molecule_type": compound.get("molecule_type"),
            },
        )

        molregno = int(compound["molregno"])

        for ind in _indications(cursor, molregno=molregno, limit=indication_limit):
            disease_id = str(ind.get("efo_id") or ind.get("mesh_id") or "").strip()
            disease_name = str(ind.get("efo_term") or ind.get("mesh_heading") or "").strip()
            if not disease_id:
                continue

            did, object_category, xrefs = _choose_canonical_disease_id(
                efo_id=str(ind.get("efo_id") or ""),
                mesh_id=str(ind.get("mesh_id") or ""),
                unified_ns=unified_disease_namespace,
                fallback_ns_list=fallback_disease_namespaces,
                phenotype_prefixes=phenotype_prefixes,
            )
            if not did:
                continue

            upsert_node(
                did,
                disease_name or did,
                object_category,
                {
                    "efo_id": ind.get("efo_id"),
                    "efo_term": ind.get("efo_term"),
                    "mesh_id": ind.get("mesh_id"),
                    "mesh_heading": ind.get("mesh_heading"),
                    "max_phase_for_ind": ind.get("max_phase_for_ind"),
                    "xref": xrefs,
                },
            )

            eid = _edge_id(compound_id, "biolink:treats", did, f"indication:{did}")
            edges_by_id[eid] = {
                "id": eid,
                "subject": compound_id,
                "object": did,
                "predicate": "biolink:treats",
                "association": "biolink:ChemicalToDiseaseOrPhenotypicFeatureAssociation",
                "provided_by": "chembl",
                "source_payload": json.dumps(ind, default=str),
            }

        for mech in _mechanisms(cursor, molregno=molregno, limit=mechanism_limit):
            target_chembl_id = str(mech.get("target_chembl_id", "")).strip()
            if not target_chembl_id:
                continue
            target_id = f"{target_prefix}:{target_chembl_id}"
            target_name = str(mech.get("target_name", "")).strip() or target_chembl_id

            upsert_node(
                target_id,
                target_name,
                "biolink:Protein",
                {
                    "target_chembl_id": target_chembl_id,
                    "organism": mech.get("target_organism"),
                    "action_type": mech.get("action_type"),
                },
            )

            predicate = "biolink:affects"
            eid = _edge_id(compound_id, predicate, target_id, f"mechanism:{target_id}")
            edges_by_id[eid] = {
                "id": eid,
                "subject": compound_id,
                "object": target_id,
                "predicate": predicate,
                "association": "biolink:ChemicalToGeneAssociation",
                "provided_by": "chembl",
                "source_payload": json.dumps(mech, default=str),
            }

    return list(nodes_by_id.values()), list(edges_by_id.values())


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


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert ChEMBL SQLite data to Biolink-shaped Nexus KG nodes/edges.")
    parser.add_argument("--config", default="configs/kg_build/chembl_sqlite_to_nexus.yaml", help="YAML config path.")
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = PROJECT_ROOT / config_path

    cfg = load_yaml_config(config_path)

    source_cfg = cfg.get("source_sqlite", {}) if isinstance(cfg, dict) else {}
    nexus_cfg = cfg.get("nexus_connection", {}) if isinstance(cfg, dict) else {}
    output_cfg = cfg.get("output", {}) if isinstance(cfg, dict) else {}

    if not isinstance(source_cfg, dict) or not isinstance(nexus_cfg, dict):
        raise ValueError("source_sqlite and nexus_connection sections are required and must be mappings.")

    namespace_policy_path_value = str(source_cfg.get("namespace_policy_path", "namespace.yaml")).strip()
    namespace_policy_path = Path(namespace_policy_path_value)
    if not namespace_policy_path.is_absolute():
        namespace_policy_path = PROJECT_ROOT / namespace_policy_path

    namespace_policy: dict[str, Any] = {}
    if namespace_policy_path.exists():
        loaded_policy = load_yaml_config(namespace_policy_path)
        if isinstance(loaded_policy, dict):
            namespace_policy = loaded_policy

    namespaces_cfg = namespace_policy.get("namespaces", {}) if isinstance(namespace_policy, dict) else {}
    if not isinstance(namespaces_cfg, dict):
        namespaces_cfg = {}

    compound_ns_cfg = namespaces_cfg.get("compound", {}) if isinstance(namespaces_cfg, dict) else {}
    target_ns_cfg = namespaces_cfg.get("target", {}) if isinstance(namespaces_cfg, dict) else {}
    disease_ns_cfg = namespaces_cfg.get("disease", {}) if isinstance(namespaces_cfg, dict) else {}
    phenotype_ns_cfg = namespaces_cfg.get("phenotype", {}) if isinstance(namespaces_cfg, dict) else {}
    if not isinstance(compound_ns_cfg, dict):
        compound_ns_cfg = {}
    if not isinstance(target_ns_cfg, dict):
        target_ns_cfg = {}
    if not isinstance(disease_ns_cfg, dict):
        disease_ns_cfg = {}
    if not isinstance(phenotype_ns_cfg, dict):
        phenotype_ns_cfg = {}

    db_path_value = str(source_cfg.get("db_path", "")).strip()
    query = str(source_cfg.get("query", "fluoxetine")).strip() or "fluoxetine"
    compound_limit = int(source_cfg.get("compound_limit", 100))
    indication_limit = int(source_cfg.get("indication_limit", 100))
    mechanism_limit = int(source_cfg.get("mechanism_limit", 100))
    compound_prefix = str(source_cfg.get("compound_prefix", "")).strip() or str(
        compound_ns_cfg.get("canonical_prefix", "CHEMBL.COMPOUND")
    ).strip() or "CHEMBL.COMPOUND"
    target_prefix = str(source_cfg.get("target_prefix", "")).strip() or str(
        target_ns_cfg.get("canonical_prefix", "CHEMBL.TARGET")
    ).strip() or "CHEMBL.TARGET"
    unified_disease_namespace = str(source_cfg.get("unified_disease_namespace", "")).strip() or str(
        disease_ns_cfg.get("canonical_prefix", "MONDO")
    ).strip() or "MONDO"

    fallback_disease_namespaces_cfg = source_cfg.get("fallback_disease_namespaces", None)
    if isinstance(fallback_disease_namespaces_cfg, list):
        fallback_disease_namespaces = [str(v).strip() for v in fallback_disease_namespaces_cfg if str(v).strip()]
    else:
        legacy_fallback = str(source_cfg.get("fallback_disease_namespace", "")).strip()
        if legacy_fallback:
            fallback_disease_namespaces = [legacy_fallback]
        else:
            policy_fallback = disease_ns_cfg.get("fallback_prefixes", ["MESH"])
            if isinstance(policy_fallback, list):
                fallback_disease_namespaces = [str(v).strip() for v in policy_fallback if str(v).strip()]
            else:
                fallback_disease_namespaces = [str(policy_fallback).strip()] if str(policy_fallback).strip() else ["MESH"]

    phenotype_prefixes_cfg = phenotype_ns_cfg.get("recognized_prefixes", ["HP"])
    if isinstance(phenotype_prefixes_cfg, list):
        phenotype_prefixes = [str(v).strip() for v in phenotype_prefixes_cfg if str(v).strip()]
    else:
        phenotype_prefixes = [str(phenotype_prefixes_cfg).strip()] if str(phenotype_prefixes_cfg).strip() else ["HP"]

    batch_size = int(source_cfg.get("batch_size", 500))
    dry_run = _as_bool(source_cfg.get("dry_run", True), default=True)
    sample_size = int(source_cfg.get("sample_size", 5))

    if not db_path_value:
        raise ValueError("source_sqlite.db_path is required.")
    db_path = Path(db_path_value)
    if not db_path.is_absolute():
        db_path = PROJECT_ROOT / db_path
    if not db_path.exists():
        raise FileNotFoundError(f"SQLite DB file not found: {db_path}")

    uri = str(nexus_cfg.get("uri", "")).strip()
    database = str(nexus_cfg.get("database", "neo4j")).strip() or "neo4j"
    user = str(nexus_cfg.get("user", "neo4j")).strip() or "neo4j"
    require_auth = _as_bool(nexus_cfg.get("require_auth", True), default=True)
    password_env = str(nexus_cfg.get("password_env", "NEO4J_PASSWORD")).strip() or "NEO4J_PASSWORD"
    password = str(nexus_cfg.get("password", "")).strip() or str(os.environ.get(password_env, "")).strip()

    if not dry_run:
        if not uri:
            raise ValueError("nexus_connection.uri is required.")
        if require_auth and not password:
            raise ValueError(f"Password required via nexus_connection.password or env var {password_env}.")

    with sqlite3.connect(db_path) as conn:
        cur = conn.cursor()
        compounds = _find_compounds(cur, query=query, limit=compound_limit)
        nodes, edges = _normalize(
            compounds,
            indication_limit=indication_limit,
            mechanism_limit=mechanism_limit,
            cursor=cur,
            compound_prefix=compound_prefix,
            target_prefix=target_prefix,
            unified_disease_namespace=unified_disease_namespace,
            fallback_disease_namespaces=fallback_disease_namespaces,
            phenotype_prefixes=phenotype_prefixes,
        )

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
            "db_path": str(db_path),
            "query": query,
            "compound_limit": compound_limit,
            "indication_limit": indication_limit,
            "mechanism_limit": mechanism_limit,
            "namespace_policy_path": str(namespace_policy_path),
            "compound_prefix": compound_prefix,
            "target_prefix": target_prefix,
            "unified_disease_namespace": unified_disease_namespace,
            "fallback_disease_namespaces": fallback_disease_namespaces,
            "phenotype_prefixes": phenotype_prefixes,
        },
        "nexus_connection": {
            "uri": uri,
            "database": database,
            "user": user,
        },
        "counts": {
            "matched_compounds": len(compounds),
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
