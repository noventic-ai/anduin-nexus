from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from api import AnduinAPIClient


def main() -> None:
    parser = argparse.ArgumentParser(description="Unified API interface runner for anduin-core.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list-sources", help="List available first-party API source adapters.")
    _ = p_list

    p_map = sub.add_parser("lincs-drug-map", help="Get LINCS cell-line to assay mapping for a drug.")
    p_map.add_argument("--drug", required=True, help="Drug/perturbagen name, e.g. Trametinib.")
    p_map.add_argument("--limit", type=int, default=10, help="Maximum molecule hits from fetchmolecules.")

    p_sig = sub.add_parser("lincs-signatures", help="Get iLINCS signatures for a LINCS perturbagen ID.")
    p_sig.add_argument("--lincs-id", required=True, help="LINCS perturbagen ID, e.g. LSM-1143.")

    p_ops = sub.add_parser("source-ops", help="List operations for one source adapter.")
    p_ops.add_argument("--source", required=True, help="Source name from list-sources.")

    p_run = sub.add_parser("run", help="Run a source operation with JSON parameters.")
    p_run.add_argument("--source", required=True, help="Source name from list-sources.")
    p_run.add_argument("--operation", required=True, help="Operation name from source-ops.")
    p_run.add_argument(
        "--params-json",
        default="{}",
        help='JSON object for operation kwargs, e.g. {"query":"trametinib","limit":5}',
    )

    args = parser.parse_args()
    client = AnduinAPIClient()

    if args.command == "list-sources":
        for src in client.list_sources():
            print(f"{src.name}\t{src.description}\toperations={','.join(src.operations)}")
        return

    if args.command == "lincs-drug-map":
        payload = client.lincs_drug_cell_assay_map(drug=args.drug, limit=args.limit)
        print(json.dumps(payload, indent=2))
        return

    if args.command == "lincs-signatures":
        payload = client.lincs_search_signatures(lincs_pert_id=args.lincs_id)
        print(json.dumps(payload, indent=2, default=str))
        return

    if args.command == "source-ops":
        sources = {src.name: src for src in client.list_sources()}
        if args.source not in sources:
            raise ValueError(f"Unknown source '{args.source}'.")
        src = sources[args.source]
        print(json.dumps({"source": src.name, "operations": src.operations}, indent=2))
        return

    if args.command == "run":
        params = json.loads(args.params_json)
        if not isinstance(params, dict):
            raise ValueError("--params-json must decode to a JSON object")
        out = client.execute(source=args.source, operation=args.operation, **params)
        print(json.dumps(out, indent=2, default=str))
        return

    raise ValueError(f"Unsupported command: {args.command}")


if __name__ == "__main__":
    main()
