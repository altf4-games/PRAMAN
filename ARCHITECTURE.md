# PRAMAN — Architecture & design decisions

This file records ambiguous design decisions and the tradeoff chosen, per the
build spec's instruction: "When a design decision is ambiguous, pick the
simpler option and record the tradeoff here rather than stopping to ask."
Entries are added phase by phase as they come up — this is not written
retroactively at the end.

## Phase 0 — Razorpay spike

**Decision:** drive an order to `captured` using Razorpay's legacy
server-to-server (S2S) JSON test-card endpoint (`/v1/payments/create/json`)
when available, falling back to `FakeRazorpayClient.simulate_payment`
otherwise.

**Why:** a payment normally originates client-side via Checkout.js; there is
no purely server-side way to produce a real captured payment without either
S2S (opt-in, disabled by default on new Razorpay accounts) or a browser
automating the Checkout flow. S2S returned 404 on our test account —
confirmed not enabled — so the spike script falls back to the Fake client and
logs the fallback explicitly, per the spec's own rule: "If the real path
isn't working in 2 hours, ship on Fake, declare it in the README, move on."
Order creation and webhook-signature verification both run against the real
API/verification code; only the "produce a captured payment" step is faked.

## Phase 1 — Foundation

**Decision:** managed infra deviates from the spec's Railway-for-everything
default — **Neon** for Postgres and **Redis Cloud (redis.com)** for Redis,
both free tier, per explicit user instruction. Railway still hosts the
FastAPI app itself. Local development still uses `docker-compose.yml` with
local Postgres/Redis containers for speed; Neon/Redis Cloud apply to the
actual deployed environment from Phase 6 onward.

**Decision:** ledger ordering uses a strictly-increasing `ts` column plus a
per-session `asyncio.Lock`, rather than a Postgres row lock (`SELECT ... FOR
UPDATE`) plus a separate monotonic sequence column. When two events for the
same session would otherwise get the same or an out-of-order timestamp, the
new event's `ts` is bumped to `last.ts + 1 microsecond` before insert. This
is sufficient for a single-process deployment (the only kind this project
ships) and lets `verify_chain` order purely by `(ts, event_id)` without a
dedicated autoincrement sequence column. It would not be correct across
multiple API processes/instances without moving to a real DB-level lock.

**Decision:** the SSE bus (`events.py`) is in-process pub/sub (an
`asyncio.Queue` per subscriber, a bounded recent-events backlog), not a
Redis-backed pub/sub channel. Simpler, and correct for a one-process demo
deployment; a subscriber connecting before the API restarts loses its
subscription (client reconnects and replays via `get_recent`). This would
need to move to Redis pub/sub if the API ever runs as more than one process.

**Decision:** `chain_hash = sha256((prev_chain_hash_hex + payload_hash_hex))`
— i.e. concatenating the two hex-string digests and hashing that as UTF-8 —
rather than concatenating raw digest bytes. Slightly less compact, but
trivial to reproduce independently (e.g. in the dispute-pack verifier or an
auditor's script) without agreeing on byte-order conventions.

**Decision:** SQLite (used for all tests, and as the zero-setup local
`DATABASE_URL` default) round-trips `DateTime(timezone=True)` values as
timezone-naive, unlike Postgres which preserves `tzinfo`. `core/ledger.py`
normalizes any naive timestamp read back from the DB to UTC before comparing
(`_as_aware_utc`) rather than requiring Postgres for tests — since we always
write UTC-aware datetimes ourselves, this is a safe assumption, not a
correctness gap.

**Decision:** private keys at rest (`Merchant.private_key_enc`) are
encrypted with Fernet, keyed by a symmetric secret derived from a single
`APP_SECRET` setting (SHA-256 → base64), rather than a full KMS/envelope
encryption setup. Adequate for a hackathon-scoped demo; a real deployment
would use a managed KMS.

## Phase 2 — Catalog ingest via VLM

**Decision:** live LLM calls (Gemini, via `adapters/llm.py`), not the
spec's own suggested fallback of shipping only pre-extracted JSON so the
demo never depends on a model call — per explicit user instruction. The
adapter is a `LLMClient` Protocol exactly like `RazorpayClient`, so the
provider is a one-line change in `get_llm_client()`; `FakeLLMClient` is
what every automated test actually exercises, so CI has no network or key
dependency even though the real pipeline does.

**Decision:** the LLM extracts a field's value AND its own confidence in
that value; a separate deterministic step (`normalise.py`) never
second-guesses the model's confidence number, only its formatting (units,
category vocabulary, duplicates). The first version of the confidence gate
naively required every one of the 8 extracted fields to clear the
threshold — which meant a nullable field like `stock` (never present in a
plain price list, so honestly reported at 0.0 confidence) sent every
single product to review, regardless of how legible the source was. Fixed
by excluding a nullable field from the gate check specifically when its
extracted value is `None`: a correctly-absent value carries no risk of
being *wrong* the way a shaky price reading does. See
`normalise.apply_confidence_gate`.

**Decision:** the two seed catalogs (`catalog_grocery.json`,
`catalog_jewellery.json`) are built from master CSVs
(`api/praman/seed/masters/`) run through the live pipeline once, then
committed as static JSON — not regenerated on every run. This keeps the
demo's catalog stable and avoids depending on a live model call (and a
finite free-tier quota, see README) at demo time, while still having
actually used the live model to produce them, rather than being
hand-authored. Master CSVs live in a directory separate from
`api/praman/seed/raw/`'s messy demo fixtures deliberately: the first
implementation put them in the same directory, and `dedupe_products`
correctly recognised the messy fixtures' "Toor Dal", "Basmati Rice", etc.
as duplicates of the master CSV's clean entries — silently keeping the
higher-confidence master copy and dropping every review-worthy example
`make ingest` was supposed to demonstrate. Separating the two directories
fixed it structurally rather than special-casing dedupe.

**Decision:** the Gemini adapter retries only on `ServerError` (transient
5xx, e.g. "high demand") with exponential backoff, never on the client
error (429 quota-exhausted, 4xx malformed request) — those are not
transient and retrying would just burn more of a small free-tier daily
quota for no benefit. `ingest_directory`/`build_catalog_from_csv` catch any
exception per source file and record it as a result error rather than
crashing the batch, so one flaky or quota-exhausted call doesn't take down
the whole ingest run.
