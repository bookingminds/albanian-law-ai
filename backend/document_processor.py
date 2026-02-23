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


# ── LangChain Chunking ────────────────────────────────────────
#
# Uses RecursiveCharacterTextSplitter with Albanian legal separators:
#   \nNeni  — article boundaries
#   \nKreu  — chapter boundaries
#   \nPika  — sub-article point boundaries
#   \n\n    — paragraph breaks
#   \n      — line breaks
#   .       — sentence boundaries
#   " "     — word boundaries (last resort)
#
# Each chunk gets: article number, section_title, page numbers.

from langchain_text_splitters import RecursiveCharacterTextSplitter

ALBANIAN_LEGAL_SEPARATORS = [
    "\nNeni ", "\nKREU ", "\nKreu ", "\nPika ",
    "\n\n", "\n", ". ", " ",
]

_MIN_CHUNK_LEN = 30


def _build_page_index(pages: list[dict]) -> tuple[str, list[tuple[int, int, int]]]:
    """Combine page texts and build a sorted offset-to-page lookup."""
    parts: list[str] = []
    spans: list[tuple[int, int, int]] = []
    offset = 0
    for p in pages:
        text = p["text"]
        spans.append((offset, offset + len(text), p["page"]))
        parts.append(text)
        offset += len(text) + 2
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


def _detect_section_title(text: str) -> str:
    """Extract a section title from chunk text (Neni X, Kreu X, etc.)."""
    patterns = [
        (r'[Nn]eni\s+(\d+[\w]*)', 'Neni'),
        (r'[Kk][Rr][Ee][Uu]\s+([IVXLCDM]+|\d+)', 'Kreu'),
        (r'[Pp]ika\s+(\d+)', 'Pika'),
        (r'[Ss]eksioni\s+([IVXLCDM]+|\d+)', 'Seksioni'),
    ]
    for pattern, prefix in patterns:
        match = re.search(pattern, text[:200])
        if match:
            return f"{prefix} {match.group(1)}"
    return ""


def _detect_article_number(text: str) -> str | None:
    """Extract article number (Neni X) from chunk text."""
    match = re.search(r'[Nn]eni\s+(\d+)', text[:200])
    return match.group(1) if match else None


def chunk_text_by_articles(pages: list[dict], chunk_size: int = None,
                           chunk_overlap: int = None) -> list[dict]:
    """Split document into chunks using LangChain RecursiveCharacterTextSplitter.

    Separators are tuned for Albanian legal documents: article, chapter,
    point, paragraph, sentence, and word boundaries.
    """
    chunk_size = chunk_size or settings.CHUNK_SIZE
    chunk_overlap = chunk_overlap or settings.CHUNK_OVERLAP

    combined_text, page_spans = _build_page_index(pages)

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        separators=ALBANIAN_LEGAL_SEPARATORS,
        keep_separator=True,
        length_function=len,
        is_separator_regex=False,
    )

    raw_chunks = splitter.split_text(combined_text)

    chunks: list[dict] = []
    search_start = 0

    for idx, chunk_text in enumerate(raw_chunks):
        if len(chunk_text.strip()) < _MIN_CHUNK_LEN:
            continue

        pos = combined_text.find(chunk_text[:80], max(search_start - 50, 0))
        if pos == -1:
            pos = search_start

        pg = _pages_for_span(pos, pos + len(chunk_text), page_spans)
        article = _detect_article_number(chunk_text)
        section = _detect_section_title(chunk_text)

        chunks.append({
            "text": chunk_text.strip(),
            "article": article,
            "section_title": section,
            "pages": pg,
            "chunk_index": len(chunks),
        })

        search_start = pos + len(chunk_text) - chunk_overlap

    # ── Logging ──
    logger.info(
        f"LangChain chunking complete: {len(chunks)} chunks "
        f"(chunk_size={chunk_size}, overlap={chunk_overlap}, "
        f"separators={len(ALBANIAN_LEGAL_SEPARATORS)})"
    )
    sizes = [len(c["text"]) for c in chunks]
    if sizes:
        logger.info(
            f"Chunk stats: count={len(sizes)}, "
            f"min={min(sizes)}, max={max(sizes)}, "
            f"avg={sum(sizes) // len(sizes)}, "
            f"total_chars={sum(sizes)}"
        )
    articles_found = sum(1 for c in chunks if c["article"])
    sections_found = sum(1 for c in chunks if c["section_title"])
    logger.info(
        f"Metadata: {articles_found} chunks with article numbers, "
        f"{sections_found} chunks with section titles"
    )

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
