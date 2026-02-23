"""PostgreSQL database for document metadata, chunks tracking, chat history, users.

Uses asyncpg with a connection pool.  Requires DATABASE_URL to be set.
"""

import asyncpg
import json
import logging
import re
from datetime import datetime
from backend.config import settings

logger = logging.getLogger("rag.database")


def _parse_ts(val) -> datetime | None:
    """Convert a string/datetime to a datetime object, or None."""
    if val is None:
        return None
    if isinstance(val, datetime):
        return val
    s = str(val).strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


_pool: asyncpg.Pool | None = None


async def _get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        url = settings.DATABASE_URL
        if not url:
            raise RuntimeError("DATABASE_URL is not set")
        _pool = await asyncpg.create_pool(url, min_size=2, max_size=10)
        logger.info("PostgreSQL connection pool created")
    return _pool


async def close_pool():
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


async def init_db():
    """Create tables, indexes, and seed data."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        # ── Users ──
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT DEFAULT '',
                is_admin BOOLEAN DEFAULT FALSE,
                supabase_uid TEXT UNIQUE,
                trial_ends_at TIMESTAMPTZ,
                trial_used_at TIMESTAMPTZ,
                signup_ip TEXT,
                stripe_customer_id TEXT,
                stripe_subscription_id TEXT,
                is_premium BOOLEAN DEFAULT FALSE,
                subscription_status TEXT DEFAULT '',
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        for col, coltype, default in [
            ("stripe_customer_id", "TEXT", "NULL"),
            ("stripe_subscription_id", "TEXT", "NULL"),
            ("is_premium", "BOOLEAN", "FALSE"),
            ("subscription_status", "TEXT", "''"),
        ]:
            try:
                await conn.execute(
                    f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {col} {coltype} DEFAULT {default}"
                )
            except Exception:
                pass

        # ── Documents ──
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS documents (
                id SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES users(id),
                filename TEXT NOT NULL,
                original_filename TEXT NOT NULL,
                file_type TEXT NOT NULL,
                file_size INTEGER NOT NULL,
                title TEXT,
                law_number TEXT,
                law_date TEXT,
                status TEXT DEFAULT 'processing',
                total_chunks INTEGER DEFAULT 0,
                page_count INTEGER DEFAULT 0,
                error_message TEXT,
                metadata_json TEXT DEFAULT '{}',
                storage_bucket TEXT DEFAULT 'Ligje',
                storage_path TEXT,
                uploaded_at TIMESTAMPTZ DEFAULT NOW(),
                processed_at TIMESTAMPTZ
            )
        """)
        # Add storage columns if table already exists without them
        for col, default in [("storage_bucket", "'Ligje'"), ("storage_path", "NULL")]:
            try:
                await conn.execute(
                    f"ALTER TABLE documents ADD COLUMN IF NOT EXISTS {col} TEXT DEFAULT {default}"
                )
            except Exception:
                pass

        # ── Document Chunks ──
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS document_chunks (
                id SERIAL PRIMARY KEY,
                document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL,
                chunk_index INTEGER NOT NULL,
                content TEXT NOT NULL,
                article TEXT,
                section_title TEXT DEFAULT '',
                pages TEXT,
                page_start INTEGER,
                page_end INTEGER,
                char_count INTEGER DEFAULT 0
            )
        """)
        try:
            await conn.execute(
                "ALTER TABLE document_chunks ADD COLUMN IF NOT EXISTS section_title TEXT DEFAULT ''"
            )
        except Exception:
            pass

        # ── Chat Messages ──
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS chat_messages (
                id SERIAL PRIMARY KEY,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                sources_json TEXT DEFAULT '[]',
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)

        # ── Subscriptions ──
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                purchase_token TEXT,
                product_id TEXT,
                platform TEXT DEFAULT 'google_play',
                status TEXT NOT NULL,
                current_period_end TIMESTAMPTZ,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)

        # ── Suggested Questions ──
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS suggested_questions (
                id SERIAL PRIMARY KEY,
                category TEXT NOT NULL,
                question TEXT NOT NULL,
                sort_order INTEGER DEFAULT 0,
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(category, question)
            )
        """)

        # ── Indexes ──
        index_statements = [
            "CREATE INDEX IF NOT EXISTS idx_documents_user_id ON documents(user_id)",
            "CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status)",
            "CREATE INDEX IF NOT EXISTS idx_documents_user_status ON documents(user_id, status)",
            "CREATE INDEX IF NOT EXISTS idx_chunks_document_id ON document_chunks(document_id)",
            "CREATE INDEX IF NOT EXISTS idx_chunks_user_id ON document_chunks(user_id)",
            "CREATE INDEX IF NOT EXISTS idx_chunks_user_doc ON document_chunks(user_id, document_id)",
            "CREATE INDEX IF NOT EXISTS idx_chat_session ON chat_messages(session_id)",
            "CREATE INDEX IF NOT EXISTS idx_sq_active ON suggested_questions(is_active)",
        ]
        for stmt in index_statements:
            await conn.execute(stmt)

        # GIN index for full-text search on chunk content
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_chunks_fts
            ON document_chunks USING GIN (to_tsvector('simple', content))
        """)

        # ── Seed suggested questions (Kodi i Procedurës Penale) ──
        count = await conn.fetchval("SELECT COUNT(*) FROM suggested_questions")
        if count == 0:
            seed_questions = [
                ("Parimet themelore", "Cili është qëllimi i procedimit penal?", 1),
                ("Parimet themelore", "Çfarë garanton Kodi për të drejtat e njeriut?", 2),
                ("Parimet themelore", "Çfarë do të thotë prezumimi i pafajësisë?", 3),
                ("Parimet themelore", "Kur një person konsiderohet fajtor ligjërisht?", 4),
                ("Parimet themelore", "A lejohet gjykimi i një personi dy herë për të njëjtën vepër?", 5),
                ("Gjykatat dhe juridiksioni", "Cilat janë gjykatat që shqyrtojnë çështjet penale?", 1),
                ("Gjykatat dhe juridiksioni", "Si përcaktohet kompetenca territoriale e gjykatës?", 2),
                ("Gjykatat dhe juridiksioni", "Kur gjykon një gjyqtar i vetëm?", 3),
                ("Gjykatat dhe juridiksioni", "Kur kërkohet trup gjykues me tre gjyqtarë?", 4),
                ("Gjykatat dhe juridiksioni", "Kur një gjyqtar është i papajtueshëm për të gjykuar?", 5),
                ("Prokurori dhe policia gjyqësore", "Cili është roli i prokurorit në procedim penal?", 1),
                ("Prokurori dhe policia gjyqësore", "Çfarë funksioni ka policia gjyqësore?", 2),
                ("Prokurori dhe policia gjyqësore", "Kur prokurori mund të pushojë çështjen?", 3),
                ("Prokurori dhe policia gjyqësore", "Si kontrollon prokurori veprimet e policisë gjyqësore?", 4),
                ("Prokurori dhe policia gjyqësore", "Kur transferohet çështja në një prokurori tjetër?", 5),
                ("I pandehuri dhe të drejtat", "Kur një person merr statusin e të pandehurit?", 1),
                ("I pandehuri dhe të drejtat", "Cilat janë të drejtat themelore të të pandehurit?", 2),
                ("I pandehuri dhe të drejtat", "A ka të drejtë i pandehuri të mos flasë?", 3),
                ("I pandehuri dhe të drejtat", "Kur është e detyrueshme mbrojtja me avokat?", 4),
                ("I pandehuri dhe të drejtat", "Çfarë të drejtash ka një person i arrestuar?", 5),
                ("Provat dhe procedurat", "Si mblidhen provat në procedimin penal?", 1),
                ("Provat dhe procedurat", "A mund të përdoren prova të paligjshme?", 2),
                ("Provat dhe procedurat", "Çfarë janë provat në favor të të pandehurit?", 3),
                ("Provat dhe procedurat", "Si bëhet marrja në pyetje e të pandehurit?", 4),
                ("Provat dhe procedurat", "A lejohet përdorimi i dhunës për të marrë deklarime?", 5),
                ("Masat e sigurimit", "Çfarë janë masat e sigurimit personal?", 1),
                ("Masat e sigurimit", "Kur vendoset arresti me burg?", 2),
                ("Masat e sigurimit", "Kur vendoset arresti në shtëpi?", 3),
                ("Masat e sigurimit", "Cilat janë kushtet për ndalimin e personit?", 4),
                ("Masat e sigurimit", "Si kontrollohet ligjshmëria e masës së sigurimit?", 5),
                ("Hetimi paraprak", "Kur fillon hetimi penal?", 1),
                ("Hetimi paraprak", "Çfarë roli ka prokurori në hetim?", 2),
                ("Hetimi paraprak", "Sa zgjat hetimi paraprak?", 3),
                ("Hetimi paraprak", "Kur pushohet hetimi?", 4),
                ("Hetimi paraprak", "Kur çështja kalon për gjykim?", 5),
                ("Gjykimi", "Si zhvillohet gjykimi penal?", 1),
                ("Gjykimi", "Cilat janë fazat e gjykimit?", 2),
                ("Gjykimi", "Si paraqiten provat në gjykatë?", 3),
                ("Gjykimi", "Kur jepet vendimi?", 4),
                ("Gjykimi", "Çfarë përmban vendimi penal?", 5),
                ("Mjetet e ankimit", "Çfarë është ankimi në apel?", 1),
                ("Mjetet e ankimit", "Kur mund të bëhet rekurs në Gjykatën e Lartë?", 2),
                ("Mjetet e ankimit", "Kush ka të drejtë të ankimojë vendimin?", 3),
                ("Mjetet e ankimit", "Brenda çfarë afati bëhet ankimi?", 4),
                ("Mjetet e ankimit", "Çfarë ndodh pas pranimit të ankimit?", 5),
                ("Ekzekutimi i vendimit", "Si ekzekutohet një vendim penal?", 1),
                ("Ekzekutimi i vendimit", "Cili institucion zbaton vendimin?", 2),
                ("Ekzekutimi i vendimit", "Kur fillon dënimi?", 3),
                ("Ekzekutimi i vendimit", "A mund të pezullohet ekzekutimi i vendimit?", 4),
                ("Ekzekutimi i vendimit", "Si trajtohen rastet e faljes ose amnistisë?", 5),
            ]
            await conn.executemany(
                "INSERT INTO suggested_questions (category, question, sort_order) "
                "VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
                seed_questions,
            )

    logger.info("Database initialized successfully")


# ── Document CRUD ──────────────────────────────────────────────

async def create_document(user_id: int, filename: str, original_filename: str,
                          file_type: str, file_size: int,
                          title: str = None, law_number: str = None,
                          law_date: str = None,
                          storage_bucket: str = "Ligje",
                          storage_path: str = None) -> int:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO documents
               (user_id, filename, original_filename, file_type, file_size,
                title, law_number, law_date, storage_bucket, storage_path, status)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, 'processing')
               RETURNING id""",
            user_id, filename, original_filename, file_type, file_size,
            title, law_number, law_date, storage_bucket, storage_path,
        )
        return row["id"]


