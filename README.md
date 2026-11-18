# Bulk Call Analysis

A light desktop app for Windows and Mac that runs bulk analyses of sales-call transcripts by chat.

- You describe what you want to learn. An assistant, running in the official Claude Code program on your own subscription, turns that into a plan, checks it against the data and runs a small sample.
- After you approve the sample, a fast model (Luna, through the official Codex program on your own ChatGPT plan) reads every lead.
- The app shows live progress, an honest time-left estimate and a stall signal until an Excel file and a summary are ready.

**Design principles**
- Runs only on subscription usage limits: no API keys and no paid credits.
- BigQuery access is read-only.
- Personal data is masked before any model sees it.
- Every lead is checkpointed, so jobs survive sleep, crashes and usage limits.
- Counts always reconcile.

**Status:** under construction. The engine and the desktop shell's back end are being built first; the user interface comes later.

## Layout

- `engine/`: the Python engine (job store, BigQuery puller, masking, Luna worker pool, chat bridge, outputs).
- `shell/`: the Tauri desktop shell (process supervision, keep-awake, tray).
- `config/site.example.toml`: placeholders for site-specific settings. Copy it to `config/site.toml`, which is gitignored.

## Development

See `AGENTS.md` for conventions. Engine checks:

```
cd engine
uv run ruff check .
uv run mypy src
uv run pytest -q
```
