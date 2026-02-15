"""RAG chat engine: retrieve context from vector store, generate answer with citations."""

from openai import OpenAI
from backend.config import settings
from backend.vector_store import search_documents

openai_client = OpenAI(api_key=settings.OPENAI_API_KEY)

# Default language for AI responses
DEFAULT_LANG = "al"

NO_DOCS_RESPONSE_AL = "Nuk mund ta konfirmoj këtë nga dokumentet e disponueshme."

SYSTEM_PROMPT_AL = """Ti je "Albanian Law AI", një asistent juridik ekspert që përgjigjet VETËM në bazë të dokumenteve juridike shqiptare të dhëna më poshtë.

RREGULLA TË PËRCAKTUARA:
1. Përdor VETËM informacion nga pjesët KONTEKST të dhëna më poshtë. Mos përdor njohuri nga jashtë.
2. Nëse konteksti nuk përmban informacion të mjaftueshëm për të përgjigjur, duhet të përgjigjesh PIKËRISHT me: "Nuk mund ta konfirmoj këtë nga dokumentet e disponueshme."
3. Citim burimet gjithmonë në fund të përgjigjes nën një pjesë "Burimet:".
4. Për çdo citim përfshi: Titulli i dokumentit, Nr. i ligjit (nëse ka), Data (nëse ka), Neni (nëse ka), Faqe(t).
5. Përgjigju GJITHMONË në gjuhën shqipe, me shqip të qartë dhe profesional juridik.
6. Jini i saktë dhe i plotë. Citoni teksti përkatës direkt kur ndihmon.
7. Nëse pyetja mund të përgjigjet pjesërisht, përgjigju atë që mundesh dhe thuaj qartë çfarë nuk mund të konfirmohet.
8. Mos trilloni nena, numra ligjesh apo referenca juridike që nuk janë në kontekstin e dhënë.
9. Numrat e ligjeve, nenet, datat dhe referencat juridike mbahen në formën origjinale (p.sh. Ligji Nr. 7850, Neni 5).

FORMATIMI I BURIMEVE:
---
Burimet:
- [Titulli i dokumentit] | Ligji Nr. [numri], datë [data] | Neni [numri] | Faqe [faqet]
"""


async def generate_answer(question: str, chat_history: list = None) -> dict:
    """Main RAG pipeline: retrieve → build prompt → generate.

    Returns:
        {
            "answer": str,
            "sources": [{"title", "law_number", "law_date", "article", "pages"}],
            "context_found": bool
        }
    """
    chunks = await search_documents(question, top_k=settings.TOP_K_RESULTS)

    if not chunks:
        return {
            "answer": NO_DOCS_RESPONSE_AL,
            "sources": [],
            "context_found": False,
        }

    relevant_chunks = [c for c in chunks if c["distance"] < 0.65]

    if not relevant_chunks:
        return {
            "answer": NO_DOCS_RESPONSE_AL,
            "sources": [],
            "context_found": False,
        }

    context_parts = []
    sources = []
    seen_sources = set()

    for i, chunk in enumerate(relevant_chunks):
        context_parts.append(
            f"--- KONTEKST {i+1} ---\n"
            f"Dokument: {chunk['title']}\n"
            f"Ligji Nr.: {chunk['law_number']}\n"
            f"Data: {chunk['law_date']}\n"
            f"Neni: {chunk['article']}\n"
            f"Faqe: {chunk['pages']}\n"
            f"Tekst:\n{chunk['text']}\n"
        )
        source_key = f"{chunk['doc_id']}_{chunk['article']}_{chunk['pages']}"
        if source_key not in seen_sources:
            seen_sources.add(source_key)
            sources.append({
                "title": chunk["title"],
                "law_number": chunk["law_number"],
                "law_date": chunk["law_date"],
                "article": chunk["article"],
                "pages": chunk["pages"],
                "doc_id": chunk["doc_id"],
            })

    context = "\n\n".join(context_parts)
    messages = [{"role": "system", "content": SYSTEM_PROMPT_AL}]

    if chat_history:
        for msg in chat_history[-6:]:
            messages.append({"role": msg["role"], "content": msg["content"]})

    user_message = f"""Bazuar në fragmentet e mëposhtëm të dokumenteve juridike, përgjigju pyetjes së përdoruesit. Përgjigju gjithmonë në shqip.

{context}

--- PYETJA E PËRDORUESIT ---
{question}
"""
    messages.append({"role": "user", "content": user_message})

    response = openai_client.chat.completions.create(
        model=settings.LLM_MODEL,
        messages=messages,
        temperature=0.1,
        max_tokens=2000,
    )

    answer = response.choices[0].message.content

    return {
        "answer": answer,
        "sources": sources,
        "context_found": True,
    }
