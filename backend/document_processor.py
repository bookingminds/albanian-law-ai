"""Document parsing and chunking pipeline — production-ready.

Supports: PDF (.pdf), Word (.docx), Text (.txt)
Pipeline: Upload → Parse text → Clean → Extract metadata → Chunk → Store embeddings

All extractors accept raw bytes (no local filesystem needed).
"""

import io
import re
import logging
import fitz  # PyMuPDF
from docx import Document as DocxDocument
from backend.config import settings

logger = logging.getLogger("rag.processor")


# ── Text Cleaning ─────────────────────────────────────────────

def clean_text(text: str) -> str:
    """Clean extracted text: fix encoding artifacts, normalize whitespace."""
    text = text.replace("\x00", "")
    text = text.replace("\xad", "-")
    text = text.replace("\u2013", "-")
    text = text.replace("\u2014", "-")
    text = text.replace("\u2018", "'")
    text = text.replace("\u2019", "'")
    text = text.replace("\u201c", '"')
    text = text.replace("\u201d", '"')
    text = text.replace("\u00ab", '"')
    text = text.replace("\u00bb", '"')

    text = re.sub(r'[^\S\n]+', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)

    lines = [line.strip() for line in text.split('\n')]
    text = '\n'.join(lines)

    text = re.sub(r'^\d{1,4}$', '', text, flags=re.MULTILINE)
    text = re.sub(r'\n{3,}', '\n\n', text)

    return text.strip()


# ── Text Extraction (from bytes) ─────────────────────────────

def extract_text_from_pdf_bytes(data: bytes) -> list[dict]:
    pages = []
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception as e:
        logger.error(f"PDF open failed ({len(data)} bytes): {e}")
        return []
    try:
        for page_num in range(len(doc)):
            page = doc[page_num]
            text = page.get_text("text")
            cleaned = clean_text(text)
            if cleaned and len(cleaned) > 10:
                pages.append({"text": cleaned, "page": page_num + 1})
    except Exception as e:
        logger.error(f"PDF page extraction error at page {page_num}: {e}")
    finally:
        doc.close()
    logger.info(f"PDF extracted: {len(pages)} pages from bytes ({len(data)} bytes)")
    return pages


def extract_text_from_docx_bytes(data: bytes) -> list[dict]:
    try:
        doc = DocxDocument(io.BytesIO(data))
    except Exception as e:
        logger.error(f"DOCX open failed ({len(data)} bytes): {e}")
        return []
    full_text = []
    for para in doc.paragraphs:
        cleaned = para.text.strip()
        if cleaned:
            full_text.append(cleaned)
    text = clean_text("\n".join(full_text))
    logger.info(f"DOCX extracted: {len(text)} chars from bytes")
    return [{"text": text, "page": 1}]


def extract_text_from_txt_bytes(data: bytes) -> list[dict]:
    encodings = ["utf-8", "latin-1", "cp1252"]
    for enc in encodings:
        try:
            text = clean_text(data.decode(enc))
            logger.info(f"TXT extracted: {len(text)} chars ({enc}) from bytes")
            return [{"text": text, "page": 1}]
        except UnicodeDecodeError:
            continue
    raise ValueError("Could not decode text file with supported encodings.")


def extract_text(file_data: bytes, file_type: str) -> list[dict]:
    """Route to the correct extractor based on file type."""
    extractors = {
        "pdf": extract_text_from_pdf_bytes,
        "docx": extract_text_from_docx_bytes,
        "txt": extract_text_from_txt_bytes,
        "doc": extract_text_from_docx_bytes,
    }
    extractor = extractors.get(file_type.lower())
    if not extractor:
        raise ValueError(f"Unsupported file type: {file_type}")
    return extractor(file_data)


# ── Metadata Extraction ──────────────────────────────────────

