# Security decisions

This document records the residual risks behind the local security decisions.

## D-PIPE and D-IPC-PROTO

The engine uses a private local endpoint, a bounded worker pool, explicit BCA1
nonce/HMAC authentication, UTF-8 JSON frames, byte limits, and deadlines. It
does not use multiprocessing's pickled authentication protocol. The listener
has a bounded accept backlog and the shim retries startup races briefly.

The client handshake authenticates the engine endpoint with the shared
per-process key. A process that can replace an endpoint after the engine exits
could still impersonate the engine to a live shim; the endpoint is random and
local, and Windows named-pipe ownership and POSIX directory permissions limit
that exposure. Read-only opener floods can consume the bounded worker slots
and delay a legitimate call, but cannot create unbounded threads.

## D-MASK-NUM-2

Every integer, numeric string, and float with an integer part whose normalized
length is 10 through 13 digits is replaced with `[NUMBER]`. Normalization
removes one leading `91` or `0`; there are no column or alias exemptions.
Queries that could reconstruct values through code-point, hex, base-encoding,
formatting, byte, character, or reversal functions are refused after comments
and literals are parsed safely.

Residual risk remains for transformations not covered by the strict refusal
set, and for character-by-character inference from permitted query results.
The SQL guard is a defense-in-depth check; BigQuery dry-run and the read-only
runner remain mandatory.

## Strict tool set

The MCP tools expose read-only metadata and masked query results, operator
notes and briefs, validated job proposals and status, opaque sample references,
leak-scanned masked transcripts, masked aggregates, and the two approved plan
files. Raw pulls and raw lead identifiers are never exposed to the model.
Raw transcript bundles are removed after masking, including masking and leak-scan
hold-backs; no raw bundle is retained once that lead reaches a terminal outcome.
