"""SQLite database for document metadata, chunks tracking, chat history, users."""

import aiosqlite
import json
from datetime import datetime
from pathlib import Path
from backend.config import settings

DB_PATH = str(settings.DB_PATH)


async def get_db():
    """Get a database connection."""
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    return db


async def init_db():
    """Initialize database tables and run migrations."""
    async with aiosqlite.connect(DB_PATH) as db:
        # ── Documents (with user_id for multi-user isolation) ──
        await db.execute("""
            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                filename TEXT NOT NULL,
                original_filename TEXT NOT NULL,
                file_type TEXT NOT NULL,
                file_size INTEGER NOT NULL,
                title TEXT,
                law_number TEXT,
                law_date TEXT,
                status TEXT DEFAULT 'processing',
                total_chunks INTEGER DEFAULT 0,
                error_message TEXT,
                metadata_json TEXT DEFAULT '{}',
                uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                processed_at TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)

        # ── Chat messages ──
        await db.execute("""
            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                sources_json TEXT DEFAULT '[]',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # ── Users ──
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT DEFAULT '',
                is_admin INTEGER DEFAULT 0,
                supabase_uid TEXT UNIQUE,
                trial_ends_at TIMESTAMP,
                trial_used_at TIMESTAMP,
                signup_ip TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # ── Subscriptions (Google Play Billing) ──
        await db.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                purchase_token TEXT,
                product_id TEXT,
                platform TEXT DEFAULT 'google_play',
                status TEXT NOT NULL,
                current_period_end TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)
        await db.commit()

        # ── Document chunks (for keyword / FTS5 search) ──
        await db.execute("""
            CREATE TABLE IF NOT EXISTS document_chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                chunk_index INTEGER NOT NULL,
                content TEXT NOT NULL,
                article TEXT,
                pages TEXT,
                page_start INTEGER,
                page_end INTEGER,
                char_count INTEGER DEFAULT 0,
                FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE
            )
        """)
        await db.commit()

        # FTS5 virtual table for keyword search
        try:
            await db.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS document_chunks_fts
                USING fts5(content, content='document_chunks', content_rowid='id')
            """)
            await db.commit()
        except Exception:
            pass  # FTS5 may already exist or not be available

        # Indexes for performance
        for idx_sql in [
            "CREATE INDEX IF NOT EXISTS idx_documents_user_id ON documents(user_id)",
            "CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status)",
            "CREATE INDEX IF NOT EXISTS idx_documents_user_status ON documents(user_id, status)",
            "CREATE INDEX IF NOT EXISTS idx_chunks_document_id ON document_chunks(document_id)",
            "CREATE INDEX IF NOT EXISTS idx_chunks_user_id ON document_chunks(user_id)",
            "CREATE INDEX IF NOT EXISTS idx_chunks_user_doc ON document_chunks(user_id, document_id)",
            "CREATE INDEX IF NOT EXISTS idx_chat_session ON chat_messages(session_id)",
        ]:
            await db.execute(idx_sql)
        await db.commit()

        # ── Migrations ──────────────────────────────────────────

        # Migration: add page_count to documents if missing
        cursor = await db.execute("PRAGMA table_info(documents)")
        rows = await cursor.fetchall()
        doc_cols_pre = [row[1] for row in rows]
        if "page_count" not in doc_cols_pre:
            await db.execute("ALTER TABLE documents ADD COLUMN page_count INTEGER DEFAULT 0")
            await db.commit()

        # Migration: add Google Play columns if missing
        cursor = await db.execute("PRAGMA table_info(subscriptions)")
        rows = await cursor.fetchall()
        columns = [row[1] for row in rows]
        if "purchase_token" not in columns:
            await db.execute("ALTER TABLE subscriptions ADD COLUMN purchase_token TEXT")
            await db.commit()
        if "product_id" not in columns:
            await db.execute("ALTER TABLE subscriptions ADD COLUMN product_id TEXT")
            await db.commit()
        if "platform" not in columns:
            await db.execute("ALTER TABLE subscriptions ADD COLUMN platform TEXT DEFAULT 'google_play'")
            await db.commit()

        # Migration: add trial and signup_ip to users if missing
        cursor = await db.execute("PRAGMA table_info(users)")
        rows = await cursor.fetchall()
        user_columns = [row[1] for row in rows]
        if "trial_ends_at" not in user_columns:
            await db.execute("ALTER TABLE users ADD COLUMN trial_ends_at TIMESTAMP")
            await db.commit()
        if "trial_used_at" not in user_columns:
            await db.execute("ALTER TABLE users ADD COLUMN trial_used_at TIMESTAMP")
            await db.commit()
        if "signup_ip" not in user_columns:
            await db.execute("ALTER TABLE users ADD COLUMN signup_ip TEXT")
            await db.commit()
        if "supabase_uid" not in user_columns:
            await db.execute("ALTER TABLE users ADD COLUMN supabase_uid TEXT")
            await db.commit()
            try:
                await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_supabase_uid ON users(supabase_uid)")
                await db.commit()
            except Exception:
                pass

        # Migration: add user_id to documents if missing
        cursor = await db.execute("PRAGMA table_info(documents)")
        rows = await cursor.fetchall()
        doc_columns = [row[1] for row in rows]
        if "user_id" not in doc_columns:
            await db.execute("ALTER TABLE documents ADD COLUMN user_id INTEGER")
            await db.commit()
            # Assign existing documents to first admin user
            cursor = await db.execute(
                "SELECT id FROM users WHERE is_admin = 1 ORDER BY id LIMIT 1"
            )
            admin_row = await cursor.fetchone()
            if admin_row:
                admin_id = admin_row[0]
                await db.execute(
                    "UPDATE documents SET user_id = ? WHERE user_id IS NULL",
                    (admin_id,)
                )
                await db.commit()

        # Migration: rename status values (processed->ready, error->failed)
        await db.execute(
            "UPDATE documents SET status = 'ready' WHERE status = 'processed'"
        )
        await db.execute(
            "UPDATE documents SET status = 'failed' WHERE status = 'error'"
        )
        await db.commit()

        # ── Suggested Questions ──
        await db.execute("""
            CREATE TABLE IF NOT EXISTS suggested_questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                question TEXT NOT NULL,
                sort_order INTEGER DEFAULT 0,
                is_active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_sq_active ON suggested_questions(is_active)"
        )
        await db.commit()

        # Seed default questions if table is empty
        cursor = await db.execute("SELECT COUNT(*) FROM suggested_questions")
        count_row = await cursor.fetchone()
        if count_row[0] == 0:
            seed_questions = [
                ("E drejta civile", "Cilat janë afatet e parashkrimit sipas Kodit Civil?", 1),
                ("E drejta civile", "Si zgjidhet një mosmarrëveshje pronësie?", 2),
                ("E drejta civile", "Cilat janë kushtet për lidhjen e një kontrate?", 3),
                ("E drejta penale", "Cilat janë dënimet për vjedhje sipas Kodit Penal?", 1),
                ("E drejta penale", "Kur konsiderohet një vepër si kundravajtje penale?", 2),
                ("E drejta penale", "Si funksionon procedimi penal në Shqipëri?", 3),
                ("E drejta e punës", "Cilat janë të drejtat e punëmarrësit sipas Kodit të Punës?", 1),
                ("E drejta e punës", "Si llogaritet kompensimi për largim nga puna?", 2),
                ("E drejta e punës", "Sa ditë leje vjetore ka një punëmarrës?", 3),
                ("E drejta familjare", "Si bëhet ndarja e pasurisë pas divorcit?", 1),
                ("E drejta familjare", "Cilat janë kushtet për birësimin e fëmijëve?", 2),
                ("E drejta familjare", "Si përcaktohet kujdestaria e fëmijëve?", 3),
                ("Procedura administrative", "Si ankimohet një vendim administrativ?", 1),
                ("Procedura administrative", "Cilat janë afatet për ankimin administrativ?", 2),
                ("Procedura administrative", "Si funksionon gjykata administrative?", 3),
            ]
            await db.executemany(
                "INSERT INTO suggested_questions (category, question, sort_order) VALUES (?, ?, ?)",
                seed_questions,
            )
            await db.commit()


