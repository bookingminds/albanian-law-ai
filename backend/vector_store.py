"""ChromaDB vector store — user-scoped document embeddings.

Features:
- User-level isolation (every chunk tagged with user_id)
- Optional document_id filter for single-doc search
- LRU embedding cache (avoid re-computing repeated queries)
- Null-embedding guard (reject empty/zero vectors)
- Similarity score logging & configurable threshold
- Debug search endpoint support
- Migration helper for existing chunks without user_id
"""

import hashlib
import logging
import time
from collections import OrderedDict
from backend.config import settings

logger = logging.getLogger("rag.vector_store")

chroma_client = None
collection = None
openai_client = None


def _ensure_initialized():
    """Lazy-init ChromaDB and OpenAI so the app can start even if they fail."""
    global chroma_client, collection, openai_client
    if collection is not None and openai_client is not None:
        return
    try:
        import chromadb
        chroma_client = chromadb.PersistentClient(
            path=str(settings.DATA_DIR / "chroma_db"),
        )
        collection = chroma_client.get_or_create_collection(
            name="albanian_laws",
            metadata={"hnsw:space": "cosine"},
        )
        logger.info("ChromaDB initialized successfully")
    except Exception as e:
        logger.error(f"ChromaDB init failed: {e}")
    try:
        from openai import OpenAI
        openai_client = OpenAI(api_key=settings.OPENAI_API_KEY)
        logger.info("OpenAI client initialized")
    except Exception as e:
        logger.error(f"OpenAI client init failed: {e}")


# ── Embedding Cache ───────────────────────────────────────────

class EmbeddingCache:
    """Simple LRU cache for embeddings to avoid redundant API calls."""

    def __init__(self, max_size: int = 256):
        self._cache: OrderedDict[str, list[float]] = OrderedDict()
        self._max_size = max_size
        self.hits = 0
        self.misses = 0

    def _key(self, text: str) -> str:
        return hashlib.sha256(text.strip().encode()).hexdigest()[:16]

    def get(self, text: str):
        k = self._key(text)
        if k in self._cache:
            self._cache.move_to_end(k)
            self.hits += 1
            return self._cache[k]
        self.misses += 1
        return None

    def put(self, text: str, embedding: list[float]):
        k = self._key(text)
        self._cache[k] = embedding
        self._cache.move_to_end(k)
        if len(self._cache) > self._max_size:
            self._cache.popitem(last=False)


_embedding_cache = EmbeddingCache(max_size=settings.EMBEDDING_CACHE_SIZE)


# ── Embedding Generation ─────────────────────────────────────

def get_embedding(text: str) -> list[float]:
    """Generate embedding for a single text with cache."""
    _ensure_initialized()
    text = text.strip()
    if not text:
        raise ValueError("Cannot generate embedding for empty text")

    # Check cache
    cached = _embedding_cache.get(text)
    if cached is not None:
        return cached

    response = openai_client.embeddings.create(
        model=settings.EMBEDDING_MODEL,
        input=text,
        timeout=30.0,
    )
    embedding = response.data[0].embedding
    if not embedding or all(v == 0.0 for v in embedding[:10]):
        raise ValueError(f"Null embedding generated for text: {text[:80]}...")

    _embedding_cache.put(text, embedding)
    return embedding


def get_embeddings_batch(texts: list[str]) -> list[list[float]]:
    """Generate embeddings for a batch of texts with null-guard."""
    _ensure_initialized()
    all_embeddings = []
    batch_size = 50
    total = len(texts)
    null_count = 0

    for i in range(0, total, batch_size):
        batch = texts[i:i + batch_size]
        cleaned_batch = [t.strip() if t.strip() else "[empty]" for t in batch]

        response = openai_client.embeddings.create(
            model=settings.EMBEDDING_MODEL,
            input=cleaned_batch,
            timeout=30.0,
        )

        for j, data in enumerate(response.data):
            emb = data.embedding
            if not emb or all(v == 0.0 for v in emb[:10]):
                null_count += 1
                logger.warning(f"Null embedding at index {i+j}: {cleaned_batch[j][:60]}...")
            all_embeddings.append(emb)

        logger.info(f"Embeddings batch {i//batch_size + 1}: {min(i+batch_size, total)}/{total} done")

    if null_count > 0:
        logger.warning(f"WARNING: {null_count} null embeddings detected out of {total}")
    else:
        logger.info(f"All {total} embeddings generated successfully (no nulls)")

    return all_embeddings


