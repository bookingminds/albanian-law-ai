# Albanian Law AI

A RAG-based (Retrieval-Augmented Generation) legal document Q&A system for Albanian law. Upload legal documents (laws, decisions, regulations) and ask questions in natural language. The AI answers **only** from your uploaded documents with precise citations.

## Features

- **3-day free trial** — New users get full access for 3 days, then €9.99/month (Stripe or PayPal)
- **Anti-abuse** — One trial per account; disposable-email blocklist; signup rate limit per IP
- **Single subscription plan** — €9.99/month for all users (Stripe or PayPal)
- **Auth** — Register and log in; first user is admin (or set `ADMIN_EMAIL`)
- **Admin Panel** — Upload PDF/DOCX/TXT legal documents, track processing status (admin only)
- **Chat Interface** — Ask legal questions in Albanian or English, get answers with citations (subscribers only)
- **RAG Pipeline** — Upload → Parse → Chunk → Embed → Retrieve → Generate
- **Smart Chunking** — Splits by Albanian legal articles (Neni) when detected
- **No Hallucination** — If insufficient context is found, the system says so
- **Source Citations** — Every answer shows: document title, law number, date, article, page

## Architecture

```
┌─────────────┐     ┌──────────────┐     ┌───────────────┐
│  Frontend    │────▶│  FastAPI      │────▶│  OpenAI API   │
│  (HTML/JS)  │◀────│  Backend      │◀────│  (GPT + Emb.) │
└─────────────┘     └──────┬───────┘     └───────────────┘
                           │
                    ┌──────┴───────┐
                    │              │
              ┌─────▼─────┐ ┌─────▼─────┐
              │  SQLite    │ │  ChromaDB  │
              │  (metadata)│ │  (vectors) │
              └───────────┘ └───────────┘
```

| Component         | Technology              | Purpose                     |
|-------------------|-------------------------|-----------------------------|
| Backend           | Python + FastAPI        | API server                  |
| Metadata DB       | SQLite                  | Document records, chat logs |
| Vector Store      | ChromaDB (embedded)     | Embeddings & similarity search |
| Document Parsing  | PyMuPDF, python-docx    | PDF/DOCX/TXT text extraction |
| Embeddings        | OpenAI text-embedding-3-small | Document & query vectors |
| LLM               | OpenAI GPT-4o-mini      | Answer generation           |
| Frontend          | Vanilla HTML/CSS/JS     | Admin panel + Chat UI       |

## Setup

### Prerequisites

