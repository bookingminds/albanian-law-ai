"""Document parsing and chunking pipeline.

Supports: PDF (.pdf), Word (.docx), Text (.txt)
Pipeline: Upload → Parse text → Extract metadata → Chunk → Store embeddings
"""

import re
import os
import fitz  # PyMuPDF
from docx import Document as DocxDocument
from pathlib import Path
from backend.config import settings


# ── Text Extraction ────────────────────────────────────────────

def extract_text_from_pdf(file_path: str) -> list[dict]:
    """Extract text from PDF, returning page-level chunks with page numbers."""
    pages = []
    doc = fitz.open(file_path)
    for page_num in range(len(doc)):
        page = doc[page_num]
        text = page.get_text("text")
        if text.strip():
            pages.append({
                "text": text.strip(),
                "page": page_num + 1,
            })
    doc.close()
    return pages


def extract_text_from_docx(file_path: str) -> list[dict]:
    """Extract text from DOCX file."""
    doc = DocxDocument(file_path)
    full_text = []
    for para in doc.paragraphs:
        if para.text.strip():
            full_text.append(para.text.strip())
    return [{"text": "\n".join(full_text), "page": 1}]


def extract_text_from_txt(file_path: str) -> list[dict]:
    """Extract text from plain text file."""
    encodings = ["utf-8", "latin-1", "cp1252"]
    for enc in encodings:
        try:
            with open(file_path, "r", encoding=enc) as f:
                text = f.read()
            return [{"text": text.strip(), "page": 1}]
        except UnicodeDecodeError:
            continue
    raise ValueError("Could not decode text file with supported encodings.")


def extract_text(file_path: str, file_type: str) -> list[dict]:
    """Route to the correct extractor based on file type."""
    extractors = {
        "pdf": extract_text_from_pdf,
        "docx": extract_text_from_docx,
        "txt": extract_text_from_txt,
        "doc": extract_text_from_docx,  # Attempt DOCX parser on .doc
    }
    extractor = extractors.get(file_type.lower())
    if not extractor:
        raise ValueError(f"Unsupported file type: {file_type}")
    return extractor(file_path)


# ── Metadata Extraction ───────────────────────────────────────

def extract_metadata(full_text: str) -> dict:
    """Try to extract Albanian law metadata from document text.

    Looks for patterns like:
    - LIGJ Nr. 7895, datë 27.1.1995
    - Ligji nr. 44/2015
    - VENDIM Nr. 123, datë 15.3.2020
    - FLETORJA ZYRTARE Nr. XX
    """
    metadata = {}

    # Law number patterns
    law_patterns = [
        r'[Ll][Ii][Gg][Jj]\s*[Nn][Rr]\.?\s*([\d/]+)',
        r'LIGJ\s*Nr\.?\s*([\d/]+)',
        r'[Ll]igji?\s+nr\.?\s*([\d/]+)',
        r'Nr\.?\s*([\d/]+)',
    ]
    for pattern in law_patterns:
        match = re.search(pattern, full_text[:2000])
        if match:
            metadata["law_number"] = match.group(1).strip()
            break

    # Date patterns
    date_patterns = [
        r'[Dd]at[ëe]\s+([\d]{1,2}[./][\d]{1,2}[./][\d]{4})',
        r'[Dd]at[ëe]s?\s+([\d]{1,2}\s+\w+\s+[\d]{4})',
        r'(\d{1,2}[./]\d{1,2}[./]\d{4})',
    ]
    for pattern in date_patterns:
        match = re.search(pattern, full_text[:2000])
        if match:
            metadata["law_date"] = match.group(1).strip()
            break

    # Title (first substantial line)
    lines = full_text.strip().split('\n')
    for line in lines[:10]:
        cleaned = line.strip()
        if len(cleaned) > 10 and not cleaned.startswith('Nr'):
            metadata["title"] = cleaned[:200]
            break

    # Article detection for chunking hints
    articles = re.findall(r'[Nn]eni\s+(\d+)', full_text)
    if articles:
        metadata["article_count"] = len(set(articles))

    return metadata


# ── Chunking ──────────────────────────────────────────────────