# ── Document CRUD ──────────────────────────────────────────────

async def create_document(user_id: int, filename: str, original_filename: str,
                          file_type: str, file_size: int,
                          title: str = None, law_number: str = None,
                          law_date: str = None) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """INSERT INTO documents
               (user_id, filename, original_filename, file_type, file_size,
                title, law_number, law_date, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'processing')""",
            (user_id, filename, original_filename, file_type, file_size,
             title, law_number, law_date)
        )
        await db.commit()
        return cursor.lastrowid


async def update_document_status(doc_id: int, status: str,
                                  total_chunks: int = None,
                                  error_message: str = None,
                                  metadata: dict = None):
    async with aiosqlite.connect(DB_PATH) as db:
        fields = ["status = ?"]
        values = [status]

        if total_chunks is not None:
            fields.append("total_chunks = ?")
            values.append(total_chunks)
        if error_message is not None:
            fields.append("error_message = ?")
            values.append(error_message)
        if metadata is not None:
            fields.append("metadata_json = ?")
            values.append(json.dumps(metadata))
            if metadata.get("title"):
                fields.append("title = COALESCE(NULLIF(title, ''), ?)")
                values.append(metadata["title"])
            if metadata.get("law_number"):
                fields.append("law_number = COALESCE(NULLIF(law_number, ''), ?)")
                values.append(metadata["law_number"])
            if metadata.get("law_date"):
                fields.append("law_date = COALESCE(NULLIF(law_date, ''), ?)")
                values.append(metadata["law_date"])
        if status in ("ready", "failed"):
            fields.append("processed_at = ?")
            values.append(datetime.utcnow().isoformat())

        values.append(doc_id)
        query = f"UPDATE documents SET {', '.join(fields)} WHERE id = ?"
        await db.execute(query, values)
        await db.commit()


