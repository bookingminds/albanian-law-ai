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
    doc = fitz.open(stream=data, filetype="pdf")
    for page_num in range(len(doc)):
        page = doc[page_num]
        text = page.get_text("text")
        cleaned = clean_text(text)
        if cleaned and len(cleaned) > 10:
            pages.append({"text": cleaned, "page": page_num + 1})
    doc.close()
    logger.info(f"PDF extracted: {len(pages)} pages from bytes ({len(data)} bytes)")
    return pages


def extract_text_from_docx_bytes(data: bytes) -> list[dict]:
    doc = DocxDocument(io.BytesIO(data))
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


# ── Chunking ─────────────────────────────────────────────────

def chunk_text_by_articles(pages: list[dict], chunk_size: int = None,
                           chunk_overlap: int = None) -> list[dict]:
    """Smart chunking that respects article boundaries."""
    chunk_size = chunk_size or settings.CHUNK_SIZE
    chunk_overlap = chunk_overlap or settings.CHUNK_OVERLAP
    chunks = []

    combined_text = ""
    char_to_page = {}
    offset = 0
    for p in pages:
        text = p["text"]
        for i in range(len(text)):
            char_to_page[offset + i] = p["page"]
        combined_text += text + "\n\n"
        offset += len(text) + 2

    article_pattern = r'(?=\b[Nn]eni\s+\d+)'
    article_splits = re.split(article_pattern, combined_text)
    article_splits = [s for s in article_splits if s.strip()]

    def get_pages_for_range(start: int, length: int) -> list[int]:
        page_set = set()
        for i in range(start, min(start + length, len(combined_text))):
            if i in char_to_page:
                page_set.add(char_to_page[i])
        return sorted(page_set) if page_set else [1]

    if len(article_splits) > 1:
        current_pos = 0
        for split_text in article_splits:
            start_pos = combined_text.find(split_text, current_pos)
            if start_pos == -1:
                start_pos = current_pos

            article_match = re.match(r'[Nn]eni\s+(\d+)', split_text.strip())
            article_num = article_match.group(1) if article_match else None
            pages_in_chunk = get_pages_for_range(start_pos, len(split_text))

            if len(split_text) > chunk_size * 1.5:
                sub_chunks = _split_by_size(split_text, chunk_size, chunk_overlap)
                for sub in sub_chunks:
                    if len(sub.strip()) >= 30:
                        chunks.append({
                            "text": sub.strip(),
                            "article": article_num,
                            "pages": pages_in_chunk,
                            "chunk_index": len(chunks),
                        })
            else:
                text = split_text.strip()
                if len(text) >= 30:
                    chunks.append({
                        "text": text,
                        "article": article_num,
                        "pages": pages_in_chunk,
                        "chunk_index": len(chunks),
                    })

            current_pos = start_pos + len(split_text)
    else:
        sub_chunks = _split_by_size(combined_text, chunk_size, chunk_overlap)
        for i, sub in enumerate(sub_chunks):
            start_pos = combined_text.find(sub[:60])
            pages_in_chunk = get_pages_for_range(max(start_pos, 0), len(sub))
            if len(sub.strip()) >= 30:
                chunks.append({
                    "text": sub.strip(),
                    "article": None,
                    "pages": pages_in_chunk,
                    "chunk_index": i,
                })

    logger.info(
        f"Chunking complete: {len(chunks)} chunks "
        f"(target size: {chunk_size}, overlap: {chunk_overlap})"
    )

    sizes = [len(c["text"]) for c in chunks]
    if sizes:
        logger.info(f"Chunk sizes: min={min(sizes)}, max={max(sizes)}, avg={sum(sizes)//len(sizes)}")

    return chunks


def _split_by_size(text: str, chunk_size: int, overlap: int) -> list[str]:
    """Split text into overlapping chunks, respecting sentence boundaries."""
    chunks = []
    start = 0
    text_len = len(text)

    while start < text_len:
        end = start + chunk_size

        if end < text_len:
            newline_pos = text.rfind('\n\n', start + chunk_size // 2, end + 100)
            if newline_pos > start:
                end = newline_pos
            else:
                period_pos = text.rfind('. ', start + chunk_size // 2, end + 50)
                if period_pos > start:
                    end = period_pos + 1
                else:
                    space_pos = text.rfind(' ', start + chunk_size // 2, end + 20)
                    if space_pos > start:
                        end = space_pos

        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)

        start = end - overlap
        if start >= text_len:
            break

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