def chunk_text_by_articles(pages: list[dict], chunk_size: int = None,
                           chunk_overlap: int = None) -> list[dict]:
    """Smart chunking that tries to respect article boundaries.

    Strategy:
    1. First try to split by articles (Neni X)
    2. If articles are too long, split further by size
    3. Maintain page references throughout
    """
    chunk_size = chunk_size or settings.CHUNK_SIZE
    chunk_overlap = chunk_overlap or settings.CHUNK_OVERLAP
    chunks = []

    # Combine all pages into one text with page markers
    page_segments = []
    for p in pages:
        page_segments.append((p["text"], p["page"]))

    # Build a combined text with page tracking
    combined_text = ""
    char_to_page = {}
    offset = 0
    for text, page_num in page_segments:
        for i in range(len(text)):
            char_to_page[offset + i] = page_num
        combined_text += text + "\n\n"
        offset += len(text) + 2

    # Try to split by articles first
    article_pattern = r'(?=\b[Nn]eni\s+\d+)'
    article_splits = re.split(article_pattern, combined_text)

    # Remove empty splits
    article_splits = [s for s in article_splits if s.strip()]

    if len(article_splits) > 1:
        # We found articles - use them as base chunks
        current_pos = 0
        for split_text in article_splits:
            start_pos = combined_text.find(split_text, current_pos)
            if start_pos == -1:
                start_pos = current_pos

            # Extract article number if present
            article_match = re.match(r'[Nn]eni\s+(\d+)', split_text.strip())
            article_num = article_match.group(1) if article_match else None

            # Determine page range
            pages_in_chunk = set()
            for i in range(start_pos, min(start_pos + len(split_text), len(combined_text))):
                if i in char_to_page:
                    pages_in_chunk.add(char_to_page[i])

            # If this article chunk is too large, split further
            if len(split_text) > chunk_size * 2:
                sub_chunks = _split_by_size(split_text, chunk_size, chunk_overlap)
                for j, sub in enumerate(sub_chunks):
                    chunks.append({
                        "text": sub.strip(),
                        "article": article_num,
                        "pages": sorted(pages_in_chunk) if pages_in_chunk else [1],
                        "chunk_index": len(chunks),
                    })
            else:
                if split_text.strip():
                    chunks.append({
                        "text": split_text.strip(),
                        "article": article_num,
                        "pages": sorted(pages_in_chunk) if pages_in_chunk else [1],
                        "chunk_index": len(chunks),
                    })

            current_pos = start_pos + len(split_text)
    else:
        # No articles found - fall back to size-based chunking
        sub_chunks = _split_by_size(combined_text, chunk_size, chunk_overlap)
        for i, sub in enumerate(sub_chunks):
            # Find pages for this chunk
            start_pos = combined_text.find(sub[:50])
            pages_in_chunk = set()
            if start_pos >= 0:
                for j in range(start_pos, min(start_pos + len(sub), len(combined_text))):
                    if j in char_to_page:
                        pages_in_chunk.add(char_to_page[j])

            chunks.append({
                "text": sub.strip(),
                "article": None,
                "pages": sorted(pages_in_chunk) if pages_in_chunk else [1],
                "chunk_index": i,
            })

    return [c for c in chunks if len(c["text"]) > 20]


def _split_by_size(text: str, chunk_size: int, overlap: int) -> list[str]:
    """Split text into overlapping chunks by character count."""
    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size

        # Try to break at a sentence or paragraph boundary
        if end < len(text):
            # Look for paragraph break
            newline_pos = text.rfind('\n\n', start + chunk_size // 2, end + 100)
            if newline_pos > start:
                end = newline_pos
            else:
                # Look for sentence end
                period_pos = text.rfind('. ', start + chunk_size // 2, end + 50)
                if period_pos > start:
                    end = period_pos + 1

        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)

        start = end - overlap
        if start >= len(text):
            break

    return chunks


# ── Full Processing Pipeline ──────────────────────────────────

async def process_document(doc_id: int, file_path: str, file_type: str):
    """Full pipeline: extract → metadata → chunk → embed.

    Returns (chunks, metadata) on success, raises on failure.
    """
    from backend.database import update_document_status
    from backend.vector_store import add_chunks_to_store

    try:
        await update_document_status(doc_id, "processing")

        # 1. Extract text
        pages = extract_text(file_path, file_type)
        if not pages:
            raise ValueError("No text could be extracted from the document.")

        full_text = "\n\n".join(p["text"] for p in pages)

        # 2. Extract metadata
        metadata = extract_metadata(full_text)

        # 3. Chunk
        chunks = chunk_text_by_articles(pages)
        if not chunks:
            raise ValueError("Document produced no usable text chunks.")

        # 4. Add to vector store
        await add_chunks_to_store(doc_id, chunks, metadata)

        # 5. Update DB
        await update_document_status(
            doc_id, "processed",
            total_chunks=len(chunks),
            metadata=metadata
        )

        return chunks, metadata

    except Exception as e:
        await update_document_status(doc_id, "error", error_message=str(e))
        raise