- Python 3.10+
- An OpenAI API key (get one at https://platform.openai.com/api-keys)

### Installation

1. **Clone / navigate to the project folder:**
   ```bash
   cd "AI LIGJE"
   ```

2. **Create a virtual environment:**
   ```bash
   python -m venv venv

   # Windows
   venv\Scripts\activate

   # macOS/Linux
   source venv/bin/activate
   ```

3. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

4. **Configure environment variables:**
   ```bash
   copy .env.example .env
   ```
   Edit `.env` and set at least:
   - `OPENAI_API_KEY` — your OpenAI API key
   - `JWT_SECRET` — a long random string (e.g. `openssl rand -hex 32`)
   - For subscriptions: `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_PRICE_ID` (see [Stripe setup](#stripe-subscription-setup) below)

5. **Run the application:**
   ```bash
   python run.py
   ```

6. **Open in browser:**
   - Home / Chat: http://localhost:8000
   - Login / Register: http://localhost:8000/login
   - Admin Panel: http://localhost:8000/admin

## Usage

### 1. Register and subscribe

1. Go to http://localhost:8000/login and **Register** with email and password (min 8 characters).
2. The first user registered is an **admin** (or set `ADMIN_EMAIL` in `.env` to assign admin to that email).
3. New accounts get a **3-day free trial** — you can use the chat immediately. When the trial ends (or if you already used a trial), subscribe via **Stripe** or **PayPal** (€9.99/month).
4. Complete payment when ready; you’ll be redirected back and the chat stays unlocked.

### 2. Upload documents (Admin only)

1. Go to http://localhost:8000/admin (must be logged in as admin).
2. Drag & drop or click to select a PDF, DOCX, or TXT file.
3. Optionally set Title, Law Number, and Date.
4. Click **Upload & Process** and wait until status is **processed**.

### 3. Ask questions (Subscribers)

1. Go to http://localhost:8000 (logged in with an active subscription).
2. Ask in Albanian or English, e.g.:
   - "Cilat janë të drejtat e punëmarrësit sipas Kodit të Punës?"
   - "What does Article 5 of Law 7895 say about property rights?"
3. Answers are based only on uploaded documents, with source citations.

## Stripe subscription setup

1. Create an account at [Stripe](https://dashboard.stripe.com).
2. In **Products**, create a product (e.g. "Albanian Law AI") and add a **recurring** price: **€9.99/month**.
3. Copy the **Price ID** (starts with `price_`) into `.env` as `STRIPE_PRICE_ID`.
4. In **Developers → API keys**, copy the **Secret key** into `.env` as `STRIPE_SECRET_KEY`.
5. For **webhooks** (so subscription status stays in sync):
   - **Developers → Webhooks → Add endpoint**: URL `https://your-domain.com/api/webhooks/stripe`.
   - Events: `checkout.session.completed`, `customer.subscription.updated`, `customer.subscription.deleted`.
   - Copy the **Signing secret** into `.env` as `STRIPE_WEBHOOK_SECRET`.
6. For local testing use the Stripe CLI: `stripe listen --forward-to localhost:8000/api/webhooks/stripe` and put the printed secret in `STRIPE_WEBHOOK_SECRET`.

## PayPal subscription setup

1. Create an account at [PayPal Developer](https://developer.paypal.com).
2. In **Dashboard → My Apps & Credentials**, create an app and copy **Client ID** and **Secret** into `.env` as `PAYPAL_CLIENT_ID` and `PAYPAL_CLIENT_SECRET`.
3. Set `PAYPAL_MODE=sandbox` for testing or `live` for production.
4. Create a **Product** and a **Plan** (recurring €9.99/month) under **Products and plans** in the Dashboard (or via [Subscriptions API](https://developer.paypal.com/docs/api/subscriptions/v1/)). Copy the **Plan ID** (starts with `P-`) into `.env` as `PAYPAL_PLAN_ID`.
5. Ensure `FRONTEND_URL` is the full base URL of your app (e.g. `http://localhost:8000`). After the user approves on PayPal they are redirected to `{FRONTEND_URL}/api/subscription/paypal/confirm?token=...`, which syncs the subscription and redirects to `/?subscription=success`.
6. Optional: add a webhook in the PayPal Dashboard for `BILLING.SUBSCRIPTION.*` events pointing to `https://your-domain.com/api/webhooks/paypal` so subscription updates (cancel, etc.) stay in sync.

## Environment Variables

| Variable               | Description |
|------------------------|-------------|
| `OPENAI_API_KEY`       | OpenAI API key (required for RAG) |
| `JWT_SECRET`           | Secret for JWT signing (use a long random string) |
| `ADMIN_EMAIL`          | Optional; this user gets admin (else first user is admin) |
| `STRIPE_SECRET_KEY`    | Stripe secret key (for subscriptions) |
| `STRIPE_WEBHOOK_SECRET`| Stripe webhook signing secret |
| `STRIPE_PRICE_ID`      | Stripe Price ID for €9.99/month |
| `FRONTEND_URL`         | Base URL for redirects (e.g. `http://localhost:8000`) |
| `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET` | PayPal API credentials (optional) |
| `PAYPAL_MODE`          | `sandbox` or `live` |
| `PAYPAL_PLAN_ID`       | PayPal Plan ID for €9.99/month (optional) |
| `TRIAL_DAYS`           | Free trial length in days (default 3) |
| `MAX_SIGNUPS_PER_IP_24H` | Max signups per IP in 24h (default 2) |
| `BLOCK_DISPOSABLE_EMAILS` | Block known disposable email domains (default true) |
| `LLM_MODEL`, `EMBEDDING_MODEL`, `CHUNK_SIZE`, `CHUNK_OVERLAP`, `TOP_K_RESULTS` | Optional RAG/config |

## API Endpoints

| Method   | Endpoint                          | Description                        |
|----------|-----------------------------------|------------------------------------|
| `GET`    | `/`                               | Chat interface                     |
| `GET`    | `/login`                          | Login / register page              |
| `GET`    | `/admin`                          | Admin panel (admin only)           |
| `POST`   | `/api/auth/register`              | Register                           |
| `POST`   | `/api/auth/login`                 | Login                              |
| `GET`    | `/api/auth/me`                    | Current user + subscription (auth) |
| `POST`   | `/api/subscription/checkout`     | Create Stripe checkout (auth)       |
| `POST`   | `/api/subscription/checkout-paypal` | Create PayPal subscription (auth) |
| `GET`    | `/api/subscription/paypal/confirm`   | PayPal return URL (syncs sub, redirects) |
| `GET`    | `/api/subscription/status`       | Subscription status (auth)          |
| `POST`   | `/api/webhooks/stripe`            | Stripe webhook                     |
| `POST`   | `/api/webhooks/paypal`            | PayPal webhook                     |
| `POST`   | `/api/documents/upload`           | Upload document (admin)            |
| `GET`    | `/api/documents`                  | List documents (admin)             |
| `DELETE` | `/api/documents/{id}`             | Delete document (admin)            |
| `POST`   | `/api/chat`                       | Send message (subscriber)          |
| `GET`    | `/api/chat/history/{session_id}`  | Chat history (subscriber)           |
| `GET`    | `/api/health`                     | Health check                       |

## Cost Estimate

- **Embeddings**: ~$0.02 per 1M tokens (~$0.001 per 100-page document)
- **Chat (GPT-4o-mini)**: ~$0.15 per 1M input tokens, ~$0.60 per 1M output tokens
- **Typical question**: ~$0.001-0.003 per question

## License

Private project. All rights reserved.
