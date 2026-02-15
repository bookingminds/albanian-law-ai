"""Albanian Law AI - FastAPI Application.

API endpoints for document management, RAG chat, auth, and subscription.
"""

import os
import uuid
import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from backend.config import settings
from backend.database import (
    init_db, create_document, get_all_documents, get_document,
    delete_document, update_document_status, save_chat_message,
    get_chat_history, create_user, get_user_by_email, get_users_count,
    count_signups_from_ip_last_24h, set_trial_used_on_subscription,
)
from backend.document_processor import process_document
from backend.vector_store import delete_document_chunks
from backend.chat import generate_answer
from backend.auth import (
    hash_password, verify_password, create_access_token,
    get_current_user, get_current_user_optional, require_admin, require_subscription,
)
from backend.subscription_service import (
    create_checkout_session,
    get_subscription_status,
    handle_webhook_event,
    SUBSCRIPTION_PRICE_EUR,
)
from backend.paypal_service import (
    create_subscription as paypal_create_subscription,
    confirm_subscription as paypal_confirm_subscription,
    handle_webhook as paypal_handle_webhook,
)
from backend.trial_abuse import is_disposable_email, get_client_ip


# ── App Lifecycle ─────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize database on startup."""
    await init_db()
    yield


app = FastAPI(
    title="Albanian Law AI",
    description="RAG-based legal document Q&A for Albanian law",
    version="1.0.0",
    lifespan=lifespan,
)

# Frontend directory
frontend_dir = Path(__file__).resolve().parent.parent / "frontend"


# ── Frontend Routes ───────────────────────────────────────────

@app.get("/")
async def serve_chat():
    """Serve the chat interface."""
    return FileResponse(str(frontend_dir / "index.html"))


@app.get("/admin")
async def serve_admin():
    """Serve the admin panel."""
    return FileResponse(str(frontend_dir / "admin.html"))


@app.get("/login")
async def serve_login():
    """Serve the login/register page."""
    return FileResponse(str(frontend_dir / "login.html"))


# ── Auth API ──────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    email: str
    password: str


class LoginRequest(BaseModel):
    email: str
    password: str