async def update_document_status(doc_id: int, status: str,
                                  total_chunks: int = None,
                                  error_message: str = None,
                                  metadata: dict = None):
    pool = await _get_pool()
    parts = ["status = $1"]
    values: list = [status]
    idx = 2

    if total_chunks is not None:
        parts.append(f"total_chunks = ${idx}"); values.append(total_chunks); idx += 1
    if error_message is not None:
        parts.append(f"error_message = ${idx}"); values.append(error_message); idx += 1
    if metadata is not None:
        parts.append(f"metadata_json = ${idx}"); values.append(json.dumps(metadata)); idx += 1
        if metadata.get("title"):
            parts.append(f"title = COALESCE(NULLIF(title, ''), ${idx})")
            values.append(metadata["title"]); idx += 1
        if metadata.get("law_number"):
            parts.append(f"law_number = COALESCE(NULLIF(law_number, ''), ${idx})")
            values.append(metadata["law_number"]); idx += 1
        if metadata.get("law_date"):
            parts.append(f"law_date = COALESCE(NULLIF(law_date, ''), ${idx})")
            values.append(metadata["law_date"]); idx += 1
    if status in ("ready", "failed"):
        parts.append(f"processed_at = ${idx}")
        values.append(datetime.utcnow()); idx += 1

    values.append(doc_id)
    query = f"UPDATE documents SET {', '.join(parts)} WHERE id = ${idx}"
    async with pool.acquire() as conn:
        await conn.execute(query, *values)


