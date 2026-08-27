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

## Phase 3 — WhatsApp vendor onboarding

**Decision:** every "[Yes] [No]" / "[₹500] [₹2,000] [₹5,000]" button in the
spec's onboarding script is sent as plain WhatsApp text with an explicit
reply instruction ("Reply YES...", "Reply 1 for ₹500..."), not a native
interactive button. Twilio's WhatsApp **Sandbox** doesn't support
interactive buttons without a pre-approved WhatsApp Business content
template — that's a real constraint of the free sandbox, disclosed in the
README, not a shortcut taken for convenience.

**Decision:** `WhatsAppClient` is a Protocol (`RealTwilioClient` /
`FakeWhatsAppClient`), same pattern as `RazorpayClient` and `LLMClient`.
`FakeWhatsAppClient` implements the *actual* Twilio webhook-signing
algorithm (HMAC-SHA1 over `url + sorted-concatenated params`, base64) so
`verify_webhook_signature` tests exercise real verification logic without
a network call — and a signature it produces verifies correctly against
`RealTwilioClient` (backed by Twilio's own `RequestValidator`), proven by
`test_webhook_accepts_correctly_signed_request`.

**Decision:** correcting a flagged catalog item during `CONFIRMING_ITEMS`
is deliberately simple: reply "YES" to confirm as-is; otherwise, if the
reply contains a parseable rupee amount it corrects the price, else the
whole reply text becomes the corrected product name. The spec's script
("taps or types a correction") doesn't specify which field a free-text
correction targets, and a real per-field correction UI needs actual
WhatsApp List/Button messages (not available in the sandbox — see above).
This simple rule covers the two most common real corrections (wrong price,
misread name) without a multi-turn "which field?" sub-dialogue.

**Decision:** `SETTING_POLICY` covers two sequential questions (spend
limit, then cooling-off hold) despite being one state in the spec's state
list — tracked by whether `agent_policy["max_txn_paise"]` is already set,
rather than adding a `SETTING_POLICY_SPEND` / `SETTING_POLICY_COOLING_OFF`
state pair. Keeps the state list matching the spec's literal five states.

**Decision:** a vendor's photos are processed as soon as they arrive in
one inbound webhook call (whatever `NumMedia` Twilio delivers in that
request) — there's no debounce window waiting for "are you done sending?".
If a vendor sends multiple separate messages each containing photos, each
batch is extracted and appended to the catalog independently, and the
"Found N items..." summary is sent again per batch. A production version
would debounce for a few seconds after the last inbound media message
before extracting; this is a deliberate scope simplification, not an
oversight.

**Decision:** `RealTwilioClient.send_text` calls the synchronous `twilio`
SDK directly inside an `async def` rather than wrapping it in a thread
pool executor. Message volume in this system is one bot reply at a time
(never concurrent under load), so a blocking call is an acceptable
simplification here — it would not be if this adapter ever needed to send
at volume.

**Finding (not a decision — a discovered platform constraint):** live
end-to-end testing over an ngrok tunnel from a real phone against a real
Twilio trial account revealed that this account type requires **every**
outbound WhatsApp message, including a same-session reply, to carry a
pre-approved Content Template `ContentSid` — `messages.create(..., body=...)`
fails with `400 ContentSid Required`. This is stricter than the WhatsApp
Business Platform's general 24-hour-customer-service-window rule (which
the build spec, and Twilio's own quickstart docs, describe: freeform
replies should be allowed within 24 hours of an inbound message). Managing
templates needs the Content API, which itself is gated: `client.content.v1
.contents.list()` returns `401 This feature is not available on a Trial
account. Please upgrade your account to gain access.` A generic public
example `ContentSid` from Twilio's own docs was also tried directly against
`messages.create` and rejected as not provisioned on this account (`400
The ContentSid is Invalid`). Two independent live API calls, not a config
mistake on our side — confirmed by first fixing an actual bug (the `From`
number defaulted to the classic Sandbox number `+14155238886` instead of
this trial's assigned number) and observing the same error persist.

Conclusion: the classic, long-lived Twilio "WhatsApp Sandbox" (what
CLAUDE.md's stack table names) allows freeform sandbox replies without
templates; this newer self-service "Twilio Console trial" flow — what
`console.twilio.com`'s current WhatsApp onboarding actually creates for a
new signup — does not, until the account is upgraded with a payment method.
`RealTwilioClient` and `whatsapp/onboarding.py` are implemented correctly
against the documented API and require no code change once upgraded;
inbound webhook handling (signature verification, merchant creation, state
transitions) was independently confirmed fully working live. Per the
user's decision, the project proceeds without the account upgrade — this
is disclosed in the README rather than worked around.