def extract_metadata(full_text: str) -> dict:
    """Extract Albanian law metadata from document text."""
    metadata = {}
    header = full_text[:3000]

    law_patterns = [
        r'LIGJ\s*[Nn][Rr]\.?\s*([\d/]+)',
        r'[Ll][Ii][Gg][Jj]\s*[Nn][Rr]\.?\s*([\d/]+)',
        r'[Ll]igji?\s+[Nn]r\.?\s*([\d/]+)',
        r'VENDIM\s*[Nn][Rr]\.?\s*([\d/]+)',
        r'KODI\s+\w+',
    ]
    for pattern in law_patterns:
        match = re.search(pattern, header)
        if match:
            metadata["law_number"] = (
                match.group(1).strip() if match.lastindex else match.group(0).strip()
            )
            break

    date_patterns = [
        r'[Dd]at[ëe]\s+([\d]{1,2}[./][\d]{1,2}[./][\d]{4})',
        r'[Dd]at[ëe]s?\s+([\d]{1,2}\s+\w+\s+[\d]{4})',
        r'(\d{1,2}[./]\d{1,2}[./]\d{4})',
    ]
    for pattern in date_patterns:
        match = re.search(pattern, header)
        if match:
            metadata["law_date"] = match.group(1).strip()
            break

    lines = header.strip().split('\n')
    for line in lines[:15]:
        cleaned = line.strip()
        if len(cleaned) > 10 and not re.match(r'^[\d\s./]+$', cleaned):
            metadata["title"] = cleaned[:200]
            break

    articles = re.findall(r'[Nn]eni\s+(\d+)', full_text)
    if articles:
        metadata["article_count"] = len(set(articles))

    logger.info(f"Metadata extracted: {metadata}")
    return metadata


# ── Chunking (3-tier hybrid) ──────────────────────────────────
#
# Tier 1: Split on legal article boundaries  (Neni X)
# Tier 2: Split long articles by paragraphs  (\n\n)
# Tier 3: Size-based split as last resort    (sentence/word)
#
# Invariant: every chunk keeps its article number, accurate pages,
#            and is between MIN_CHUNK_LEN and ~chunk_size chars.

_MIN_CHUNK_LEN = 40


def _build_page_index(pages: list[dict]) -> tuple[str, list[tuple[int, int, int]]]:
    """Combine page texts and build a sorted offset→page lookup.

    Returns (combined_text, spans) where spans is a sorted list of
    (start_offset, end_offset, page_number) for binary-search lookups.
    """
    parts: list[str] = []
    spans: list[tuple[int, int, int]] = []
    offset = 0
    for p in pages:
        text = p["text"]
        spans.append((offset, offset + len(text), p["page"]))
        parts.append(text)
        offset += len(text) + 2          # +2 for the "\n\n" joiner
    combined = "\n\n".join(parts)
    return combined, spans


def _pages_for_span(start: int, end: int,
                    spans: list[tuple[int, int, int]]) -> list[int]:
    """Return sorted page numbers that overlap [start, end)."""
    result: set[int] = set()
    for sp_start, sp_end, page in spans:
        if sp_start >= end:
            break
        if sp_end > start:
            result.add(page)
    return sorted(result) if result else [1]


def _split_article_by_paragraphs(text: str, chunk_size: int,
                                  overlap: int) -> list[str]:
    """Tier 2: split a long article into paragraph-aligned chunks.

    Paragraphs (separated by blank lines) are accumulated until adding
    the next paragraph would exceed chunk_size.  If a single paragraph
    is still too long, Tier 3 (_split_by_size) handles it.
    """
    paragraphs = re.split(r'\n\s*\n', text)
    paragraphs = [p.strip() for p in paragraphs if p.strip()]

    if len(paragraphs) <= 1:
        return _split_by_size(text, chunk_size, overlap)

    chunks: list[str] = []
    buffer: list[str] = []
    buf_len = 0

    def flush():
        nonlocal buffer, buf_len
        if not buffer:
            return
        joined = "\n\n".join(buffer)
        if len(joined) > chunk_size * 1.5:
            chunks.extend(_split_by_size(joined, chunk_size, overlap))
        elif len(joined) >= _MIN_CHUNK_LEN:
            chunks.append(joined)
        buffer = []
        buf_len = 0

    for para in paragraphs:
        para_len = len(para)

        if para_len > chunk_size * 1.5:
            flush()
            chunks.extend(_split_by_size(para, chunk_size, overlap))
            continue

        would_be = buf_len + para_len + (2 if buffer else 0)
        if would_be > chunk_size and buffer:
            flush()

        buffer.append(para)
        buf_len += para_len + (2 if len(buffer) > 1 else 0)

    flush()

    if overlap > 0 and len(chunks) > 1:
        chunks = _add_overlap(chunks, overlap)

    return chunks


