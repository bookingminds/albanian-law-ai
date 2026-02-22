"""Accuracy-first RAG chat engine — legal assistant for Albanian law.

Pipeline (speed is NOT a priority, quality IS):
1. Query expansion: 10-15 intent-preserving variants
2. Multi-query hybrid search: 150 per method per variant → merge → top 40
3. Confidence gate
4. Context stitching: ±2 neighbor chunks for complete passages
5. Evidence-only answer with structured format + mandatory citations
6. Looping coverage self-check (up to 3 passes) until all parts addressed
7. Conflict detection: cite both sides if documents disagree
8. Role-based source display (admin vs user)
"""

import json
import logging
import time
from backend.config import settings

logger = logging.getLogger("rag.chat")

openai_client = None


def _ensure_openai():
    global openai_client
    if openai_client is not None:
        return
    from openai import OpenAI
    openai_client = OpenAI(api_key=settings.OPENAI_API_KEY)
    logger.info("OpenAI client initialized (chat)")

NO_CONTEXT_RESPONSE = (
    "Nuk mund ta konfirmoj këtë nga dokumentet e disponueshme."
)
SUGGEST_REPHRASE = (
    "Provoni të riformuloni pyetjen ose zgjidhni një dokument specifik."
)

# ── Accuracy-first system prompt with structured output ──────

SYSTEM_PROMPT = """\
Ti je asistent juridik ekspert për legjislacionin shqiptar.
Detyra jote është të japësh përgjigje të sakta, të plota dhe të bazuara
VETËM në fragmentet e dokumenteve që të jepen si KONTEKST.

RREGULLA ABSOLUTE:
1. Përgjigju VETËM nga KONTEKSTI i dhënë. MOS përdor njohuri të tjera.
2. MOS shpik, MOS supoz, MOS gjeneroj informacion jashtë kontekstit.
3. Nëse përgjigja NUK gjendet qartë në kontekst, thuaj SAKTËSISHT:
   "Nuk mund ta konfirmoj këtë nga dokumentet e disponueshme."
4. Nëse përgjigja gjendet vetëm pjesërisht, jep atë që gjen dhe
   thuaj qartë: "Informacion shtesë për [temën] nuk u gjet në dokumente."

FORMATI I PËRGJIGJES (i detyrueshëm):

**Përgjigja:**
[Përgjigje e drejtpërdrejtë, koncize, 1-3 fjali]

**Arsyetimi juridik:**
[Hap pas hapi, bazuar në kontekstin e gjetur. Cito tekst të shkurtër
në thonjëza kur ndihmon. Përdor numra nenesh/ligjesh nga origjinali.]

**Konflikte (nëse ka):**
[Nëse dokumentet janë kontradiktore, cito TË DY burimet dhe
shpjego ndryshimin. P.sh.: "Sipas Neni X: '...', ndërsa Neni Y: '...'"]

**Burimet:**
- [Titulli], Faqe [X] | Neni [nr]
- [Titulli], Faqe [Y] | Neni [nr]

RREGULLA CITIMI:
- Çdo pretendim DUHET të ketë burim (dokument + faqe/neni).
- Numrat e ligjeve, nenet, datat — gjithmonë në formën origjinale.
- MOS trillo numra ligjesh, nenesh apo datash që NUK janë në kontekst.
- Përdor gjuhë formale juridike shqipe.
"""

# ── Coverage check prompt (iterative) ────────────────────────

COVERAGE_CHECK_PROMPT = """\
Je kontrollues cilësie për përgjigje juridike.

Pyetja origjinale e përdoruesit:
"{question}"

Përgjigja aktuale:
"{answer}"

Detyra: Kontrollo nëse ÇDOGJË pjesë e pyetjes u përgjigj me evidencë.

Analizo:
1. Cilat aspekte të pyetjes u mbuluan plotësisht? (me evidencë)
2. Cilat aspekte NUK u mbuluan ose janë të paqarta?
3. A ka kontradikta në përgjigje?

Përgjigju me JSON:
- Nëse gjithçka u mbulua: {{"status": "COMPLETE", "coverage_pct": 100}}
- Nëse ka boshllëqe: {{"status": "GAPS", "coverage_pct": [0-99],
    "missing_aspects": ["aspekt1", "aspekt2"],
    "gap_queries": ["kërkim1 në shqip", "kërkim2 në shqip"],
    "has_conflicts": false}}

VETËM JSON, pa tekst shtesë.
"""


