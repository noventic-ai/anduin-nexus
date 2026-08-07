from __future__ import annotations

import argparse
from ast import literal_eval
from pprint import pprint

import pandas as pd

from common.config import load_yaml_config
from kgraph.fastbmrag import RAG


def _load_update_dataframe(path: str) -> pd.DataFrame:
    input_df = pd.read_csv(path, converters={"main_text": literal_eval})
    main_text = list(input_df["main_text"])
    if main_text and not isinstance(main_text[0], list):
        main_text = [x.split("\n") for x in main_text]
    input_df = input_df.copy()
    input_df["main_text"] = main_text
    return input_df


def main() -> None:
    parser = argparse.ArgumentParser(description="Run FastBioMedRAG update/query jobs.")
    parser.add_argument("--config", type=str, default="configs/kgraph/fastbmrag.yaml", help="Path to YAML config.")
    parser.add_argument("--job", type=str, choices=["update", "query"], default=None, help="Optional override for run.job.")
    parser.add_argument("--document", type=str, default=None, help="CSV path for update job override.")
    parser.add_argument("--question", type=str, default=None, help="Query text override for query job.")
    args = parser.parse_args()

    config = load_yaml_config(args.config)
    run_cfg = config.get("run", {}) if isinstance(config, dict) else {}
    kgraph_cfg = config.get("kgraph", {}) if isinstance(config, dict) else {}
    update_cfg = config.get("update", {}) if isinstance(config, dict) else {}
    query_cfg = config.get("query", {}) if isinstance(config, dict) else {}

    if not isinstance(run_cfg, dict):
        run_cfg = {}
    if not isinstance(kgraph_cfg, dict):
        kgraph_cfg = {}
    if not isinstance(update_cfg, dict):
        update_cfg = {}
    if not isinstance(query_cfg, dict):
        query_cfg = {}

    job = args.job or str(run_cfg.get("job", "query"))
    if job not in {"update", "query"}:
        raise ValueError("run.job must be either 'update' or 'query'.")

    rag = RAG(
        working_dir=str(kgraph_cfg.get("working_dir", "./outputs/kgraph/fastbmrag")),
        collection_name=str(kgraph_cfg.get("collection_name", "paper")),
        llm_index_model_name=str(kgraph_cfg.get("llm_update_model_name", "gpt-4.1-mini")),
        llm_query_model_name=str(kgraph_cfg.get("llm_query_model_name", "gpt-4.1-mini")),
        embed_model_name=str(kgraph_cfg.get("embed_model_name", "text-embedding-3-small")),
        embed_size=int(kgraph_cfg.get("embed_size", 1536)),
        embedding_similarity=float(kgraph_cfg.get("embedding_similarity", 0.8)),
        backend=str(kgraph_cfg.get("backend", "openai")),
    )

    try:
        if job == "update":
            document_path = args.document or str(update_cfg.get("document", ""))
            if not document_path:
                raise ValueError("Update job requires update.document in config or --document.")
            input_df = _load_update_dataframe(document_path)
            rag.insert_paper(input_df)
            print("Done")
            return

        question = args.question or str(query_cfg.get("question", "Which genes are associated with endometriosis?"))
        top_match = int(query_cfg.get("top_match", 20))
        temperature = float(query_cfg.get("temperature", 0.75))
        question_analysis = bool(query_cfg.get("question_analysis", True))
        filter_importance = float(query_cfg.get("filter_importance", -1.0))
        similarity_score = float(query_cfg.get("similarity_score", float(kgraph_cfg.get("embedding_similarity", 0.8))))

        gene = query_cfg.get("gene", [])
        disease = query_cfg.get("disease", [])
        paper_id = query_cfg.get("paper_id", [])
        if not isinstance(gene, list):
            gene = []
        if not isinstance(disease, list):
            disease = []
        if not isinstance(paper_id, list):
            paper_id = []

        output = rag.query(
            question=question,
            top_results=top_match,
            gene=gene,
            disease=disease,
            paper_id=paper_id,
            question_analysis=question_analysis,
            filter_importance=filter_importance,
            temperature=temperature,
            similarity_score=similarity_score,
        )

        if isinstance(output, dict):
            print(output.get("outcome", ""))
            print("\nReferences:")
            pprint(output.get("reference"))
        else:
            print(output)
    finally:
        rag.close()


if __name__ == "__main__":
    main()
