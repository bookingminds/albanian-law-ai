# Deploy Albanian Law AI

## Recommended: Render (simplest, free tier available)

**Cost:** Free (750 hours/month) or $7/month for always-on | **Time:** 5 minutes

### Step 1: Push to GitHub

```bash
git init
git add .
git commit -m "Initial commit"
git remote add origin https://github.com/YOUR_USER/albanian-law-ai.git
git push -u origin main
```

### Step 2: Create Render Web Service

1. Go to [render.com](https://render.com) and sign in with GitHub.
2. Click **"New +"** > **"Web Service"** > connect your GitHub repo.
3. Fill in settings:

| Setting | Value |
|---------|-------|
| **Name** | `albanian-law-ai` |
| **Region** | Frankfurt (EU) or your preferred |
| **Runtime** | Python |
| **Build Command** | `pip install -r requirements.txt` |
| **Start Command** | `uvicorn backend.main:app --host 0.0.0.0 --port $PORT` |
| **Instance Type** | Free (or Starter $7/month for always-on) |
| **Health Check Path** | `/health` |

### Step 3: Add a Disk (persistent storage)

Go to your service > **Disks** > **Add Disk**:

| Setting | Value |
|---------|-------|
| **Name** | `app-data` |
| **Mount Path** | `/opt/render/project/src/data` |
| **Size** | 1 GB |

Also add another disk for uploads:

| Setting | Value |
|---------|-------|
| **Name** | `uploads` |
| **Mount Path** | `/opt/render/project/src/uploads` |
| **Size** | 1 GB |

### Step 4: Set Environment Variables

Go to your service > **Environment** > **Add Environment Variable**.

Add these (REQUIRED):

| Key | Value |
|-----|-------|
| `OPENAI_API_KEY` | `sk-proj-...` (your OpenAI key) |
| `JWT_SECRET` | Generate: `python -c "import secrets; print(secrets.token_hex(64))"` |
| `SUPABASE_URL` | `https://xxxxx.supabase.co` |
| `SUPABASE_ANON_KEY` | `eyJhbG...` (from Supabase Dashboard) |
| `SUPABASE_SERVICE_ROLE_KEY` | `eyJhbG...` (from Supabase Dashboard) |
| `SERVER_URL` | `https://albanian-law-ai.onrender.com` (your Render URL) |
| `FRONTEND_URL` | `https://albanian-law-ai.onrender.com` (same as above) |
| `PYTHON_VERSION` | `3.12.8` |

Optional (have safe defaults):

| Key | Default | Description |
|-----|---------|-------------|
| `LLM_MODEL` | `gpt-4o-mini` | OpenAI model |
| `TRIAL_DAYS` | `1` | Free trial length |
| `SUBSCRIPTION_PRICE_EUR` | `4.99` | Subscription price |
| `ADMIN_EMAIL` | (empty) | Force a specific email as admin |

### Step 5: Deploy

Click **"Create Web Service"**. Render will:
1. Clone your repo
2. Run `pip install -r requirements.txt`
3. Run `uvicorn backend.main:app --host 0.0.0.0 --port $PORT`
4. Give you a URL like `https://albanian-law-ai.onrender.com`

### Step 6: Verify

Open `https://albanian-law-ai.onrender.com/health` in your browser. You should see:
```json
{"status": "healthy", "documents_total": 0, "documents_ready": 0, ...}
```

---

## Alternative: Railway

**Cost:** ~$5/month (usage-based) | **Time:** 5 minutes

### Step 1: Push to GitHub (same as above)

### Step 2: Create Railway Project

1. Go to [railway.app](https://railway.app) and sign in with GitHub.
2. Click **"New Project"** > **"Deploy from GitHub repo"** > select your repo.
3. Railway auto-detects the `Procfile` or `Dockerfile`.

### Step 3: Configure

Click on the deployed service, then:

1. **Settings** > **Networking** > **Generate Domain** (gives you `xxx.up.railway.app`)
2. **Variables** > **Raw Editor** > paste all env vars:

```
OPENAI_API_KEY=sk-proj-...
JWT_SECRET=your-generated-64-char-hex
SUPABASE_URL=https://xxxxx.supabase.co
SUPABASE_ANON_KEY=eyJhbG...
SUPABASE_SERVICE_ROLE_KEY=eyJhbG...
SERVER_URL=https://xxx.up.railway.app
FRONTEND_URL=https://xxx.up.railway.app
```

3. **Settings** > **Deploy** > **Custom Start Command**:
```
uvicorn backend.main:app --host 0.0.0.0 --port $PORT
```

4. **Settings** > **Health Check Path**: `/health`

### Step 4: Add Persistent Volume

Railway > your service > **Volumes** > **Add Volume**:
- Mount path: `/app/data`
- Size: 1 GB

### Step 5: Deploy

Railway deploys automatically on push. Check the deployment logs for success.

---

## After Deployment Checklist

- [ ] Backend is live and `/health` returns 200
- [ ] Register your admin account at `/login` (first user = admin)
- [ ] Upload legal documents at `/admin`
- [ ] Test the chat at `/`
- [ ] Update `SERVER_URL` in `mobile/App.js` to your production URL
- [ ] Build release APK: `cd mobile && npx eas-cli build --platform android --profile preview`
- [ ] Test the mobile app connects to the production backend
- [ ] Create `law_ai_monthly` subscription in Google Play Console (4.99 EUR/month, 1-day free trial)

---

## Commands Reference

| Action | Command |
|--------|---------|
| Install deps | `pip install -r requirements.txt` |
| Run locally | `python run.py` |
| Run production | `uvicorn backend.main:app --host 0.0.0.0 --port $PORT` |
| Build Docker | `docker build -t albanian-law-ai .` |
| Run Docker | `docker run -d -p 8000:8000 --env-file .env -v data:/app/data -v uploads:/app/uploads albanian-law-ai` |
| Health check | `curl https://your-domain.com/health` |
