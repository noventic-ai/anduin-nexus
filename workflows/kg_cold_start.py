from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np


@dataclass(frozen=True)
class KGEmbeddingStore:
    """Entity and relation embedding tables used for KG link prediction."""

    entity_ids: list[str]
    entity_embeddings: np.ndarray
    relation_ids: list[str]
    relation_embeddings: np.ndarray


def _load_ids(path: str | Path) -> list[str]:
    with Path(path).open("r", encoding="utf-8") as handle:
        return [line.strip() for line in handle if line.strip()]


def _as_vector(array: np.ndarray) -> np.ndarray:
    if array.ndim == 1:
        return array.astype(np.float32, copy=False)
    if array.ndim == 2 and array.shape[0] == 1:
        return array[0].astype(np.float32, copy=False)
    raise ValueError("Expected vector shape [d] or [1, d].")


def _cosine_scores(query: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    query = _as_vector(query)
    query = query / np.clip(np.linalg.norm(query), 1e-12, None)
    matrix = matrix.astype(np.float32, copy=False)
    matrix_norm = np.linalg.norm(matrix, axis=1, keepdims=True)
    matrix_norm = np.clip(matrix_norm, 1e-12, None)
    matrix = matrix / matrix_norm
    return matrix @ query


def load_kg_embedding_store(
    entity_embeddings_path: str | Path,
    entity_ids_path: str | Path,
    relation_embeddings_path: str | Path,
    relation_ids_path: str | Path,
) -> KGEmbeddingStore:
    entity_embeddings = np.load(entity_embeddings_path)
    relation_embeddings = np.load(relation_embeddings_path)

    if entity_embeddings.ndim != 2:
        raise ValueError("entity_embeddings must have shape [num_entities, embedding_dim].")
    if relation_embeddings.ndim != 2:
        raise ValueError("relation_embeddings must have shape [num_relations, embedding_dim].")
    if entity_embeddings.shape[1] != relation_embeddings.shape[1]:
        raise ValueError("Entity and relation embedding dimensions must match.")

    entity_ids = _load_ids(entity_ids_path)
    relation_ids = _load_ids(relation_ids_path)
    if len(entity_ids) != entity_embeddings.shape[0]:
        raise ValueError("Entity id count does not match entity embedding rows.")
    if len(relation_ids) != relation_embeddings.shape[0]:
        raise ValueError("Relation id count does not match relation embedding rows.")

    return KGEmbeddingStore(
        entity_ids=entity_ids,
        entity_embeddings=entity_embeddings.astype(np.float32, copy=False),
        relation_ids=relation_ids,
        relation_embeddings=relation_embeddings.astype(np.float32, copy=False),
    )


def _entity_index(store: KGEmbeddingStore, entity_id: str) -> int:
    try:
        return store.entity_ids.index(entity_id)
    except ValueError as exc:
        raise KeyError(f"Unknown entity id: {entity_id}") from exc


def _relation_index(store: KGEmbeddingStore, relation_id: str) -> int:
    try:
        return store.relation_ids.index(relation_id)
    except ValueError as exc:
        raise KeyError(f"Unknown relation id: {relation_id}") from exc


def _transe_tail_scores(
    head_embedding: np.ndarray,
    relation_embedding: np.ndarray,
    tail_embeddings: np.ndarray,
) -> np.ndarray:
    """PertKGE-like translation score: higher is better (negative distance)."""
    target = head_embedding + relation_embedding
    distances = np.linalg.norm(tail_embeddings - target[None, :], axis=1)
    return -distances


def rank_candidate_tails(
    store: KGEmbeddingStore,
    head_entity_id: str,
    relation_id: str,
    top_k: int,
) -> list[tuple[str, float]]:
    if top_k <= 0:
        raise ValueError("top_k must be > 0")

    head_index = _entity_index(store, head_entity_id)
    relation_index = _relation_index(store, relation_id)

    scores = _transe_tail_scores(
        head_embedding=store.entity_embeddings[head_index],
        relation_embedding=store.relation_embeddings[relation_index],
        tail_embeddings=store.entity_embeddings,
    )
    top_indices = np.argsort(-scores)[:top_k]
    return [(store.entity_ids[idx], float(scores[idx])) for idx in top_indices]


def initialize_cold_start_entity(
    text_embedding: np.ndarray,
    store: KGEmbeddingStore,
    top_k_neighbors: int,
) -> np.ndarray:
    if top_k_neighbors <= 0:
        raise ValueError("top_k_neighbors must be > 0")

    similarity = _cosine_scores(text_embedding, store.entity_embeddings)
    top_indices = np.argsort(-similarity)[:top_k_neighbors]
    return store.entity_embeddings[top_indices].mean(axis=0)


def rank_candidate_tails_from_cold_start(
    store: KGEmbeddingStore,
    head_text_embedding: np.ndarray,
    relation_id: str,
    top_k: int,
    top_k_neighbors: int,
) -> list[tuple[str, float]]:
    if top_k <= 0:
        raise ValueError("top_k must be > 0")

    relation_index = _relation_index(store, relation_id)
    cold_start_head = initialize_cold_start_entity(head_text_embedding, store, top_k_neighbors=top_k_neighbors)
    scores = _transe_tail_scores(
        head_embedding=cold_start_head,
        relation_embedding=store.relation_embeddings[relation_index],
        tail_embeddings=store.entity_embeddings,
    )
    top_indices = np.argsort(-scores)[:top_k]
    return [(store.entity_ids[idx], float(scores[idx])) for idx in top_indices]