async def get_all_documents():
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM documents ORDER BY uploaded_at DESC")
        return [dict(r) for r in rows]


async def get_user_documents(user_id: int):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM documents WHERE user_id = $1 ORDER BY uploaded_at DESC",
            user_id,
        )
        return [dict(r) for r in rows]


async def get_user_ready_documents(user_id: int):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, title, original_filename, total_chunks, uploaded_at
               FROM documents WHERE user_id = $1 AND status = 'ready'
               ORDER BY uploaded_at DESC""",
            user_id,
        )
        return [dict(r) for r in rows]


async def get_all_ready_documents():
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, user_id, title, original_filename, total_chunks
               FROM documents WHERE status = 'ready'
               ORDER BY uploaded_at DESC"""
        )
        return [dict(r) for r in rows]


async def get_document(doc_id: int):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM documents WHERE id = $1", doc_id)
        return dict(row) if row else None


async def get_document_for_user(doc_id: int, user_id: int):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM documents WHERE id = $1 AND user_id = $2",
            doc_id, user_id,
        )
        return dict(row) if row else None


async def delete_document(doc_id: int):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM documents WHERE id = $1", doc_id)


async def count_user_documents(user_id: int) -> int:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT COUNT(*) FROM documents WHERE user_id = $1", user_id
        )


