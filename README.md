# Nexus

Nexus is the dedicated prior-knowledge API layer extracted from anduin-core.

## What is included

- Unified API adapter interface and source adapters
- FastBioMedRAG knowledge graph indexing/query module
- KG cold-start retrieval workflow
- CLI runners and YAML configs for API/KG tasks
- Connectivity check utility scripts and sample assets

## Quick start

1. Install dependencies:

   pip install -r requirements.txt

2. List API sources:

   python mains/run_api_interface.py list-sources

3. Run a configured adapter call:

   python mains/run_api_from_config.py --config configs/api/chembl_search_trametinib.yaml

4. Run FastBioMedRAG query:

   python mains/run_kgraph_fastbmrag.py --config configs/kgraph/fastbmrag.yaml

5. Run KG cold-start workflow:

   python mains/run_workflow_kg_cold_start.py --config configs/workflows/kg_cold_start.yaml
