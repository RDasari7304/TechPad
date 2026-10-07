# Deploying TechPad to production

The app is one process (web + API + X bot), SQLite + an uploads folder on disk, and headless Chromium.
The Dockerfile handles Chromium's system dependencies, so **deploy as a Docker service** anywhere that
runs containers and gives you a persistent disk. Render is the quickest; a VPS is the cheapest.

## 0. Before you deploy (5 min)

1. Buy the domain (Namecheap, Cloudflare, Porkbun — any registrar).
2. `git init && git add . && git commit -m "TechPad"` and push to a **private** GitHub repo.
   `.gitignore` already excludes `.env`, `data/` and `.venv/`.
3. Decide your public URL, e.g. `https://techpad.xyz`. It gets written into every launched token's
   metadata as the website, so pick it once.
4. Rotate your Anthropic key if it has ever been pasted anywhere (console.anthropic.com → API keys).

## Option A — Render (recommended, ~15 min)

1. Render dashboard → **New → Blueprint** → pick your repo. It reads `render.yaml` and creates the
   service + 5 GB disk. (Or **New → Web Service → Docker** and fill the same env vars by hand.)
2. In the service's **Environment** tab set the secrets marked `sync: false`:
   `ANTHROPIC_API_KEY` (required), `GITHUB_TOKEN` (recommended), the five `X_*` keys (only if you run
   the bot). Set `LARPCHECK_PUBLIC_URL` to your real domain **with https://**.
3. Plan: **Standard (2 GB)** or higher. Chromium + 4 workers will not fit in 512 MB.
4. Deploy. First build takes ~5 min (pulls the Playwright image). Check `https://<service>.onrender.com/api/status`
   returns `{"ok": true, ...}`.
5. **Custom domain**: service → Settings → Custom Domains → add `techpad.xyz` and `www.techpad.xyz`.
   Render shows the DNS records: at your registrar add
   - `A` record `@` → the IP Render shows (or `ALIAS/ANAME @ → <service>.onrender.com` if your DNS supports it)
   - `CNAME` `www` → `<service>.onrender.com`
   TLS certificates are issued automatically once DNS propagates (5–60 min).
6. Re-check `/api/status` on the domain, run one test from the Test tab, and open `/coin/<anything>`
   to confirm the 404 page renders (proves routing works).

## Option B — Any VPS with Docker (Hetzner / DigitalOcean / Fly.io machines), ~30 min

```bash
# on the server (Ubuntu 22.04+, 4 GB RAM recommended)
sudo apt-get update && sudo apt-get install -y docker.io docker-compose-v2 caddy
git clone <your repo> techpad && cd techpad
cp .env.example .env && nano .env        # fill ANTHROPIC_API_KEY, LARPCHECK_PUBLIC_URL=https://techpad.xyz, X keys
docker build -t techpad .
docker run -d --name techpad --restart unless-stopped --env-file .env \
  -v /srv/techpad-data:/app/data -p 127.0.0.1:8000:8000 techpad
```
Reverse proxy + automatic HTTPS with Caddy (`/etc/caddy/Caddyfile`):
```
techpad.xyz, www.techpad.xyz {
    reverse_proxy 127.0.0.1:8000
    encode gzip
    request_body { max_size 60MB }      # video uploads
}
```
`sudo systemctl reload caddy`. DNS: `A @ → server IP`, `A www → server IP`. Caddy fetches the cert itself.

Updates: `git pull && docker build -t techpad . && docker rm -f techpad && docker run ... (same command)`.

## Option C — Railway / Fly.io

Both accept the Dockerfile as-is. Add a volume mounted at `/app/data`, set the same env vars, attach the
domain in their dashboard and add the CNAME they give you. Fly needs `fly volumes create techpad_data` and
`[mounts] source="techpad_data" destination="/app/data"` in `fly.toml`.

## After it's live

- **X bot**: in developer.x.com give the app Read+Write with user authentication, regenerate the access
  token/secret *after* changing permissions, put the five keys in the env, redeploy. Reply to any tweet
  with `@techpad check this` to test. Replies link to `LARPCHECK_PUBLIC_URL/#t/<id>`.
- **First launch**: do a throwaway launch yourself first. The pump.fun IPFS upload and PumpPortal create
  are third-party endpoints; if either errors, the Deploy step shows the message and nothing is spent.
- **Backups**: everything is in `/app/data` (SQLite + uploads). On Render, enable disk snapshots; on a VPS,
  `tar czf backup.tgz /srv/techpad-data` on a cron.
- **Cost control**: `LARPCHECK_BUDGET_USD` (deep tests), `LARPCHECK_QUICK_BUDGET_USD` (launch audits),
  `LARPCHECK_CACHE_HOURS` (reuse verdicts), `LARPCHECK_WORKERS` (parallel browsers — each ~300 MB RAM).
- **Abuse**: the public Test endpoint has no auth or rate limit. If it gets hammered, put Cloudflare in
  front (free plan, rate-limit rule on `/api/tests`) — that's a 10-minute change and needs no code.

## Checklist

- [ ] `LARPCHECK_PUBLIC_URL` = `https://your-domain` (no trailing slash)
- [ ] `ANTHROPIC_API_KEY` set, old key revoked
- [ ] Persistent disk mounted at `/app/data`
- [ ] Plan has ≥ 2 GB RAM
- [ ] DNS A/CNAME records added, HTTPS green
- [ ] `/api/status` OK on the domain
- [ ] One test run, one throwaway launch done
