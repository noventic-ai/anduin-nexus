from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from common.config import load_yaml_config


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


def _activity_summary(cursor: sqlite3.Cursor, molregno: int, top_n: int) -> dict[str, Any]:
    count_sql = "SELECT COUNT(*) AS n FROM activities WHERE molregno = ?"
    cursor.execute(count_sql, (molregno,))
    total = int(cursor.fetchone()[0])

    top_sql = """
    SELECT
        standard_type,
        standard_units,
        COUNT(*) AS row_count,
        AVG(pchembl_value) AS mean_pchembl,
        MIN(standard_value) AS min_standard_value,
        MAX(standard_value) AS max_standard_value
    FROM activities
    WHERE molregno = ?
      AND standard_type IS NOT NULL
      AND standard_value IS NOT NULL
    GROUP BY standard_type, standard_units
    ORDER BY row_count DESC
    LIMIT ?
    """
    top_measurements = _fetchall_dict(cursor, top_sql, (molregno, top_n))

    return {
        "total_activity_rows": total,
        "top_measurements": top_measurements,
    }


def run_query(
    db_path: Path,
    query: str,
    compound_limit: int,
    indication_limit: int,
    mechanism_limit: int,
    activity_top_n: int,
) -> dict[str, Any]:
    connection = sqlite3.connect(db_path)
    try:
        cursor = connection.cursor()
        compounds = _find_compounds(cursor, query=query, limit=compound_limit)

        for compound in compounds:
            molregno = int(compound["molregno"])
            compound["indications"] = _indications(cursor, molregno=molregno, limit=indication_limit)
            compound["mechanisms"] = _mechanisms(cursor, molregno=molregno, limit=mechanism_limit)
            compound["activity_summary"] = _activity_summary(cursor, molregno=molregno, top_n=activity_top_n)

        return {
            "database": str(db_path),
            "query": query,
            "match_count": len(compounds),
            "compounds": compounds,
        }
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Simple local ChEMBL SQLite query runner for KG extraction bootstrapping.")
    parser.add_argument("--config", default="", help="Optional YAML config path.")
    parser.add_argument("--db-path", default="", help="Path to ChEMBL SQLite database (e.g. chembl_37.db).")
    parser.add_argument("--query", default="", help="Prefix query for molecule_dictionary.pref_name.")
    parser.add_argument("--compound-limit", type=int, default=0, help="Maximum compounds to return.")
    parser.add_argument("--indication-limit", type=int, default=0, help="Maximum indications per compound.")
    parser.add_argument("--mechanism-limit", type=int, default=0, help="Maximum mechanisms per compound.")
    parser.add_argument("--activity-top-n", type=int, default=0, help="Top grouped activity measurements per compound.")
    parser.add_argument("--output", default="", help="Optional output path for JSON payload.")
    args = parser.parse_args()

    cfg: dict[str, Any] = {}
    if args.config:
        config_path = Path(args.config)
        if not config_path.is_absolute():
            config_path = PROJECT_ROOT / config_path
        cfg = load_yaml_config(config_path)

    db_path_value = args.db_path or str(cfg.get("db_path", ""))
    query_value = args.query or str(cfg.get("query", "fluoxetine"))
    compound_limit_value = args.compound_limit if args.compound_limit > 0 else int(cfg.get("compound_limit", 5))
    indication_limit_value = args.indication_limit if args.indication_limit > 0 else int(cfg.get("indication_limit", 20))
    mechanism_limit_value = args.mechanism_limit if args.mechanism_limit > 0 else int(cfg.get("mechanism_limit", 20))
    activity_top_n_value = args.activity_top_n if args.activity_top_n > 0 else int(cfg.get("activity_top_n", 10))
    output_value = args.output or str(cfg.get("output", ""))

    if not db_path_value:
        raise ValueError("db_path is required (via --db-path or config).")

    db_path = Path(db_path_value)
    if not db_path.is_absolute():
        db_path = PROJECT_ROOT / db_path

    if not db_path.exists():
        raise FileNotFoundError(f"Database file not found: {db_path}")

    payload = run_query(
        db_path=db_path,
        query=query_value,
        compound_limit=compound_limit_value,
        indication_limit=indication_limit_value,
        mechanism_limit=mechanism_limit_value,
        activity_top_n=activity_top_n_value,
    )

    rendered = json.dumps(payload, indent=2, default=str)
    print(rendered)

    if output_value:
        output_path = Path(output_value)
        if not output_path.is_absolute():
            output_path = PROJECT_ROOT / output_path
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