@app.post("/api/auth/register")
async def register(data: RegisterRequest, request: Request):
    """Register a new user. First user becomes admin.
    3-day free trial starts at signup: trial_ends_at = signup + TRIAL_DAYS. Full access during trial."""
    email = data.email.strip().lower()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Email i pavlefshëm.")
    if len(data.password) < 8:
        raise HTTPException(status_code=400, detail="Fjalëkalimi duhet të ketë të paktën 8 karaktere.")
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
    is_admin = count == 0 or (settings.ADMIN_EMAIL and email == settings.ADMIN_EMAIL.strip().lower())
    trial_ends_at = (datetime.utcnow() + timedelta(days=settings.TRIAL_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
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


@app.post("/api/auth/login")
async def login(data: LoginRequest):
    """Login and return JWT."""
    user = await get_user_by_email(data.email.strip().lower())
    if not user or not verify_password(data.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Email ose fjalëkalim i gabuar.")
    token = create_access_token(user["id"], user["email"], bool(user.get("is_admin")))
    return {"token": token, "user": {"id": user["id"], "email": user["email"], "is_admin": bool(user.get("is_admin"))}}


@app.get("/api/auth/me")
async def auth_me(user: dict = Depends(get_current_user)):
    """Return current user, subscription status, and trial info."""
    sub = await get_subscription_status(user["id"])
    trial_ends_at = user.get("trial_ends_at")
    trial_used_at = user.get("trial_used_at")
    in_trial = False
    trial_days_left = None
    trial_hours_left = None
    if not sub and trial_ends_at and not trial_used_at:
        try:
            end = datetime.fromisoformat(trial_ends_at.replace("Z", ""))
            now = datetime.utcnow()
            if now < end:
                in_trial = True
                delta = end - now
                trial_days_left = max(0, delta.days)
                trial_hours_left = max(0, int(delta.total_seconds() / 3600))
        except Exception:
            pass
    return {
        "user": {"id": user["id"], "email": user["email"], "is_admin": bool(user.get("is_admin"))},
        "subscription": sub,
        "subscription_price_eur": SUBSCRIPTION_PRICE_EUR,
        "trial": {
            "trial_ends_at": trial_ends_at,
            "trial_used_at": trial_used_at,
            "in_trial": in_trial,
            "trial_days_left": trial_days_left,
            "trial_hours_left": trial_hours_left,
            "trial_days": settings.TRIAL_DAYS,
        },
    }


# ── Subscription API ─────────────────────────────────────────

@app.post("/api/subscription/checkout")
async def subscription_checkout(user: dict = Depends(get_current_user)):
    """Create Stripe Checkout session and return URL for redirect."""
    success_url = f"{settings.FRONTEND_URL}/?subscription=success"
    cancel_url = f"{settings.FRONTEND_URL}/?subscription=cancel"
    url = await create_checkout_session(user["id"], user["email"], success_url, cancel_url)
    return {"checkout_url": url}


@app.post("/api/subscription/checkout-paypal")
async def subscription_checkout_paypal(user: dict = Depends(get_current_user)):
    """Create PayPal subscription and return approval URL for redirect."""
    base = settings.FRONTEND_URL.rstrip("/")
    return_url = f"{base}/api/subscription/paypal/confirm"
    cancel_url = f"{base}/?subscription=cancel"
    approval_url, subscription_id = await paypal_create_subscription(
        user["id"], return_url, cancel_url
    )
    return {"approval_url": approval_url, "subscription_id": subscription_id}


@app.get("/api/subscription/paypal/confirm")
async def paypal_confirm_redirect(request: Request):
    """PayPal redirects here after user approves. token=subscription_id. Sync and redirect to frontend."""
    from fastapi.responses import RedirectResponse
    token = request.query_params.get("token")
    if not token:
        return RedirectResponse(url=f"{settings.FRONTEND_URL}/?subscription=error")
    try:
        await paypal_confirm_subscription(token)
    except Exception:
        pass
    return RedirectResponse(url=f"{settings.FRONTEND_URL}/?subscription=success")


@app.get("/api/subscription/status")
async def subscription_status(user: dict = Depends(get_current_user)):
    """Return current subscription status."""
    sub = await get_subscription_status(user["id"])
    return {"subscription": sub, "price_eur": SUBSCRIPTION_PRICE_EUR}


@app.post("/api/webhooks/stripe")
async def stripe_webhook(request: Request):
    """Stripe webhook: subscription lifecycle events."""
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    if not settings.STRIPE_WEBHOOK_SECRET:
        raise HTTPException(status_code=503, detail="Webhook not configured")
    try:
        import stripe
        event = stripe.Webhook.construct_event(payload, sig, settings.STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid signature: {e}")
    await handle_webhook_event(event)
    return {"received": True}


@app.post("/api/webhooks/paypal")
async def paypal_webhook(request: Request):
    """PayPal webhook: subscription lifecycle events."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    await paypal_handle_webhook(body)
    return {"received": True}


# ── Document API (Admin only) ──────────────────────────────────

@app.post("/api/documents/upload")
async def upload_document(
    file: UploadFile = File(...),
    title: str = Form(None),
    law_number: str = Form(None),
    law_date: str = Form(None),
    user: dict = Depends(require_admin),
):
    """Upload a legal document for processing (admin only)."""
    # Validate file type
    allowed_types = {"pdf", "docx", "doc", "txt"}
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in allowed_types:
        raise HTTPException(
            status_code=400,
            detail=f"Lloji i skedarit '.{ext}' nuk mbështetet. Të lejuara: {', '.join(allowed_types)}"
        )

    # Save file
    unique_name = f"{uuid.uuid4().hex}_{file.filename}"
    file_path = settings.UPLOAD_DIR / unique_name
    content = await file.read()

    with open(file_path, "wb") as f:
        f.write(content)

    # Create DB record
    doc_id = await create_document(
        filename=unique_name,
        original_filename=file.filename,
        file_type=ext,
        file_size=len(content),
        title=title,
        law_number=law_number,
        law_date=law_date,
    )

    # Process in background
    asyncio.create_task(_process_in_background(doc_id, str(file_path), ext))

    return JSONResponse({
        "id": doc_id,
        "filename": file.filename,
        "status": "uploaded",
        "message": "Document uploaded and processing started.",
    })


async def _process_in_background(doc_id: int, file_path: str, file_type: str):
    """Background task for document processing."""
    try:
        await process_document(doc_id, file_path, file_type)
    except Exception as e:
        print(f"[ERROR] Processing document {doc_id}: {e}")


@app.get("/api/documents")
async def list_documents(user: dict = Depends(require_admin)):
    """List all uploaded documents with status (admin only)."""
    docs = await get_all_documents()
    return {"documents": docs}


@app.get("/api/documents/{doc_id}")
async def get_document_detail(doc_id: int, user: dict = Depends(require_admin)):
    """Get details of a specific document (admin only)."""
    doc = await get_document(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Dokumenti nuk u gjet.")
    return doc


@app.delete("/api/documents/{doc_id}")
async def remove_document(doc_id: int, user: dict = Depends(require_admin)):
    """Delete a document and its chunks from the vector store (admin only)."""
    doc = await get_document(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Dokumenti nuk u gjet.")

    # Delete from vector store
    await delete_document_chunks(doc_id)

    # Delete file
    file_path = settings.UPLOAD_DIR / doc["filename"]
    if file_path.exists():
        os.remove(file_path)

    # Delete from DB
    await delete_document(doc_id)

    return {"message": "Document deleted successfully."}


# ── Chat API ──────────────────────────────────────────────────

class ChatRequest(BaseModel):
    question: str
    session_id: str = None


@app.post("/api/chat")
async def chat(request: ChatRequest, user: dict = Depends(require_subscription)):
    """Ask a question - RAG pipeline (requires active subscription)."""
    if not request.question.strip():
        raise HTTPException(status_code=400, detail="Pyetja nuk mund të jetë bosh.")

    session_id = request.session_id or uuid.uuid4().hex

    # Get chat history for context
    history = await get_chat_history(session_id)
    history_for_llm = [
        {"role": m["role"], "content": m["content"]}
        for m in history
    ]

    # Save user message
    await save_chat_message(session_id, "user", request.question)

    # Generate answer via RAG
    result = await generate_answer(request.question, history_for_llm)

    # Save assistant response
    await save_chat_message(
        session_id, "assistant", result["answer"], result["sources"]
    )

    return {
        "answer": result["answer"],
        "sources": result["sources"],
        "session_id": session_id,
        "context_found": result["context_found"],
    }


@app.get("/api/chat/history/{session_id}")
async def get_chat_history_endpoint(session_id: str, user: dict = Depends(require_subscription)):
    """Get chat history for a session (requires subscription)."""
    messages = await get_chat_history(session_id)
    for msg in messages:
        if isinstance(msg.get("sources_json"), str):
            msg["sources"] = json.loads(msg["sources_json"])
        else:
            msg["sources"] = msg.get("sources_json", [])
    return {"messages": messages}


# ── Health Check ──────────────────────────────────────────────

@app.get("/api/health")
async def health_check():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "documents_indexed": len((await get_all_documents())),
    }


# ── Static Files (must be LAST to avoid shadowing API routes) ──

app.mount("/static", StaticFiles(directory=str(frontend_dir)), name="static")
