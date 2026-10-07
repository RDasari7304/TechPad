# 🧪 TechPad

> Don't fall for larp. Make sure the tech works, then ape.

Production hosting: see **DEPLOY.md** (Dockerfile + render.yaml included). The Python package is still named
`larpcheck` internally (env vars keep the `LARPCHECK_` prefix) so nothing you configured breaks.

An AI agent that reads what a Solana "tech" token *claims* to be, then **actually tries the product** and
tells you whether it works or it's a larp. Three front doors, one engine:

* **CMD line** — `python -m larpcheck test <CA or X link>`
* **Web app** — Test tab with live progress, Archive tab of every past test (searchable by CA / name / handle)
* **X bot** — reply `@techpad check this out, real or larp?` under a project's tweet; it answers with the verdict + report link

Hundreds of simultaneous requests are fine: an unbounded queue feeds N parallel agent workers, identical
CAs are deduped (many people asking about the same coin = one run, everyone gets the result), and
finished verdicts are cached for 24h.

## How a test works

```
request ──► 0. research   token metadata (pump.fun, DexScreener, on-chain mint) + X profile/tweet
                          + website + docs + GitHub — no LLM yet, just fetching
        ──► 1. understand Claude turns the material into a list of concrete, falsifiable CLAIMS
                          (and says which are untestable hype)
        ──► 2. test       Claude gets tools and tries each claim: headless Chromium for dApps,
                          HTTP client for APIs, GitHub inspector, read-only Solana RPC, Python + shell
                          to clone/run code. Every step is recorded.
        ──► 3. verdict    WORKS / PARTIAL / UNVERIFIED / LARP, 0-100 score, evidence, red flags,
                          what would change the verdict, and a tweet-sized reply
```

Money: each test has a hard LLM budget (`LARPCHECK_BUDGET_USD`, default $1.50). The agent stops and
writes a verdict with what it has when the cap is hit. Total spend is tracked in the archive.
The agent **never pays, signs, or submits keys** — it observes wallet flows and tests everything public.

## Setup (Windows CMD / PowerShell, also fine in WSL)

```bat
cd larpcheck
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
copy .env.example .env
notepad .env        :: paste your ANTHROPIC_API_KEY
```

## Try it from the command line first

```bat
:: by contract address
python -m larpcheck test 7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr

:: by X link (tweet or profile), or both — natural language is fine
python -m larpcheck test "check this out and tell me if it works or fake larp https://x.com/someproject/status/123 CA: 7GCi...W2hr"

:: options
python -m larpcheck test <CA> --x https://x.com/someproject --force --json
python -m larpcheck archive            :: everything tested so far
python -m larpcheck archive larp --verdict LARP
python -m larpcheck show <id> --json   :: full report incl. tool transcript
```

Smoke test without an API key or network: `set LARPCHECK_MOCK=1` and run any `test` — the whole pipeline
(queue, research, DB, UI) runs with a fake LLM.

## Web app

```bat
python -m larpcheck serve
```
Open http://localhost:8000. One page: animated landing → scroll → **Test / Live / Archive** tabs.

* **Test** — submit, then watch the **bot cam**: a carousel of real screenshots from the agent's headless
  browser. During research it opens the tweet (via X's embed), the X profile, the pump.fun and DexScreener
  pages, the website, docs and GitHub, scrolling each; during testing every page it fetches or drives is
  captured too. API calls and code probes show as terminal cards. ◀ back / next ▶ step through history,
  ⏺ LIVE jumps to the newest frame (arrow keys work). Frames stream over SSE and are never stored.
* **Live** — every test running right now, with its current phase, viewer count and last camera frame.
  Click a room to spectate it in real time (many viewers share one run).
* **Archive** — every past test, searchable by CA / name / handle; `#t/<id>` links are shareable.

API:

| method | path | |
|---|---|---|
| POST | `/api/tests` `{"text": "...", "force": false}` | queue a test → `{id, status: queued\|attached\|done, cached}` |
| GET | `/api/tests/{id}` | full result |
| GET | `/api/tests/{id}/stream` | SSE live progress |
| GET | `/api/lookup?key=<CA or @handle>` | already tested? |
| GET | `/api/archive?q=&verdict=&limit=&offset=` | archive + stats |
| GET | `/api/status` | queue / workers / spend |
| GET | `/api/live` | rooms: running/queued tests with phase, viewers, last frame |

Concurrency: `LARPCHECK_WORKERS` agent threads (each may hold a Chromium tab). 6–10 on a laptop, more on a
server. The queue itself has no limit; a flood of requests just waits its turn and the UI shows the position.

## Launchpad (audited launches on pump.fun)

The **Launch** tab lets a developer launch a tech token *through* TechPad so it carries an audit:

1. **Details** — name, ticker, description, image, demo video (upload ≤40 MB or YouTube/Loom/Vimeo link),
   GitHub, app URL, docs, X, Telegram, onboarding notes (markdown).
2. **AI audit** — a *quick audit*: one tool loop, at most `LARPCHECK_QUICK_MAX_TOOL_CALLS` (3) calls,
   `LARPCHECK_QUICK_MAX_SECONDS` (30) seconds and `LARPCHECK_QUICK_BUDGET_USD` ($0.35). The agent picks the
   single most decisive thing (open the app / inspect the repo / hit the API), observes it once and reasons to a
   certificate. Progress streams inline in the Launch tab; these audits are private (not in Live). Deploy is gated on
   the score, i.e. the agent's confidence the product is real: score ≥ `LARPCHECK_LAUNCH_MIN_SCORE` (default
   50) and verdict in `LARPCHECK_LAUNCH_VERDICTS` (default anything but LARP). If it fails, the dev sees a
   concrete fix list (what to add, what was broken, which links were dead), can edit the submission with the
   form prefilled, and re-audit for free. The wizard stays on step 2 until it passes.