def _add_overlap(chunks: list[str], overlap: int) -> list[str]:
    """Prepend up to `overlap` chars from the previous chunk's tail."""
    result = [chunks[0]]
    for i in range(1, len(chunks)):
        prev_tail = chunks[i - 1][-overlap:]
        nl = prev_tail.find('\n')
        if nl != -1:
            prev_tail = prev_tail[nl + 1:]
        if prev_tail.strip():
            result.append(prev_tail.strip() + "\n\n" + chunks[i])
        else:
            result.append(chunks[i])
    return result


def chunk_text_by_articles(pages: list[dict], chunk_size: int = None,
                           chunk_overlap: int = None) -> list[dict]:
    """Hybrid 3-tier chunking that preserves semantic integrity.

    Tier 1 – Article boundaries:  split on "Neni \\d+"
    Tier 2 – Paragraph grouping:  accumulate \\n\\n-separated paragraphs
    Tier 3 – Size-based fallback:  sentence → word boundary splitting
    """
    chunk_size = chunk_size or settings.CHUNK_SIZE
    chunk_overlap = chunk_overlap or settings.CHUNK_OVERLAP

    combined_text, page_spans = _build_page_index(pages)

    # ── Tier 1: split on article boundaries ───────────────
    article_pattern = r'(?=\b[Nn]eni\s+\d+)'
    article_splits = re.split(article_pattern, combined_text)
    article_splits = [s for s in article_splits if s.strip()]

    use_articles = len(article_splits) > 1

    chunks: list[dict] = []

    if use_articles:
        cursor = 0
        for split_text in article_splits:
            start_pos = combined_text.find(split_text, cursor)
            if start_pos == -1:
                start_pos = cursor

            article_match = re.match(r'[Nn]eni\s+(\d+)', split_text.strip())
            article_num = article_match.group(1) if article_match else None

            if len(split_text.strip()) > chunk_size:
                sub_texts = _split_article_by_paragraphs(
                    split_text.strip(), chunk_size, chunk_overlap
                )
            else:
                sub_texts = [split_text.strip()] if len(split_text.strip()) >= _MIN_CHUNK_LEN else []

            sub_offset = start_pos
            for st in sub_texts:
                local_start = combined_text.find(st[:80], max(sub_offset - 20, 0))
                if local_start == -1:
                    local_start = sub_offset
                pg = _pages_for_span(local_start, local_start + len(st), page_spans)
                chunks.append({
                    "text": st,
                    "article": article_num,
                    "pages": pg,
                    "chunk_index": len(chunks),
                })
                sub_offset = local_start + len(st)

            cursor = start_pos + len(split_text)
    else:
        sub_texts = _split_article_by_paragraphs(
            combined_text, chunk_size, chunk_overlap
        )
        for st in sub_texts:
            pos = combined_text.find(st[:80])
            pg = _pages_for_span(max(pos, 0), max(pos, 0) + len(st), page_spans)
            chunks.append({
                "text": st,
                "article": None,
                "pages": pg,
                "chunk_index": len(chunks),
            })

    logger.info(
        f"Chunking complete: {len(chunks)} chunks "
        f"(target size: {chunk_size}, overlap: {chunk_overlap}, "
        f"articles_detected: {use_articles})"
    )
    sizes = [len(c["text"]) for c in chunks]
    if sizes:
        logger.info(
            f"Chunk sizes: min={min(sizes)}, max={max(sizes)}, "
            f"avg={sum(sizes) // len(sizes)}"
        )

    return chunks


