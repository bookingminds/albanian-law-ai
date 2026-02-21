"""Context stitcher — pull neighbor chunks to avoid cutting off context.

For each selected chunk, retrieves ±N neighbor chunks from the same
document (by chunk_index), merges them into continuous passages,
and deduplicates.
"""

import logging
from backend.database import _get_pool

logger = logging.getLogger("rag.stitcher")


async def stitch_neighbors(chunks: list[dict], window: int = 1) -> list[dict]:
    """For each chunk, pull ±window neighbor chunks and merge content.

    Args:
        chunks: list of chunk dicts (must have 'doc_id'/'document_id' and 'chunk_index')
        window: how many neighbors on each side (1 = ±1, 2 = ±2)

    Returns:
        Enhanced chunk list with 'stitched_text' containing the merged passage
        and 'neighbor_indices' listing which chunk_indexes were merged.
    """
    if not chunks or window < 1:
        for c in chunks:
            c["stitched_text"] = c.get("text") or c.get("content", "")
            c["neighbor_indices"] = [c.get("chunk_index", 0)]
        return chunks

    doc_chunks: dict[str, list[dict]] = {}
    for c in chunks:
        doc_id = str(c.get("doc_id") or c.get("document_id", ""))
        doc_chunks.setdefault(doc_id, []).append(c)

    pool = await _get_pool()
    async with pool.acquire() as conn:
        for doc_id, doc_chunk_list in doc_chunks.items():
            if not doc_id:
                for c in doc_chunk_list:
                    c["stitched_text"] = c.get("text") or c.get("content", "")
                    c["neighbor_indices"] = [c.get("chunk_index", 0)]
                continue

            needed_indexes = set()
            for c in doc_chunk_list:
                idx = c.get("chunk_index", 0)
                for offset in range(-window, window + 1):
                    needed_indexes.add(idx + offset)

            sorted_indexes = sorted(needed_indexes)
            placeholders = ", ".join(f"${i+2}" for i in range(len(sorted_indexes)))
            rows = await conn.fetch(
                f"""SELECT chunk_index, content
                    FROM document_chunks
                    WHERE document_id = $1 AND chunk_index IN ({placeholders})
                    ORDER BY chunk_index""",
                int(doc_id), *sorted_indexes,
            )
            fetched = {row["chunk_index"]: row["content"] for row in rows}

            for c in doc_chunk_list:
                idx = c.get("chunk_index", 0)
                parts = []
                neighbor_ids = []
                for offset in range(-window, window + 1):
                    neighbor_idx = idx + offset
                    if neighbor_idx in fetched:
                        parts.append(fetched[neighbor_idx])
                        neighbor_ids.append(neighbor_idx)

                if parts:
                    c["stitched_text"] = "\n\n".join(parts)
                else:
                    c["stitched_text"] = c.get("text") or c.get("content", "")

                c["neighbor_indices"] = sorted(neighbor_ids)

    logger.info(
        f"Stitched {len(chunks)} chunks with window={window}, "
        f"avg passage length = "
        f"{sum(len(c.get('stitched_text', '')) for c in chunks) // max(len(chunks), 1)} chars"
    )
    return chunks