3. **Deploy** — non-custodial, pump.fun protocol:
   * the browser generates the mint keypair (so the CA is known up front),
   * the server uploads image + metadata to pump.fun's IPFS with **website locked to
     `PUBLIC_URL/coin/<CA>`**, X/Telegram passed through, and "Audited by TechPad" appended to the description,
   * PumpPortal (`trade-local`, action `create`, pool `pump`) returns the unsigned create tx,
   * the dev signs with Phantom (mint key + wallet), the server verifies the mint exists on-chain, then marks it
     launched and re-keys the audit so the Archive finds it by CA.
   Creator rewards go to the deploying wallet — that is pump.fun's own mechanism, nothing to configure.
4. **Coin page** — `/coin/<CA>` (the link pump.fun shows as the website): image, CA, audit certificate
   (verdict, score, cert id, link to the full report), demo video, what-it-does, audit findings, onboarding,
   links, pump.fun button. `/badge/<CA>.svg` is an embeddable badge; `/api/coin/<CA>` is the JSON.

Set `LARPCHECK_PUBLIC_URL` to your real domain before launching anything — it is baked into the token's
metadata permanently. pump.fun's IPFS endpoint and PumpPortal's API are third-party and can change; both are
configurable (`PUMP_IPFS_URL`, `PUMPPORTAL_URL`). Test on a throwaway first.

## X bot

1. Create an app at developer.x.com with **Read and Write** permissions and user authentication.
2. Put bearer token + API key/secret + access token/secret in `.env`, set `X_BOT_HANDLE`.
3. `python -m larpcheck xbot` (or `python -m larpcheck all` to run web + bot in one process).

The bot polls mentions. If someone replies to a project's tweet with `@yourbot check / test / larp / real?`,
it takes **the tweet being replied to** as the project's X link, pulls any CA from either tweet, runs the
test, and replies with the verdict and a link to the full report (`LARPCHECK_PUBLIC_URL/#t/<id>`).

## Tuning for vague vs. detailed projects

* `detail_level` in the understanding step drives behaviour: projects with no product link produce few
  testable claims and come back **UNVERIFIED** with an explicit "what would prove it" list, rather than a
  fake LARP verdict. Projects with an app/API/repo get hands-on testing.
* `LARPCHECK_MAX_TOOL_CALLS` / `LARPCHECK_MAX_TEST_SECONDS` bound how deep the tester goes.
* `LARPCHECK_ALLOW_LOCAL=1` lets the agent open `localhost` URLs (off by default for safety).
* Set `GITHUB_TOKEN` to avoid GitHub's 60 req/h unauthenticated limit when many tests inspect repos.
* Swap `LARPCHECK_MODEL` (and the two price vars so the budget cap stays accurate).

## Deploying

Single process, SQLite in `data/`. Works on Render/Railway/a VPS: `python -m larpcheck all` as the start
command, mount `data/` as a persistent disk, run `playwright install --with-deps chromium` in the build step.

## Layout

```
larpcheck/
  config.py    env settings & budget
  llm.py       Claude Messages API client, tool loop, cost tracking, budget cap
  tools.py     fetch_url, http_request, browser_*, github_inspect, solana_rpc, x_lookup, run_python, run_shell
  research.py  request parsing + pre-LLM dossier
  agent.py     understand → test → verdict
  db.py        SQLite archive
  queue.py     parallel workers, dedupe, cache, live events
  server.py    Starlette API + static frontend
  xbot.py      X mentions poller + OAuth1 reply
  launchpad.py audit → pump.fun metadata → PumpPortal create tx → on-chain confirm → coin page/certificate
  cli.py       command line
frontend/index.html   single page: landing, Test (bot cam), Live, Archive, Launch
frontend/coin.html    public coin page served at /coin/<CA>
```