# ═══════════════════════════════════════════════════════════════
#  MAIN PIPELINE — accuracy-first
# ═══════════════════════════════════════════════════════════════

async def generate_answer(question: str, user_id: int,
                          doc_id: int = None,
                          chat_history: list = None,
                          debug_mode: bool = False,
                          is_admin: bool = False) -> dict:
    _ensure_openai()
    """Accuracy-first RAG pipeline. Quality > speed.

    Steps:
    1. Expand query → 10-15 variants
    2. Multi-query hybrid search (150 per method, merge, top 40)
    3. Confidence gate
    4. Context stitch ±2 neighbors
    5. Generate structured answer
    6. Looping coverage check (up to 3 passes)
    """
    from backend.query_expander import expand_query
    from backend.hybrid_search import multi_query_hybrid_search
    from backend.context_stitcher import stitch_neighbors

    total_start = time.time()
    search_user_id = user_id if is_admin else None
    coverage_passes = []

    # ── 1. Query expansion ────────────────────────────────
    expand_start = time.time()
    query_variants = await expand_query(question)
    expand_time = int((time.time() - expand_start) * 1000)

    logger.info(
        f"Query expansion [{user_id}]: '{question[:60]}' -> "
        f"{len(query_variants)} variants ({expand_time}ms)"
    )

    # ── 2. Multi-query hybrid search ─────────────────────
    search_result = await multi_query_hybrid_search(
        queries=query_variants,
        user_id=search_user_id,
        doc_id=doc_id,
    )
    search_time = search_result["search_time_ms"]
    chunks = search_result["chunks"]

    if not chunks:
        logger.info(f"No chunks for user={user_id}: '{question[:80]}'")
        return _no_context_response(search_time + expand_time, debug_mode,
                                     search_result, query_variants)

    # ── 3. Confidence gate ────────────────────────────────
    has_vector_results = any(c.get("similarity", 0) > 0 for c in chunks)
    top_similarity = max(c.get("similarity", 0) for c in chunks)
    has_keyword_results = any("keyword" in c.get("sources", []) for c in chunks)

    if has_vector_results and top_similarity < settings.CONFIDENCE_MIN_SIMILARITY:
        logger.info(
            f"Confidence BLOCKED: top_sim={top_similarity:.4f} < "
            f"threshold={settings.CONFIDENCE_MIN_SIMILARITY}"
        )
        return _low_confidence_response(
            search_time + expand_time, top_similarity,
            settings.CONFIDENCE_MIN_SIMILARITY,
            debug_mode, search_result, query_variants
        )
    if not has_vector_results and has_keyword_results:
        logger.info(
            f"Confidence gate bypassed: no vector results but "
            f"{len(chunks)} keyword chunks found"
        )

    # ── 4. Context stitching (±2 neighbors) ──────────────
    stitch_start = time.time()
    stitch_window = settings.MQ_STITCH_WINDOW
    chunks = await stitch_neighbors(chunks, window=stitch_window)
    stitch_time = int((time.time() - stitch_start) * 1000)

    # ── 5. Generate structured answer ─────────────────────
    context_parts, all_sources = _build_context(chunks)
    context = "\n\n".join(context_parts)
    messages = _build_messages(context, question, chat_history)

    gen_start = time.time()
    try:
        response = openai_client.chat.completions.create(
            model=settings.LLM_MODEL,
            messages=messages,
            temperature=0.05,
            max_tokens=3000,
            top_p=0.9,
        )
        answer = response.choices[0].message.content
    except Exception as e:
        logger.error(f"OpenAI API error during answer generation: {e}")
        return {
            "answer": "Na vjen keq, ndodhi nje gabim teknik gjate gjenerimit te pergjigjes. Ju lutem provoni perseri.",
            "sources": [], "all_sources": all_sources,
            "context_found": True, "chunks_used": len(chunks),
            "top_similarity": top_similarity,
            "search_time_ms": search_time, "expand_time_ms": expand_time,
            "stitch_time_ms": 0, "generation_time_ms": 0,
            "coverage_check_ms": 0, "queries_used": 0, "coverage_passes": 0,
        }
    gen_time = int((time.time() - gen_start) * 1000)

    # ── 6. Looping coverage self-check ────────────────────
    supplemental_time = 0
    max_passes = settings.MQ_COVERAGE_MAX_PASSES

    if not _is_refusal(answer):
        existing_keys = {
            f"{c.get('doc_id', '')}_{c.get('chunk_index', 0)}"
            for c in chunks
        }

        for pass_num in range(1, max_passes + 1):
            cov_start = time.time()
            cov_result = await _coverage_check_iteration(
                question=question,
                answer=answer,
                search_user_id=search_user_id,
                doc_id=doc_id,
                existing_keys=existing_keys,
                context_parts=context_parts,
                all_sources=all_sources,
                chat_history=chat_history,
                pass_num=pass_num,
            )
            cov_time = int((time.time() - cov_start) * 1000)
            supplemental_time += cov_time

            coverage_passes.append({
                "pass": pass_num,
                "status": cov_result.get("status", "ERROR"),
                "coverage_pct": cov_result.get("coverage_pct", 0),
                "time_ms": cov_time,
                "extra_chunks": cov_result.get("extra_chunk_count", 0),
            })

            if cov_result.get("status") == "COMPLETE":
                logger.info(
                    f"Coverage pass {pass_num}: COMPLETE "
                    f"({cov_result.get('coverage_pct', 100)}%)"
                )
                break

            if cov_result.get("updated"):
                answer = cov_result["answer"]
                if cov_result.get("extra_sources"):
                    all_sources.extend(cov_result["extra_sources"])
                if cov_result.get("extra_context"):
                    context_parts.extend(cov_result["extra_context"])
                gen_time += cov_result.get("gen_time", 0)

                logger.info(
                    f"Coverage pass {pass_num}: GAPS found, "
                    f"+{cov_result.get('extra_chunk_count', 0)} chunks, "
                    f"regenerated ({cov_time}ms)"
                )
            else:
                logger.info(
                    f"Coverage pass {pass_num}: GAPS but no new evidence "
                    f"({cov_time}ms)"
                )
                break

    total_time = int((time.time() - total_start) * 1000)
    sources = _deduplicate_sources(all_sources, max_display=5)

    logger.info(
        f"Answer [{user_id}]: {len(answer)} chars | "
        f"chunks={len(chunks)} | expand={expand_time}ms "
        f"search={search_time}ms stitch={stitch_time}ms "
        f"gen={gen_time}ms coverage={supplemental_time}ms "
        f"({len(coverage_passes)} passes) total={total_time}ms"
    )

    result = {
        "answer": answer,
        "sources": sources,
        "all_sources": all_sources,
        "context_found": True,
        "chunks_used": len(chunks),
        "chunks_retrieved": search_result.get("total_candidates", 0),
        "top_similarity": round(top_similarity, 4),
        "search_time_ms": search_time,
        "expand_time_ms": expand_time,
        "stitch_time_ms": stitch_time,
        "generation_time_ms": gen_time,
        "coverage_check_ms": supplemental_time,
        "coverage_passes": len(coverage_passes),
        "queries_used": len(query_variants),
    }

    if debug_mode:
        result["debug"] = search_result.get("debug", {})
        result["debug"]["query_variants"] = query_variants
        result["debug"]["coverage_passes"] = coverage_passes

    return result