def _split_by_size(text: str, chunk_size: int, overlap: int) -> list[str]:
    """Tier 3: size-based splitting that respects sentence boundaries.

    Boundary preference order: paragraph break → sentence end → word break.
    """
    chunks: list[str] = []
    start = 0
    text_len = len(text)

    while start < text_len:
        end = start + chunk_size

        if end < text_len:
            search_lo = start + chunk_size // 2
            # Prefer paragraph boundary
            pos = text.rfind('\n\n', search_lo, end + 100)
            if pos > start:
                end = pos
            else:
                # Prefer sentence boundary
                pos = text.rfind('. ', search_lo, end + 50)
                if pos > start:
                    end = pos + 1
                else:
                    # Fall back to word boundary
                    pos = text.rfind(' ', search_lo, end + 20)
                    if pos > start:
                        end = pos

        chunk = text[start:end].strip()
        if chunk and len(chunk) >= _MIN_CHUNK_LEN:
            chunks.append(chunk)

        next_start = end - overlap
        if next_start <= start:
            next_start = end
        start = next_start

    return chunks


# ── Full Processing Pipeline ─────────────────────────────────

async def process_document(doc_id: int, user_id: int,
                           file_data: bytes, file_type: str):
    """Full pipeline: extract → clean → metadata → chunk → embed.

    Args:
        doc_id: Document database ID
        user_id: Owner user ID (for vector store isolation)
        file_data: Raw bytes of the uploaded file
        file_type: File extension (pdf, docx, txt)
    """
    from backend.database import (
        update_document_status, update_document_page_count,
        insert_chunks, delete_chunks_for_document,
        get_document,
    )
    from backend.vector_store import add_chunks_to_store

    try:
        await update_document_status(doc_id, "processing")
        logger.info(f"[doc:{doc_id}] Starting processing ({file_type}, {len(file_data)} bytes)")

        pages = extract_text(file_data, file_type)
        if not pages:
            raise ValueError("No text could be extracted from the document.")
        total_chars = sum(len(p["text"]) for p in pages)
        page_count = len(pages)
        logger.info(f"[doc:{doc_id}] Extracted {page_count} pages, {total_chars} chars total")

        await update_document_page_count(doc_id, page_count)

        full_text = "\n\n".join(p["text"] for p in pages)
        metadata = extract_metadata(full_text)

        db_doc = await get_document(doc_id)
        if db_doc:
            db_title = db_doc.get("title") or ""
            db_orig = db_doc.get("original_filename") or ""
            if db_title and len(db_title) > 3:
                metadata["title"] = db_title
            elif db_orig:
                metadata["title"] = re.sub(r'\.[^.]+$', '', db_orig)

        chunks = chunk_text_by_articles(pages)
        if not chunks:
            raise ValueError("Document produced no usable text chunks.")
        logger.info(f"[doc:{doc_id}] Created {len(chunks)} chunks")

        await add_chunks_to_store(doc_id, user_id, chunks, metadata)
        logger.info(f"[doc:{doc_id}] Stored {len(chunks)} embeddings (user_id={user_id})")

        await delete_chunks_for_document(doc_id)
        await insert_chunks(doc_id, user_id, chunks)
        logger.info(f"[doc:{doc_id}] Stored {len(chunks)} chunks in FTS index")

        await update_document_status(
            doc_id, "ready",
            total_chunks=len(chunks),
            metadata=metadata
        )

        logger.info(f"[doc:{doc_id}] Processing complete: {len(chunks)} chunks, {page_count} pages")
        return chunks, metadata

    except Exception as e:
        logger.error(f"[doc:{doc_id}] Processing failed: {e}")
        await update_document_status(doc_id, "failed", error_message=str(e))
        raise
