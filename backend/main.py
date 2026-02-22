"""Albanian Law AI - FastAPI Application.

API endpoints for document management, RAG chat, auth, and subscription.
Multi-user document QA with per-user isolation.
"""

import os
import uuid
import asyncio
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from contextlib import asynccontextmanager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("rag.api")

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from backend.config import settings
from backend.database import (
    init_db, create_document, get_all_documents, get_document,
    get_user_documents, get_user_ready_documents, get_all_ready_documents,
    get_document_for_user,
    delete_document, update_document_status, save_chat_message,
    get_chat_history, create_user, get_user_by_email, get_user_by_id, get_users_count,
    count_signups_from_ip_last_24h, set_trial_used_on_subscription,
    count_user_documents, rename_document, delete_chunks_for_document,
)
from backend.document_processor import process_document
from backend.vector_store import (
    delete_document_chunks, migrate_chunks_add_user_id, get_user_chunk_count,
)
from backend.chat import generate_answer, generate_answer_stream
from backend.auth import (
    hash_password, verify_password, create_access_token,
    get_current_user, get_current_user_optional, require_admin, require_subscription,
)
from backend.database import get_active_subscription, upsert_subscription
from backend.database import (
    get_active_suggested_questions, get_all_suggested_questions,
    create_suggested_question, update_suggested_question, delete_suggested_question,
)
from backend.database import _get_pool
from backend.file_storage import (
    upload_file as storage_upload, download_file as storage_download,
    delete_file as storage_delete, storage_path_for_doc,
    check_storage_health, list_bucket_files,
)


def _resolve_storage_path(doc: dict) -> str:
    """Get the Supabase Storage path from DB record, or reconstruct it."""
    if doc.get("storage_path"):
        return doc["storage_path"]
    return storage_path_for_doc(doc.get("user_id", 0), doc["filename"])


SUBSCRIPTION_PRICE_EUR = 4.99
from backend.trial_abuse import is_disposable_email, get_client_ip


# ── App Lifecycle ─────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize database and run migrations on startup."""
    try:
        await init_db()
        logger.info("Database initialized successfully")
    except Exception as e:
        logger.error(f"Database init failed: {e}")
    try:
        await _run_chroma_migration()
    except Exception as e:
        logger.warning(f"ChromaDB migration skipped: {e}")
    try:
        asyncio.get_event_loop().run_in_executor(None, _build_topic_index)
    except Exception as e:
        logger.warning(f"Topic index build skipped: {e}")
    logger.info("Application startup complete")
    yield
    from backend.database import close_pool
    await close_pool()


async def _run_chroma_migration():
    """One-time migration: tag existing ChromaDB chunks with user_id."""
    try:
        docs = await get_all_documents()
        doc_id_to_user_id = {}
        for d in docs:
            doc_id_to_user_id[str(d["id"])] = str(d.get("user_id") or 1)
        if doc_id_to_user_id:
            updated = await migrate_chunks_add_user_id(doc_id_to_user_id)
            if updated:
                logger.info(f"ChromaDB migration: {updated} chunks updated with user_id")
    except Exception as e:
        logger.warning(f"ChromaDB migration skipped: {e}")


limiter = Limiter(key_func=get_remote_address)

app = FastAPI(
    title="Albanian Law AI",
    description="RAG-based legal document Q&A for Albanian law",
    version="2.0.0",
    lifespan=lifespan,
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    if request.url.path.startswith("/api"):
        logger.error(f"Unhandled error on {request.url.path}: {exc}")
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error", "error": str(exc)},
        )
    raise exc


_cors_origins = list({
    settings.FRONTEND_URL,
    settings.SERVER_URL,
    "http://localhost:8000",
    "http://localhost:3000",
})
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

frontend_dir = Path(__file__).resolve().parent.parent / "frontend"

MAX_DOCS_PER_USER = 20
MAX_FILE_SIZE_MB = 50


# ── Frontend Routes ───────────────────────────────────────────

@app.get("/")
async def serve_landing():
    return FileResponse(str(frontend_dir / "landing.html"))


@app.get("/app")
async def serve_chat():
    return FileResponse(str(frontend_dir / "index.html"))


@app.get("/pricing")
async def serve_pricing():
    return FileResponse(str(frontend_dir / "pricing.html"))


@app.get("/admin")
async def serve_admin():
    return FileResponse(str(frontend_dir / "admin.html"))


@app.get("/login")
async def serve_login():
    return FileResponse(str(frontend_dir / "login.html"))


@app.get("/documents")
async def serve_documents():
    return FileResponse(str(frontend_dir / "documents.html"))


# ── Auth API ──────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    email: str
    password: str


class LoginRequest(BaseModel):
    email: str
    password: str


