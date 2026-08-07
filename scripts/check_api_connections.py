from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from api import AnduinAPIClient


@dataclass
class ProbeResult:
    source: str
    ok: bool
    check: str
    detail: str
    elapsed_ms: int


def _probe_url(url: str, timeout: int) -> tuple[bool, str]:
    req = Request(url=url, headers={"user-agent": "anduin-core-api-check/1.0", "accept": "application/json"})
    try:
        with urlopen(req, timeout=timeout) as resp:  # noqa: S310
            _ = resp.read(1)
            return True, f"http_{resp.status}"
    except urllib.error.HTTPError as exc:
        # HTTP errors still prove network reachability + endpoint response.
        return True, f"http_{exc.code}"
    except Exception as exc:  # noqa: BLE001
        return False, f"network_error: {exc}"


def _run_direct_check(client: AnduinAPIClient, source: str) -> tuple[bool, str]:
    if source == "lincs_dcic":
        out = client.execute("lincs_dcic", "drug_cell_assay_map", drug="Trametinib", limit=1)
        if isinstance(out, dict) and "hit_count" in out:
            return True, f"hit_count={out.get('hit_count')}"
        return False, "unexpected response shape"

    if source == "ilincs":
        out = client.execute("ilincs", "search_datasets", term="trametinib", lincs_only=True)
        if isinstance(out, list):
            return True, f"records={len(out)}"
        return False, "unexpected response shape"

    if source == "chembl":
        out = client.execute("chembl", "search_molecules", query="aspirin", limit=1)
        if isinstance(out, list):
            return True, f"records={len(out)}"
        return False, "unexpected response shape"

    if source == "pubchem":
        out = client.execute("pubchem", "compound_properties_by_name", name="aspirin")
        if isinstance(out, list):
            return True, f"records={len(out)}"
        return False, "unexpected response shape"

    if source == "uniprot":
        out = client.execute("uniprot", "search_uniprotkb", query="TP53", size=1)
        if isinstance(out, dict):
            return True, f"keys={len(out.keys())}"
        return False, "unexpected response shape"

    if source == "opentargets":
        out = client.execute("opentargets", "search", query="aspirin", entity="drug", size=1)
        if isinstance(out, dict):
            return True, f"keys={len(out.keys())}"
        return False, "unexpected response shape"

    return False, "no direct check implemented"


def _try_probe_source(client: AnduinAPIClient, source: str, timeout: int) -> ProbeResult:
    started = time.time()
    check = ""
    detail = ""
    ok = False

    try:
        info = next((s for s in client.list_sources() if s.name == source), None)
        if info is None:
            raise KeyError(f"source '{source}' not found")

        if source in {"lincs_dcic", "ilincs", "chembl", "pubchem", "uniprot", "opentargets"}:
            check = "direct_operation"
            ok, detail = _run_direct_check(client, source)
        else:
            # Generic adapters expose base URL via describe.
            check = "base_url_probe"
            desc = client.execute(source, "describe")
            base_url = str(desc.get("base_url", ""))
            if not base_url:
                raise ValueError("missing base_url in describe response")
            ok, detail = _probe_url(base_url, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        ok = False
        detail = f"error: {exc}"
        if not check:
            check = "exception"

    elapsed_ms = int((time.time() - started) * 1000)
    return ProbeResult(source=source, ok=ok, check=check, detail=detail, elapsed_ms=elapsed_ms)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Probe and validate anduin-core API adapter connectivity.")
    parser.add_argument(
        "--sources",
        default="all",
        help="Comma-separated sources to test, or 'all' (default).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=10,
        help="Network timeout in seconds for URL probes (default: 10).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print full results as JSON.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    client = AnduinAPIClient()

    all_sources = [src.name for src in client.list_sources()]
    if args.sources.strip().lower() == "all":
        selected = all_sources
    else:
        selected = [s.strip() for s in args.sources.split(",") if s.strip()]

    results = [_try_probe_source(client, source, timeout=args.timeout) for source in selected]

    passed = sum(1 for r in results if r.ok)
    failed = len(results) - passed

    if args.json:
        print(
            json.dumps(
                {
                    "total": len(results),
                    "passed": passed,
                    "failed": failed,
                    "results": [r.__dict__ for r in results],
                },
                indent=2,
            )
        )
    else:
        for r in results:
            status = "PASS" if r.ok else "FAIL"
            print(f"[{status}] {r.source} | {r.check} | {r.detail} | {r.elapsed_ms}ms")
        print(f"\nSummary: total={len(results)} passed={passed} failed={failed}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