# ═══════════════════════════════════════════════════════════════
#  STREAMING VERSION
# ═══════════════════════════════════════════════════════════════

async def generate_answer_stream(question: str, user_id: int,
                                  doc_id: int = None,
                                  chat_history: list = None,
                                  is_admin: bool = False):
    """Streaming accuracy-first pipeline.

    Yields JSON-encoded SSE lines with progress updates, answer tokens,
    and final sources/metrics.
    """
    _ensure_openai()
    from backend.query_expander import expand_query
    from backend.hybrid_search import multi_query_hybrid_search
    from backend.context_stitcher import stitch_neighbors

    total_start = time.time()
    search_user_id = user_id if is_admin else None

    yield json.dumps({"type": "status", "text": "Duke analizuar pyetjen..."})

    # 1. Query expansion
    query_variants = await expand_query(question)
    expand_time = int((time.time() - total_start) * 1000)

    yield json.dumps({
        "type": "status",
        "text": f"Duke kërkuar me {len(query_variants)} variante..."
    })

    # 2. Multi-query hybrid search
    search_result = await multi_query_hybrid_search(
        queries=query_variants,
        user_id=search_user_id,
        doc_id=doc_id,
    )
    search_time = search_result["search_time_ms"]
    chunks = search_result["chunks"]

    if not chunks:
        yield json.dumps({"type": "chunk", "text": NO_CONTEXT_RESPONSE})
        yield json.dumps({"type": "done", "context_found": False,
                          "search_time_ms": search_time})
        return

    # 3. Confidence gate
    has_vector = any(c.get("similarity", 0) > 0 for c in chunks)
    top_similarity = max(c.get("similarity", 0) for c in chunks)
    has_kw = any("keyword" in c.get("sources", []) for c in chunks)

    if has_vector and top_similarity < settings.CONFIDENCE_MIN_SIMILARITY:
        yield json.dumps({
            "type": "chunk",
            "text": f"{NO_CONTEXT_RESPONSE}\n\n{SUGGEST_REPHRASE}"
        })
        yield json.dumps({"type": "done", "context_found": False,
                          "search_time_ms": search_time,
                          "top_similarity": round(top_similarity, 4)})
        return

    # 4. Context stitching ±2
    stitch_window = settings.MQ_STITCH_WINDOW
    chunks = await stitch_neighbors(chunks, window=stitch_window)

    yield json.dumps({
        "type": "status",
        "text": (f"U gjetën {len(chunks)} fragmente nga "
                 f"{search_result.get('total_candidates', '?')} kandidatë. "
                 f"Duke gjeneruar përgjigjen...")
    })

    # 5. Build context and stream answer
    context_parts, all_sources = _build_context(chunks)
    context = "\n\n".join(context_parts)
    messages = _build_messages(context, question, chat_history)

    gen_start = time.time()
    try:
        stream = openai_client.chat.completions.create(
            model=settings.LLM_MODEL,
            messages=messages,
            temperature=0.05,
            max_tokens=3000,
            top_p=0.9,
            stream=True,
        )

        full_answer = ""
        for chunk_resp in stream:
            delta = chunk_resp.choices[0].delta
            if delta.content:
                full_answer += delta.content
                yield json.dumps({"type": "chunk", "text": delta.content})
    except Exception as e:
        logger.error(f"OpenAI API error during streaming: {e}")
        error_msg = "Na vjen keq, ndodhi nje gabim teknik. Ju lutem provoni perseri."
        yield json.dumps({"type": "chunk", "text": error_msg})
        yield json.dumps({"type": "done", "sources": [], "error": True})
        return

    gen_time = int((time.time() - gen_start) * 1000)

    # 6. Coverage check (non-streaming — runs after answer is complete)
    coverage_passes = 0
    supplemental_time = 0

    if not _is_refusal(full_answer):
        existing_keys = {
            f"{c.get('doc_id', '')}_{c.get('chunk_index', 0)}"
            for c in chunks
        }

        for pass_num in range(1, settings.MQ_COVERAGE_MAX_PASSES + 1):
            cov_start = time.time()
            cov_result = await _coverage_check_iteration(
                question=question,
                answer=full_answer,
                search_user_id=search_user_id,
                doc_id=doc_id,
                existing_keys=existing_keys,
                context_parts=context_parts,
                all_sources=all_sources,
                chat_history=chat_history,
                pass_num=pass_num,
            )
            supplemental_time += int((time.time() - cov_start) * 1000)
            coverage_passes += 1

            if cov_result.get("status") == "COMPLETE":
                break

            if cov_result.get("updated"):
                # Append supplemental answer chunk
                supplement = cov_result.get("supplement_text", "")
                if supplement:
                    yield json.dumps({
                        "type": "chunk",
                        "text": f"\n\n---\n**Informacion shtesë (pas verifikimit):**\n{supplement}"
                    })
                    full_answer += supplement

                if cov_result.get("extra_sources"):
                    all_sources.extend(cov_result["extra_sources"])
            else:
                break

    total_time = int((time.time() - total_start) * 1000)
    sources = _deduplicate_sources(all_sources, max_display=5)

    yield json.dumps({
        "type": "sources", "sources": sources, "all_sources": all_sources
    })
    yield json.dumps({
        "type": "done",
        "context_found": True,
        "chunks_used": len(chunks),
        "chunks_retrieved": search_result.get("total_candidates", 0),
        "queries_used": len(query_variants),
        "top_similarity": round(top_similarity, 4),
        "search_time_ms": search_time,
        "expand_time_ms": expand_time,
        "generation_time_ms": gen_time,
        "coverage_check_ms": supplemental_time,
        "coverage_passes": coverage_passes,
    })


