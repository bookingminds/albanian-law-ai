"""ChromaDB vector store for document embeddings."""

import chromadb
from chromadb.config import Settings as ChromaSettings
from openai import OpenAI
from backend.config import settings

# Initialize ChromaDB (persistent, local)
chroma_client = chromadb.PersistentClient(
    path=str(settings.DATA_DIR / "chroma_db"),
)

# Get or create collection
collection = chroma_client.get_or_create_collection(
    name="albanian_laws",
    metadata={"hnsw:space": "cosine"},
)

# OpenAI client for embeddings
openai_client = OpenAI(api_key=settings.OPENAI_API_KEY)


def get_embedding(text: str) -> list[float]:
    """Generate embedding for a single text using OpenAI."""
    response = openai_client.embeddings.create(
        model=settings.EMBEDDING_MODEL,
        input=text,
    )
    return response.data[0].embedding


def get_embeddings_batch(texts: list[str]) -> list[list[float]]:
    """Generate embeddings for a batch of texts."""
    # OpenAI allows up to ~8000 tokens per batch call; process in chunks
    all_embeddings = []
    batch_size = 50
    for i in range(0, len(texts), batch_size):
        batch = texts[i:i + batch_size]
        response = openai_client.embeddings.create(
            model=settings.EMBEDDING_MODEL,
            input=batch,
        )
        all_embeddings.extend([d.embedding for d in response.data])
    return all_embeddings


async def add_chunks_to_store(doc_id: int, chunks: list[dict],
                               doc_metadata: dict):
    """Add document chunks to ChromaDB with embeddings."""
    texts = [c["text"] for c in chunks]
    embeddings = get_embeddings_batch(texts)

    ids = []
    metadatas = []
    for i, chunk in enumerate(chunks):
        chunk_id = f"doc{doc_id}_chunk{i}"
        ids.append(chunk_id)
        metadatas.append({
            "doc_id": str(doc_id),
            "chunk_index": i,
            "article": chunk.get("article") or "",
            "pages": ",".join(str(p) for p in chunk.get("pages", [])),
            "title": doc_metadata.get("title", ""),
            "law_number": doc_metadata.get("law_number", ""),
            "law_date": doc_metadata.get("law_date", ""),
        })

    collection.add(
        ids=ids,
        embeddings=embeddings,
        documents=texts,
        metadatas=metadatas,
    )


async def search_documents(query: str, top_k: int = None) -> list[dict]:
    """Search the vector store for relevant chunks."""
    top_k = top_k or settings.TOP_K_RESULTS

    # Check if collection has any documents
    if collection.count() == 0:
        return []

    query_embedding = get_embedding(query)

    results = collection.query(
        query_embeddings=[query_embedding],
        n_results=min(top_k, collection.count()),
        include=["documents", "metadatas", "distances"],
    )

    chunks = []
    if results and results["documents"]:
        for i in range(len(results["documents"][0])):
            meta = results["metadatas"][0][i]
            chunks.append({
                "text": results["documents"][0][i],
                "distance": results["distances"][0][i],
                "doc_id": meta.get("doc_id", ""),
                "article": meta.get("article", ""),
                "pages": meta.get("pages", ""),
                "title": meta.get("title", ""),
                "law_number": meta.get("law_number", ""),
                "law_date": meta.get("law_date", ""),
            })

    return chunks


async def delete_document_chunks(doc_id: int):
    """Remove all chunks for a given document from the vector store."""
    # Get all chunk IDs for this document
    try:
        results = collection.get(
            where={"doc_id": str(doc_id)},
        )
        if results and results["ids"]:
            collection.delete(ids=results["ids"])
    except Exception:
        # If collection is empty or doc doesn't exist, that's fine
        pass
