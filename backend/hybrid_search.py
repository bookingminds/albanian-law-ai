"""Hybrid search engine: vector + keyword with smart re-ranking.

Pipeline:
1. Vector search (ChromaDB)  → top HYBRID_FETCH_K results
2. Keyword search (FTS5)     → top HYBRID_FETCH_K results
3. Merge using Reciprocal Rank Fusion (RRF)
4. Apply post-RRF boosts:
   a) Exact keyword boost   — chunks that contain the query words verbatim
   b) Article-number boost  — chunks whose "Neni XX" matches a Neni in the query
   c) Article cohesion      — keep sibling chunks from the same article together
5. Return top HYBRID_FINAL_K chunks
"""

import logging
import re
import time
from backend.config import settings

logger = logging.getLogger("rag.hybrid")

# ── Albanian stemming helpers (shared with database._build_fts_query) ─

_ALBANIAN_STOPWORDS = frozenset(
    'dhe ose per nga nje tek te ne me se ka si do jane eshte nuk qe i e '
    'ky kjo keto ato por nese edhe mund duhet'.split()
)


def _extract_query_keywords(query: str) -> list[str]:
    """Extract meaningful keywords from query, lowercased, without stopwords."""
    words = re.findall(r'\b\w{2,}\b', query.lower())
    return [w for w in words if w not in _ALBANIAN_STOPWORDS]


def _extract_neni_numbers(query: str) -> set[str]:
    """Extract article numbers from query like 'Neni 57' → {'57'}."""
    return set(re.findall(r'[Nn]eni\s+(\d+)', query))