async def rename_document(doc_id: int, new_title: str):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE documents SET title = $1 WHERE id = $2",
            new_title.strip(), doc_id,
        )


async def update_document_page_count(doc_id: int, page_count: int):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE documents SET page_count = $1 WHERE id = $2",
            page_count, doc_id,
        )


# ── Document Chunks (for keyword search) ──────────────────────

async def insert_chunks(document_id: int, user_id: int, chunks: list[dict]):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        records = []
        for c in chunks:
            pages_str = ",".join(str(p) for p in c.get("pages", []))
            page_list = c.get("pages", [])
            records.append((
                document_id, user_id, c.get("chunk_index", 0),
                c["text"], c.get("article") or "",
                c.get("section_title") or "",
                pages_str,
                min(page_list) if page_list else 0,
                max(page_list) if page_list else 0,
                len(c["text"]),
            ))
        await conn.executemany(
            """INSERT INTO document_chunks
               (document_id, user_id, chunk_index, content, article,
                section_title, pages, page_start, page_end, char_count)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)""",
            records,
        )


async def delete_chunks_for_document(document_id: int):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM document_chunks WHERE document_id = $1", document_id
        )


async def keyword_search_chunks(query: str, user_id: int = None,
                                 document_id: int = None,
                                 limit: int = 30) -> list[dict]:
    """Full-text keyword search using PostgreSQL tsvector."""
    pool = await _get_pool()
    tsquery = _build_pg_tsquery(query)
    if not tsquery:
        return []

    where_parts = ["to_tsvector('simple', dc.content) @@ to_tsquery('simple', $1)"]
    params: list = [tsquery]
    idx = 2

    if user_id is not None:
        where_parts.append(f"dc.user_id = ${idx}"); params.append(user_id); idx += 1
    if document_id:
        where_parts.append(f"dc.document_id = ${idx}"); params.append(document_id); idx += 1

    params.append(limit)
    where_clause = " AND ".join(where_parts)

    sql = f"""
        SELECT dc.*,
               ts_rank(to_tsvector('simple', dc.content),
                       to_tsquery('simple', $1)) AS fts_rank
        FROM document_chunks dc
        WHERE {where_clause}
        ORDER BY fts_rank DESC
        LIMIT ${idx}
    """

    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)
            return [dict(r) for r in rows]
    except Exception as e:
        logger.warning(f"FTS query failed: {e}")
        return []


