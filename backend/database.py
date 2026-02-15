"""SQLite database for document metadata and chat history."""

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
    """Initialize database tables."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT NOT NULL,
                original_filename TEXT NOT NULL,
                file_type TEXT NOT NULL,
                file_size INTEGER NOT NULL,
                title TEXT,
                law_number TEXT,
                law_date TEXT,
                status TEXT DEFAULT 'uploaded',
                total_chunks INTEGER DEFAULT 0,
                error_message TEXT,
                metadata_json TEXT DEFAULT '{}',
                uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                processed_at TIMESTAMP
            )
        """)
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
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                is_admin INTEGER DEFAULT 0,
                stripe_customer_id TEXT,
                trial_ends_at TIMESTAMP,
                trial_used_at TIMESTAMP,
                signup_ip TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                stripe_subscription_id TEXT UNIQUE,
                stripe_price_id TEXT,
                paypal_subscription_id TEXT UNIQUE,
                status TEXT NOT NULL,
                current_period_end TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)
        await db.commit()
        # Migration: add paypal_subscription_id if table was created by an older version
        cursor = await db.execute("PRAGMA table_info(subscriptions)")
        rows = await cursor.fetchall()
        columns = [row[1] for row in rows]
        if "paypal_subscription_id" not in columns:
            await db.execute("ALTER TABLE subscriptions ADD COLUMN paypal_subscription_id TEXT")
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


# ── Document CRUD ──────────────────────────────────────────────

async def create_document(filename: str, original_filename: str,
                          file_type: str, file_size: int,
                          title: str = None, law_number: str = None,
                          law_date: str = None) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """INSERT INTO documents
               (filename, original_filename, file_type, file_size, title, law_number, law_date)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (filename, original_filename, file_type, file_size, title, law_number, law_date)
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
        if status in ("processed", "error"):
            fields.append("processed_at = ?")
            values.append(datetime.utcnow().isoformat())

        values.append(doc_id)
        query = f"UPDATE documents SET {', '.join(fields)} WHERE id = ?"
        await db.execute(query, values)
        await db.commit()


async def get_all_documents():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM documents ORDER BY uploaded_at DESC"
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


async def delete_document(doc_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
        await db.commit()


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
            (email.lower().strip(), password_hash, 1 if is_admin else 0, trial_ends_at or "", signup_ip or "")
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
        cursor = await db.execute("SELECT * FROM users WHERE email = ?", (email.lower().strip(),))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_users_count() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM users")
        row = await cursor.fetchone()
        return row[0] if row else 0


async def set_stripe_customer_id(user_id: int, customer_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET stripe_customer_id = ? WHERE id = ?",
            (customer_id, user_id)
        )
        await db.commit()


async def count_signups_from_ip_last_24h(ip: str) -> int:
    """Count users created from this IP in the last 24 hours (anti-abuse)."""
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


async def mark_trial_used(user_id: int, at: str = None):
    """Mark that this user's trial has been used (expired or converted to paid)."""
    at = at or datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET trial_used_at = ? WHERE id = ?",
            (at, user_id)
        )
        await db.commit()


async def set_trial_used_on_subscription(user_id: int):
    """When user gets an active subscription, mark trial as used (no second trial if they cancel)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET trial_used_at = COALESCE(trial_used_at, ?) WHERE id = ?",
            (datetime.utcnow().isoformat(), user_id)
        )
        await db.commit()


# ── Subscriptions ───────────────────────────────────────────

async def upsert_subscription(user_id: int, stripe_subscription_id: str,
                              status: str, current_period_end: str,
                              stripe_price_id: str = None):
    """Insert or update subscription by stripe_subscription_id."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id FROM subscriptions WHERE stripe_subscription_id = ?",
            (stripe_subscription_id,)
        )
        row = await cursor.fetchone()
        now = datetime.utcnow().isoformat()
        if row:
            await db.execute(
                """UPDATE subscriptions SET status = ?, current_period_end = ?,
                   updated_at = ?, stripe_price_id = ?
                   WHERE stripe_subscription_id = ?""",
                (status, current_period_end, now, stripe_price_id or "", stripe_subscription_id)
            )
        else:
            await db.execute(
                """INSERT INTO subscriptions (user_id, stripe_subscription_id, stripe_price_id, status, current_period_end, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (user_id, stripe_subscription_id, stripe_price_id or "", status, current_period_end, now)
            )
        await db.commit()


async def get_active_subscription(user_id: int):
    """Return the active subscription for user if any (status active or trialing)."""
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


async def get_subscription_by_stripe_id(stripe_subscription_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM subscriptions WHERE stripe_subscription_id = ?",
            (stripe_subscription_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def upsert_subscription_paypal(user_id: int, paypal_subscription_id: str,
                                      status: str, current_period_end: str):
    """Insert or update subscription by paypal_subscription_id."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id FROM subscriptions WHERE paypal_subscription_id = ?",
            (paypal_subscription_id,)
        )
        row = await cursor.fetchone()
        now = datetime.utcnow().isoformat()
        if row:
            await db.execute(
                """UPDATE subscriptions SET status = ?, current_period_end = ?, updated_at = ?
                   WHERE paypal_subscription_id = ?""",
                (status, current_period_end, now, paypal_subscription_id)
            )
        else:
            await db.execute(
                """INSERT INTO subscriptions (user_id, paypal_subscription_id, status, current_period_end, updated_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (user_id, paypal_subscription_id, status, current_period_end, now)
            )
        await db.commit()


async def get_subscription_by_paypal_id(paypal_subscription_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM subscriptions WHERE paypal_subscription_id = ?",
            (paypal_subscription_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None
