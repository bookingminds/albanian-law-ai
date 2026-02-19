# Albanian Law AI

A RAG-based (Retrieval-Augmented Generation) legal document Q&A system for Albanian law. Upload legal documents (laws, decisions, regulations) and ask questions in natural language. The AI answers **only** from your uploaded documents with precise citations.

## Features

- **1-day free trial** — New users get full access for 24 hours, then 4.99 EUR/month (Google Play Billing)
- **Anti-abuse** — One trial per account; disposable-email blocklist; signup rate limit per IP
- **Google Play subscription** — 4.99 EUR/month via Google Play Billing (subscription ID: `law_ai_monthly`)
- **Supabase Auth** — Email/password authentication with auto-confirmation (local JWT fallback)
- **Password reset** — Forgot-password flow via Supabase Auth
- **Admin Panel** — Upload PDF/DOCX/TXT legal documents, track processing status (admin only)
- **Chat Interface** — Ask legal questions in Albanian, get answers with citations (subscribers only)
- **Advanced RAG Pipeline** — Multi-query expansion, hybrid search (vector + keyword), re-ranking, context stitching, coverage self-check
- **Smart Chunking** — Splits by Albanian legal articles (Neni) when detected
- **No Hallucination** — Confidence gate + strict evidence-only answering
- **Source Citations** — Every answer shows: document title, law number, date, article, page
- **Mobile App** — Expo/React Native wrapper with native Google Play Billing
- **Rate Limiting** — Login (5/min), registration (5/min), chat (30/min)
- **CORS** — Configured for production origins

## Architecture

```
+---------------+     +----------------+     +-----------------+
|  Frontend     |---->|  FastAPI       |---->|  OpenAI API     |
|  (HTML/JS)    |<----|  Backend       |<----|  (GPT + Emb.)   |
+---------------+     +--------+-------+     +-----------------+
                               |
+---------------+     +--------+-------+
|  Mobile App   |     |                |
|  (Expo/RN)    |     v                v
+---------------+  +--------+  +----------+
|  WebView      |  | SQLite |  | ChromaDB |
+---------------+  +--------+  +----------+
                               |
                        +------+------+
                        | Supabase    |
                        | (Auth)      |
                        +-------------+
```

| Component         | Technology              | Purpose                     |
|-------------------|-------------------------|-----------------------------|
| Backend           | Python + FastAPI        | API server                  |
| Metadata DB       | SQLite                  | Document records, chat logs |
| Vector Store      | ChromaDB (embedded)     | Embeddings & similarity search |
| Document Parsing  | PyMuPDF, python-docx    | PDF/DOCX/TXT text extraction |
| Embeddings        | OpenAI text-embedding-3-small | Document & query vectors |
| LLM               | OpenAI GPT-4o-mini      | Answer generation           |
| Auth              | Supabase Auth + JWT     | User authentication         |
| Payments          | Google Play Billing     | Subscriptions (expo-iap)    |
| Frontend          | Vanilla HTML/CSS/JS     | Admin panel + Chat UI       |
| Mobile            | Expo + React Native     | Android app (WebView)       |

## Setup

### Prerequisites

- Python 3.10+
- An OpenAI API key (get one at https://platform.openai.com/api-keys)
- A Supabase project (get one at https://supabase.com)

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
   - `JWT_SECRET` — generate with `python -c "import secrets; print(secrets.token_hex(64))"`
   - `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY` — from Supabase Dashboard > Settings > API

5. **Run the application:**
   ```bash
   python run.py
   ```

6. **Open in browser:**
   - Home / Chat: http://localhost:8000
   - Login / Register: http://localhost:8000/login
   - Admin Panel: http://localhost:8000/admin

## Google Play Billing Setup

1. In Google Play Console, create a subscription product with ID `law_ai_monthly` (4.99 EUR/month, 1-day free trial).
2. For server-side verification (recommended), create a Google Cloud Service Account with Android Publisher API access and set `GOOGLE_PLAY_SERVICE_ACCOUNT_JSON` env var to the path of the credentials JSON file.
3. The mobile app uses `expo-iap` to handle purchase flow natively.

## Environment Variables

| Variable               | Description |
|------------------------|-------------|
| `OPENAI_API_KEY`       | OpenAI API key (required) |
| `JWT_SECRET`           | Secret for JWT signing (use a long random hex string) |
| `SUPABASE_URL`         | Supabase project URL |
| `SUPABASE_ANON_KEY`    | Supabase anon/public key |
| `SUPABASE_SERVICE_ROLE_KEY` | Supabase service role key (keep secret!) |
| `ADMIN_EMAIL`          | Optional; this user gets admin (else first user is admin) |
| `SERVER_URL`           | Production backend URL |
| `FRONTEND_URL`         | Base URL for redirects |
| `GOOGLE_PLAY_PACKAGE_NAME` | Android package name (default: `com.zagrid.albanianlawai`) |
| `GOOGLE_PLAY_PRODUCT_ID` | Google Play subscription ID (default: `law_ai_monthly`) |
| `SUBSCRIPTION_PRICE_EUR` | Subscription price (default: `4.99`) |
| `TRIAL_DAYS`           | Free trial length in days (default: `1`) |
| `MAX_SIGNUPS_PER_IP_24H` | Max signups per IP in 24h (default: `5`) |
| `BLOCK_DISPOSABLE_EMAILS` | Block disposable email domains (default: `true`) |
| `RATE_LIMIT_LOGIN`     | Login rate limit (default: `5/minute`) |
| `RATE_LIMIT_CHAT`      | Chat rate limit (default: `30/minute`) |

## API Endpoints

| Method   | Endpoint                          | Description                        |
|----------|-----------------------------------|------------------------------------|
| `GET`    | `/`                               | Chat interface                     |
| `GET`    | `/login`                          | Login / register page              |
| `GET`    | `/admin`                          | Admin panel (admin only)           |
| `GET`    | `/documents`                      | Document library                   |
| `POST`   | `/api/auth/register`             | Register (rate limited: 5/min)     |
| `POST`   | `/api/auth/login`                | Login (rate limited: 5/min)        |
| `POST`   | `/api/auth/logout`               | Server-side logout                 |
| `POST`   | `/api/auth/forgot-password`      | Send password reset email          |
| `GET`    | `/api/auth/me`                   | Current user + subscription info   |
| `POST`   | `/api/subscription/verify-google-play` | Verify Google Play purchase  |
| `POST`   | `/api/subscription/restore`      | Restore subscription               |
| `GET`    | `/api/subscription/status`       | Subscription status                |
| `POST`   | `/api/user/documents/upload`     | Upload document (admin only)       |
| `GET`    | `/api/user/documents`            | List documents                     |
| `DELETE` | `/api/user/documents/{id}`       | Delete document (admin only)       |
| `POST`   | `/api/chat`                      | Send message (rate limited: 30/min)|
| `GET`    | `/api/chat/history/{session_id}` | Chat history                       |
| `POST`   | `/api/suggest-questions`         | Search-assist suggestions          |
| `GET`    | `/api/suggest-topics`            | Topic suggestions                  |
| `GET`    | `/api/health`                    | Health check                       |
| `POST`   | `/api/debug/search`             | Debug search (admin only)          |

## Cost Estimate

- **Embeddings**: ~$0.02 per 1M tokens (~$0.001 per 100-page document)
- **Chat (GPT-4o-mini)**: ~$0.15 per 1M input tokens, ~$0.60 per 1M output tokens
- **Typical question**: ~$0.001-0.003 per question

## License

Private project. All rights reserved.