# ═══════════════════════════════════════════════════════════════
#  LOOPING COVERAGE SELF-CHECK
# ═══════════════════════════════════════════════════════════════

async def _coverage_check_iteration(
    question: str, answer: str,
    search_user_id: int, doc_id: int,
    existing_keys: set,
    context_parts: list, all_sources: list,
    chat_history: list = None,
    pass_num: int = 1,
) -> dict:
    """Single iteration of coverage self-check.

    1. Ask LLM to identify unsupported parts of the answer
    2. If gaps found, run targeted retrieval for missing aspects
    3. If new evidence found, regenerate with augmented context

    Returns:
        {"status": "COMPLETE"} or
        {"status": "GAPS", "updated": bool, "answer": str, ...}
    """
    try:
        check_resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system",
                 "content": "Kontrollo mbulimin e përgjigjes juridike."},
                {"role": "user",
                 "content": COVERAGE_CHECK_PROMPT.format(
                     question=question, answer=answer[:3000]
                 )},
            ],
            temperature=0.1,
            max_tokens=400,
            response_format={"type": "json_object"},
        )
        raw = check_resp.choices[0].message.content.strip()
        parsed = json.loads(raw)

        status = parsed.get("status", "COMPLETE")
        coverage_pct = parsed.get("coverage_pct", 100)

        if status == "COMPLETE" or coverage_pct >= 95:
            return {"status": "COMPLETE", "coverage_pct": coverage_pct,
                    "raw": parsed}

        gap_queries = parsed.get("gap_queries", [])
        missing = parsed.get("missing_aspects", [])

        if not gap_queries:
            return {"status": "GAPS", "updated": False,
                    "coverage_pct": coverage_pct, "raw": parsed}

        logger.info(
            f"Coverage pass {pass_num}: {coverage_pct}% covered, "
            f"missing: {missing}, {len(gap_queries)} gap queries"
        )

        # Run targeted retrieval for gaps
        from backend.hybrid_search import hybrid_search

        extra_chunks = []
        extra_sources = []
        extra_k = settings.MQ_COVERAGE_EXTRA_K

        for gq in gap_queries[:4]:
            result = await hybrid_search(
                query=gq, user_id=search_user_id,
                doc_id=doc_id, final_k=extra_k,
            )
            for chunk in result.get("chunks", []):
                key = f"{chunk.get('doc_id', '')}_{chunk.get('chunk_index', 0)}"
                if key not in existing_keys:
                    existing_keys.add(key)
                    extra_chunks.append(chunk)

        if not extra_chunks:
            return {
                "status": "GAPS", "updated": False,
                "coverage_pct": coverage_pct,
                "extra_chunk_count": 0,
                "raw": parsed,
            }

        # Stitch neighbors for extra chunks too
        from backend.context_stitcher import stitch_neighbors
        extra_chunks = await stitch_neighbors(
            extra_chunks, window=settings.MQ_STITCH_WINDOW
        )

        # Build augmented context
        extra_context_parts = []
        offset = len(context_parts)
        for i, chunk in enumerate(extra_chunks[:12]):
            doc_title = (chunk.get("title") or
                         f"Dokument #{chunk.get('doc_id', '?')}")
            pages = chunk.get("pages", "")
            article = chunk.get("article", "")
            text = chunk.get("stitched_text") or chunk.get("text", "")

            extra_context_parts.append(
                f"--- KONTEKST SHTESË {offset + i + 1} ---\n"
                f"Dokument: {doc_title}\n"
                f"Faqe: {pages or 'N/A'}\n"
                f"Neni: {article or 'N/A'}\n"
                f"Tekst:\n{text}\n"
            )

            extra_sources.append({
                "title": doc_title,
                "document_id": chunk.get("doc_id", ""),
                "page": chunk.get("page_start", 0) or pages,
                "pages": pages,
                "article": article,
                "chunk_index": chunk.get("chunk_index", 0),
                "similarity": round(chunk.get("similarity", 0), 4),
            })

        # Regenerate with full context
        full_context = "\n\n".join(context_parts + extra_context_parts)
        messages = _build_messages(full_context, question, chat_history)

        gen_start = time.time()
        try:
            response = openai_client.chat.completions.create(
                model=settings.LLM_MODEL,
                messages=messages,
                temperature=0.05,
                max_tokens=3000,
                top_p=0.9,
            )
            new_answer = response.choices[0].message.content
        except Exception as e:
            logger.error(f"OpenAI API error during coverage regeneration: {e}")
            return {"status": "COMPLETE", "extra_sources": extra_sources}
        supp_gen_time = int((time.time() - gen_start) * 1000)

        return {
            "status": "GAPS",
            "updated": True,
            "answer": new_answer,
            "extra_sources": extra_sources,
            "extra_context": extra_context_parts,
            "extra_chunk_count": len(extra_chunks),
            "gen_time": supp_gen_time,
            "coverage_pct": coverage_pct,
            "raw": parsed,
        }

    except Exception as e:
        logger.warning(f"Coverage check pass {pass_num} failed: {e}")
        return {"status": "ERROR", "updated": False,
                "raw": {"error": str(e)}}