async def get_all_documents():
    """Get all documents (admin view)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM documents ORDER BY uploaded_at DESC"
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def get_user_documents(user_id: int):
    """Get documents owned by a specific user."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM documents WHERE user_id = ? ORDER BY uploaded_at DESC",
            (user_id,)
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def get_user_ready_documents(user_id: int):
    """Get only ready (processed) documents for a user — used for search dropdown."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT id, title, original_filename, total_chunks, uploaded_at
               FROM documents
               WHERE user_id = ? AND status = 'ready'
               ORDER BY uploaded_at DESC""",
            (user_id,)
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def get_all_ready_documents():
    """Get ALL ready documents regardless of owner — for global chat search."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT id, user_id, title, original_filename, total_chunks
               FROM documents WHERE status = 'ready'
               ORDER BY uploaded_at DESC"""
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def get_document(doc_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM documents WHERE id = ?", (doc_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_document_for_user(doc_id: int, user_id: int):
    """Get a document only if it belongs to the user (RLS)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM documents WHERE id = ? AND user_id = ?",
            (doc_id, user_id)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def delete_document(doc_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
        await db.commit()


async def count_user_documents(user_id: int) -> int:
    """Count total documents for a user."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM documents WHERE user_id = ?", (user_id,)
        )
        row = await cursor.fetchone()
        return row[0] if row else 0


async def rename_document(doc_id: int, new_title: str):
    """Rename a document's title."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE documents SET title = ? WHERE id = ?",
            (new_title.strip(), doc_id)
        )
        await db.commit()


async def update_document_page_count(doc_id: int, page_count: int):
    """Set the page count for a document."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE documents SET page_count = ? WHERE id = ?",
            (page_count, doc_id)
        )
        await db.commit()


# ── Document Chunks (for keyword search) ──────────────────────

async def insert_chunks(document_id: int, user_id: int, chunks: list[dict]):
    """Insert chunk texts into SQLite for FTS keyword search."""
    async with aiosqlite.connect(DB_PATH) as db:
        for c in chunks:
            pages_str = ",".join(str(p) for p in c.get("pages", []))
            page_list = c.get("pages", [])
            cursor = await db.execute(
                """INSERT INTO document_chunks
                   (document_id, user_id, chunk_index, content, article,
                    pages, page_start, page_end, char_count)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (document_id, user_id, c.get("chunk_index", 0),
                 c["text"], c.get("article") or "",
                 pages_str,
                 min(page_list) if page_list else 0,
                 max(page_list) if page_list else 0,
                 len(c["text"]))
            )
            # Sync FTS index
            try:
                await db.execute(
                    "INSERT INTO document_chunks_fts(rowid, content) VALUES (?, ?)",
                    (cursor.lastrowid, c["text"])
                )
            except Exception:
                pass
        await db.commit()


async def delete_chunks_for_document(document_id: int):
    """Delete all chunks for a document from SQLite (and FTS)."""
    async with aiosqlite.connect(DB_PATH) as db:
        # Get chunk IDs for FTS cleanup
        cursor = await db.execute(
            "SELECT id FROM document_chunks WHERE document_id = ?",
            (document_id,)
        )
        rows = await cursor.fetchall()
        for row in rows:
            try:
                await db.execute(
                    "INSERT INTO document_chunks_fts(document_chunks_fts, rowid, content) "
                    "VALUES('delete', ?, '')",
                    (row[0],)
                )
            except Exception:
                pass
        await db.execute(
            "DELETE FROM document_chunks WHERE document_id = ?",
            (document_id,)
        )
        await db.commit()


async def keyword_search_chunks(query: str, user_id: int = None,
                                 document_id: int = None,
                                 limit: int = 30) -> list[dict]:
    """Full-text keyword search using FTS5.

    If user_id is None, search ALL chunks globally (used for normal-user chat).
    If user_id is set, scope to that user's chunks.
    Optional document_id further narrows to a single document.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        fts_query = _build_fts_query(query)

        where_parts = ["document_chunks_fts MATCH ?"]
        params: list = [fts_query]

        if user_id is not None:
            where_parts.append("dc.user_id = ?")
            params.append(user_id)
        if document_id:
            where_parts.append("dc.document_id = ?")
            params.append(document_id)

        params.append(limit)
        where_clause = " AND ".join(where_parts)

        sql = f"""
            SELECT dc.*, fts.rank AS fts_rank
            FROM document_chunks dc
            JOIN document_chunks_fts fts ON fts.rowid = dc.id
            WHERE {where_clause}
            ORDER BY fts.rank
            LIMIT ?
        """

        try:
            cursor = await db.execute(sql, params)
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]
        except Exception:
            return []


