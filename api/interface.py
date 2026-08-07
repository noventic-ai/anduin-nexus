from __future__ import annotations

import dataclasses
import json
import ssl
import subprocess
import time
import urllib.error
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


class APIRequestError(RuntimeError):
    """Raised when an API request fails after retries."""


@dataclasses.dataclass(frozen=True)
class SourceInfo:
    """Metadata for a source adapter."""

    name: str
    description: str
    operations: list[str]


class HTTPJSONClient:
    """Dependency-free HTTP client with JSON helpers and retry/backoff."""

    def __init__(
        self,
        timeout: int = 60,
        retries: int = 3,
        backoff_seconds: float = 1.0,
        ca_bundle: Path | None = None,
    ) -> None:
        self.timeout = timeout
        self.retries = retries
        self.backoff_seconds = backoff_seconds
        self.ca_bundle = ca_bundle

    def get_json(
        self,
        url: str,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        request_url = url
        if params:
            request_url = f"{url}?{urlencode(params, doseq=True)}"
        return self._request_json("GET", request_url, headers=headers, body=None)

    def post_json(
        self,
        url: str,
        data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        body = None if data is None else urlencode({k: str(v) for k, v in data.items()}).encode("utf-8")
        return self._request_json("POST", url, headers=headers, body=body)

    def _request_json(
        self,
        method: str,
        request_url: str,
        headers: dict[str, str] | None,
        body: bytes | None,
    ) -> Any:
        req_headers = {
            "accept": "application/json",
            "user-agent": "anduin-core/1.0",
        }
        if headers:
            req_headers.update(headers)

        request = Request(url=request_url, headers=req_headers, data=body, method=method)
        ssl_context = ssl.create_default_context(cafile=str(self.ca_bundle)) if self.ca_bundle else None

        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                with urlopen(request, context=ssl_context, timeout=self.timeout) as response:  # noqa: S310
                    payload = response.read()
                text = self._decode_bytes(payload)
                return json.loads(text)
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt == self.retries:
                    break
                time.sleep(self.backoff_seconds * attempt)

        raise APIRequestError(f"{method} {request_url} failed after {self.retries} attempts: {last_error}")

    @staticmethod
    def _decode_bytes(raw: bytes) -> str:
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.decode("latin-1")


class LincsCertManager:
    """Build a local CA bundle for LINCS DCIC certificate chain quirks."""

    def __init__(self, cert_dir: Path) -> None:
        self.cert_dir = cert_dir

    def ensure_bundle(self) -> Path:
        self.cert_dir.mkdir(parents=True, exist_ok=True)
        intermediate_der = self.cert_dir / "InCommonRSAOVSSLCA3.crt"
        intermediate_pem = self.cert_dir / "InCommonRSAOVSSLCA3.pem"
        bundle = self.cert_dir / "curl-ca-bundle.pem"

        if not intermediate_pem.exists() or intermediate_pem.stat().st_size == 0:
            from urllib.request import urlopen as _urlopen

            with _urlopen("http://crt.sectigo.com/InCommonRSAOVSSLCA3.crt", timeout=30) as response:  # noqa: S310
                intermediate_der.write_bytes(response.read())
            subprocess.run(
                [
                    "openssl",
                    "x509",
                    "-inform",
                    "der",
                    "-in",
                    str(intermediate_der),
                    "-out",
                    str(intermediate_pem),
                ],
                check=True,
                capture_output=True,
                text=True,
            )

        system_ca = Path("/etc/ssl/certs/ca-certificates.crt")
        if not system_ca.exists():
            raise FileNotFoundError(f"System CA bundle not found at {system_ca}")

        bundle.write_text(
            system_ca.read_text(encoding="utf-8") + intermediate_pem.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        return bundle


class AdapterBase:
    """Base class for source adapters."""

    source_name: str
    description: str

    def operations(self) -> dict[str, Callable[..., Any]]:
        raise NotImplementedError


class GenericRESTAdapter(AdapterBase):
    """Generic REST adapter generated from endpoint catalog metadata."""

    def __init__(self, source_name: str, description: str, base_url: str) -> None:
        self.source_name = source_name
        self.description = description
        self.base_url = base_url.rstrip("/")
        self._client = HTTPJSONClient()

    def operations(self) -> dict[str, Callable[..., Any]]:
        return {
            "describe": self.describe,
            "get_json": self.get_json,
            "post_json": self.post_json,
        }

    def describe(self) -> dict[str, Any]:
        return {
            "source": self.source_name,
            "description": self.description,
            "base_url": self.base_url,
            "operations": sorted(self.operations().keys()),
        }

    def get_json(
        self,
        path: str = "",
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        url = f"{self.base_url}/{path.lstrip('/')}" if path else self.base_url
        return self._client.get_json(url, params=params, headers=headers)

    def post_json(
        self,
        path: str = "",
        data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        url = f"{self.base_url}/{path.lstrip('/')}" if path else self.base_url
        return self._client.post_json(url, data=data, headers=headers)


class LincsDCICAdapter(AdapterBase):
    """Direct LINCS DCIC adapter for drug/cell/assay workflows."""

    source_name = "lincs_dcic"
    description = "LINCS DCIC API (fetchmolecules/fetchdata)"

    def __init__(self, cert_manager: LincsCertManager) -> None:
        self._cert_manager = cert_manager

    def operations(self) -> dict[str, Callable[..., Any]]:
        return {
            "drug_cell_assay_map": self.drug_cell_assay_map,
            "dataset_records_for_cell_assay": self.dataset_records_for_cell_assay,
            "signatures_by_drug_dcic": self.signatures_by_drug_dcic,
        }

    def signatures_by_drug_dcic(
        self,
        drug: str,
        molecule_limit: int = 10,
        max_lincs_ids: int = 10,
        fetchdata_limit: int = 10000,
        max_records_per_id: int = 0,
        include_empty: bool = False,
    ) -> dict[str, Any]:
        """Return signature-like records from main LINCS DCIC fetchdata."""
        if not str(drug).strip():
            raise ValueError("drug is required")
        if molecule_limit < 1:
            raise ValueError("molecule_limit must be >= 1")
        if max_lincs_ids < 1:
            raise ValueError("max_lincs_ids must be >= 1")
        if fetchdata_limit < 1:
            raise ValueError("fetchdata_limit must be >= 1")
        if max_records_per_id < 0:
            raise ValueError("max_records_per_id must be >= 0")

        client = HTTPJSONClient(ca_bundle=self._cert_manager.ensure_bundle())
        molecules_payload = client.get_json(
            "https://lincsportal.ccs.miami.edu/dcic/api/fetchmolecules",
            params={"searchTerm": f"Name:{drug}", "limit": str(molecule_limit)},
        )
        docs = self._extract_documents(molecules_payload)

        seen: set[str] = set()
        lincs_ids: list[str] = []
        for doc in docs:
            lincs_id = str(doc.get("lincsidentifier") or doc.get("entityId") or "").strip()
            if not lincs_id or lincs_id in seen:
                continue
            seen.add(lincs_id)
            lincs_ids.append(lincs_id)
        lincs_ids = lincs_ids[:max_lincs_ids]

        groups: list[dict[str, Any]] = []
        total_records = 0
        for lincs_id in lincs_ids:
            fetchdata_payload = client.get_json(
                "https://lincsportal.ccs.miami.edu/dcic/api/fetchdata",
                params={"searchTerm": lincs_id, "limit": str(fetchdata_limit)},
            )
            records = self._extract_documents(fetchdata_payload)
            if max_records_per_id > 0:
                records = records[:max_records_per_id]

            record_count = len(records)
            total_records += record_count
            if record_count > 0 or include_empty:
                groups.append(
                    {
                        "lincs_id": lincs_id,
                        "record_count": record_count,
                        "records": records,
                    }
                )

        return {
            "adapter": "lincs",
            "operation": "signatures_by_drug_dcic",
            "source_database": "lincs_dcic",
            "query": {
                "drug": drug,
                "molecule_limit": molecule_limit,
                "max_lincs_ids": max_lincs_ids,
                "fetchdata_limit": fetchdata_limit,
                "max_records_per_id": max_records_per_id,
                "include_empty": include_empty,
            },
            "molecule_hit_count": len(docs),
            "matched_lincs_ids": lincs_ids,
            "record_group_count": len(groups),
            "total_record_count": total_records,
            "results": groups,
        }

    def drug_cell_assay_map(self, drug: str, limit: int = 10) -> dict[str, Any]:
        client = HTTPJSONClient(ca_bundle=self._cert_manager.ensure_bundle())
        molecules_payload = client.get_json(
            "https://lincsportal.ccs.miami.edu/dcic/api/fetchmolecules",
            params={"searchTerm": f"Name:{drug}", "limit": str(limit)},
        )

        docs = self._extract_documents(molecules_payload)
        if not docs:
            return {
                "drug": drug,
                "hit_count": 0,
                "molecules": [],
                "record_count": 0,
                "pair_count": 0,
                "cell_assays": [],
            }

        cell_to_assays: dict[str, set[str]] = {}
        record_ids: set[str] = set()

        for doc in docs:
            lincs_id = str(doc.get("lincsidentifier") or doc.get("entityId") or "").strip()
            if not lincs_id:
                continue

            fetchdata_payload = client.get_json(
                "https://lincsportal.ccs.miami.edu/dcic/api/fetchdata",
                params={"searchTerm": lincs_id, "limit": "10000"},
            )
            recs = self._extract_documents(fetchdata_payload)

            for rec in recs:
                rec_id = rec.get("id")
                if rec_id is not None:
                    record_ids.add(str(rec_id))

                cells = self._normalize_multi(rec.get("cellline"))
                assays = self._normalize_multi(rec.get("assayname"))
                if not cells or not assays:
                    continue
                for cell in cells:
                    cell_to_assays.setdefault(cell, set()).update(assays)

        molecules = [
            {
                "name": doc.get("Name"),
                "lincs_id": doc.get("lincsidentifier") or doc.get("entityId"),
                "dataset_count": doc.get("dataset_count"),
                "assay_count": len(self._normalize_multi(doc.get("assays"))),
                "cell_line_count": len(self._normalize_multi(doc.get("cells"))),
                "centers": self._normalize_multi(doc.get("centers")),
                "subject_areas": self._normalize_multi(doc.get("subject_area")),
            }
            for doc in docs
        ]

        cell_assays = [
            {"cell_line": cell, "assays": sorted(assays)}
            for cell, assays in sorted(cell_to_assays.items(), key=lambda kv: kv[0])
        ]

        return {
            "drug": drug,
            "hit_count": len(docs),
            "molecules": molecules,
            "record_count": len(record_ids),
            "pair_count": int(sum(len(v) for v in cell_to_assays.values())),
            "cell_assays": cell_assays,
        }

    def dataset_records_for_cell_assay(
        self,
        lincs_id: str,
        cell_line: str,
        assay: str,
    ) -> list[dict[str, Any]]:
        client = HTTPJSONClient(ca_bundle=self._cert_manager.ensure_bundle())
        payload = client.get_json(
            "https://lincsportal.ccs.miami.edu/dcic/api/fetchdata",
            params={"searchTerm": lincs_id, "limit": "10000"},
        )
        recs = self._extract_documents(payload)

        out: list[dict[str, Any]] = []
        for rec in recs:
            cells = set(self._normalize_multi(rec.get("cellline")))
            assays = set(self._normalize_multi(rec.get("assayname")))
            if cell_line in cells and assay in assays:
                out.append(
                    {
                        "id": rec.get("id"),
                        "datasetid": rec.get("datasetid"),
                        "datasetname": rec.get("datasetname"),
                        "ldplink": rec.get("ldplink"),
                        "datalevels": rec.get("datalevels"),
                        "latestversions": rec.get("latestversions"),
                    }
                )
        return out

    @staticmethod
    def _extract_documents(payload: Any) -> list[dict[str, Any]]:
        if not isinstance(payload, dict):
            return []
        result = payload.get("results", {})
        if not isinstance(result, dict):
            return []
        docs = result.get("documents", [])
        return docs if isinstance(docs, list) else []

    @staticmethod
    def _normalize_multi(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, list):
            return [str(x).strip() for x in value if str(x).strip()]
        text = str(value).strip()
        if not text:
            return []
        if ";" in text:
            return [x.strip() for x in text.split(";") if x.strip()]
        return [text]


class ILincsAdapter(AdapterBase):
    """Direct iLINCS adapter for signatures and datasets."""

    source_name = "ilincs"
    description = "iLINCS public API"

    def __init__(self) -> None:
        self._client = HTTPJSONClient()

    def operations(self) -> dict[str, Callable[..., Any]]:
        return {
            "search_signatures": self.search_signatures,
            "search_datasets": self.search_datasets,
        }

    def search_signatures(self, lincs_pert_id: str) -> list[dict[str, Any]]:
        filter_obj = {"where": {"lincspertid": lincs_pert_id}}
        payload = self._client.get_json(
            "https://www.ilincs.org/api/SignatureMeta",
            params={"filter": json.dumps(filter_obj, separators=(",", ":"))},
        )
        return payload if isinstance(payload, list) else []

    def search_datasets(self, term: str, lincs_only: bool = True) -> list[dict[str, Any]]:
        payload = self._client.post_json(
            "https://www.ilincs.org/api/PublicDatasets/findTermMeta",
            data={"term": term, "lincs": "true" if lincs_only else "false"},
        )
        if not isinstance(payload, dict):
            return []
        data = payload.get("data", [])
        return data if isinstance(data, list) else []


class LincsAdapter(AdapterBase):
    """Composite LINCS adapter that encapsulates LINCS-specific workflows."""

    source_name = "lincs"
    description = "LINCS composite adapter (DCIC + iLINCS workflows)"

    def __init__(self, cert_manager: LincsCertManager) -> None:
        self._dcic = LincsDCICAdapter(cert_manager)
        self._ilincs = ILincsAdapter()

    def operations(self) -> dict[str, Callable[..., Any]]:
        return {
            "drug_cell_assay_map": self._dcic.drug_cell_assay_map,
            "dataset_records_for_cell_assay": self._dcic.dataset_records_for_cell_assay,
            "signatures_by_drug_dcic": self._dcic.signatures_by_drug_dcic,
            "search_signatures": self._ilincs.search_signatures,
            "search_datasets": self._ilincs.search_datasets,
            "signatures_by_drug": self.signatures_by_drug,
        }

    def signatures_by_drug(
        self,
        drug: str,
        molecule_limit: int = 10,
        max_lincs_ids: int = 10,
        max_signatures_per_id: int = 0,
        include_empty: bool = False,
    ) -> dict[str, Any]:
        if not str(drug).strip():
            raise ValueError("drug is required")
        if molecule_limit < 1:
            raise ValueError("molecule_limit must be >= 1")
        if max_lincs_ids < 1:
            raise ValueError("max_lincs_ids must be >= 1")
        if max_signatures_per_id < 0:
            raise ValueError("max_signatures_per_id must be >= 0")

        drug_map = self._dcic.drug_cell_assay_map(drug=drug, limit=molecule_limit)
        molecules = drug_map.get("molecules", []) if isinstance(drug_map, dict) else []
        if not isinstance(molecules, list):
            molecules = []

        seen: set[str] = set()
        lincs_ids: list[str] = []
        for molecule in molecules:
            if not isinstance(molecule, dict):
                continue
            lincs_id = str(molecule.get("lincs_id", "")).strip()
            if not lincs_id or lincs_id in seen:
                continue
            seen.add(lincs_id)
            lincs_ids.append(lincs_id)

        lincs_ids = lincs_ids[:max_lincs_ids]
        signature_groups: list[dict[str, Any]] = []
        for lincs_id in lincs_ids:
            signatures = self._ilincs.search_signatures(lincs_pert_id=lincs_id)
            if not isinstance(signatures, list):
                signatures = []
            if max_signatures_per_id > 0:
                signatures = signatures[:max_signatures_per_id]

            if signatures or include_empty:
                signature_groups.append(
                    {
                        "lincs_id": lincs_id,
                        "signature_count": len(signatures),
                        "signatures": signatures,
                    }
                )

        return {
            "adapter": self.source_name,
            "operation": "signatures_by_drug",
            "query": {
                "drug": drug,
                "molecule_limit": molecule_limit,
                "max_lincs_ids": max_lincs_ids,
                "max_signatures_per_id": max_signatures_per_id,
                "include_empty": include_empty,
            },
            "molecule_hit_count": int(drug_map.get("hit_count", 0) or 0) if isinstance(drug_map, dict) else 0,
            "matched_lincs_ids": lincs_ids,
            "signature_group_count": len(signature_groups),
            "total_signature_count": sum(group["signature_count"] for group in signature_groups),
            "results": signature_groups,
        }


class ChEMBLAdapter(AdapterBase):
    """Direct ChEMBL adapter."""

    source_name = "chembl"
    description = "EMBL-EBI ChEMBL REST API"

    def __init__(self) -> None:
        self._client = HTTPJSONClient()

    def operations(self) -> dict[str, Callable[..., Any]]:
        return {"search_molecules": self.search_molecules}

    def search_molecules(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        payload = self._client.get_json(
            "https://www.ebi.ac.uk/chembl/api/data/molecule/search",
            params={"q": query, "limit": str(limit), "format": "json"},
        )
        if not isinstance(payload, dict):
            return []
        molecules = payload.get("molecules", [])
        return molecules if isinstance(molecules, list) else []


class PubChemAdapter(AdapterBase):
    """Direct PubChem adapter."""

    source_name = "pubchem"
    description = "PubChem PUG REST API"

    def __init__(self) -> None:
        self._client = HTTPJSONClient()

    def operations(self) -> dict[str, Callable[..., Any]]:
        return {"compound_properties_by_name": self.compound_properties_by_name}

    def compound_properties_by_name(self, name: str) -> list[dict[str, Any]]:
        encoded = quote(name, safe="")
        payload = self._client.get_json(
            f"https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/{encoded}/property/Title,CanonicalSMILES,IsomericSMILES,InChIKey,MolecularFormula,MolecularWeight/JSON"
        )
        if not isinstance(payload, dict):
            return []
        props = payload.get("PropertyTable", {}).get("Properties", [])
        return props if isinstance(props, list) else []


class UniProtAdapter(AdapterBase):
    """Direct UniProt adapter."""

    source_name = "uniprot"
    description = "UniProt REST API"

    def __init__(self) -> None:
        self._client = HTTPJSONClient()

    def operations(self) -> dict[str, Callable[..., Any]]:
        return {"search_uniprotkb": self.search_uniprotkb}

    def search_uniprotkb(self, query: str, size: int = 25) -> dict[str, Any]:
        payload = self._client.get_json(
            "https://rest.uniprot.org/uniprotkb/search",
            params={"query": query, "format": "json", "size": str(size)},
        )
        return payload if isinstance(payload, dict) else {}


class OpenTargetsAdapter(AdapterBase):
    """Direct Open Targets adapter."""

    source_name = "opentargets"
    description = "Open Targets Platform API"

    def __init__(self) -> None:
        self._client = HTTPJSONClient()

    def operations(self) -> dict[str, Callable[..., Any]]:
        return {"search": self.search}

    def search(self, query: str, entity: str = "drug", size: int = 25) -> dict[str, Any]:
        payload = self._client.get_json(
            "https://platform-api.opentargets.org/api/v4/search",
            params={"q": query, "entity": entity, "size": str(size)},
        )
        return payload if isinstance(payload, dict) else {}


BIOCLIENTS_REFERENCE_ENDPOINTS: dict[str, tuple[str, str]] = {
    "badapple": ("chiltepin.health.unm.edu", "/badapple2/api/v1"),
    "biogrid": ("webservice.thebiogrid.org", ""),
    "biomarkerkb": ("api.biomarkerkb.org", ""),
    "bioregistry": ("bioregistry.io", "/api"),
    "cas": ("commonchemistry.cas.org", "/api"),
    "chebi": ("www.ebi.ac.uk", "/chebi/backend/api/public"),
    "chembl_ref": ("www.ebi.ac.uk", "/chembl/api/data"),
    "chemidplus": ("chem.nlm.nih.gov", "/api"),
    "clinicaltrials": ("clinicaltrials.gov", "/api/v2"),
    "disgenet": ("api.disgenet.com", "/api/v1"),
    "emblebi.identifiers": ("resolver.api.identifiers.org", ""),
    "emblebi.unichem": ("www.ebi.ac.uk", "/unichem/api/v1"),
    "ensembl": ("rest.ensembl.org", ""),
    "ensembl.biomart": ("www.ensembl.org", "/biomart/martservice"),
    "fda.aer": ("api.fda.gov", "/drug/event.json"),
    "geneontology": ("api.geneontology.org", "/api"),
    "glygen": ("api.glygen.org", ""),
    "gtex": ("gtexportal.org", "/rest/v1"),
    "gwascatalog": ("www.ebi.ac.uk", "/gwas/rest/api"),
    "hubmap": ("entity.api.hubmapconsortium.org", ""),
    "hugo": ("rest.genenames.org", ""),
    "icite": ("icite.od.nih.gov", "/api/pubs"),
    "idg.pharos": ("ncats-ifx.appspot.com", "/graphql"),
    "idg.rss": ("rss.ccs.miami.edu", "/rss-api"),
    "idg.tinx": ("api.newdrugtargets.org", ""),
    "jensenlab": ("api.jensenlab.org", ""),
    "lincs_ref": ("www.ilincs.org", "/api"),
    "lincs.clue": ("api.clue.io", "/api"),
    "lincs.sigcom": ("maayanlab.cloud", "/sigcom-lincs/metadata-api"),
    "maayanlab.harmonizome": ("amp.pharm.mssm.edu", "/Harmonizome/api/1.0"),
    "medline.connect": ("apps.nlm.nih.gov", "/medlineplus/services/mpconnect_service.cfm"),
    "medline.genetics": ("wsearch.nlm.nih.gov", "/ws"),
    "monarch": ("monarchinitiative.org", ""),
    "ncats.gsrs": ("gsrs.ncats.nih.gov", "/ginas/app/api/v1"),
    "ncbo": ("data.bioontology.org", ""),
    "oncotree": ("oncotree.mskcc.org", "/api"),
    "openphacts": ("beta.openphacts.org", "/2.1"),
    "pdb": ("data.rcsb.org", "/rest/v1"),
    "pubchem_ref": ("pubchem.ncbi.nlm.nih.gov", "/rest/pug"),
    "pubchem.soap": ("pubchem.ncbi.nlm.nih.gov", "/pug/pug.cgi"),
    "pubmed": ("eutils.ncbi.nlm.nih.gov", "/entrez/eutils"),
    "reactome": ("reactome.org", "/ContentService"),
    "rxnorm": ("rxnav.nlm.nih.gov", "/REST"),
    "stringdb": ("string-db.org", "/api"),
    "ubkg": ("datadistillery.api.sennetconsortium.org", ""),
    "umls": ("uts-ws.nlm.nih.gov", "/rest"),
    "uniprot_ref": ("rest.uniprot.org", "/uniprotkb"),
}


class AnduinAPIClient:
    """Unified, self-sufficient API interface with first-party adapters."""

    def __init__(
        self,
        project_root: str | Path | None = None,
        cert_dir: str | Path | None = None,
    ) -> None:
        self.project_root = Path(project_root or Path(__file__).resolve().parents[2])
        self.cert_dir = Path(cert_dir or Path.home() / ".config" / "anduin-core" / "certs")

        cert_manager = LincsCertManager(self.cert_dir)
        self._adapters: dict[str, AdapterBase] = {
            "lincs": LincsAdapter(cert_manager),
            "lincs_dcic": LincsDCICAdapter(cert_manager),
            "ilincs": ILincsAdapter(),
            "chembl": ChEMBLAdapter(),
            "pubchem": PubChemAdapter(),
            "uniprot": UniProtAdapter(),
            "opentargets": OpenTargetsAdapter(),
        }
        self._register_reference_catalog_adapters()

    def list_sources(self) -> list[SourceInfo]:
        """List available API source adapters and operations."""
        return [
            SourceInfo(name=name, description=adapter.description, operations=sorted(adapter.operations().keys()))
            for name, adapter in sorted(self._adapters.items(), key=lambda kv: kv[0])
        ]

    def execute(self, source: str, operation: str, **kwargs: Any) -> Any:
        """Execute a named operation against a source adapter."""
        if source not in self._adapters:
            known = ", ".join(sorted(self._adapters.keys()))
            raise KeyError(f"Unknown source '{source}'. Available sources: {known}")

        adapter = self._adapters[source]
        ops = adapter.operations()
        if operation not in ops:
            known_ops = ", ".join(sorted(ops.keys()))
            raise KeyError(f"Unknown operation '{operation}' for source '{source}'. Available operations: {known_ops}")
        return ops[operation](**kwargs)

    def lincs_drug_cell_assay_map(self, drug: str, limit: int = 10) -> dict[str, Any]:
        return self.execute("lincs_dcic", "drug_cell_assay_map", drug=drug, limit=limit)

    def lincs_search_signatures(self, lincs_pert_id: str) -> list[dict[str, Any]]:
        return self.execute("ilincs", "search_signatures", lincs_pert_id=lincs_pert_id)

    def _register_reference_catalog_adapters(self) -> None:
        """Register generic adapters for every endpoint family from reference metadata."""
        for source_name, (host, base_path) in sorted(BIOCLIENTS_REFERENCE_ENDPOINTS.items(), key=lambda kv: kv[0]):
            if source_name in self._adapters:
                continue
            base_url = f"https://{host}{base_path}"
            description = f"Reference-derived adapter for {source_name} ({base_url})"
            self._adapters[source_name] = GenericRESTAdapter(source_name, description, base_url)
