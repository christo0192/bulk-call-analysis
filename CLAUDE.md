# Bulk Call Analysis: project instructions

A light desktop app (Windows and Mac) for running bulk analyses of sales-call transcripts by chat.

- A chat assistant (the official Claude Code program, run headless) plans each job.
- A fast model (Luna, through the official Codex app-server) reads every lead.
- An engine pulls the data read-only from BigQuery, masks personal data, runs the reads with checkpoints, and writes an Excel file plus a summary.

Local-only notes (the design doc, decisions and research) live in `internal/`, which is never committed:
@internal/NOTES.md

## Rules for every change

- **Subscription usage only.** Never add code that uses API keys, paid credits, extra usage, top-ups, rate-limit reset credits or fast/priority tiers, for Claude or Codex.
  - Child processes get an allowlisted environment.
  - API-key variables are always stripped.
- **Logins.** Never read, copy, store or proxy login tokens or credential files. Sign-in always runs the vendor's own official flow.
- **Official programs.** Never modify the Claude Code or Codex programs, and never fake a client's identity.
- **Personal data.** Mask it before any text reaches any model. Tests use synthetic data only, never real transcripts.
- **BigQuery is read-only.** No DML, no DDL, no destination tables, no exports.
- **Public repo.**
  - No internal names, table names, paths, people's names or company details in tracked files.
  - Site-specific values go in a gitignored `config/site.toml`. `config/site.example.toml` holds placeholders.
- **No automatic model switching.** Model ids are configuration, changed only by the operator.
- **Simulation tests use fakes** for Claude, Codex and BigQuery, so they cost no model usage. CI never calls a real model.
- **Commits and PRs carry no AI-assistant attribution lines.**
