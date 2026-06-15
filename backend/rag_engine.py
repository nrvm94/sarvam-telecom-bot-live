"""
RAG Engine — Airtel Knowledge Base
Uses ChromaDB as vector store and sentence-transformers for embeddings.
Documents are loaded from ../db/airtel_kb.json on first run and persisted.
"""

import json
import logging
import os
from pathlib import Path

import chromadb
from chromadb.config import Settings
from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)

# Path to the JSON knowledge base relative to this file
_KB_PATH = Path(__file__).parent.parent / "db" / "airtel_kb.json"


class AirtelKnowledgeBase:
    """
    Manages the Airtel customer-support knowledge base using ChromaDB.
    Embeddings are generated with a lightweight local sentence-transformer model.
    """

    def __init__(self):
        logger.info("Initialising AirtelKnowledgeBase ...")

        # ---- ChromaDB setup -----------------------------------------------
        # Persist data inside backend/chromadb so restarts are fast
        persist_dir = str(Path(__file__).parent / "chromadb")
        logger.debug("ChromaDB persist directory: %s", persist_dir)

        self.chroma_client = chromadb.Client(
            Settings(
                persist_directory=persist_dir,
                anonymized_telemetry=False,
                is_persistent=True,
            )
        )

        # ---- Embedding model ----------------------------------------------
        logger.info("Loading embedding model: all-MiniLM-L6-v2 ...")
        self.embed_model = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
        logger.info("Embedding model loaded.")

        # ---- Collection ---------------------------------------------------
        self.collection = self.chroma_client.get_or_create_collection(
            name="airtel_kb",
            metadata={"hnsw:space": "cosine"},
        )
        logger.info(
            "ChromaDB collection 'airtel_kb' ready | current doc count=%d",
            self.collection.count(),
        )

        # ---- Populate only if empty ---------------------------------------
        if self.collection.count() == 0:
            self._load_documents()
        else:
            logger.info(
                "Knowledge base already populated (%d docs). Skipping reload.",
                self.collection.count(),
            )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _load_documents(self):
        """Load documents from airtel_kb.json and insert into ChromaDB."""
        if not _KB_PATH.exists():
            logger.error("Knowledge base file not found: %s", _KB_PATH)
            return

        with open(_KB_PATH, "r", encoding="utf-8") as f:
            documents = json.load(f)

        logger.info("Embedding %d knowledge-base documents ...", len(documents))

        ids = []
        embeddings = []
        metadatas = []
        contents = []

        for doc in documents:
            doc_id = str(doc.get("id", ""))
            content = doc.get("content", "")
            category = doc.get("category", "general")

            if not doc_id or not content:
                logger.warning("Skipping invalid document: %s", doc)
                continue

            embedding = self.embed_model.encode(content).tolist()

            ids.append(doc_id)
            embeddings.append(embedding)
            metadatas.append({"category": category, "id": doc_id})
            contents.append(content)

        self.collection.add(
            ids=ids,
            embeddings=embeddings,
            documents=contents,
            metadatas=metadatas,
        )
        logger.info(
            "Knowledge base populated with %d documents.", len(ids)
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def query(self, question: str, top_k: int = 3) -> str:
        """
        Retrieve the top-k most relevant knowledge base documents for a query.

        Args:
            question: Customer query text.
            top_k: Number of documents to retrieve.

        Returns:
            Concatenated context string from matching documents.
        """
        logger.debug("RAG query | question=%r | top_k=%d", question[:100], top_k)

        if not question or not question.strip():
            logger.warning("RAG query called with empty question — returning empty context.")
            return ""

        query_embedding = self.embed_model.encode(question).tolist()

        results = self.collection.query(
            query_embeddings=[query_embedding],
            n_results=min(top_k, self.collection.count()),
            include=["documents", "metadatas", "distances"],
        )

        docs = results.get("documents", [[]])[0]
        metas = results.get("metadatas", [[]])[0]
        distances = results.get("distances", [[]])[0]

        logger.debug(
            "RAG results | count=%d | distances=%s",
            len(docs),
            [round(d, 4) for d in distances],
        )

        if not docs:
            logger.info("RAG query returned no results for: %r", question[:80])
            return ""

        # Filter out results that are too distant (not relevant enough)
        DISTANCE_THRESHOLD = 0.5
        filtered = [
            (doc, meta) for doc, meta, dist in zip(docs, metas, distances)
            if dist < DISTANCE_THRESHOLD
        ]
        if not filtered:
            logger.info("RAG: all results above distance threshold %.1f — returning empty", DISTANCE_THRESHOLD)
            return ""
        docs, metas = zip(*filtered)

        # Format context with category labels for clarity
        context_parts = []
        for doc, meta in zip(docs, metas):
            category = meta.get("category", "general")
            context_parts.append(f"[{category.upper()}]\n{doc}")

        context = "\n\n".join(context_parts)
        logger.info(
            "RAG done | question=%r | results=%d | context_len=%d",
            question[:60],
            len(docs),
            len(context),
        )
        return context
