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

**Follow-on decision:** the discovery above surfaced a real bug, not just a
platform limitation — `_send()` originally called `whatsapp.send_text()`
*before* most of a handler's actual work (e.g. `_handle_awaiting_media`
sends "Reading them…" before running extraction), so a delivery failure
raised an exception that stopped the state machine before it did anything
useful, and Twilio saw a 500. Fixed: `_send()` now catches a send failure,
logs it, records the outbound `WhatsAppMessage` with `wa_message_id=None`,
and ledgers `WHATSAPP_OUTBOUND` with `delivered: false` and the error —
an honest record of an attempted-but-failed delivery — then lets the
caller's actual work continue regardless. A vendor on a send-blocked
account (or hitting a transient Twilio/network hiccup) now still gets
their photo actually extracted and their catalog actually built, even
though they never see a reply. Proven in
`test_processing_continues_even_when_every_reply_fails_to_send`.

## Phase 4 — Registry, quotes, envelope

**Decision:** `GateResult` (decision/reason_code/detail/remedy/rule_id)
lives in a new shared `core/gate_types.py` rather than in `core/gate.py`
(which doesn't exist until Phase 5) or being duplicated per module.
`envelope.py` and `quotes.py` both return it now; Phase 5's `gate.py` will
import it rather than define its own competing shape. Avoids a rename or
an import cycle later.

**Decision:** `verify_cart_within_envelope` operates on plain frozen
dataclasses (`Cart`, `CartItem`, `Envelope`) defined in `envelope.py`
itself, not on the SQLAlchemy `CartMandate`/`IntentEnvelope` rows. This is
what makes it trivially pure and DB-free per the spec's own requirement
("Pure. No I/O."); a `cart_from_mandate`/`envelope_from_row` conversion
(added when Phase 5's gate wires this into real requests) is a thin,
untested-by-itself mapping step, not where the money-path logic lives.

**Decision:** the soft stock hold in Redis is one key per `(product_id,
quote_id)` pair (`stock_hold:{product_id}:{quote_id}` → qty, `EX`=the
quote's own TTL), summed via `SCAN` + `MGET` in `held_stock_for_product`
rather than a single atomic counter. A counter would need to be
decremented on quote expiry, which Redis key expiry can't trigger for you
without keyspace-notification plumbing; a per-quote key expiring on its
own and being summed on read is simpler and self-correcting (an expired
hold just stops appearing), at the cost of not being a single atomic
number and needing a `SCAN` under real concurrency. Acceptable given the
name is literally "soft hold," not a hard reservation — R07's real stock
check in Phase 5 still reads live `Product.stock` as the source of truth.

**Decision:** the detached agent-request signature is computed over
`f"{method}\n{sha256(body).hexdigest()}\n{timestamp}\n{nonce}"` — newline-
joined fields, not a JSON/JCS structure. Quotes and cart mandates use JCS
canonicalization because their *content* needs a canonical form multiple
parties independently reconstruct field-by-field; a request signature's
inputs are just four already-unambiguous strings/hex-digests, so a fixed
delimited format is simpler and equally unambiguous.

**Decision:** nonce TTL in Redis is `2 * AGENT_CLOCK_SKEW_TOLERANCE_S + 10`
(130s), not exactly the skew tolerance (60s). A request timestamped up to
60s in the past AND one timestamped up to 60s in the future are both
valid, so a nonce must stay rejected-as-replay across that full ~120s
window from either side, not just 60s from when it was first seen.

**Decision:** `LocalRegistry.is_revoked` returns `True` for an agent the
registry has never heard of, not `False`. Fail-closed per CLAUDE.md §0: an
unknown agent is untrusted, not innocent-until-proven-revoked.
`verify_agent_request` folds "unknown" and "revoked" into the same
`AGENT_REVOKED` reason code (R02) since the caller's remedy is identical
either way — register (or re-register) a valid, active agent.

## Phase 5 — Reversibility Ladder + Policy Gate

**Decision:** R03 ("envelope valid & unrevoked") is implemented as a
simple existence lookup — does an `IntentEnvelope` row with this
`envelope_id` exist at all — separate from R04
(`verify_cart_within_envelope`), even though R04 *also* checks
`revoked_at` and the valid window as its own first two sub-checks. This
looks redundant but isn't: R03 catches "this envelope_id doesn't exist"
(`ENVELOPE_INVALID`, a request-shape problem) before R04 ever runs its
pure business-logic checks against a real, resolved envelope
(`ENVELOPE_REVOKED`/`ENVELOPE_EXPIRED`, policy problems). Keeping them
separate matches the spec's own R01-R12 table listing them as distinct
rules with distinct reason codes.

**Decision:** R08 (`reversibility >= env.min_reversibility`) and R09
(`band == amber`) are two independent policy layers, not one collapsed
check, exactly as the spec's rule table lists them. This means a red-band
cart with `env.min_reversibility` set low (or 0) can pass R08 without
escalating — R08 is the envelope-configurable "does this buyer's policy
require human oversight below score X" trigger, while R09 is a fixed
system policy: an amber-band cart always gets a mandatory cooling-off
hold, regardless of what the envelope's own minimum says. In practice a
sensibly-configured envelope sets `min_reversibility` at or above the
amber threshold (0.40) so red bands always escalate too — but the gate
enforces exactly what the spec's table says, not an inferred stronger
guarantee.

**Decision:** R10's velocity limit (`VELOCITY_WINDOW_S` /
`VELOCITY_MAX_TRANSACTIONS`) and R12's idempotency-key TTL are not given
numeric values by the spec — chosen as reasonable defaults and named in
`config.py` rather than inlined, per the non-negotiable rule. Velocity is
tracked as a Redis sorted set of per-agent timestamps (`ZADD`/
`ZREMRANGEBYSCORE`/`ZCARD`), incremented only on a final `ALLOW` — an
attempt that fails an earlier rule was never actually a transaction, so it
shouldn't count against the agent's rate.

**Decision:** `GateRequest` takes an already-assembled `Cart`,
`reversibility_items`, and `quotes` rather than `run_gate` resolving a
cart from scratch out of raw request fields. The gate's job (per the
spec's R01-R12 table) is evaluating a fully-formed request, not building
one — cart assembly (matching SKUs to quotes, defaulting
`return_window_days`/`fulfilment_hours` from a product) is Phase 6's
concern (checkout orchestration), kept out of this phase's already-large
surface.

**Honesty note — `harness/labels.json`:** the 60 hand-labeled carts were
authored by this AI assistant reasoning independently about each cart
(is the item custom/personalised, how large a slice of a plausible budget
is it, how easily could it be resold), *not* by a human reviewer, and
`scripts/gen_labels.py` deliberately never imports or calls
`reversibility_score_detailed` so the label isn't secretly derived from
the formula it's meant to validate. That keeps the Phase 9 accuracy
measurement non-circular, but an AI-authored "hand label" is a materially
weaker evidentiary claim than a genuine human-reviewed one, and the
project says so plainly (README "What's real vs mocked") rather than
presenting these labels as more authoritative than they are.

## Phase 6 — Checkout, cooling-off, substitution, merchant approvals

**Decision (a real bug found and fixed, not just a design choice):**
re-running the gate after a merchant's WhatsApp Approve reuses the agent's
*original* signed request — the server never holds the agent's private key
and can't re-sign as them. That request's nonce was already consumed
marking the first (ESCALATE) pass, and by the time a human approves,
minutes have usually elapsed. The first implementation re-ran the full
R01-R12 chain unconditionally and failed every real approval: the nonce
tripped `NONCE_REPLAYED`, and even after fixing that, the clock-skew check
tripped `CLOCK_SKEW_EXCEEDED` against a now-stale original timestamp.
Fixed by having `GateRequest.human_present=True` also set
`skip_nonce_check=True` on the R01/R02 call, which skips *both* the nonce
and skew checks — both exist to catch a stale/replayed *external* request,
neither is meaningful when the server is deliberately continuing a request
it already authenticated once. `skip_nonce_check` is documented as safe
for exactly that one internal caller and must never be reachable from an
actual inbound HTTP request.

**Decision:** the exact `GateRequest` that produced an ESCALATE is
serialized into a new `Order.pending_gate_request` JSON column
(`gate.py`'s `serialize_gate_request`/`deserialize_gate_request`) rather
than re-deriving one from scratch when the merchant replies. Cart items,
reversibility inputs, and quotes are all reconstructed byte-for-byte from
what was actually signed, not re-fetched from current DB state — R04-R07
re-checking against *current* envelope/price/stock state (while R01's
signature is checked against the *original* request) is exactly the point
of re-running the full chain, not a shortcut around it.

**Decision:** `Order.idempotency_key` is deterministic
(`sha256(cart_id || agent_did)`) and unique-constrained, so a
merchant-approval retry can never insert a second `Order` row for the same
cart — it must update the original `pending_approval` row in place.
`_create_order_row` grew an `existing_order` parameter for exactly this;
every other caller still inserts fresh. Discovered by first writing the
naive "always insert" version and hitting the unique constraint in a test.

**Decision (a second real bug, same root cause):** `checkout.py`
originally computed `now = datetime.now(UTC)` internally instead of
threading an injected value through, unlike `gate.py`/`envelope.py`'s own
established discipline. This broke `Order.created_at` under test (used by
the merchant-approval timeout sweep) and was silently masked until a test
using a fixed historical `now` collided with the sandbox's real wall
clock. Fixed by deriving `now` from `gate_req.now` throughout
`checkout.py`, and by making `whatsapp/approvals.py`'s `handle_merchant_reply`
accept an optional injectable `now` (defaulting to the real clock in
production) rather than hardcoding it.

**Decision:** the buyer's cooling-off undo message goes to
`IntentEnvelope.user_whatsapp`, not the merchant's WhatsApp number — an
easy mistake since checkout code is already holding a `Merchant` object at
that point for the message's "from {merchant.name}" text. Caught in code
review before it shipped, not after a live test; worth naming because it's
exactly the kind of bug that's invisible until you specifically assert on
`sent_messages[0].to`.

**Decision:** `RazorpayClient` gained `drive_to_captured` and the module-
level `create_and_capture_order` helper, reusing the exact real-then-fake
fallback Phase 0's spike established (attempt the S2S test-card capture;
on `S2SUnavailableError`, fall back to `FakeRazorpayClient` and report
which path actually ran). Checkout always goes through this helper rather
than calling `create_order`/`capture_payment` separately, so the fallback
logic lives in exactly one place.

**Decision:** substitution's deterministic filter computes each
candidate's reversibility band using the *same* `reversibility_score_detailed`
function as the real gate (via a single-item cart against the real
envelope), not a simplified heuristic — "band no worse than the original"
needs the actual band the gate would compute, not an approximation that
could disagree with it.

### Phase 6, part 2 — REST surface, MCP, webhook, scheduler, buyer undo

**Decision (a real design bug found via testing, not just a choice):**
the first version of every signed route (`quote_request`, `cart_confirm`,
`checkout_execute`, `substitution_accept`) put `timestamp`/`nonce`/
`signature` on the same JSON body whose hash the signature covers. That's
self-referential — the agent signs `sha256(body)` *before* it knows the
signature, so the signature can't also live inside the bytes it signs
without the server hashing a different body than the agent actually
signed. The route received the body, computed its hash including the
now-embedded signature field, and got a hash the agent never signed —
every request failed `AGENT_SIG_INVALID` in the very first integration
test written against it. Fixed by moving those three fields to
`X-Praman-Timestamp` / `X-Praman-Nonce` / `X-Praman-Signature` headers
(`api/deps.py::get_signature_headers`), so the raw bytes a route hashes
(`await request.body()`) are exactly, and only, the semantic payload —
the same separation Twilio and Razorpay's own webhook signing use.

**Decision:** every field that determines a cart's reversibility score or
envelope eligibility (`category`, `category_class`, `is_personalised`,
`return_window_days`, `fulfilment_hours`, `restocking_cost_pct`) is read
from the `Product` row a quote references, never trusted from request
body fields — `cart_confirm` and `checkout_execute` both re-derive these
server-side. An agent can declare its own `qty` and reuse a merchant-signed
`unit_price_paise`, but it cannot declare an item more reversible than the
merchant's own catalog says it is.

**Decision:** `cart_confirm` doesn't run the R01-R12 gate — only R04's
envelope pre-check, informationally, alongside computing and persisting
the `CartMandate`'s reversibility score/band. `checkout_execute` is the
*only* HTTP-reachable caller of `run_gate`, matching the MCP table's
`destructiveHint: true` on exactly one tool. An agent gets fast, useful
feedback on an obviously-doomed cart (wrong envelope, disallowed category)
before it ever commits to a second signed call.

**Decision:** agent registration (`POST /api/agents/register`) is not one
of CLAUDE.md §6's ten MCP tools, so it's REST-only and never wrapped into
`mcp/server.py`. It exists because something has to create the `agents`
rows the rest of the surface assumes — a real deployment would register an
agent out-of-band (or via NPCI's future UAP). Supplying your own
`public_key` never lets the server see a private key, matching `Agent`
having no `private_key_enc` column (unlike `Merchant`); omitting it is a
demo-only convenience that generates a keypair server-side and returns the
private key once, in the response body, never persisted.

**Decision:** `core/checkout.py` gained `cancel_order` — refund +
mark-cancelled for a still-held cooling-off order — used identically by
the REST `POST /api/orders/{id}/undo` route and the buyer's WhatsApp
CANCEL reply (`whatsapp/cooling_off_notify.py`), so the two entry points
share one implementation rather than drifting. It takes `amount_paise`
as an explicit parameter rather than trying to derive it from `Order`
(which has no amount column of its own) — every caller already has the
cart in scope to read the true total from.

**Decision:** `scheduler.py`'s sweep functions (`sweep_cooling_off_dispatch`,
`sweep_approvals`) take an explicit `AsyncSession` and `now`, following the
same testability discipline as `gate.py`/`checkout.py` — only the module's
`_run_sweeps` (the actual APScheduler job, run every 5s) opens a real
`SessionLocal`. The cooling-off dispatch query originally compared a
sqlite-round-tripped (naive) `cooling_off_until` against a tz-aware `now`
and raised `TypeError: can't compare offset-naive and offset-aware
datetimes` in the first test run — the same round-trip quirk
`timeutil.py::as_aware_utc` already exists to fix elsewhere; applied here
too.

**Decision:** the FastMCP app (`mcp/server.py`) wraps the REST routes over
real HTTP (`httpx.AsyncClient` against `settings.public_base_url`) rather
than calling route handlers as Python functions directly — "thin wrappers
over the REST routes" per CLAUDE.md §6 means genuinely going through the
same HTTP surface an external caller would, not a shortcut that only looks
like one. `mcp.http_app(path="/mcp")` already serves at its own internal
`/mcp` path; mounting it a second time at `/mcp` in `main.py` produced
`/mcp/mcp` (caught by manually probing the mounted app before committing)
— fixed by mounting at `/` instead, and mounting it *last*, after every
other route, since Starlette matches mounts by registration order and an
earlier root mount would have swallowed `/health` and everything else
registered after it.

**Decision:** the Razorpay webhook (`POST /webhooks/razorpay`) only
reconciles/audits — this build's money path already captures and refunds
synchronously inside `execute_checkout`/`cancel_order`, so no order's
status is ever *first* set by the webhook arriving. Verifies the HMAC
signature before touching the payload at all, exactly like the inbound
Twilio webhook does.

**Not built in this phase, by design:** a full re-quote-and-retry loop for
`substitution_accept` — it re-points the cart at the accepted product and
tells the caller to request a fresh quote and call `checkout_execute`
again, rather than doing that chaining server-side. A substitute is a
different product with its own live price/stock; giving it its own signed
quote (rather than synthesizing one) keeps R05/R06/R07 checking a quote
that's genuinely fresh, not one this route manufactured.