async def hybrid_search(query: str, user_id: int = None,
                        doc_id: int = None,
                        final_k: int = None) -> dict:
    """Run hybrid vector + keyword search with smart re-ranking.

    Returns:
        {
            "chunks": list[dict],
            "vector_count": int,
            "keyword_count": int,
            "total_candidates": int,
            "search_time_ms": int,
            "debug": {...}
        }
    """
    from backend.vector_store import search_documents
    from backend.database import keyword_search_chunks

    final_k = final_k or settings.HYBRID_FINAL_K
    fetch_k = settings.HYBRID_FETCH_K
    rrf_k = 60

    start_time = time.time()

    # Pre-compute query signals for boosting
    query_keywords = _extract_query_keywords(query)
    query_neni_numbers = _extract_neni_numbers(query)

    # ── 1. Vector search ──────────────────────────────────
    vector_start = time.time()
    vector_results = await search_documents(
        query=query,
        user_id=user_id,
        doc_id=doc_id,
        top_k=fetch_k,
        threshold=1.0,
    )
    vector_time = int((time.time() - vector_start) * 1000)

    # ── 2. Keyword search (FTS5) ──────────────────────────
    keyword_start = time.time()
    keyword_results = await keyword_search_chunks(
        query=query,
        user_id=user_id,
        document_id=doc_id,
        limit=fetch_k,
    )
    keyword_time = int((time.time() - keyword_start) * 1000)

    # ── 3. Build candidate pool ───────────────────────────
    candidates: dict[str, dict] = {}
    debug_vector = []
    debug_keyword = []

    for rank, chunk in enumerate(vector_results):
        key = _chunk_key(chunk)
        if key not in candidates:
            candidates[key] = _make_candidate(chunk, vector_rank=rank + 1)
        else:
            cand = candidates[key]
            cand["vector_rank"] = rank + 1
            if "vector" not in cand["sources"]:
                cand["sources"].append("vector")
            if chunk.get("similarity", 0) > cand["similarity"]:
                cand["similarity"] = chunk["similarity"]
                cand["distance"] = chunk["distance"]

        debug_vector.append({
            "rank": rank + 1,
            "similarity": chunk.get("similarity", 0),
            "text_preview": chunk["text"][:80],
        })

    for rank, kw_chunk in enumerate(keyword_results):
        text = kw_chunk.get("content", "")
        key = _chunk_key_kw(kw_chunk)
        if key not in candidates:
            candidates[key] = _make_candidate_kw(kw_chunk, keyword_rank=rank + 1)
        else:
            cand = candidates[key]
            cand["keyword_rank"] = rank + 1
            if "keyword" not in cand["sources"]:
                cand["sources"].append("keyword")

        debug_keyword.append({
            "rank": rank + 1,
            "fts_rank": kw_chunk.get("fts_rank", 0),
            "text_preview": text[:80],
        })

    # ── 4. Compute base RRF scores ────────────────────────
    v_weight = settings.HYBRID_VECTOR_WEIGHT
    k_weight = settings.HYBRID_KEYWORD_WEIGHT

    for cand in candidates.values():
        score = 0.0
        if cand["vector_rank"] is not None:
            score += v_weight * (1.0 / (rrf_k + cand["vector_rank"]))
        if cand["keyword_rank"] is not None:
            score += k_weight * (1.0 / (rrf_k + cand["keyword_rank"]))
        cand["rrf_score"] = score

    # ── 5. Post-RRF boosts ────────────────────────────────
    for cand in candidates.values():
        boost = 0.0
        text_lower = cand["text"].lower()
        article = (cand.get("article") or "").strip()

        # (a) Exact keyword boost — reward chunks containing query words
        if query_keywords:
            matches = sum(1 for kw in query_keywords if kw in text_lower)
            keyword_ratio = matches / len(query_keywords)
            boost += keyword_ratio * 0.005  # up to +0.005

        # (b) Article-number boost — if query asks for "Neni 57" and this
        #     chunk's article is "57", give a strong boost
        if query_neni_numbers and article:
            # Extract number from article field (may be "57" or "Neni 57")
            art_nums = set(re.findall(r'\d+', article))
            if art_nums & query_neni_numbers:
                boost += 0.02  # strong boost for exact article match

        # (c) "Neni" presence boost — legal-article chunks are generally
        #     more useful than preamble/transition chunks
        if re.search(r'\bNeni\s+\d+', cand["text"]):
            boost += 0.001

        cand["boost"] = round(boost, 6)
        cand["final_score"] = cand["rrf_score"] + boost

    # ── 6. Sort by final_score, select top candidates ─────
    all_ranked = sorted(candidates.values(),
                        key=lambda x: x["final_score"], reverse=True)

    # Take wide pool (2× final_k) for cohesion grouping
    pool = all_ranked[:final_k * 3]

    # ── 7. Article cohesion — if top chunks belong to article X,
    #     pull in neighbouring chunks from the same article ──
    final_chunks = _apply_article_cohesion(pool, final_k)

    total_time = int((time.time() - start_time) * 1000)

    from_vector = sum(1 for c in final_chunks if "vector" in c["sources"])
    from_keyword = sum(1 for c in final_chunks if "keyword" in c["sources"])
    from_both = sum(1 for c in final_chunks if len(c["sources"]) > 1)

    logger.info(
        f"Hybrid search [user={user_id}]: "
        f"vector={len(vector_results)}, keyword={len(keyword_results)}, "
        f"candidates={len(candidates)}, final={len(final_chunks)} "
        f"(v={from_vector}, k={from_keyword}, both={from_both}) | "
        f"time={total_time}ms (vec={vector_time}ms, kw={keyword_time}ms)"
    )

    return {
        "chunks": final_chunks,
        "vector_count": from_vector,
        "keyword_count": from_keyword,
        "both_count": from_both,
        "total_candidates": len(candidates),
        "search_time_ms": total_time,
        "debug": {
            "vector_results": len(vector_results),
            "keyword_results": len(keyword_results),
            "vector_time_ms": vector_time,
            "keyword_time_ms": keyword_time,
            "query_keywords": query_keywords,
            "query_neni_numbers": list(query_neni_numbers),
            "vector_top5": debug_vector[:5],
            "keyword_top5": debug_keyword[:5],
            "final_ranking": [
                {
                    "rrf_score": round(c["rrf_score"], 6),
                    "boost": c.get("boost", 0),
                    "final_score": round(c.get("final_score", 0), 6),
                    "similarity": c["similarity"],
                    "vector_rank": c["vector_rank"],
                    "keyword_rank": c["keyword_rank"],
                    "article": c.get("article", ""),
                    "sources": c["sources"],
                    "text_preview": c["text"][:100],
                }
                for c in final_chunks
            ],
        },
    }


# ── Article Cohesion ──────────────────────────────────────