# ── Store Operations ─────────────────────────────────────────

async def add_chunks_to_store(doc_id: int, user_id: int,
                               chunks: list[dict], doc_metadata: dict):
    """Add document chunks to ChromaDB with embeddings and user isolation.

    Every chunk is tagged with user_id and doc_id for scoped retrieval.
    """
    _ensure_initialized()
    texts = [c["text"] for c in chunks]
    logger.info(f"[doc:{doc_id}] Generating embeddings for {len(texts)} chunks...")

    start_time = time.time()
    embeddings = get_embeddings_batch(texts)
    embed_time = time.time() - start_time
    logger.info(f"[doc:{doc_id}] Embeddings generated in {embed_time:.1f}s")

    ids = []
    metadatas = []
    valid_embeddings = []
    valid_texts = []

    doc_title = doc_metadata.get("title", "") or doc_metadata.get("original_filename", "")

    for i, chunk in enumerate(chunks):
        emb = embeddings[i]
        if not emb or all(v == 0.0 for v in emb[:10]):
            logger.warning(f"[doc:{doc_id}] Skipping chunk {i} — null embedding")
            continue

        chunk_id = f"u{user_id}_doc{doc_id}_chunk{i}"
        ids.append(chunk_id)
        valid_embeddings.append(emb)
        valid_texts.append(texts[i])
        metadatas.append({
            "user_id": str(user_id),
            "doc_id": str(doc_id),
            "chunk_index": i,
            "article": chunk.get("article") or "",
            "pages": ",".join(str(p) for p in chunk.get("pages", [])),
            "title": doc_title,
            "law_number": doc_metadata.get("law_number", ""),
            "law_date": doc_metadata.get("law_date", ""),
            "char_count": len(texts[i]),
        })

    if not ids:
        raise ValueError("No valid embeddings were generated for this document")

    collection.add(
        ids=ids,
        embeddings=valid_embeddings,
        documents=valid_texts,
        metadatas=metadatas,
    )

    skipped = len(chunks) - len(ids)
    if skipped:
        logger.warning(f"[doc:{doc_id}] Stored {len(ids)} chunks, skipped {skipped} (null embeddings)")
    else:
        logger.info(f"[doc:{doc_id}] All {len(ids)} chunks stored (user_id={user_id})")


# ── Search ───────────────────────────────────────────────────

def _build_where_filter(user_id: int = None, doc_id: int = None) -> dict | None:
    """Build ChromaDB where filter.

    - user_id=None, doc_id=None → no filter (global search)
    - user_id set              → scope to that user
    - doc_id set               → scope to that document
    - both set                 → intersection
    """
    parts = []
    if user_id is not None:
        parts.append({"user_id": {"$eq": str(user_id)}})
    if doc_id is not None:
        parts.append({"doc_id": {"$eq": str(doc_id)}})

    if len(parts) == 0:
        return None
    if len(parts) == 1:
        return parts[0]
    return {"$and": parts}