@app.post("/api/auth/register")
@limiter.limit("5/minute")
async def register(data: RegisterRequest, request: Request):
    """Register a new user. Uses Supabase Auth if configured, else local."""
    from backend.auth import _supabase_configured, supabase_sign_up
    from backend.database import create_user_from_supabase

    try:
        email = data.email.strip().lower()
        if not email or "@" not in email:
            raise HTTPException(status_code=400, detail="Email i pavlefshëm.")
        if len(data.password) < 8:
            raise HTTPException(
                status_code=400,
                detail="Fjalëkalimi duhet të ketë të paktën 8 karaktere.",
            )
        if settings.BLOCK_DISPOSABLE_EMAILS and is_disposable_email(email):
            raise HTTPException(
                status_code=400,
                detail="Nuk lejohen adresa email të përkohshme ose të përdorura një herë.",
            )
        client_ip = get_client_ip(request)
        signups_from_ip = await count_signups_from_ip_last_24h(client_ip)
        if signups_from_ip >= settings.MAX_SIGNUPS_PER_IP_24H:
            raise HTTPException(
                status_code=429,
                detail="Shumë llogari të krijuara nga rrjeti juaj. Provoni më vonë.",
            )
        existing = await get_user_by_email(email)
        if existing:
            raise HTTPException(status_code=409, detail="Ky email është tashmë i regjistruar.")
        count = await get_users_count()
        is_admin = bool(
            count == 0
            or (settings.ADMIN_EMAIL and email == settings.ADMIN_EMAIL.strip().lower())
        )
        trial_ends_at = (
            datetime.utcnow() + timedelta(days=settings.TRIAL_DAYS)
        ).strftime("%Y-%m-%dT%H:%M:%S")

        if _supabase_configured:
            sb_data = await supabase_sign_up(email, data.password)
            sb_user = sb_data.get("user") or {}
            sb_uid = sb_user.get("id", "")
            access_token = (sb_data.get("session") or {}).get("access_token", "")

            user_id = await create_user_from_supabase(
                email, supabase_uid=sb_uid, is_admin=is_admin,
                trial_ends_at=trial_ends_at, signup_ip=client_ip or "",
            )
            token = access_token or create_access_token(user_id, email, is_admin)
        else:
            user_id = await create_user(
                email, hash_password(data.password), is_admin=is_admin,
                trial_ends_at=trial_ends_at, signup_ip=client_ip or "",
            )
            token = create_access_token(user_id, email, is_admin)

        return {
            "token": token,
            "user": {"id": user_id, "email": email, "is_admin": is_admin},
            "trial_ends_at": trial_ends_at,
            "trial_days": settings.TRIAL_DAYS,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Registration failed for {data.email}: {type(e).__name__}: {e}")
        return JSONResponse(status_code=500, content={"detail": f"Registration error: {str(e)}"})


@app.post("/api/auth/login")
@limiter.limit("5/minute")
async def login(data: LoginRequest, request: Request):
    """Login. Uses Supabase Auth if configured, else local."""
    from backend.auth import _supabase_configured, supabase_sign_in
    from backend.database import get_user_by_supabase_uid, create_user_from_supabase, link_supabase_uid

    email = data.email.strip().lower()

    try:
        if _supabase_configured:
            try:
                sb_data = await supabase_sign_in(email, data.password)
                access_token = sb_data.get("access_token", "")
                sb_user = sb_data.get("user") or {}
                sb_uid = sb_user.get("id", "")

                user = await get_user_by_supabase_uid(sb_uid)
                if not user:
                    user = await get_user_by_email(email)
                    if user:
                        await link_supabase_uid(user["id"], sb_uid)
                    else:
                        count = await get_users_count()
                        is_admin = bool(count == 0 or (settings.ADMIN_EMAIL and email == settings.ADMIN_EMAIL.strip().lower()))
                        trial_ends_at = (datetime.utcnow() + timedelta(days=settings.TRIAL_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
                        user_id = await create_user_from_supabase(
                            email, supabase_uid=sb_uid, is_admin=is_admin,
                            trial_ends_at=trial_ends_at,
                        )
                        user = await get_user_by_id(user_id)

                return {
                    "token": access_token,
                    "user": {
                        "id": user["id"],
                        "email": user["email"],
                        "is_admin": bool(user.get("is_admin")),
                    },
                }
            except HTTPException:
                user = await get_user_by_email(email)
                if user and user.get("password_hash") and not user.get("supabase_uid"):
                    if verify_password(data.password, user["password_hash"]):
                        token = create_access_token(user["id"], user["email"], bool(user.get("is_admin")))
                        return {
                            "token": token,
                            "user": {
                                "id": user["id"],
                                "email": user["email"],
                                "is_admin": bool(user.get("is_admin")),
                            },
                        }
                raise HTTPException(status_code=401, detail="Email ose fjalëkalim i gabuar.")
        else:
            user = await get_user_by_email(email)
            if not user or not verify_password(data.password, user["password_hash"]):
                raise HTTPException(status_code=401, detail="Email ose fjalëkalim i gabuar.")
            token = create_access_token(user["id"], user["email"], bool(user.get("is_admin")))
            return {
                "token": token,
                "user": {
                    "id": user["id"],
                    "email": user["email"],
                    "is_admin": bool(user.get("is_admin")),
                },
            }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Login failed for {data.email}: {type(e).__name__}: {e}")
        return JSONResponse(status_code=500, content={"detail": f"Login error: {str(e)}"})


class ForgotPasswordRequest(BaseModel):
    email: str


@app.post("/api/auth/forgot-password")
@limiter.limit("3/minute")
async def forgot_password(data: ForgotPasswordRequest, request: Request):
    """Send a password reset email via Supabase Auth."""
    from backend.auth import _supabase_configured, supabase_reset_password
    email = data.email.strip().lower()
    if not _supabase_configured:
        raise HTTPException(status_code=501, detail="Rivendosja e fjalëkalimit kërkon Supabase Auth.")
    await supabase_reset_password(email)
    return {"message": "Nëse ky email ekziston, do të merrni një link rivendosjeje."}


@app.post("/api/auth/logout")
async def logout(request: Request, user: dict = Depends(get_current_user)):
    """Server-side logout: revoke Supabase session."""
    from backend.auth import _supabase_configured, supabase_sign_out
    token = None
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
    if _supabase_configured and token:
        await supabase_sign_out(token)
    return {"message": "U dol me sukses."}


@app.get("/api/auth/me")
async def auth_me(user: dict = Depends(get_current_user)):
    """Return current user, subscription status, and trial info."""
    from backend.database import set_trial_ends_at
    sub_row = await get_active_subscription(user["id"])
    sub = {"status": sub_row["status"], "current_period_end": str(sub_row["current_period_end"]) if sub_row.get("current_period_end") else "", "price_eur": SUBSCRIPTION_PRICE_EUR} if sub_row else None
    trial_ends_at = user.get("trial_ends_at")
    trial_used_at = user.get("trial_used_at")
    in_trial = False
    trial_days_left = None
    trial_hours_left = None
    if not trial_ends_at or (isinstance(trial_ends_at, str) and not trial_ends_at.strip()):
        if not trial_used_at:
            new_end = (
                datetime.utcnow() + timedelta(days=settings.TRIAL_DAYS)
            ).strftime("%Y-%m-%dT%H:%M:%S")
            await set_trial_ends_at(user["id"], new_end)
            trial_ends_at = new_end
    if not sub and trial_ends_at and not trial_used_at:
        try:
            if isinstance(trial_ends_at, datetime):
                end = trial_ends_at.replace(tzinfo=None)
            else:
                end = datetime.fromisoformat(str(trial_ends_at).replace("Z", ""))
            now = datetime.utcnow()
            if now < end:
                in_trial = True
                delta = end - now
                trial_days_left = max(0, delta.days)
                trial_hours_left = max(0, int(delta.total_seconds() / 3600))
        except Exception:
            pass
    return {
        "user": {
            "id": user["id"],
            "email": user["email"],
            "is_admin": bool(user.get("is_admin")),
        },
        "subscription": sub,
        "subscription_price_eur": SUBSCRIPTION_PRICE_EUR,
        "trial": {
            "trial_ends_at": str(trial_ends_at) if trial_ends_at else None,
            "trial_used_at": str(trial_used_at) if trial_used_at else None,
            "in_trial": in_trial,
            "trial_days_left": trial_days_left,
            "trial_hours_left": trial_hours_left,
            "trial_days": settings.TRIAL_DAYS,
        },
    }


# ── Subscription API (Google Play Billing only) ──────────────

@app.get("/api/subscription/status")
async def subscription_status(user: dict = Depends(get_current_user)):
    sub = await get_active_subscription(user["id"])
    result = {"status": sub["status"], "current_period_end": str(sub["current_period_end"]) if sub.get("current_period_end") else "", "price_eur": SUBSCRIPTION_PRICE_EUR} if sub else None
    return {"subscription": result, "price_eur": SUBSCRIPTION_PRICE_EUR}


class GooglePlayVerifyRequest(BaseModel):
    purchase_token: str
    product_id: str
    package_name: str = "com.zagrid.albanianlawai"


@app.post("/api/subscription/verify-google-play")
async def verify_google_play_purchase(
    req: GooglePlayVerifyRequest,
    user: dict = Depends(get_current_user),
):
    """Verify a Google Play subscription purchase and activate it.

    Uses Google Play Developer API for server-side verification when
    GOOGLE_PLAY_SERVICE_ACCOUNT_JSON is configured. Falls back to
    client-trust mode for development/testing.
    """
    import httpx

    verified = False
    period_end_str = None

    if os.environ.get("GOOGLE_PLAY_SERVICE_ACCOUNT_JSON"):
        try:
            from google.oauth2 import service_account
            from google.auth.transport.requests import Request as GRequest
            import json as _json

            creds_path = os.environ["GOOGLE_PLAY_SERVICE_ACCOUNT_JSON"]
            creds = service_account.Credentials.from_service_account_file(
                creds_path,
                scopes=["https://www.googleapis.com/auth/androidpublisher"],
            )
            creds.refresh(GRequest())

            api_url = (
                f"https://androidpublisher.googleapis.com/androidpublisher/v3/"
                f"applications/{req.package_name}/purchases/subscriptions/"
                f"{req.product_id}/tokens/{req.purchase_token}"
            )
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(
                    api_url,
                    headers={"Authorization": f"Bearer {creds.token}"},
                )

            if r.status_code == 200:
                data = r.json()
                payment_state = data.get("paymentState")
                expiry_ms = int(data.get("expiryTimeMillis", 0))
                if payment_state in (0, 1) and expiry_ms > 0:
                    from datetime import timezone
                    period_end = datetime.fromtimestamp(
                        expiry_ms / 1000, tz=timezone.utc
                    )
                    period_end_str = period_end.strftime("%Y-%m-%dT%H:%M:%S")
                    verified = True
                    logger.info(
                        f"Google Play purchase VERIFIED for user {user['id']}, "
                        f"expires {period_end_str}"
                    )
                else:
                    logger.warning(
                        f"Google Play purchase invalid state for user {user['id']}: "
                        f"paymentState={payment_state}"
                    )
                    raise HTTPException(
                        status_code=400,
                        detail="Blerja nuk u verifikua. Provoni përsëri.",
                    )
            else:
                logger.warning(
                    f"Google Play API error {r.status_code}: {r.text[:200]}"
                )
                raise HTTPException(
                    status_code=400,
                    detail="Nuk u verifikua blerja me Google Play.",
                )
        except HTTPException:
            raise
        except ImportError:
            logger.warning(
                "google-auth not installed. Install google-auth and "
                "google-auth-httplib2 for server-side verification."
            )
        except Exception as e:
            logger.error(f"Google Play verification error: {e}")
    else:
        logger.info(
            "GOOGLE_PLAY_SERVICE_ACCOUNT_JSON not set — "
            "trusting client purchase (dev mode)"
        )

    if not verified:
        now = datetime.utcnow()
        period_end_str = (now + timedelta(days=31)).strftime("%Y-%m-%dT%H:%M:%S")

    await upsert_subscription(
        user_id=user["id"],
        purchase_token=req.purchase_token,
        product_id=req.product_id,
        status="active",
        current_period_end=period_end_str,
        platform="google_play",
    )
    await set_trial_used_on_subscription(user["id"])
    logger.info(f"Google Play subscription activated for user {user['id']}")
    return {"status": "active", "current_period_end": period_end_str}


@app.post("/api/subscription/restore")
async def restore_purchase(user: dict = Depends(get_current_user)):
    """Check if user has an active subscription (for restore purchase flow)."""
    sub = await get_active_subscription(user["id"])
    if sub and sub["status"] in ("active", "trialing"):
        return {"restored": True, "status": sub["status"], "current_period_end": str(sub["current_period_end"]) if sub.get("current_period_end") else ""}
    return {"restored": False, "message": "Nuk u gjet asnjë abonim aktiv."}


# ── User Document API (admin only for management) ─────────────

@app.post("/api/user/documents/upload")
async def user_upload_document(
    file: UploadFile = File(...),
    title: str = Form(None),
    user: dict = Depends(require_admin),
):
    """Upload a document. Only admin can upload documents."""
    # Validate file type
    allowed_types = {"pdf", "docx", "doc", "txt"}
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in allowed_types:
        raise HTTPException(
            status_code=400,
            detail=f"Lloji i skedarit '.{ext}' nuk mbështetet. Të lejuara: {', '.join(allowed_types)}"
        )

    # Check document limit
    user_doc_count = await count_user_documents(user["id"])
    if user_doc_count >= MAX_DOCS_PER_USER:
        raise HTTPException(
            status_code=400,
            detail=f"Keni arritur limitin e {MAX_DOCS_PER_USER} dokumenteve."
        )

    # Read and check size
    content = await file.read()
    if len(content) > MAX_FILE_SIZE_MB * 1024 * 1024:
        raise HTTPException(
            status_code=400,
            detail=f"Skedari është shumë i madh. Maksimumi: {MAX_FILE_SIZE_MB}MB"
        )

    unique_name = f"{uuid.uuid4().hex}_{file.filename}"
    spath = storage_path_for_doc(user["id"], unique_name)
    content_type = file.content_type or "application/octet-stream"
    await storage_upload(spath, content, content_type)

    doc_id = await create_document(
        user_id=user["id"],
        filename=unique_name,
        original_filename=file.filename,
        file_type=ext,
        file_size=len(content),
        title=title,
        storage_bucket="Ligje",
        storage_path=spath,
    )

    asyncio.create_task(
        _process_in_background(doc_id, user["id"], content, ext)
    )

    return JSONResponse({
        "id": doc_id,
        "filename": file.filename,
        "status": "processing",
        "message": "Dokumenti u ngarkua. Përpunimi ka filluar.",
    })


@app.get("/api/user/documents")
async def list_user_documents(user: dict = Depends(require_admin)):
    """List all documents belonging to admin. Only admin can view."""
    docs = await get_user_documents(user["id"])
    return {
        "documents": [
            {
                "id": d["id"],
                "title": d.get("title") or d.get("original_filename", ""),
                "original_filename": d.get("original_filename", ""),
                "file_type": d.get("file_type", ""),
                "file_size": d.get("file_size", 0),
                "status": d.get("status", "processing"),
                "total_chunks": d.get("total_chunks", 0),
                "page_count": d.get("page_count", 0),
                "error_message": d.get("error_message"),
                "uploaded_at": str(d["uploaded_at"]) if d.get("uploaded_at") else None,
            }
            for d in docs
        ]
    }


@app.get("/api/user/documents/ready")
async def list_user_ready_documents(user: dict = Depends(require_admin)):
    """List only ready documents for dropdown filter. Admin only."""
    docs = await get_user_ready_documents(user["id"])
    return {
        "documents": [
            {
                "id": d["id"],
                "title": d.get("title") or d.get("original_filename", ""),
                "total_chunks": d.get("total_chunks", 0),
            }
            for d in docs
        ]
    }


@app.delete("/api/user/documents/{doc_id}")
async def user_delete_document(doc_id: int, user: dict = Depends(require_admin)):
    """Delete a document. Admin only."""
    doc = await get_document_for_user(doc_id, user["id"])
    if not doc:
        raise HTTPException(status_code=404, detail="Dokumenti nuk u gjet.")

    await delete_document_chunks(doc_id)

    spath = _resolve_storage_path(doc)
    await storage_delete(spath)

    await delete_document(doc_id)
    return {"message": "Dokumenti u fshi me sukses."}


@app.post("/api/user/documents/{doc_id}/retry")
async def user_retry_document(doc_id: int, user: dict = Depends(require_admin)):
    """Retry processing a failed document. Admin only."""
    doc = await get_document_for_user(doc_id, user["id"])
    if not doc:
        raise HTTPException(status_code=404, detail="Dokumenti nuk u gjet.")
    if doc.get("status") not in ("failed", "error"):
        raise HTTPException(
            status_code=400,
            detail="Vetëm dokumentet e dështuara mund të ripërpunohen."
        )

    spath = _resolve_storage_path(doc)
    try:
        file_bytes = await storage_download(spath)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Skedari nuk u gjet në storage.")

    await delete_document_chunks(doc_id)

    asyncio.create_task(
        _process_in_background(doc_id, user["id"], file_bytes, doc["file_type"])
    )

    return {"status": "processing", "doc_id": doc_id, "message": "Ripërpunimi ka filluar."}


class RenameRequest(BaseModel):
    title: str


@app.patch("/api/user/documents/{doc_id}/rename")
async def user_rename_document(
    doc_id: int, data: RenameRequest, user: dict = Depends(require_admin)
):
    """Rename a document's title (only if it belongs to the user)."""
    doc = await get_document_for_user(doc_id, user["id"])
    if not doc:
        raise HTTPException(status_code=404, detail="Dokumenti nuk u gjet.")
    if not data.title.strip():
        raise HTTPException(status_code=400, detail="Titulli nuk mund të jetë bosh.")
    await rename_document(doc_id, data.title.strip())
    return {"message": "Titulli u ndryshua.", "title": data.title.strip()}


@app.get("/api/user/documents/{doc_id}/pdf")
async def serve_user_document_pdf(
    doc_id: int,
    request: Request,
    user: dict = Depends(get_current_user_optional),
):
    """Serve the original PDF file for viewing. Admin only.
    Accepts token via Bearer header or ?token= query param for new-tab viewing.
    """
    # Support ?token= query param for opening in new tab
    if user is None:
        token = request.query_params.get("token")
        if token:
            from backend.auth import decode_token
            from backend.database import get_user_by_id
            payload = decode_token(token)
            if payload:
                uid = payload.get("sub") or payload.get("user_id")
                if uid:
                    user = await get_user_by_id(int(uid))
    if user is None:
        raise HTTPException(status_code=401, detail="Nuk jeni i autentifikuar.")
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Akses i kufizuar vetëm për administratorë.")

    doc = await get_document_for_user(doc_id, user["id"])
    if not doc:
        raise HTTPException(status_code=404, detail="Dokumenti nuk u gjet.")
    spath = _resolve_storage_path(doc)
    try:
        file_bytes = await storage_download(spath)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Skedari nuk u gjet.")
    return Response(
        content=file_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{doc.get("original_filename", "document.pdf")}"'},
    )


async def _process_in_background(doc_id: int, user_id: int,
                                  file_bytes: bytes, file_type: str):
    """Background task for document processing."""
    try:
        await process_document(doc_id, user_id, file_bytes, file_type)
    except Exception as e:
        logger.error(f"[ERROR] Processing document {doc_id}: {e}")


# ── Admin Document API (admin only, legacy) ──────────────────

@app.post("/api/documents/upload")
async def upload_document(
    file: UploadFile = File(...),
    title: str = Form(None),
    law_number: str = Form(None),
    law_date: str = Form(None),
    user: dict = Depends(require_admin),
):
    """Upload a legal document (admin only). Assigned to admin's user_id."""
    allowed_types = {"pdf", "docx", "doc", "txt"}
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in allowed_types:
        raise HTTPException(
            status_code=400,
            detail=f"Lloji i skedarit '.{ext}' nuk mbështetet."
        )

    unique_name = f"{uuid.uuid4().hex}_{file.filename}"
    content = await file.read()
    spath = storage_path_for_doc(user["id"], unique_name)
    content_type = file.content_type or "application/octet-stream"
    await storage_upload(spath, content, content_type)

    doc_id = await create_document(
        user_id=user["id"],
        filename=unique_name,
        original_filename=file.filename,
        file_type=ext,
        file_size=len(content),
        title=title,
        law_number=law_number,
        law_date=law_date,
        storage_bucket="Ligje",
        storage_path=spath,
    )

    asyncio.create_task(
        _process_in_background(doc_id, user["id"], content, ext)
    )

    return JSONResponse({
        "id": doc_id,
        "filename": file.filename,
        "status": "processing",
        "message": "Document uploaded and processing started.",
    })


@app.get("/api/documents")
async def list_documents(user: dict = Depends(require_admin)):
    """List all uploaded documents (admin view)."""
    docs = await get_all_documents()
    return {"documents": docs}


@app.get("/api/documents/{doc_id}")
async def get_document_detail(doc_id: int, user: dict = Depends(require_admin)):
    doc = await get_document(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Dokumenti nuk u gjet.")
    return doc


@app.delete("/api/documents/{doc_id}")
async def remove_document(doc_id: int, user: dict = Depends(require_admin)):
    doc = await get_document(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Dokumenti nuk u gjet.")
    await delete_document_chunks(doc_id)
    spath = _resolve_storage_path(doc)
    await storage_delete(spath)
    await delete_document(doc_id)
    return {"message": "Document deleted successfully."}


# ── Sync from Storage ────────────────────────────────────────

async def _sync_download_and_process(doc_id: int, user_id: int,
                                      spath: str, ext: str):
    """Background: download file from storage then process it."""
    try:
        file_bytes = await storage_download(spath)
        await process_document(doc_id, user_id, file_bytes, ext)
    except Exception as e:
        logger.error(f"[sync] Processing {spath} (doc {doc_id}) failed: {e}")
        from backend.database import update_document_status
        await update_document_status(doc_id, "failed", error_message=str(e)[:500])


@app.post("/api/admin/sync-storage")
async def sync_from_storage(user: dict = Depends(require_admin)):
    """Scan Supabase Storage bucket and import files missing from the DB.

    Inserts metadata rows immediately, then kicks off background processing
    for each new file. Returns fast — processing happens asynchronously.
    """
    bucket_files = await list_bucket_files()
    if not bucket_files:
        return {"synced": 0, "message": "Nuk u gjetën skedarë në storage."}

    existing_docs = await get_all_documents()
    known_paths = set()
    known_filenames = set()
    for d in existing_docs:
        if d.get("storage_path"):
            known_paths.add(d["storage_path"])
        if d.get("filename"):
            known_filenames.add(d["filename"])
        if d.get("original_filename"):
            known_filenames.add(d["original_filename"])

    allowed_extensions = {"pdf", "docx", "doc", "txt"}
    synced = 0
    errors = []

    for bf in bucket_files:
        spath = bf.get("full_path", bf.get("name", ""))
        fname = spath.rsplit("/", 1)[-1] if "/" in spath else spath
        ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""

        if ext not in allowed_extensions:
            continue
        if spath in known_paths or fname in known_filenames:
            continue

        file_size = bf.get("metadata", {}).get("size", 0) if isinstance(bf.get("metadata"), dict) else 0
        title = fname.rsplit(".", 1)[0] if "." in fname else fname

        try:
            doc_id = await create_document(
                user_id=user["id"],
                filename=fname,
                original_filename=fname,
                file_type=ext,
                file_size=file_size,
                title=title,
                storage_bucket="Ligje",
                storage_path=spath,
            )
            synced += 1
            asyncio.create_task(
                _sync_download_and_process(doc_id, user["id"], spath, ext)
            )
        except Exception as ins_err:
            logger.warning(f"Sync: insert failed for {spath}: {ins_err}")
            errors.append(f"{fname}: {str(ins_err)[:100]}")

    result = {
        "synced": synced,
        "total_in_bucket": len(bucket_files),
        "already_known": len(bucket_files) - synced - len(errors),
        "message": f"U sinkronizuan {synced} dokumente të reja. Përpunimi vazhdon në sfond.",
    }
    if errors:
        result["errors"] = errors
    return result


# ── Chat API ──────────────────────────────────────────────────

class ChatRequest(BaseModel):
    question: str
    session_id: Optional[str] = None
    document_id: Optional[int] = None
    debug: Optional[bool] = False
    stream: Optional[bool] = False


async def _validate_chat_prereqs(user_id: int, document_id: int = None,
                                  is_admin: bool = False):
    """Validate that ready documents exist for chat.

    Admin: checks own documents. Normal user: checks all ready docs globally.
    """
    if is_admin:
        ready_docs = await get_user_ready_documents(user_id)
        if not ready_docs:
            all_user_docs = await get_user_documents(user_id)
            processing = [d for d in all_user_docs if d.get("status") == "processing"]
            if processing:
                return "Dokumentet tuaja janë ende duke u përpunuar. Ju lutem prisni pak."
            return "Ju lutem ngarkoni një dokument PDF përpara se të bëni pyetje."

        if document_id:
            doc = await get_document_for_user(document_id, user_id)
            if not doc:
                raise HTTPException(
                    status_code=404,
                    detail="Dokumenti i zgjedhur nuk u gjet ose nuk ju përket."
                )
            if doc.get("status") != "ready":
                raise HTTPException(
                    status_code=400,
                    detail="Dokumenti i zgjedhur nuk është ende gati."
                )
    else:
        # Normal users search globally — check if ANY ready documents exist
        ready_docs = await get_all_ready_documents()
        if not ready_docs:
            return "Sistemi nuk ka dokumente të gatshme aktualisht. Ju lutem provoni më vonë."

    return None


@app.post("/api/chat")
@limiter.limit("30/minute")
async def chat(request: Request, body: ChatRequest, user: dict = Depends(require_subscription)):
    """Ask a question — hybrid RAG pipeline.

    Admin: searches their own documents.
    Normal user: searches all ready documents globally (knowledge base).
    """
    if not body.question.strip():
        raise HTTPException(status_code=400, detail="Pyetja nuk mund të jetë bosh.")

    user_id = user["id"]
    is_admin = bool(user.get("is_admin"))

    # Normal users cannot filter by document_id (they don't see docs)
    doc_id = body.document_id if is_admin else None

    # Validate prerequisites
    prereq_msg = await _validate_chat_prereqs(user_id, doc_id, is_admin)
    if prereq_msg:
        return {
            "answer": prereq_msg, "sources": [],
            "session_id": body.session_id or uuid.uuid4().hex,
            "context_found": False,
        }

    # Streaming mode
    if body.stream:
        from fastapi.responses import StreamingResponse

        async def event_stream():
            async for chunk_json in generate_answer_stream(
                question=body.question,
                user_id=user_id,
                doc_id=doc_id,
                is_admin=is_admin,
            ):
                yield f"data: {chunk_json}\n\n"

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    session_id = body.session_id or uuid.uuid4().hex

    history = await get_chat_history(session_id)
    history_for_llm = [
        {"role": m["role"], "content": m["content"]} for m in history
    ]

    await save_chat_message(session_id, "user", body.question)

    result = await generate_answer(
        question=body.question,
        user_id=user_id,
        doc_id=doc_id,
        chat_history=history_for_llm,
        debug_mode=body.debug or False,
        is_admin=is_admin,
    )

    await save_chat_message(
        session_id, "assistant", result["answer"], result["sources"]
    )

    response = {
        "answer": result["answer"],
        "sources": result["sources"],
        "all_sources": result.get("all_sources", []),
        "session_id": session_id,
        "context_found": result["context_found"],
        "chunks_used": result.get("chunks_used", 0),
        "top_similarity": result.get("top_similarity", 0),
        "search_time_ms": result.get("search_time_ms", 0),
        "expand_time_ms": result.get("expand_time_ms", 0),
        "stitch_time_ms": result.get("stitch_time_ms", 0),
        "generation_time_ms": result.get("generation_time_ms", 0),
        "coverage_check_ms": result.get("coverage_check_ms", 0),
        "queries_used": result.get("queries_used", 0),
        "coverage_passes": result.get("coverage_passes", 0),
    }

    if result.get("confidence_blocked"):
        response["confidence_blocked"] = True

    if body.debug and result.get("debug"):
        response["debug"] = result["debug"]

    return response


@app.get("/api/chat/history/{session_id}")
async def get_chat_history_endpoint(
    session_id: str, user: dict = Depends(require_subscription)
):
    messages = await get_chat_history(session_id)
    for msg in messages:
        if isinstance(msg.get("sources_json"), str):
            msg["sources"] = json.loads(msg["sources_json"])
        else:
            msg["sources"] = msg.get("sources_json", [])
    return {"messages": messages}


# ── Precomputed Topic Index (loaded on startup) ─────────────
# In-memory list of {keyword, doc_title, article, suggestion} for instant matching.

import re as _re_mod

_topic_index: list[dict] = []       # [{kw, title, article, suggestion}, ...]
_topic_index_ready = False

_SQ_STOP = frozenset(
    'dhe ose per nga nje tek te ne me se ka si do jane eshte nuk qe i e u '
    'ky kjo keto ato por nese edhe mund duhet cfare cilat cili kane '
    'neni ligj ligji ligje kodi kodit nr date sipas '
    'ndaj kete ketij asaj atij tij ketyre atyre tjeter '
    'bene beri here mire keq behet kur pasi para deri '
    'brenda jashte mbi nen nder ndermjet midis drejt kunder '
    'shume pak tere gjithe asnje ndonje disa shumice '
    'ishte jene esht osht asht mos'.split()
)


def _short_title(title: str) -> str:
    if not title:
        return "ligji"
    t = _re_mod.sub(r'\s+I\s+REPUBLIK.*$', '', title, flags=_re_mod.IGNORECASE).strip()
    t = _re_mod.sub(r'\s+', ' ', t).strip()
    return t[:37] + "..." if len(t) > 40 else t


_EXTENDED_STOP_RAW = (
    'eshte jane kete kesaj ketij atij asaj tyre nenit duke rast fuqi '
    'ndryshuar tjera kane nese date ligjin jane parashikuar '
    'baze mase pase saje qene nder shteti shtetit kunder '
    'pika pikat nenin parashikuara percaktuara parashikohet '
    'percaktohet zbatimit zbatimin kerkon kerkuar mundesine '
    'rastin rastet rregullat rregullave vendimin vendimi '
    'shoqerise republikes shqiperise dispozitave dispozitat '
    'personit personave subjektet subjektit ligjit ligjore '
    'procedura procedurave neneve nenin neneve sipas '
    # Albanian diacritical forms
    'është janë këtë kësaj këtij nëse datë kanë pikës '
    'tjetër përputhje ndërtimit këto kurrë këtyre atyre '
    'përkatëse përkatës çdo mundësinë punës organeve '
)

def _norm_alb(w: str) -> str:
    return w.replace('ë', 'e').replace('ç', 'c').replace('Ë', 'E').replace('Ç', 'C').lower()

_EXTENDED_STOP = frozenset(
    list(_SQ_STOP) +
    list(_EXTENDED_STOP_RAW.split()) +
    [_norm_alb(w) for w in _EXTENDED_STOP_RAW.split()]
)


def _build_topic_index():
    """Extract meaningful legal keywords from ALL chunks in ChromaDB."""
    global _topic_index, _topic_index_ready
    from backend.vector_store import collection as _col, _ensure_initialized
    _ensure_initialized()
    try:
        if not _col:
            _topic_index_ready = True
            return
        total = _col.count()
        if total == 0:
            _topic_index_ready = True
            return

        batch_size = 5000
        kw_map: dict[str, dict] = {}

        for offset in range(0, total, batch_size):
            batch = _col.get(
                offset=offset, limit=batch_size,
                include=["metadatas", "documents"],
            )
            for meta, doc in zip(batch["metadatas"] or [], batch["documents"] or []):
                title = (meta or {}).get("title", "")
                article = (meta or {}).get("article", "")
                words = _re_mod.findall(r'\b\w{4,}\b', (doc or "").lower())
                for w in words:
                    if w in _EXTENDED_STOP or _norm_alb(w) in _EXTENDED_STOP or len(w) < 4:
                        continue
                    if w not in kw_map:
                        kw_map[w] = {"title": title, "article": article, "cnt": 0}
                    kw_map[w]["cnt"] += 1

        # Keep top 600 keywords by frequency (after aggressive filtering)
        top_kws = sorted(kw_map.items(), key=lambda x: -x[1]["cnt"])[:600]
        entries = []
        for kw, info in top_kws:
            short = _short_title(info["title"])
            art = info["article"]
            if art:
                sug = f"Çfarë parashikon Neni {art} për {kw}?"
            else:
                sug = f"Çfarë thotë {short} për {kw}?"
            entries.append({
                "kw": kw,
                "title": short,
                "article": art,
                "suggestion": sug,
            })
        _topic_index = entries
        _topic_index_ready = True
        logger.info(f"Topic index built: {len(entries)} keywords from {total} chunks")
    except Exception as exc:
        logger.warning(f"Topic index build failed: {exc}")
        _topic_index_ready = True


def _instant_suggestions(partial: str) -> dict | None:
    """Match user input against precomputed topic index — 0ms."""
    if not _topic_index:
        return None
    words = set(_re_mod.findall(r'\b\w{3,}\b', partial.lower())) - _SQ_STOP
    if not words:
        return None

    scored = []
    for entry in _topic_index:
        kw = entry["kw"]
        for w in words:
            if kw.startswith(w) or w.startswith(kw):
                scored.append(entry)
                break

    if not scored:
        return None

    seen = set()
    suggestions = []
    related = []
    for e in scored[:12]:
        s = e["suggestion"]
        if s.lower() not in seen and len(suggestions) < 3:
            suggestions.append(s)
            seen.add(s.lower())
        t = e["title"]
        a = e["article"]
        label = f"Neni {a} — {t}" if a else t
        if label.lower() not in seen and len(related) < 3:
            related.append(label)
            seen.add(label.lower())

    if not suggestions:
        return None

    return {"suggestions": suggestions, "related": related, "local": True}


# ── Suggestion cache ─────────────────────────────────────────

_suggest_cache: dict[str, dict] = {}
_SG_CACHE_MAX = 400
_SG_CACHE_TTL = 600  # 10 min


def _sg_norm(t: str) -> str:
    return _re_mod.sub(r'\s+', ' ', t.lower().strip())


def _sg_get(key: str):
    import time as _t
    e = _suggest_cache.get(key)
    if e and (_t.time() - e["ts"]) < _SG_CACHE_TTL:
        return e["r"]
    for n in range(len(key) - 1, 7, -1):
        e = _suggest_cache.get(key[:n])
        if e and (_t.time() - e["ts"]) < _SG_CACHE_TTL:
            return e["r"]
    return None


def _sg_set(key: str, result: dict):
    import time as _t
    if len(_suggest_cache) >= _SG_CACHE_MAX:
        oldest = min(_suggest_cache, key=lambda k: _suggest_cache[k]["ts"])
        _suggest_cache.pop(oldest, None)
    _suggest_cache[key] = {"r": result, "ts": _t.time()}


# ── Suggestion templates ─────────────────────────────────────

_SQ_TEMPLATES = [
    "Çfarë thotë {doc} për {topic}?",
    "Si rregullohet {topic} sipas {doc}?",
    "Cilat janë dispozitat për {topic} në {doc}?",
]

_SQ_TEMPLATES_ART = [
    "Çfarë parashikon Neni {art} për {topic}?",
    "Si interpretohet Neni {art} i {doc}?",
    "Cilat janë rregullat e Nenit {art} të {doc}?",
]


def _build_suggestions(partial: str, chunks: list[dict]) -> dict:
    """Build suggestions deterministically from chunks — zero LLM."""
    partial_lower = partial.lower().strip()
    partial_words = set(_re_mod.findall(r'\b\w{3,}\b', partial_lower)) - _SQ_STOP

    seen_docs = {}
    seen_articles = []

    for c in chunks[:10]:
        title = (c.get("title") or "").strip()
        article = (c.get("article") or "").strip()
        if title and title not in seen_docs:
            seen_docs[title] = article
        if article and len(seen_articles) < 5:
            seen_articles.append((article, title))

    core = [w for w in _re_mod.findall(r'\b\w{3,}\b', partial_lower)
            if w not in _SQ_STOP]
    topic = " ".join(core[:5]) if core else partial_lower[:30]

    suggestions = []
    used = set()

    if seen_articles:
        art, title = seen_articles[0]
        sd = _short_title(title)
        s = _SQ_TEMPLATES_ART[0].format(art=art, doc=sd, topic=topic)
        suggestions.append(s)
        used.add(s.lower())

    for title in list(seen_docs.keys())[:3]:
        if len(suggestions) >= 3:
            break
        sd = _short_title(title)
        for tmpl in _SQ_TEMPLATES:
            s = tmpl.format(doc=sd, topic=topic)
            if s.lower() not in used:
                suggestions.append(s)
                used.add(s.lower())
                break

    if len(suggestions) < 3:
        clean = partial.rstrip("?!. ").strip()
        suggestions.append(f"Sipas ligjit, {clean.lower()}?")

    related = []
    seen_topics = set()
    for c in chunks[:10]:
        article = (c.get("article") or "").strip()
        title = (c.get("title") or "").strip()
        if article and len(related) < 3:
            short = _short_title(title)
            label = f"Neni {article} — {short}" if short else f"Neni {article}"
            if label.lower() not in seen_topics:
                related.append(label)
                seen_topics.add(label.lower())
    for title in list(seen_docs.keys()):
        if len(related) >= 3:
            break
        short = _short_title(title)
        if short and short.lower() not in seen_topics:
            related.append(short)
            seen_topics.add(short.lower())

    return {"suggestions": suggestions[:3], "related": related[:3]}


# ── Endpoints ────────────────────────────────────────────────

class SuggestRequest(BaseModel):
    partial: str
    document_id: Optional[int] = None


@app.get("/api/suggest-topics")
async def suggest_topics_endpoint(user: dict = Depends(get_current_user)):
    """Return precomputed topic index for client-side instant matching."""
    return {"topics": _topic_index[:300], "ready": _topic_index_ready}


@app.post("/api/suggest-questions")
async def suggest_questions(
    request: SuggestRequest, user: dict = Depends(get_current_user)
):
    """Ultra-fast grounded suggestions — NO LLM.

    Pipeline (<300ms target):
    1. Cache check → 0ms
    2. Single vector search k=12 → ~150-350ms
    3. Deterministic template build → ~1ms
    """
    partial = request.partial.strip()
    if len(partial) < 8:
        return {"suggestions": [], "related": []}

    import time
    start = time.time()

    ck = _sg_norm(partial)

    # ── 1. Cache hit → instant ────────────────────────────
    cached = _sg_get(ck)
    if cached:
        return {**cached, "ms": 0, "from_cache": True}

    # ── 2. Vector search k=12, single query, no rerank ───
    from backend.vector_store import search_documents

    is_admin = bool(user.get("is_admin"))
    doc_id = request.document_id if is_admin else None

    chunks = await search_documents(
        query=partial,
        user_id=None,
        doc_id=doc_id,
        top_k=12,
        threshold=1.0,
    )

    if not chunks:
        empty = {"suggestions": [], "related": [], "ms": int((time.time() - start) * 1000)}
        return empty

    # ── 3. Build from chunks (deterministic, ~1ms) ───────
    result = _build_suggestions(partial, chunks)
    ms = int((time.time() - start) * 1000)
    result["ms"] = ms

    _sg_set(ck, result)
    logger.info(f"Suggest: '{partial[:30]}' → {len(result['suggestions'])}s {ms}ms")
    return result


# ── Health Check ──────────────────────────────────────────────

# ── Suggested Questions API ────────────────────────────────────

@app.get("/api/suggested-questions")
async def get_suggested_questions_api():
    """Public: return active questions grouped by category."""
    questions = await get_active_suggested_questions()
    grouped = {}
    for q in questions:
        cat = q["category"]
        if cat not in grouped:
            grouped[cat] = []
        grouped[cat].append({"id": q["id"], "question": q["question"]})
    return {"categories": [{"name": k, "questions": v} for k, v in grouped.items()]}


@app.get("/api/admin/suggested-questions")
async def admin_list_suggested_questions(user: dict = Depends(require_admin)):
    """Admin: list all questions (active + inactive)."""
    return {"questions": await get_all_suggested_questions()}


@app.post("/api/admin/suggested-questions")
async def admin_create_suggested_question(request: Request, user: dict = Depends(require_admin)):
    body = await request.json()
    category = body.get("category", "").strip()
    question = body.get("question", "").strip()
    sort_order = body.get("sort_order", 0)
    if not category or not question:
        raise HTTPException(status_code=400, detail="Category and question are required.")
    qid = await create_suggested_question(category, question, sort_order)
    return {"ok": True, "id": qid}


@app.patch("/api/admin/suggested-questions/{qid}")
async def admin_update_suggested_question(qid: int, request: Request, user: dict = Depends(require_admin)):
    body = await request.json()
    await update_suggested_question(
        qid,
        category=body.get("category"),
        question=body.get("question"),
        is_active=body.get("is_active"),
        sort_order=body.get("sort_order"),
    )
    return {"ok": True}


@app.delete("/api/admin/suggested-questions/{qid}")
async def admin_delete_suggested_question(qid: int, user: dict = Depends(require_admin)):
    await delete_suggested_question(qid)
    return {"ok": True}


@app.post("/api/admin/promote")
async def promote_to_admin(request: Request):
    """One-time admin promotion secured by JWT_SECRET."""
    body = await request.json()
    secret = body.get("secret", "")
    email = body.get("email", "").strip().lower()
    if not email or secret != settings.JWT_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE users SET is_admin = TRUE WHERE email = $1", email)
        row = await conn.fetchrow("SELECT id, email, is_admin FROM users WHERE email = $1", email)
    if not row:
        raise HTTPException(status_code=404, detail="User not found")
    return {"ok": True, "user": {"id": row["id"], "email": row["email"], "is_admin": bool(row["is_admin"])}}


@app.get("/health")
@app.get("/api/health")
async def health_check():
    db_status = "ok"
    try:
        pool = await _get_pool()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
    except Exception as e:
        db_status = f"error: {e}"
    storage = await check_storage_health()
    return {"status": "ok", "db": db_status, "storage": storage.get("status", "unknown")}


@app.get("/api/health/detailed")
async def health_check_detailed():
    from backend.vector_store import get_store_stats
    db_status = "ok"
    try:
        pool = await _get_pool()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
    except Exception as e:
        db_status = f"error: {e}"
    storage = await check_storage_health()
    docs = await get_all_documents()
    ready = [d for d in docs if d.get("status") == "ready"]
    stats = get_store_stats()
    return {
        "status": "healthy",
        "db": db_status,
        "storage": storage,
        "documents_total": len(docs),
        "documents_ready": len(ready),
        "vector_store": stats,
    }


# ── Debug / Testing Endpoints (admin only) ───────────────────

class DebugSearchRequest(BaseModel):
    query: str
    top_k: Optional[int] = None
    document_id: Optional[int] = None


@app.post("/api/debug/search")
async def debug_search(
    request: DebugSearchRequest, user: dict = Depends(require_admin)
):
    """Test vector search with user-scoped filtering. Admin-only."""
    from backend.vector_store import search_documents_debug
    return await search_documents_debug(
        request.query, user["id"], request.document_id, request.top_k
    )


@app.get("/api/debug/store-stats")
async def debug_store_stats(user: dict = Depends(require_admin)):
    from backend.vector_store import get_store_stats
    docs = await get_all_documents()
    return {
        "vector_store": get_store_stats(),
        "documents": [
            {
                "id": d["id"],
                "user_id": d.get("user_id"),
                "title": d.get("title", ""),
                "status": d.get("status", ""),
                "chunks": d.get("total_chunks", 0),
                "filename": d.get("original_filename", ""),
            }
            for d in docs
        ],
    }


@app.get("/api/debug/db-chunks")
async def debug_db_chunks(user: dict = Depends(require_admin)):
    """Check how many chunks exist in PostgreSQL and test FTS."""
    from backend.database import keyword_search_chunks, _build_pg_tsquery
    pool = await _get_pool()
    async with pool.acquire() as conn:
        total = await conn.fetchval("SELECT COUNT(*) FROM document_chunks")
        user_chunks = await conn.fetchval(
            "SELECT COUNT(*) FROM document_chunks WHERE user_id = $1", user["id"]
        )
    test_query = "pushim vjetor"
    tsquery = _build_pg_tsquery(test_query)
    kw_results = await keyword_search_chunks(test_query, user_id=user["id"], limit=3)
    kw_results_all = await keyword_search_chunks(test_query, user_id=None, limit=3)
    return {
        "total_chunks_in_db": total,
        "chunks_for_user": user_chunks,
        "test_query": test_query,
        "tsquery": tsquery,
        "kw_results_user_filtered": len(kw_results),
        "kw_results_no_filter": len(kw_results_all),
        "kw_preview": [{"id": r["id"], "text": r["content"][:100]} for r in kw_results[:2]],
        "kw_preview_all": [{"id": r["id"], "text": r["content"][:100]} for r in kw_results_all[:2]],
    }


@app.post("/api/debug/reprocess/{doc_id}")
async def debug_reprocess(doc_id: int, user: dict = Depends(require_admin)):
    """Re-process a document (delete old chunks from both stores). Admin-only."""
    doc = await get_document(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found.")
    spath = _resolve_storage_path(doc)
    try:
        file_bytes = await storage_download(spath)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="File not found in storage.")
    doc_user_id = doc.get("user_id") or user["id"]
    await delete_document_chunks(doc_id)
    await delete_chunks_for_document(doc_id)
    asyncio.create_task(
        _process_in_background(doc_id, doc_user_id, file_bytes, doc["file_type"])
    )
    return {
        "status": "reprocessing",
        "doc_id": doc_id,
        "filename": doc["original_filename"],
    }


# ── Static Files (must be LAST to avoid shadowing API routes) ──

app.mount("/static", StaticFiles(directory=str(frontend_dir)), name="static")
