# TrueGraph repo — TWO services, mid-split (read this before deploying)

> **State as of 2026-07-05.** This repo historically contained two unrelated
> services tangled together. The split into subfolders is staged; the repo
> ROOT is legacy and will be removed once Railway is repointed.

| Folder | Service | Start command | Port |
|---|---|---|---|
| `content/` | **TrueGraph** — knowledge-graph / atom-selection compute (`api:app`) | `python -m uvicorn api:app` | 8300 |
| `chimera/` | **Chimera Secured** — MS Graph mailbox monitor, BEC detection (`src.app:app`) | `python -m uvicorn src.app:app` | (Railway `$PORT`) |
| root | ⚠️ LEGACY tangle of both — do not develop here | — | — |

**Which one is live today?** The root `railway.json` (builder=DOCKERFILE,
`src.app:app`) wins over `railway.toml`, so the existing Railway service
deploys **Chimera**, despite this repo being named TrueGraph.

## Migration checklist (Railway dashboard — one-time)

1. Existing Railway service (currently deploying Chimera from root):
   **Settings → Root Directory → `/chimera`**. It keeps its env vars
   (Azure AD, CPA_BASE_URL, DATABASE_URL, MODE…). Rename the service
   to `chimera-secured` for sanity.
2. New Railway service for TrueGraph: same repo,
   **Root Directory → `/content`**, env vars from `content/.env.example`
   (set `TRUEGRAPH_API_KEY` — auth is enforced only when it is set).
3. Point TDE's `TRUEGRAPH_API_URL` at the new TrueGraph service URL.
4. After both deploys are green: delete the legacy root files
   (`api.py`, `src/`, `static/`, `Dockerfile`, `railway.json`,
   `railway.toml`, `nixpacks.toml`, root `requirements.txt`,
   root `.env.example`) and, longer term, move `chimera/` to its own repo.

## Canonical references

- Architecture + decisions: "WinTech Platform Master Plan"
  (Desktop\Silk-and-Crown-Samples) — TrueGraph is the JUDGMENT pillar
  (atom set selection), Chimera is a CPP-E consumer.
- Canonical port map: 8300 TrueGraph · 8400 TDE · 8500 Orchestrator ·
  8600 TrueArtifact · 8700 CPA engine.