async def search_documents(query: str, user_id: int = None,
                           doc_id: int = None,
                           top_k: int = None,
                           threshold: float = None) -> list[dict]:
    """Search the vector store for relevant chunks.

    If user_id is None, searches ALL chunks globally (for normal-user chat).
    If user_id is set, scopes to that user's chunks.

    Args:
        query: The search query
        user_id: Required — only search this user's chunks
        doc_id: Optional — restrict to a single document
        top_k: Number of results (default from settings)
        threshold: Distance threshold (default from settings)

    Returns chunks sorted by relevance (lowest distance first).
    """
    _ensure_initialized()
    top_k = top_k or settings.TOP_K_RESULTS
    threshold = threshold or settings.SIMILARITY_THRESHOLD

    if not collection or collection.count() == 0:
        logger.warning("Search called but collection is empty")
        return []

    where_filter = _build_where_filter(user_id, doc_id)

    # Count available chunks for this user/filter
    try:
        available = collection.count()
        get_kwargs = {"limit": 1}
        if where_filter is not None:
            get_kwargs["where"] = where_filter
        user_results = collection.get(**get_kwargs)
        if not user_results or not user_results["ids"]:
            logger.info(f"No chunks found for user_id={user_id}" +
                        (f", doc_id={doc_id}" if doc_id else ""))
            return []
    except Exception:
        pass

    start_time = time.time()
    query_embedding = get_embedding(query)
    embed_time = time.time() - start_time

    n_results = min(top_k, collection.count())

    query_kwargs = {
        "query_embeddings": [query_embedding],
        "n_results": n_results,
        "include": ["documents", "metadatas", "distances"],
    }
    if where_filter is not None:
        query_kwargs["where"] = where_filter

    results = collection.query(**query_kwargs)

    search_time = time.time() - start_time
    chunks = []

    if results and results["documents"] and results["documents"][0]:
        for i in range(len(results["documents"][0])):
            meta = results["metadatas"][0][i]
            distance = results["distances"][0][i]
            text = results["documents"][0][i]

            chunks.append({
                "text": text,
                "distance": distance,
                "similarity": round(1.0 - distance, 4),
                "doc_id": meta.get("doc_id", ""),
                "user_id": meta.get("user_id", ""),
                "article": meta.get("article", ""),
                "pages": meta.get("pages", ""),
                "title": meta.get("title", ""),
                "law_number": meta.get("law_number", ""),
                "law_date": meta.get("law_date", ""),
                "char_count": meta.get("char_count", len(text)),
                "chunk_index": meta.get("chunk_index", 0),
            })

    filter_desc = f"user={user_id}" + (f", doc={doc_id}" if doc_id else " (all docs)")
    logger.info(
        f"Search [{filter_desc}]: query='{query[:60]}...' | "
        f"results={len(chunks)} | threshold={threshold} | "
        f"embed_time={embed_time:.2f}s | total_time={search_time:.2f}s"
    )
    for i, c in enumerate(chunks[:5]):
        status = "PASS" if c["distance"] < threshold else "FAIL"
        logger.debug(
            f"  [{status}] #{i+1} dist={c['distance']:.4f} sim={c['similarity']:.4f} "
            f"doc={c['doc_id']} art={c['article'] or 'N/A'} | {c['text'][:80]}..."
        )

    return chunks


async def search_documents_debug(query: str, user_id: int,
                                  doc_id: int = None,
                                  top_k: int = None) -> dict:
    """Debug version of search — returns full details including all scores."""
    top_k = top_k or settings.TOP_K_RESULTS
    threshold = settings.SIMILARITY_THRESHOLD

    if collection.count() == 0:
        return {
            "query": query,
            "total_chunks_in_store": 0,
            "user_id": user_id,
            "results": [],
            "message": "Vector store is empty — no documents uploaded",
        }

    where_filter = _build_where_filter(user_id, doc_id)

    start_time = time.time()
    query_embedding = get_embedding(query)
    embed_time = time.time() - start_time

    debug_query_kwargs = {
        "query_embeddings": [query_embedding],
        "n_results": min(top_k * 2, collection.count()),
        "include": ["documents", "metadatas", "distances"],
    }
    if where_filter is not None:
        debug_query_kwargs["where"] = where_filter

    results = collection.query(**debug_query_kwargs)

    search_time = time.time() - start_time
    all_results = []
    passed = 0

    if results and results["documents"] and results["documents"][0]:
        for i in range(len(results["documents"][0])):
            meta = results["metadatas"][0][i]
            distance = results["distances"][0][i]
            text = results["documents"][0][i]
            passes_threshold = distance < threshold

            if passes_threshold:
                passed += 1

            all_results.append({
                "rank": i + 1,
                "distance": round(distance, 6),
                "similarity": round(1.0 - distance, 6),
                "passes_threshold": passes_threshold,
                "doc_id": meta.get("doc_id", ""),
                "article": meta.get("article", "") or "N/A",
                "pages": meta.get("pages", ""),
                "title": meta.get("title", ""),
                "law_number": meta.get("law_number", ""),
                "law_date": meta.get("law_date", ""),
                "text_preview": text[:200] + ("..." if len(text) > 200 else ""),
                "text_length": len(text),
            })

    return {
        "query": query,
        "user_id": user_id,
        "doc_id_filter": doc_id,
        "total_chunks_in_store": collection.count(),
        "threshold": threshold,
        "top_k": top_k,
        "embed_time_ms": round(embed_time * 1000),
        "search_time_ms": round(search_time * 1000),
        "total_results": len(all_results),
        "passed_threshold": passed,
        "results": all_results,
    }


