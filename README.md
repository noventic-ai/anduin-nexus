# Anduin Nexus KG Notes

This repository builds a Nexus knowledge graph from multiple biomedical sources.

## CURIE and Biolink

- CURIE stands for **Compact URI** (sometimes expanded as Compact URI Expression).
- A CURIE is a compact identifier in the form `prefix:local_id`.
- Examples:
  - `MONDO:0004985`
  - `MESH:D001007`
  - `CHEMBL.COMPOUND:CHEMBL41`

### Why CURIEs and Biolink are both used

- CURIEs provide **identity**: exactly which entity a node refers to.
- Biolink provides **semantics**: what type of entity it is and what relationships mean.

Examples of Biolink semantics:

- Categories: `biolink:Disease`, `biolink:ChemicalEntity`, `biolink:PhenotypicFeature`
- Predicates: `biolink:treats`

In short:

- Identity = CURIE
- Semantics = Biolink

Using both preserves source fidelity while enabling cross-source interoperability.

## Namespace Policy Decision

The canonical namespace policy file is stored at repo root:

- `namespace.yaml`

Configuration files should reference it as:

- `namespace_policy_path: namespace.yaml`

The ChEMBL converter resolves non-absolute namespace policy paths relative to repo root, so the above path is the intended default behavior.

## Current ChEMBL Config Pattern

In `configs/kg_build/chembl_sqlite_to_nexus.yaml`:

- Keep `namespace_policy_path: namespace.yaml`
- Avoid duplicating disease namespace overrides in the per-source config when they are already defined in `namespace.yaml`

## Cross-Source Identity Behavior (Important)

Current merge behavior is ID-based, not name-based.

- Nodes are merged with `MERGE (n:NexusNode {id: row.id})`.
- This means two records only become one node when they share the same canonical `id`.

For compounds like fluoxetine:

- ChEMBL provides a canonical CURIE-like ID such as `CHEMBL.COMPOUND:CHEMBL41`.
- Another source that only provides text like `fluoxetine` (or a different identifier) will create a different node unless mapped to the same canonical ID.

Practical implication:

- Name matching alone is not used for automatic node merging.
- LLM-based node merge proposals are a possible solution
