from __future__ import annotations

import argparse

import numpy as np

from common.config import load_yaml_config
from workflows.kg_cold_start import (
    load_kg_embedding_store,
    rank_candidate_tails,
    rank_candidate_tails_from_cold_start,
)


def _load_vector(path: str) -> np.ndarray:
    value = np.load(path)
    if value.ndim == 1:
        return value.astype(np.float32, copy=False)
    if value.ndim == 2 and value.shape[0] == 1:
        return value[0].astype(np.float32, copy=False)
    raise ValueError(f"Expected vector at {path} with shape [d] or [1, d].")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run workflow-3 KG cold-start link prediction.")
    parser.add_argument("--config", type=str, default="configs/workflows/kg_cold_start.yaml", help="Path to YAML config.")
    args = parser.parse_args()

    config = load_yaml_config(args.config)
    kg_cfg = config.get("kg", {}) if isinstance(config, dict) else {}
    query_cfg = config.get("query", {}) if isinstance(config, dict) else {}
    search_cfg = config.get("search", {}) if isinstance(config, dict) else {}

    if not isinstance(kg_cfg, dict) or not isinstance(query_cfg, dict) or not isinstance(search_cfg, dict):
        raise ValueError("kg, query, and search sections are required and must be mappings.")

    store = load_kg_embedding_store(
        entity_embeddings_path=str(kg_cfg.get("entity_embeddings_path", "")),
        entity_ids_path=str(kg_cfg.get("entity_ids_path", "")),
        relation_embeddings_path=str(kg_cfg.get("relation_embeddings_path", "")),
        relation_ids_path=str(kg_cfg.get("relation_ids_path", "")),
    )

    relation_id = str(query_cfg.get("relation_id", ""))
    if not relation_id:
        raise ValueError("query.relation_id is required.")

    top_k = int(search_cfg.get("top_k", 20))
    head_entity_id = query_cfg.get("head_entity_id")
    head_text_embedding_path = query_cfg.get("head_text_embedding_path")

    if isinstance(head_entity_id, str) and head_entity_id:
        results = rank_candidate_tails(store, head_entity_id=head_entity_id, relation_id=relation_id, top_k=top_k)
    elif isinstance(head_text_embedding_path, str) and head_text_embedding_path:
        head_text_embedding = _load_vector(head_text_embedding_path)
        top_k_neighbors = int(search_cfg.get("cold_start_neighbors", 25))
        results = rank_candidate_tails_from_cold_start(
            store,
            head_text_embedding=head_text_embedding,
            relation_id=relation_id,
            top_k=top_k,
            top_k_neighbors=top_k_neighbors,
        )
    else:
        raise ValueError("Provide either query.head_entity_id or query.head_text_embedding_path.")

    for rank, (entity_id, score) in enumerate(results, start=1):
        print(f"{rank}\t{entity_id}\t{score:.6f}")


if __name__ == "__main__":
    main()