# ── Albanian FTS helpers ──────────────────────────────────────

_ALBANIAN_STOPWORDS = frozenset(
    'dhe ose per nga nje tek te ne me se ka si do jane eshte nuk qe i e '
    'ky kjo keto ato por nese edhe mund duhet'.split()
)

_STEM_FAMILIES = {
    'arsim': [
        'arsim', 'arsimi', 'arsimin', 'arsimit', 'arsimim', 'arsimimi',
        'arsimor', 'arsimore', 'arsimtar', 'arsimtare',
    ],
    'pun': [
        'pun', 'pune', 'punen', 'punes', 'punesim', 'punesimi', 'punesimin',
        'punetor', 'punetore', 'punedhenes', 'punesimit',
    ],
    'shtet': [
        'shtet', 'shteti', 'shtetin', 'shtetit', 'shteteri', 'shteterore',
        'shteteriore', 'shtetas', 'shtetasi',
    ],
    'gjykat': [
        'gjykat', 'gjykata', 'gjykate', 'gjykaten', 'gjykates', 'gjykatesi',
        'gjyqesor', 'gjyqesore', 'gjyqtar', 'gjyqtare', 'gjyqtaret',
    ],
    'kushtetut': [
        'kushtetut', 'kushtetuta', 'kushtetuten', 'kushtetutes',
        'kushtetuese', 'kushtetutshme', 'kushtetutshem',
    ],
    'ligj': [
        'ligj', 'ligji', 'ligjin', 'ligjit', 'ligjor', 'ligjore',
        'ligjet', 'ligjeve',
    ],
    'drejt': [
        'drejt', 'drejte', 'drejta', 'drejten', 'drejtes',
        'drejtesi', 'drejtesise', 'drejtesine',
    ],
    'pronesi': [
        'pronesi', 'pronesise', 'pronesine', 'prone', 'pronen',
        'pronar', 'pronare', 'pronaret',
    ],
    'tatim': [
        'tatim', 'tatimi', 'tatimin', 'tatimit', 'tatimor', 'tatimore',
        'tatimet', 'tatimeve',
    ],
    'familj': [
        'familj', 'familja', 'familje', 'familjen', 'familjes',
        'familjar', 'familjare',
    ],
    'shendet': [
        'shendet', 'shendeti', 'shendetin', 'shendetit', 'shendetesor',
        'shendetesore', 'shendetesi', 'shendetesise',
    ],
    'mjedis': [
        'mjedis', 'mjedisi', 'mjedisin', 'mjedisit', 'mjedisor', 'mjedisore',
    ],
    'siguri': [
        'siguri', 'sigurine', 'sigurise', 'siguria', 'sigurim', 'sigurimi',
        'sigurimet',
    ],
    'zgjedh': [
        'zgjedh', 'zgjedhje', 'zgjedhjen', 'zgjedhjes', 'zgjedhjeve',
        'zgjedhur',
    ],
    'liri': ['liri', 'lirine', 'lirise', 'liria', 'lirite'],
    'detyr': [
        'detyr', 'detyra', 'detyren', 'detyres', 'detyrim', 'detyrimi',
        'detyrimeve', 'detyruesh', 'detyrueshm',
    ],
}