_ALBANIAN_STOPWORDS = frozenset(
    'dhe ose per nga nje tek te ne me se ka si do jane eshte nuk qe i e '
    'ky kjo keto ato por nese edhe mund duhet'.split()
)

# Albanian legal-term stem families — map any variant to its root + siblings
# so a search for "arsimi" also matches "arsim", "arsimor", etc.
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

# Reverse index: word -> stem family root
_WORD_TO_STEM: dict[str, str] = {}
for _root, _variants in _STEM_FAMILIES.items():
    for _v in _variants:
        _WORD_TO_STEM[_v] = _root


def _build_fts_query(query: str) -> str:
    """Build an FTS5 query string with Albanian stemming + legal term handling.

    Strategy:
    - Extract exact "Neni XX" phrases and keep them quoted
    - Expand Albanian word forms to their stem family for broader recall
    - Use prefix matching (word*) as final fallback
    - Strip stopwords
    """
    import re

    tokens = []

    # 1. Keep exact article references as phrases
    neni_matches = re.findall(r'[Nn]eni\s+\d+', query)
    for m in neni_matches:
        tokens.append(f'"{m}"')

    ligj_matches = re.findall(r'[Ll]igj\w*\s+[Nn]r\.?\s*\d+', query)
    for m in ligj_matches:
        tokens.append(f'"{m}"')

    # 2. Extract individual words, expand stems
    words = re.findall(r'\b\w{2,}\b', query)
    seen = set()
    for w in words:
        wl = w.lower()
        if wl in _ALBANIAN_STOPWORDS:
            continue
        if wl in seen:
            continue
        seen.add(wl)

        # Check if this word belongs to a stem family
        stem_root = _WORD_TO_STEM.get(wl)
        if stem_root:
            # Add all family variants with OR
            family = _STEM_FAMILIES[stem_root]
            group = " OR ".join(family)
            tokens.append(f"({group})")
        else:
            # Use prefix matching for 3+ char words
            if len(wl) >= 4:
                tokens.append(f"{wl}*")
            else:
                tokens.append(wl)

    if not tokens:
        return query

    return " OR ".join(tokens)


# ── Chat CRUD ──────────────────────────────────────────────────

async def save_chat_message(session_id: str, role: str, content: str,
                            sources: list = None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO chat_messages (session_id, role, content, sources_json)
               VALUES (?, ?, ?, ?)""",
            (session_id, role, content, json.dumps(sources or []))
        )
        await db.commit()


async def get_chat_history(session_id: str, limit: int = 20):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT * FROM chat_messages
               WHERE session_id = ?
               ORDER BY created_at DESC LIMIT ?""",
            (session_id, limit)
        )
        rows = await cursor.fetchall()
        results = [dict(row) for row in rows]
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
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """INSERT INTO users (email, password_hash, is_admin, trial_ends_at, signup_ip)
               VALUES (?, ?, ?, ?, ?)""",
            (email.lower().strip(), password_hash, 1 if is_admin else 0,
             trial_ends_at or "", signup_ip or "")
        )
        await db.commit()
        return cursor.lastrowid


