# Deployment Guide – Albanian Law AI

## Option 1: Railway (Easiest – recommended)

**Cost:** ~$5/month | **Time:** 5 minutes

1. Push your code to GitHub:
   ```bash
   git init
   git add .
   git commit -m "Initial commit"
   git remote add origin https://github.com/YOUR_USER/albanian-law-ai.git
   git push -u origin main
   ```

2. Go to [railway.app](https://railway.app) and sign in with GitHub.

3. Click **"New Project"** → **"Deploy from GitHub repo"** → select your repo.

4. Railway auto-detects the `Dockerfile`. Set environment variables:
   - Click the service → **Variables** → **Raw Editor**, paste your `.env.production` values.
   - Set `FRONTEND_URL` to your Railway URL (e.g. `https://albanian-law-ai-production.up.railway.app`).

5. Click **Deploy**. Your app will be live in ~2 minutes.

6. **Custom domain** (optional): Settings → Networking → Custom Domain → add your domain.

---

## Option 2: Render (Free tier available)

**Cost:** Free (with limits) or $7/month | **Time:** 5 minutes

1. Push code to GitHub (same as above).

2. Go to [render.com](https://render.com) and sign in.

3. Click **"New +"** → **"Web Service"** → connect your repo.

4. Settings:
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `uvicorn backend.main:app --host 0.0.0.0 --port $PORT`
   - **Environment:** Add all variables from `.env.production`.

5. Add a **Disk** (Settings → Disks): mount path `/app/data`, size 1 GB.

6. Click **Deploy**.

---

## Option 3: VPS (DigitalOcean / Hetzner)

**Cost:** €4-6/month | **Time:** 15 minutes

1. Create an Ubuntu 22.04 server (1 GB RAM minimum).

2. SSH in and run:
   ```bash
   # Install Docker
   curl -fsSL https://get.docker.com | sh

   # Clone your repo
   git clone https://github.com/YOUR_USER/albanian-law-ai.git
   cd albanian-law-ai

   # Create .env
   cp .env.production .env
   nano .env  # fill in your values

   # Run with Docker Compose
   docker compose up -d

   # Check logs
   docker compose logs -f
   ```

3. Set up a reverse proxy (Caddy is easiest):
   ```bash
   apt install -y caddy
   ```
   Edit `/etc/caddy/Caddyfile`:
   ```
   your-domain.com {
       reverse_proxy localhost:8000
   }
   ```
   ```bash
   systemctl restart caddy
   ```
   Caddy auto-provisions HTTPS via Let's Encrypt.

---

## Option 4: Docker (any server)

```bash
# Build
docker build -t albanian-law-ai .

# Run
docker run -d \
  --name albanian-law-ai \
  -p 8000:8000 \
  --env-file .env \
  -v albanian_law_data:/app/data \
  -v albanian_law_uploads:/app/uploads \
  --restart unless-stopped \
  albanian-law-ai
```

---

## After deployment checklist

- [ ] Set `FRONTEND_URL` to your actual domain (e.g. `https://ligje.al`)
- [ ] Set a strong random `JWT_SECRET`
- [ ] Add your `OPENAI_API_KEY`
- [ ] Set up Stripe: create product → price → webhook → add keys to env
- [ ] Register your admin account (first user = admin, or set `ADMIN_EMAIL`)
- [ ] Upload legal documents via `/admin`
- [ ] Test the chat
- [ ] Set up Stripe webhook URL: `https://your-domain.com/api/webhooks/stripe`

## Stripe webhook events to subscribe to

- `checkout.session.completed`
- `customer.subscription.updated`
- `customer.subscription.deleted`