def _apply_article_cohesion(pool: list[dict], final_k: int) -> list[dict]:
    """From the ranked pool, keep top chunks but pull in siblings from
    the same article so related legal content stays together.

    Rules:
    - Start with the #1 ranked chunk.
    - If article X appears in top 3, collect all pool chunks from article X.
    - Fill remaining slots with next-best non-duplicate chunks.
    - Never exceed final_k total.
    """
    if not pool:
        return []

    selected: list[dict] = []
    selected_keys: set[str] = set()
    boosted_articles: set[str] = set()

    # Identify which articles dominate the top 3 positions
    for cand in pool[:3]:
        art = (cand.get("article") or "").strip()
        if art:
            boosted_articles.add(art)

    # Phase 1: Add all chunks from boosted articles (respecting final_k)
    if boosted_articles:
        for cand in pool:
            art = (cand.get("article") or "").strip()
            if art in boosted_articles:
                key = _candidate_key(cand)
                if key not in selected_keys:
                    selected.append(cand)
                    selected_keys.add(key)
                    if len(selected) >= final_k:
                        break

    # Phase 2: Fill remaining slots with best-ranked non-duplicate chunks
    for cand in pool:
        if len(selected) >= final_k:
            break
        key = _candidate_key(cand)
        if key not in selected_keys:
            selected.append(cand)
            selected_keys.add(key)

    # Re-sort final selection by final_score so the answer context is ordered
    selected.sort(key=lambda x: x.get("final_score", 0), reverse=True)
    return selected[:final_k]


# ── Candidate builders ────────────────────────────────────

def _make_candidate(chunk: dict, vector_rank: int) -> dict:
    return {
        "text": chunk["text"],
        "doc_id": chunk.get("doc_id", ""),
        "user_id": chunk.get("user_id", ""),
        "article": chunk.get("article", ""),
        "pages": chunk.get("pages", ""),
        "page_start": _parse_page_start(chunk.get("pages", "")),
        "title": chunk.get("title", ""),
        "law_number": chunk.get("law_number", ""),
        "law_date": chunk.get("law_date", ""),
        "chunk_index": chunk.get("chunk_index", 0),
        "char_count": chunk.get("char_count", len(chunk["text"])),
        "similarity": chunk.get("similarity", 0),
        "distance": chunk.get("distance", 1.0),
        "vector_rank": vector_rank,
        "keyword_rank": None,
        "rrf_score": 0.0,
        "boost": 0.0,
        "final_score": 0.0,
        "sources": ["vector"],
    }


def _make_candidate_kw(kw_chunk: dict, keyword_rank: int) -> dict:
    text = kw_chunk.get("content", "")
    return {
        "text": text,
        "doc_id": str(kw_chunk.get("document_id", "")),
        "user_id": str(kw_chunk.get("user_id", "")),
        "article": kw_chunk.get("article", ""),
        "pages": kw_chunk.get("pages", ""),
        "page_start": kw_chunk.get("page_start", 0),
        "title": "",
        "law_number": "",
        "law_date": "",
        "chunk_index": kw_chunk.get("chunk_index", 0),
        "char_count": kw_chunk.get("char_count", len(text)),
        "similarity": 0,
        "distance": 1.0,
        "vector_rank": None,
        "keyword_rank": keyword_rank,
        "rrf_score": 0.0,
        "boost": 0.0,
        "final_score": 0.0,
        "sources": ["keyword"],
    }


# ── Helpers ───────────────────────────────────────────────

def _chunk_key(chunk: dict) -> str:
    return f"{chunk.get('doc_id', '')}_{chunk.get('chunk_index', 0)}"


def _chunk_key_kw(kw_chunk: dict) -> str:
    return f"{kw_chunk.get('document_id', '')}_{kw_chunk.get('chunk_index', 0)}"


def _candidate_key(cand: dict) -> str:
    return f"{cand.get('doc_id', '')}_{cand.get('chunk_index', 0)}"


def _parse_page_start(pages_str: str) -> int:
    if not pages_str:
        return 0
    try:
        return int(pages_str.split(",")[0])
    except (ValueError, IndexError):
        return 0


# ── Multi-query hybrid search ────────────────────────────────