# ═══════════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════════

def _build_context(chunks: list[dict]) -> tuple[list[str], list[dict]]:
    """Build numbered context passages and source list from chunks."""
    context_parts = []
    all_sources = []

    for i, chunk in enumerate(chunks):
        doc_title = (chunk.get("title") or
                     f"Dokument #{chunk.get('doc_id', '?')}")
        pages = chunk.get("pages", "")
        page_start = chunk.get("page_start", 0)
        article = chunk.get("article", "")
        sim = chunk.get("similarity", 0)
        rrf = chunk.get("rrf_score", 0)

        text = chunk.get("stitched_text") or chunk.get("text", "")

        neighbor_info = ""
        neighbors = chunk.get("neighbor_indices", [])
        if len(neighbors) > 1:
            neighbor_info = (
                f"  [pasazh i bashkuar: chunk "
                f"{neighbors[0]}-{neighbors[-1]}]\n"
            )

        context_parts.append(
            f"--- KONTEKST {i+1} ---\n"
            f"Dokument: {doc_title}\n"
            f"Faqe: {pages or 'N/A'}\n"
            f"Neni: {article or 'N/A'}\n"
            f"{neighbor_info}"
            f"Tekst:\n{text}\n"
        )

        all_sources.append({
            "title": doc_title,
            "document_id": chunk.get("doc_id", ""),
            "page": page_start or pages,
            "pages": pages,
            "article": article,
            "chunk_index": chunk.get("chunk_index", 0),
            "law_number": chunk.get("law_number", ""),
            "law_date": chunk.get("law_date", ""),
            "similarity": round(sim, 4),
            "rrf_score": round(rrf, 6),
        })

    return context_parts, all_sources


