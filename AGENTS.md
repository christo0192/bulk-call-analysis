# Instructions for coding agents

This repository is public. Read this file fully before you change anything.

## Stack

- **Engine:** Python 3.12, managed with `uv`, in `engine/`.
  - Lint: `uv run ruff check .` and `uv run ruff format --check .`
  - Types: `uv run mypy src`
  - Tests: `uv run pytest -q`
- **Desktop shell:** Tauri 2 (Rust), in `shell/`. Built in CI only.
- Windows and macOS are both first-class. Use `pathlib`, never hard-code paths or drive letters, and keep everything OS-specific behind the `platform` modules.

## How to work

- Do exactly the task you were given. Keep the change small and focused.
- Write tests for every behaviour you add, and run lint, types and tests before you finish.
- Never weaken, skip or delete a test to make it pass. Fix the code.
- Tests must not use the network, real accounts, real models or real data.
  - Use the fakes in `engine/tests/fakes/`: the fake Codex app-server, the fake Claude Code, and the fake BigQuery client.
  - Use synthetic transcripts only.
- Handle errors explicitly. Never swallow an exception silently.
- Every count must reconcile. Never drop items without recording why.
- Write files with LF line endings and UTF-8.

## Hard rules

- **Subscription usage only.** Never add code paths that use API keys, credits, extra usage, top-ups, rate-limit reset credits or fast tiers. Child-process environments are built from an allowlist that strips API-key variables.
- **Logins.** Never read, copy or store login tokens or credential files (for example `auth.json` or `.credentials.json`). Sign-in always runs the vendor's official login flow.
- **Official programs.** Never modify the Claude Code or Codex binaries.
- **Personal data.** Mask it before any text is sent to any model.
- **BigQuery is read-only.** No DML, no DDL, no destination tables, no exports.
- **Nothing site-specific in tracked files.** No company names, people's names, internal table or dataset names, or local paths.
  - Site values belong in `config/site.toml`, which is gitignored.
  - Add placeholders to `config/site.example.toml`.
- **No secrets.** No keys, tokens or credentials anywhere in the repo.

## UI

The desktop UI uses Preact 10, TypeScript, Vite, and plain CSS. From `shell/ui`, run `npm run dev` for the mock bridge, `npm run typecheck`, `npm test`, `npm run build`, and `npm run e2e`. Keep labels wrap-safe: do not use ellipsis, clipping, or fixed heights on text containers. Use real buttons and links for interactions, no inline scripts, and synthetic fixtures only. The visual specification is `shell/ui/design/mockups.html`.