async def multi_query_hybrid_search(
    queries: list[str],
    user_id: int = None,
    doc_id: int = None,
    fetch_k: int = None,
    final_k: int = None,
) -> dict:
    """Accuracy-first multi-query hybrid search.

    Runs hybrid search for each query variant (up to 15), merges all
    candidates, deduplicates, applies multi-query boost, and re-ranks.

    Uses MQ_FETCH_K (default 150) per method per variant for maximum recall.
    Returns MQ_FINAL_K (default 40) chunks after merge+rerank.
    """
    import asyncio

    fetch_k = fetch_k or settings.MQ_FETCH_K
    final_k = final_k or settings.MQ_FINAL_K

    start_time = time.time()

    original_fetch_k = settings.HYBRID_FETCH_K
    original_final_k = settings.HYBRID_FINAL_K

    # Override settings for wide recall per query
    settings.HYBRID_FETCH_K = fetch_k
    settings.HYBRID_FINAL_K = fetch_k  # don't truncate per-query

    all_candidates: dict[str, dict] = {}
    per_query_debug = []

    try:
        # Run all queries concurrently for maximum parallelism
        tasks = [
            hybrid_search(query=q, user_id=user_id, doc_id=doc_id,
                          final_k=fetch_k)
            for q in queries
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for i, result in enumerate(results):
            q = queries[i]
            if isinstance(result, Exception):
                logger.warning(f"Multi-query failed for variant {i}: {result}")
                per_query_debug.append({
                    "query": q[:80], "error": str(result), "chunks": 0
                })
                continue

            chunks = result.get("chunks", [])
            per_query_debug.append({
                "query": q[:80],
                "chunks": len(chunks),
                "search_time_ms": result.get("search_time_ms", 0),
            })

            for chunk in chunks:
                key = _candidate_key(chunk)
                if key not in all_candidates:
                    chunk["query_hits"] = 1
                    chunk["found_by_queries"] = [i]
                    all_candidates[key] = chunk
                else:
                    existing = all_candidates[key]
                    existing["query_hits"] = existing.get("query_hits", 1) + 1
                    existing.setdefault("found_by_queries", []).append(i)
                    if chunk.get("similarity", 0) > existing.get("similarity", 0):
                        existing["similarity"] = chunk["similarity"]
                        existing["distance"] = chunk["distance"]
                    if chunk.get("final_score", 0) > existing.get("final_score", 0):
                        existing["final_score"] = chunk["final_score"]
                        existing["rrf_score"] = chunk["rrf_score"]
                        existing["boost"] = chunk["boost"]

    finally:
        settings.HYBRID_FETCH_K = original_fetch_k
        settings.HYBRID_FINAL_K = original_final_k

    # ── Multi-query boost (stronger for accuracy-first) ──
    # Chunks found by more query variants are more likely relevant
    max_queries = len(queries)
    for cand in all_candidates.values():
        hits = cand.get("query_hits", 1)
        # Scale boost by fraction of queries that found this chunk
        # Found by 1/15 = 0, found by 5/15 = +0.004, found by 10/15 = +0.009
        ratio = (hits - 1) / max(max_queries - 1, 1)
        multi_boost = 0.012 * ratio
        cand["multi_query_boost"] = round(multi_boost, 6)
        cand["final_score"] = cand.get("final_score", 0) + multi_boost

    # ── Final ranking and selection ──────────────────────
    ranked = sorted(all_candidates.values(),
                    key=lambda x: x.get("final_score", 0), reverse=True)

    # Apply article cohesion on a wide pool (3× final_k)
    final_chunks = _apply_article_cohesion(ranked[:final_k * 3], final_k)

    total_time = int((time.time() - start_time) * 1000)

    multi_hit = sum(1 for c in all_candidates.values() if c.get("query_hits", 1) > 1)

    logger.info(
        f"Multi-query search: {len(queries)} variants, "
        f"{len(all_candidates)} unique candidates ({multi_hit} multi-hit), "
        f"{len(final_chunks)} final | {total_time}ms"
    )

    return {
        "chunks": final_chunks,
        "total_candidates": len(all_candidates),
        "queries_used": len(queries),
        "search_time_ms": total_time,
        "debug": {
            "per_query": per_query_debug,
            "total_unique_candidates": len(all_candidates),
            "multi_hit_chunks": multi_hit,
            "final_ranking": [
                {
                    "final_score": round(c.get("final_score", 0), 6),
                    "similarity": c.get("similarity", 0),
                    "query_hits": c.get("query_hits", 1),
                    "multi_query_boost": c.get("multi_query_boost", 0),
                    "article": c.get("article", ""),
                    "sources": c.get("sources", []),
                    "text_preview": c.get("text", "")[:100],
                }
                for c in final_chunks
            ],
        },
    }