def _build_messages(context: str, question: str,
                    chat_history: list = None) -> list[dict]:
    """Build the LLM messages array."""
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    if chat_history:
        for msg in chat_history[-4:]:
            messages.append({"role": msg["role"], "content": msg["content"]})

    user_message = (
        f"Bazuar VETËM në fragmentet e mëposhtëm të dokumenteve juridike, "
        f"përgjigju pyetjes duke ndjekur formatin e kërkuar "
        f"(Përgjigja / Arsyetimi juridik / Konflikte / Burimet).\n"
        f"Nëse përgjigja nuk gjendet në kontekst, thuaj saktësisht:\n"
        f'"{NO_CONTEXT_RESPONSE}"\n\n'
        f"{context}\n\n"
        f"--- PYETJA ---\n{question}"
    )
    messages.append({"role": "user", "content": user_message})
    return messages


def _is_refusal(answer: str) -> bool:
    """Check if the answer is a refusal/no-context response."""
    refusal_phrases = [
        "nuk mund ta konfirmoj",
        "nuk gjendet",
        "nuk ka informacion",
        "dokumentet e disponueshme",
        "dokumentet e ngarkuara",
    ]
    lower = answer.lower()[:300]
    return any(p in lower for p in refusal_phrases)


def _deduplicate_sources(all_sources: list[dict],
                         max_display: int = 5) -> list[dict]:
    """Group sources by document, merge articles/pages, keep top entries."""
    if not all_sources:
        return []

    by_doc: dict[str, list[dict]] = {}
    for s in all_sources:
        key = str(s.get("document_id", ""))
        by_doc.setdefault(key, []).append(s)

    grouped: list[dict] = []
    for _doc_id, entries in by_doc.items():
        entries.sort(key=lambda x: x.get("similarity", 0), reverse=True)
        best = entries[0]

        articles = []
        pages_set = set()
        seen_art = set()
        for e in entries:
            art = (e.get("article") or "").strip()
            if art and art not in seen_art:
                seen_art.add(art)
                articles.append(art)
            for p in str(e.get("pages", "")).split(","):
                p = p.strip()
                if p and p != "0":
                    pages_set.add(p)

        merged = {
            "title": best["title"],
            "document_id": best["document_id"],
            "law_number": best.get("law_number", ""),
            "law_date": best.get("law_date", ""),
            "similarity": best["similarity"],
            "rrf_score": best.get("rrf_score", 0),
            "articles": articles[:8],
            "pages_list": sorted(
                pages_set,
                key=lambda x: int(x) if x.isdigit() else 0
            )[:10],
            "chunk_count": len(entries),
            "page": best.get("page", ""),
            "article": best.get("article", ""),
        }
        grouped.append(merged)

    grouped.sort(key=lambda x: x.get("similarity", 0), reverse=True)
    return grouped[:max_display]