async def get_user_by_id(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_user_by_email(email: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM users WHERE email = ?", (email.lower().strip(),)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_users_count() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM users")
        row = await cursor.fetchone()
        return row[0] if row else 0


async def get_user_by_supabase_uid(uid: str):
    if not uid:
        return None
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM users WHERE supabase_uid = ?", (uid,))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def link_supabase_uid(user_id: int, uid: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET supabase_uid = ? WHERE id = ?", (uid, user_id))
        await db.commit()


async def create_user_from_supabase(
    email: str,
    supabase_uid: str,
    is_admin: bool = False,
    trial_ends_at: str = None,
    signup_ip: str = None,
) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """INSERT INTO users (email, password_hash, is_admin, supabase_uid, trial_ends_at, signup_ip)
               VALUES (?, '', ?, ?, ?, ?)""",
            (email.lower().strip(), 1 if is_admin else 0,
             supabase_uid, trial_ends_at or "", signup_ip or "")
        )
        await db.commit()
        return cursor.lastrowid




async def count_signups_from_ip_last_24h(ip: str) -> int:
    if not ip or not ip.strip():
        return 0
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """SELECT COUNT(*) FROM users
               WHERE signup_ip = ? AND created_at > datetime('now', '-1 day')""",
            (ip.strip(),)
        )
        row = await cursor.fetchone()
        return row[0] if row else 0


async def set_trial_ends_at(user_id: int, trial_ends_at: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET trial_ends_at = ? WHERE id = ?",
            (trial_ends_at, user_id)
        )
        await db.commit()


async def mark_trial_used(user_id: int, at: str = None):
    at = at or datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET trial_used_at = ? WHERE id = ?",
            (at, user_id)
        )
        await db.commit()


async def set_trial_used_on_subscription(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET trial_used_at = COALESCE(trial_used_at, ?) WHERE id = ?",
            (datetime.utcnow().isoformat(), user_id)
        )
        await db.commit()


# ── Subscriptions (Google Play Billing only) ─────────────────

async def upsert_subscription(user_id: int, purchase_token: str,
                              product_id: str, status: str,
                              current_period_end: str,
                              platform: str = "google_play"):
    """Upsert subscription from Google Play purchase."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id FROM subscriptions WHERE purchase_token = ?",
            (purchase_token,)
        )
        row = await cursor.fetchone()
        now = datetime.utcnow().isoformat()
        if row:
            await db.execute(
                """UPDATE subscriptions SET status = ?, current_period_end = ?,
                   updated_at = ?, product_id = ?, platform = ?
                   WHERE purchase_token = ?""",
                (status, current_period_end, now, product_id, platform,
                 purchase_token)
            )
        else:
            await db.execute(
                """INSERT INTO subscriptions
                   (user_id, purchase_token, product_id, status,
                    current_period_end, platform, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (user_id, purchase_token, product_id, status,
                 current_period_end, platform, now)
            )
        await db.commit()


async def get_active_subscription(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT * FROM subscriptions
               WHERE user_id = ? AND status IN ('active', 'trialing')
               AND (current_period_end IS NULL OR current_period_end > datetime('now'))
               ORDER BY updated_at DESC LIMIT 1""",
            (user_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


# ── Suggested Questions CRUD ──────────────────────────────────

async def get_active_suggested_questions():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, category, question FROM suggested_questions WHERE is_active = 1 ORDER BY category, sort_order"
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_all_suggested_questions():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM suggested_questions ORDER BY category, sort_order"
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def create_suggested_question(category: str, question: str, sort_order: int = 0):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO suggested_questions (category, question, sort_order) VALUES (?, ?, ?)",
            (category, question, sort_order),
        )
        await db.commit()
        return cursor.lastrowid


async def update_suggested_question(qid: int, category: str = None, question: str = None,
                                     is_active: bool = None, sort_order: int = None):
    fields, values = [], []
    if category is not None:
        fields.append("category = ?"); values.append(category)
    if question is not None:
        fields.append("question = ?"); values.append(question)
    if is_active is not None:
        fields.append("is_active = ?"); values.append(1 if is_active else 0)
    if sort_order is not None:
        fields.append("sort_order = ?"); values.append(sort_order)
    if not fields:
        return
    values.append(qid)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE suggested_questions SET {', '.join(fields)} WHERE id = ?", values)
        await db.commit()


async def delete_suggested_question(qid: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM suggested_questions WHERE id = ?", (qid,))
        await db.commit()