_WORD_TO_STEM: dict[str, str] = {}
for _root, _variants in _STEM_FAMILIES.items():
    for _v in _variants:
        _WORD_TO_STEM[_v] = _root


def _build_pg_tsquery(query: str) -> str:
    """Build a PostgreSQL tsquery string with Albanian stemming."""
    tokens = []
    words = re.findall(r'\b\w{2,}\b', query)
    seen = set()
    for w in words:
        wl = w.lower()
        if wl in _ALBANIAN_STOPWORDS or wl in seen:
            continue
        seen.add(wl)

        stem_root = _WORD_TO_STEM.get(wl)
        if stem_root:
            family = _STEM_FAMILIES[stem_root]
            tokens.append("(" + " | ".join(family) + ")")
        else:
            if len(wl) >= 4:
                tokens.append(f"{wl}:*")
            else:
                tokens.append(wl)

    return " | ".join(tokens) if tokens else ""


# ── Chat CRUD ──────────────────────────────────────────────────

async def save_chat_message(session_id: str, role: str, content: str,
                            sources: list = None):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO chat_messages (session_id, role, content, sources_json)
               VALUES ($1, $2, $3, $4)""",
            session_id, role, content, json.dumps(sources or []),
        )


async def get_chat_history(session_id: str, limit: int = 20):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT * FROM chat_messages
               WHERE session_id = $1
               ORDER BY created_at DESC LIMIT $2""",
            session_id, limit,
        )
        results = [dict(r) for r in rows]
        results.reverse()
        return results


# ── Users ────────────────────────────────────────────────────

async def create_user(
    email: str,
    password_hash: str,
    is_admin: bool = False,
    trial_ends_at: str = None,
    signup_ip: str = None,
) -> int:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO users (email, password_hash, is_admin, trial_ends_at, signup_ip)
               VALUES ($1, $2, $3, $4, $5) RETURNING id""",
            email.lower().strip(), password_hash, is_admin,
            _parse_ts(trial_ends_at), signup_ip or None,
        )
        return row["id"]


async def get_user_by_id(user_id: int):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM users WHERE id = $1", user_id)
        return dict(row) if row else None


async def get_user_by_email(email: str):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM users WHERE email = $1", email.lower().strip()
        )
        return dict(row) if row else None


async def get_users_count() -> int:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT COUNT(*) FROM users")


async def get_user_by_supabase_uid(uid: str):
    if not uid:
        return None
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM users WHERE supabase_uid = $1", uid)
        return dict(row) if row else None


async def link_supabase_uid(user_id: int, uid: str):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET supabase_uid = $1 WHERE id = $2", uid, user_id
        )


async def create_user_from_supabase(
    email: str,
    supabase_uid: str,
    is_admin: bool = False,
    trial_ends_at: str = None,
    signup_ip: str = None,
) -> int:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO users (email, password_hash, is_admin, supabase_uid, trial_ends_at, signup_ip)
               VALUES ($1, '', $2, $3, $4, $5) RETURNING id""",
            email.lower().strip(), is_admin,
            supabase_uid or None, _parse_ts(trial_ends_at), signup_ip or None,
        )
        return row["id"]


async def count_signups_from_ip_last_24h(ip: str) -> int:
    if not ip or not ip.strip():
        return 0
    pool = await _get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            """SELECT COUNT(*) FROM users
               WHERE signup_ip = $1 AND created_at > NOW() - INTERVAL '1 day'""",
            ip.strip(),
        )


async def set_trial_ends_at(user_id: int, trial_ends_at: str):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET trial_ends_at = $1 WHERE id = $2",
            _parse_ts(trial_ends_at), user_id,
        )


async def mark_trial_used(user_id: int, at: str = None):
    ts = _parse_ts(at) or datetime.utcnow()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET trial_used_at = $1 WHERE id = $2", ts, user_id
        )


