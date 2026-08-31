"""
embed_skills.py

Embeds the skills_library.jsonl file using BAAI/bge-m3 (via the Hugging Face
Inference API — see test_embeddings_model.py) and builds a local vector store
you can query with a user's question to get back the highest-similarity
skills, before handing those to the LLM planner.

Usage:
    pip install huggingface_hub python-dotenv numpy

    Set HF_TOKEN in .env (already present in this project).

    python embed_skills.py build      # embeds skills_library.jsonl -> skills_index/
    python embed_skills.py search "find me issues in the logs"

Swap-out note:
    This uses a plain numpy cosine-similarity index so the prototype has no
    extra infra dependency. If you're pushing to a real vector store instead
    (Chroma, Pinecone, pgvector, Qdrant, etc.), keep `embed_texts()` as-is and
    swap out `VectorStore` for that store's client — the embedding step
    doesn't change.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import dotenv
import numpy as np
from huggingface_hub import InferenceClient

SKILLS_FILE = Path("skills_library.jsonl")
INDEX_DIR = Path("skills_index")
MODEL_NAME = "BAAI/bge-m3"

# bge-m3 was trained with an instruction prefix on the *query* side only.
# Skill descriptions (the "documents" being searched) are embedded as-is.
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_skills(path: Path = SKILLS_FILE) -> list[dict]:
    skills = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                skills.append(json.loads(line))
    return skills


# ---------------------------------------------------------------------------
# Embedding (via the Hugging Face Inference API)
# ---------------------------------------------------------------------------

_client: InferenceClient | None = None


def get_client() -> InferenceClient:
    global _client
    if _client is None:
        dotenv.load_dotenv()
        _client = InferenceClient(provider="hf-inference", api_key=os.environ["HF_TOKEN"])
    return _client


def embed_texts(texts: list[str]) -> np.ndarray:
    client = get_client()
    embeddings = np.asarray(
        client.feature_extraction(texts, model=MODEL_NAME), dtype=np.float32
    )
    norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
    return embeddings / norms  # so cosine similarity == dot product


# ---------------------------------------------------------------------------
# Local vector store (swap for Chroma/Pinecone/pgvector/etc. as needed)
# ---------------------------------------------------------------------------

class VectorStore:
    def __init__(self, embeddings: np.ndarray, metadata: list[dict]):
        self.embeddings = embeddings  # shape: (n_skills, dim), already normalized
        self.metadata = metadata      # parallel list of skill dicts

    def search(self, query_embedding: np.ndarray, top_k: int = 3) -> list[dict]:
        scores = self.embeddings @ query_embedding  # cosine similarity
        top_idx = np.argsort(-scores)[:top_k]
        return [
            {**self.metadata[i], "similarity": float(scores[i])}
            for i in top_idx
        ]

    def save(self, out_dir: Path = INDEX_DIR) -> None:
        out_dir.mkdir(parents=True, exist_ok=True)
        np.save(out_dir / "embeddings.npy", self.embeddings)
        with (out_dir / "metadata.json").open("w", encoding="utf-8") as f:
            json.dump(self.metadata, f, indent=2)

    @classmethod
    def load(cls, in_dir: Path = INDEX_DIR) -> "VectorStore":
        embeddings = np.load(in_dir / "embeddings.npy")
        with (in_dir / "metadata.json").open("r", encoding="utf-8") as f:
            metadata = json.load(f)
        return cls(embeddings, metadata)


# ---------------------------------------------------------------------------
# Build + search
# ---------------------------------------------------------------------------

def build_index() -> None:
    skills = load_skills()
    descriptions = [s["description"] for s in skills]
    print(f"Embedding {len(descriptions)} skill descriptions with {MODEL_NAME} ...")
    embeddings = embed_texts(descriptions)
    store = VectorStore(embeddings, skills)
    store.save()
    print(f"Saved index to {INDEX_DIR}/ ({embeddings.shape[0]} vectors, dim={embeddings.shape[1]})")


def search(query: str, top_k: int = 3) -> list[dict]:
    store = VectorStore.load()
    query_embedding = embed_texts([QUERY_PREFIX + query])[0]
    return store.search(query_embedding, top_k=top_k)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python embed_skills.py [build|search <query>]")
        sys.exit(1)

    command = sys.argv[1]

    if command == "build":
        build_index()

    elif command == "search":
        if len(sys.argv) < 3:
            print("Usage: python embed_skills.py search \"<query text>\"")
            sys.exit(1)
        query_text = " ".join(sys.argv[2:])
        results = search(query_text, top_k=3)
        print(f"\nTop matches for: {query_text!r}\n")
        for r in results:
            print(f"  {r['similarity']:.4f}  {r['id']:<25} {r['name']}")

    else:
        print(f"Unknown command: {command}")
        sys.exit(1)
