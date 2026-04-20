# Upgrade guide — migrating to gemini-3-flash-preview

This upgrade switches the default LLM backend from Polza
(OpenAI-compat proxy) to direct Gemini API and swaps the model from
`gemini-3.1-flash-lite-preview` to `gemini-3-flash-preview`. See
`benchmarking_files/results/PDF_BENCHMARK.md` for the benchmark
numbers that justified the switch.

## One-time prerequisite: add `GEMINI_API_KEY` to GitHub Secrets

The new prod config uses direct Gemini. If you haven't already, add a
Gemini API key as a repo secret so CI can render it into `.env`:

1. Get a free key at <https://aistudio.google.com/apikey>.
2. In your repo: **Settings → Secrets and variables → Actions →
   New repository secret**.
3. Name: `GEMINI_API_KEY`. Value: the key starting with `AIza…`.

The CI `prepare-server` step fails loudly if this secret is missing.

## Deploy

On the next push to `main`, CI will:
1. Run tests (backend + ml + frontend).
2. Build + push fresh Docker images to the registry.
3. Render `.env` from `.env.prod` using the new secret and copy it to
   the server along with `docker-compose.yml`.

Then trigger the per-service deploy workflow manually
(**Actions → Deploy → Run workflow**) once for each of `ml`,
`backend`, `frontend` — in that order. The workflow does
`docker compose pull <service>` + `docker compose up -d --no-deps
<service>` on the server.

## What changed in config

| key | old | new |
|---|---|---|
| `LLM_BACKEND` | `polza` | `gemini` |
| `GEMINI_MODEL` | `gemini-3.1-flash-lite-preview` | `gemini-3-flash-preview` |
| `GEMINI_THINKING_BUDGET` | not set (=2048 default) | `4096` |
| `GEMINI_MAX_OUTPUT_TOKENS` | not set (=16384 default) | `65536` |
| `MAX_MESSAGE_CHARS` / `REQUEST_CHAR_LIMIT` | 12000 | 20000 |

## Rollback

If something breaks after the upgrade, roll back the .env values
without redeploying the image: SSH to the server, edit
`/opt/bpmn-project/.env`, flip `LLM_BACKEND=polza` (and set
`POLZA_MODEL=google/gemini-3-flash-preview` or back to
`…flash-lite-preview` if you want the old behavior), then run
`docker compose up -d --no-deps ml backend`.

## Known downsides

- ~3× slower per request (27–32 s vs 8 s on flash-lite-preview) on
  complex 10–13 KB specs. Acceptable because the old output was
  flat / incorrect on those specs.
- ~2× more expensive per call. Daily cap is already in place
  (`DAILY_SPEND_LIMIT_USD=5.0`) and trips to HTTP 429 if exceeded.