async def set_trial_used_on_subscription(user_id: int):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET trial_used_at = COALESCE(trial_used_at, $1) WHERE id = $2",
            datetime.utcnow(), user_id,
        )


# ── Billing helpers ───────────────────────────────────────────

async def update_user_billing(user_id: int, *,
                               is_premium: bool = None,
                               subscription_status: str = None):
    parts, values = [], []
    idx = 1
    if is_premium is not None:
        parts.append(f"is_premium = ${idx}"); values.append(is_premium); idx += 1
    if subscription_status is not None:
        parts.append(f"subscription_status = ${idx}"); values.append(subscription_status); idx += 1
    if not parts:
        return
    values.append(user_id)
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            f"UPDATE users SET {', '.join(parts)} WHERE id = ${idx}",
            *values,
        )


async def expire_user_trial(user_id: int):
    """Force-expire a user's trial (for admin debug testing)."""
    pool = await _get_pool()
    now = datetime.utcnow()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET trial_ends_at = $1, trial_used_at = $2 WHERE id = $3",
            now, now, user_id,
        )


# ── Subscriptions ─────────────────────────────────────────────

async def upsert_subscription(user_id: int, purchase_token: str,
                              product_id: str, status: str,
                              current_period_end: str,
                              platform: str = "google_play"):
    pool = await _get_pool()
    now = datetime.utcnow()
    period_end = _parse_ts(current_period_end)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM subscriptions WHERE purchase_token = $1",
            purchase_token,
        )
        if row:
            await conn.execute(
                """UPDATE subscriptions SET status = $1, current_period_end = $2,
                   updated_at = $3, product_id = $4, platform = $5
                   WHERE purchase_token = $6""",
                status, period_end, now, product_id, platform,
                purchase_token,
            )
        else:
            await conn.execute(
                """INSERT INTO subscriptions
                   (user_id, purchase_token, product_id, status,
                    current_period_end, platform, updated_at)
                   VALUES ($1, $2, $3, $4, $5, $6, $7)""",
                user_id, purchase_token, product_id, status,
                period_end, platform, now,
            )


async def get_active_subscription(user_id: int):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT * FROM subscriptions
               WHERE user_id = $1 AND status IN ('active', 'trialing')
               AND (current_period_end IS NULL OR current_period_end > NOW())
               ORDER BY updated_at DESC LIMIT 1""",
            user_id,
        )
        return dict(row) if row else None


# ── Suggested Questions CRUD ──────────────────────────────────

async def get_active_suggested_questions():
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, category, question FROM suggested_questions WHERE is_active = TRUE ORDER BY category, sort_order"
        )
        return [dict(r) for r in rows]


async def get_all_suggested_questions():
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM suggested_questions ORDER BY category, sort_order"
        )
        return [dict(r) for r in rows]


async def create_suggested_question(category: str, question: str, sort_order: int = 0):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO suggested_questions (category, question, sort_order) VALUES ($1, $2, $3) RETURNING id",
            category, question, sort_order,
        )
        return row["id"]


async def update_suggested_question(qid: int, category: str = None, question: str = None,
                                     is_active: bool = None, sort_order: int = None):
    parts, values = [], []
    idx = 1
    if category is not None:
        parts.append(f"category = ${idx}"); values.append(category); idx += 1
    if question is not None:
        parts.append(f"question = ${idx}"); values.append(question); idx += 1
    if is_active is not None:
        parts.append(f"is_active = ${idx}"); values.append(is_active); idx += 1
    if sort_order is not None:
        parts.append(f"sort_order = ${idx}"); values.append(sort_order); idx += 1
    if not parts:
        return
    values.append(qid)
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            f"UPDATE suggested_questions SET {', '.join(parts)} WHERE id = ${idx}",
            *values,
        )


async def delete_suggested_question(qid: int):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM suggested_questions WHERE id = $1", qid)