# ── Deletion ─────────────────────────────────────────────────

async def delete_document_chunks(doc_id: int):
    """Remove all chunks for a given document from the vector store."""
    _ensure_initialized()
    try:
        results = collection.get(
            where={"doc_id": str(doc_id)},
        )
        if results and results["ids"]:
            count = len(results["ids"])
            collection.delete(ids=results["ids"])
            logger.info(f"[doc:{doc_id}] Deleted {count} chunks from vector store")
        else:
            logger.info(f"[doc:{doc_id}] No chunks found to delete")
    except Exception as e:
        logger.warning(f"[doc:{doc_id}] Error deleting chunks: {e}")


async def delete_user_chunks(user_id: int):
    """Remove ALL chunks for a user (account deletion)."""
    _ensure_initialized()
    try:
        results = collection.get(
            where={"user_id": str(user_id)},
        )
        if results and results["ids"]:
            count = len(results["ids"])
            collection.delete(ids=results["ids"])
            logger.info(f"[user:{user_id}] Deleted {count} chunks")
    except Exception as e:
        logger.warning(f"[user:{user_id}] Error deleting chunks: {e}")


# ── Stats ─────────────────────────────────────────────────────

def get_store_stats() -> dict:
    """Return vector store statistics for monitoring."""
    _ensure_initialized()
    if not collection:
        return {"total_chunks": 0, "collection_name": "albanian_laws",
                "similarity_metric": "cosine", "embedding_model": settings.EMBEDDING_MODEL}
    count = collection.count()
    return {
        "total_chunks": count,
        "collection_name": "albanian_laws",
        "similarity_metric": "cosine",
        "embedding_model": settings.EMBEDDING_MODEL,
    }


def get_user_chunk_count(user_id: int) -> int:
    """Count chunks belonging to a specific user."""
    _ensure_initialized()
    try:
        if not collection:
            return 0
        results = collection.get(
            where={"user_id": str(user_id)},
        )
        return len(results["ids"]) if results and results["ids"] else 0
    except Exception:
        return 0


# ── Migration ─────────────────────────────────────────────────

async def migrate_chunks_add_user_id(doc_id_to_user_id: dict):
    """One-time migration: add user_id to existing chunks that lack it.

    Args:
        doc_id_to_user_id: mapping {doc_id (str): user_id (str)}
    """
    _ensure_initialized()
    try:
        if not collection:
            return 0
        all_data = collection.get(include=["metadatas"])
        if not all_data or not all_data["ids"]:
            logger.info("Migration: no chunks to migrate")
            return 0

        ids_to_update = []
        metadatas_to_update = []

        for i, chunk_id in enumerate(all_data["ids"]):
            meta = all_data["metadatas"][i]
            if not meta.get("user_id"):
                doc_id = meta.get("doc_id", "")
                user_id = doc_id_to_user_id.get(doc_id, "1")
                new_meta = dict(meta)
                new_meta["user_id"] = str(user_id)
                ids_to_update.append(chunk_id)
                metadatas_to_update.append(new_meta)

        if ids_to_update:
            batch_size = 100
            for i in range(0, len(ids_to_update), batch_size):
                batch_ids = ids_to_update[i:i + batch_size]
                batch_meta = metadatas_to_update[i:i + batch_size]
                collection.update(ids=batch_ids, metadatas=batch_meta)

            logger.info(f"Migration: updated {len(ids_to_update)} chunks with user_id")
        else:
            logger.info("Migration: all chunks already have user_id")

        return len(ids_to_update)

    except Exception as e:
        logger.error(f"Migration failed: {e}")
        return 0