def _no_context_response(search_time_ms: int = 0,
                          debug_mode: bool = False,
                          search_result: dict = None,
                          query_variants: list = None) -> dict:
    result = {
        "answer": NO_CONTEXT_RESPONSE,
        "sources": [],
        "all_sources": [],
        "context_found": False,
        "chunks_used": 0,
        "chunks_retrieved": 0,
        "top_similarity": 0,
        "search_time_ms": search_time_ms,
        "generation_time_ms": 0,
        "queries_used": len(query_variants or []),
    }
    if debug_mode:
        result["debug"] = (
            search_result.get("debug", {}) if search_result else {}
        )
        if query_variants:
            result["debug"]["query_variants"] = query_variants
    return result


def _low_confidence_response(search_time_ms: int,
                              top_similarity: float,
                              threshold: float,
                              debug_mode: bool = False,
                              search_result: dict = None,
                              query_variants: list = None) -> dict:
    result = {
        "answer": f"{NO_CONTEXT_RESPONSE}\n\n{SUGGEST_REPHRASE}",
        "sources": [],
        "all_sources": [],
        "context_found": False,
        "confidence_blocked": True,
        "top_similarity": round(top_similarity, 4),
        "confidence_threshold": threshold,
        "chunks_used": 0,
        "chunks_retrieved": (
            search_result.get("total_candidates", 0)
            if search_result else 0
        ),
        "search_time_ms": search_time_ms,
        "generation_time_ms": 0,
        "queries_used": len(query_variants or []),
    }
    if debug_mode:
        result["debug"] = (
            search_result.get("debug", {}) if search_result else {}
        )
        if query_variants:
            result["debug"]["query_variants"] = query_variants
    return result
