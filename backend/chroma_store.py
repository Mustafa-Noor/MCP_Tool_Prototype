"""
chroma_store.py

Chroma-backed vector store for the skills library, using BAAI/bge-m3 for
embeddings. Drop-in replacement for the numpy VectorStore in embed_skills.py
once you're ready to move off the plain-numpy prototype.

Usage:
    pip install chromadb sentence-transformers numpy

    python chroma_store.py build      # embeds skills_library.jsonl -> ./chroma_db/
    python chroma_store.py search "find me issues in the logs"
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import chromadb
from chromadb import Documents, EmbeddingFunction, Embeddings
from sentence_transformers import SentenceTransformer

SKILLS_FILE = Path("skills_library.jsonl")
CHROMA_DIR = Path("chroma_db")
COLLECTION_NAME = "skills_library"
MODEL_NAME = "BAAI/bge-m3"

# bge-m3 was trained with an instruction prefix on the *query* side only.
# Skill descriptions (the "documents" being indexed) are embedded as-is.
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
# bge-m3 embedding function, wired into Chroma's EmbeddingFunction interface
# ---------------------------------------------------------------------------

class BGEEmbeddingFunction(EmbeddingFunction):
    """
    Wraps BAAI/bge-m3 so Chroma can call it directly when adding documents
    or running queries. Chroma calls this the same way for both — so we
    detect query-time embedding via `embed_query()` below instead, since
    the query needs the instruction prefix and documents don't.
    """

    def __init__(self, model_name: str = MODEL_NAME):
        self._model = SentenceTransformer(model_name)

    def __call__(self, input: Documents) -> Embeddings:  # noqa: A002 - Chroma's required signature
        embeddings = self._model.encode(
            list(input),
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return embeddings.tolist()

    def embed_query(self, query: str) -> list[float]:
        embedding = self._model.encode(
            [QUERY_PREFIX + query],
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return embedding[0].tolist()


# ---------------------------------------------------------------------------
# Build + search
# ---------------------------------------------------------------------------

def get_collection(embed_fn: BGEEmbeddingFunction):
    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    return client.get_or_create_collection(
        name=COLLECTION_NAME,
        embedding_function=embed_fn,
        metadata={"hnsw:space": "cosine"},
    )


def build_index() -> None:
    skills = load_skills()
    embed_fn = BGEEmbeddingFunction()
    collection = get_collection(embed_fn)

    print(f"Embedding {len(skills)} skill descriptions with {MODEL_NAME} ...")
    collection.upsert(
        ids=[s["id"] for s in skills],
        documents=[s["description"] for s in skills],
        metadatas=[
            {
                "name": s["name"],
                "category": s.get("category", ""),
                "steps": json.dumps(s.get("steps", [])),
                "tags": json.dumps(s.get("tags", [])),
            }
            for s in skills
        ],
    )
    print(f"Saved index to {CHROMA_DIR}/ (collection '{COLLECTION_NAME}', {collection.count()} vectors)")


def search(query: str, top_k: int = 3) -> list[dict]:
    embed_fn = BGEEmbeddingFunction()
    collection = get_collection(embed_fn)

    query_embedding = embed_fn.embed_query(query)
    results = collection.query(
        query_embeddings=[query_embedding],
        n_results=top_k,
    )

    matches = []
    for i in range(len(results["ids"][0])):
        distance = results["distances"][0][i]  # cosine distance: 0 = identical
        matches.append({
            "id": results["ids"][0][i],
            "similarity": 1 - distance,
            "name": results["metadatas"][0][i]["name"],
            "category": results["metadatas"][0][i]["category"],
            "steps": json.loads(results["metadatas"][0][i]["steps"]),
            "description": results["documents"][0][i],
        })
    return matches


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python chroma_store.py [build|search <query>]")
        sys.exit(1)

    command = sys.argv[1]

    if command == "build":
        build_index()

    elif command == "search":
        if len(sys.argv) < 3:
            print('Usage: python chroma_store.py search "<query text>"')
            sys.exit(1)
        query_text = " ".join(sys.argv[2:])
        matches = search(query_text, top_k=3)
        print(f"\nTop matches for: {query_text!r}\n")
        for m in matches:
            print(f"  {m['similarity']:.4f}  {m['id']:<25} {m['name']}")

    else:
        print(f"Unknown command: {command}")
        sys.exit(1)
