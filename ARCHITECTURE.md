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

**Superseded (see Phase 6, part 3 below):** the Fake fallback described
here was later replaced with a real Checkout.js round-trip, once it became
clear that fabricating a fake payment under a real order's id meant no
checkout in this build could ever produce a genuine Payment visible in the
Razorpay dashboard — a real observable gap, not just a documentation one.

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
the design spec's stack table names) allows freeform sandbox replies without
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
registry has never heard of, not `False`. Fail-closed per the design spec §0: an
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

### Phase 6, part 3 — a real Checkout.js path, replacing the silent Fake fallback

**Decision (a real gap found from a live user report, not a design
review):** `create_and_capture_order`'s S2S-unavailable fallback used to
construct a throwaway `FakeRazorpayClient`, capture a fake payment against
it, and report that payment under the *real* order's id — every "real"
checkout on this account therefore always produced an order that could
never show up as a real Payment in the Razorpay test dashboard. This was
caught because it manifested as an observable symptom ("transactions don't
appear in Razorpay dashboard"), not because it was flagged in review.

**Fix:** `create_and_capture_order` now returns a third path,
`"pending_real_checkout"`, whenever `client` is real and S2S capture is
unavailable — `payment` is `None`, and the order (genuinely created via
Razorpay's real API) is left in `awaiting_payment`/`awaiting_payment_amber`
rather than silently marked captured. `core/checkout.py::confirm_real_payment`
is the second half: it verifies a Razorpay Checkout.js browser callback's
signature server-side (`RazorpayClient.verify_payment_signature` — HMAC of
`order_id|payment_id` under the key secret, the same scheme Razorpay's own
widget produces), re-confirms the payment is actually `captured` via
`fetch_payment` (capturing it explicitly if it only shows `authorized`),
and only then applies whatever the original gate decision was — immediate
dispatch for green, the start of the cooling-off window for amber. The
`/live` frontend (`payWithRazorpay` in `web/app/live/page.tsx`) loads
Razorpay's actual `checkout.js` and opens it with the real order id, amount,
and publishable key id (`GET /api/orders/{id}` now returns
`amount_paise`/`razorpay_key_id` only when a real payment is still pending);
on the widget's success callback it posts to the new
`POST /api/checkout/{order_id}/confirm` route, which is the only thing
that ever changes the order's status — the widget's own claimed success is
never trusted on its own.

**Why not make S2S work instead:** confirmed directly against the live key
(`POST /v1/payments/create/json` → `404`) — it is genuinely disabled on
this account and only enabled by Razorpay support on request, not
something more code can route around. A browser is architecturally
required to produce a real captured payment without it; the earlier
"silently fake it" behavior was a design bug this fix corrects, not
something the new code compensates for.

**Tradeoff accepted:** an order that needs Checkout.js can no longer
dispatch or start cooling-off the instant the gate ALLOWs/HOLDs it — it
waits on a human at a browser completing a real card form. `FakeRazorpayClient`
(every test, the harness, and any `RAZORPAY_USE_FAKE=true` deployment)
is unaffected and still captures synchronously with no browser involved,
so this only touches the live deployed API's real-money path.

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
of the design spec's §6 ten MCP tools, so it's REST-only and never wrapped into
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
over the REST routes" per the design spec §6 means genuinely going through the
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

**Finding from local testing (not fixed — left for Phase 9's harness to
measure honestly, per the design spec's "never tune after seeing results"
rule):** no SKU in either committed seed catalog can score `green` under
`reversibility_score_detailed`, and no grocery SKU ever can, structurally.
Grocery items are `perishable`/`consumable` with `return_window_days` 0-2
(Phase 2's own catalog spec, since perishables genuinely can't have long
return windows), and `f_return` alone is 35% of the score. Even a
hypothetical grocery item with the best possible price and fulfilment
speed tops out around 0.69 — below the 0.75 green threshold — because
`f_return` caps at `0.35 × (2/14) ≈ 0.05` regardless of anything else. The
committed jewellery catalog's non-personalised "stock" items all carry
`return_window_days=7` (the spec described a 7-15 day range; the seed data
never varied it), which similarly caps them around 0.6-0.7. In practice,
this means the demo's `/live` page will only ever show amber and red
carts from the real catalogs, never a green one, unless the operator
constructs one by hand. `harness/labels.json` labels 25 carts "green" by
independent human/AI reasoning (e.g. a single `toor-dal-1kg` purchase) —
those will very likely disagree with the deterministic score once Phase 9
actually runs the comparison, which is exactly the honest accuracy gap the
harness exists to surface, not something to quietly fix beforehand.

**Not built in this phase, by design:** a full re-quote-and-retry loop for
`substitution_accept` — it re-points the cart at the accepted product and
tells the caller to request a fresh quote and call `checkout_execute`
again, rather than doing that chaining server-side. A substitute is a
different product with its own live price/stock; giving it its own signed
quote (rather than synthesizing one) keeps R05/R06/R07 checking a quote
that's genuinely fresh, not one this route manufactured.

### Deploy — Railway + Neon + Redis Cloud

**Decision (a real build bug, caught by the first Railway deploy attempt):**
`Dockerfile`'s `apt-get install` listed `libgdk-pixbuf2.0-0` (a WeasyPrint/
Cairo dependency for the eventual Phase 8 PDF dispute pack). Railway's
build image runs a newer Debian release than was current when this line
was written, where that exact package name has "no installation
candidate" — it was renamed upstream to `libgdk-pixbuf-2.0-0`. Confirmed
via `apt-cache search` inside the same base image before touching the
Dockerfile, then verified with a full local `docker build` before
redeploying, rather than iterating blind against Railway's build queue.

**Deploy sequence actually used:** `railway login` (browser OAuth — the
CLI opened the system browser, which already had an active Railway
session, so no password was ever typed into anything this session
controlled) → `railway init` → `railway up` → fix the Dockerfile bug above
→ `railway up` again → `railway domain` to provision
`praman-production.up.railway.app` → set `PUBLIC_BASE_URL` to that domain
(the app reads it for the Twilio webhook signature check and the
`/.well-known/agent-commerce.json` manifest) → redeploy. Every other env
var (`DATABASE_URL` → Neon, `REDIS_URL` → Redis Cloud, Razorpay/Twilio/
Gemini credentials) was set from the same values already in the local
`.env`, kept in sync manually rather than via a `railway.toml` (not
written this phase — the CLI's own variable/domain commands covered
everything needed).

**Verification, not just "the build succeeded":** after deploy, the exact
same green→amber(+buyer WhatsApp undo, real Twilio-signature-verified
`/wa/webhook` call)→red(+merchant WhatsApp approve, same route) smoke
flow that was run locally against docker-compose Postgres/Redis was
re-run against the live `praman-production.up.railway.app` URL, with real
signed REST requests end to end. All three passed, confirming Neon and
Redis Cloud are reachable from Railway's network, migrations ran
correctly on container start (`Dockerfile`'s `CMD` runs `alembic upgrade
head` before `uvicorn`), and the Twilio HMAC signature check works
against the real `PUBLIC_BASE_URL`.

**Note:** this sandbox's own outbound network is restricted to HTTPS
(port 443) — direct TCP connections to Neon's Postgres port (5432) timed
out when tested from here, though a later attempt through what may have
been a different connection path succeeded (seeding data directly against
Neon did work). This didn't block anything: Railway's own network reaches
both Neon and Redis Cloud without restriction, which is the only thing
that actually matters for the deployed app, and `railway`/`curl` calls
(all HTTPS) worked throughout.

## Phase 7 — Frontend

**Stack substitution:** `create-next-app`'s current latest is Next.js
16.3.3 / React 19.2, not the "Next.js 15" the design spec names — the spec was
written against whatever was current at the time; there is no reason to
pin an older major deliberately. Tailwind v4 (CSS-first `@theme`, no
`tailwind.config.js`) came along with it, which changes *where* the
design tokens live (`app/globals.css`'s `@theme inline` block) but not
what they are.

**Decision:** the design tokens (the design spec's §7 colour/type table) are
defined in exactly two places kept in sync by hand — CSS custom properties
in `app/globals.css` (which Tailwind's `@theme` turns into utility classes
like `bg-paper`/`text-band-green`) and a mirrored `lib/tokens.ts` object.
The duplication is deliberate, not an oversight: `ReversibilityGauge`'s
SVG-adjacent inline styles need literal colour *strings*, which Tailwind
classes can't provide, and threading CSS-variable lookups through JS for
one component wasn't worth the indirection. `lib/tokens.ts`'s own comment
says as much and tells future edits to keep both in sync.

**Decision:** this is a single deliberate palette ("ink on paper"), not a
light/dark pair. The design spec never asked for a dark mode, and
dark-mode-awareness requirements are specific to embedded, sandboxed
preview surfaces — this is a normal deployed Next.js site, so that
constraint doesn't apply here.

**Decision (a real design bug, caught before it shipped):** the first
version of `useLedgerStream` (`lib/sse.ts`) used the browser's native
`EventSource`, whose `onmessage` only fires for the *default*, unnamed SSE
event type. The backend's stream (`api/routes_events.py`) names every
frame's `event:` field after that event's own `event_type` — there are
dozens (`GATE_DECISION`, `CART_CONFIRMED`, `ORDER_DISPATCHED`, `MERCHANT_APPROVED`,
...) — so `EventSource` would have silently received none of them. Fixed
by dropping `EventSource` for a manual `fetch` + `ReadableStream` parser
that reads every frame regardless of its event name (the JSON payload
already carries `event_type` internally, so the SSE-level name is
informational, not required).

**Decision (a second real bug, found via live testing in the browser
against a real deployed session, not a unit test):** that manual parser's
frame-boundary search looked for `"\n\n"`, but `sse_starlette` terminates
lines with CRLF (`\r\n`), so a blank-line frame boundary is actually
`"\r\n\r\n"` on the wire — the search never matched, so the parser
silently buffered forever and the ledger stream showed zero events despite
a healthy 200 OK connection. Caught by fetching the raw stream directly
from the browser console and inspecting the bytes rather than trusting
the UI's "connected" indicator, which was accurate but uninformative on
its own. Fixed by normalizing `\r\n` to `\n` once per decoded chunk before
the frame search.

**Decision:** `/catalog`'s Confirm/Edit buttons are disabled stubs. There
is no REST route for actually confirming or correcting a low-confidence
product — that logic lives only inside the WhatsApp state machine's
plain-text matching (`whatsapp/onboarding.py::_handle_confirming_items`).
Per the design spec's own cut order ("/catalog and /metrics pages → /live and
/approvals carry the demo"), building a second, REST-only confirmation
flow that duplicates WhatsApp's wasn't worth it; the page stays an honest,
labelled read-only mirror instead of a broken-looking interactive one.

**Decision:** three small REST routes were pulled forward from later
phases because a page needed real content, not a stub: `GET
/api/dispute-pack/{cart_id}` (Phase 8's dispute pack, assembled from
already-existing pieces — `core/ledger.py::dispute_pack_events`,
`GateDecision` rows, the `CartMandate`/`IntentEnvelope`/`Order` joins —
for `/dispute/[orderId]`), `GET /api/metrics` (a live, honest count of
this deployment's own gate decisions and orders — explicitly *not* the
Phase 9 harness's Arm A/B benchmark, and `/metrics` says so on the page
rather than implying otherwise), and `GET/POST /api/merchants`,
`/api/catalog/review-queue`, `/api/approvals` (+`/decide`) for the picker
UIs and the approvals inbox. `/api/approvals/{id}/decide` reuses
`whatsapp/approvals.py`'s exact `_approve`/`_decline` functions via a new
`decide_by_order_id`, so a click in the frontend and a WhatsApp reply can
never produce different outcomes for the same order — the design spec's §7
explicit requirement.

**Decision:** `/live` signs its own agent requests in the browser
(`lib/sign.ts`, `@noble/ed25519` + `@noble/hashes`) using a demo keypair
`POST /api/agents/register` generates server-side and returns once. This
is the one place a private key ever touches client-side JS in this
codebase, and it's clearly scoped to that: a real agent operator would
never sign from a browser, and the route's own docstring says the
key-generation path exists only for this demo convenience. Signing
requires re-serializing the exact JSON string that gets POSTed — `lib/api.ts`'s
signed calls take a pre-serialized `raw` string rather than a plain
object, specifically so nothing re-stringifies (and potentially
byte-shifts) the body between signing and sending.

**Decision:** "Break the ledger" (`components/LedgerStream.tsx`) never
touches the real backend — it's a purely client-side visual: corrupt the
*displayed* hash for rows from a chosen index onward and flip the
chain-proof strip red. A button that actually corrupted the real,
hash-chained dispute ledger would be a genuinely destructive action on
audit data, which this codebase treats as a serious thing (see the whole
point of Phase 1's `verify_chain`); simulating the same visual proof
without ever writing a bad hash anywhere real gets the demo moment
without that risk.

**Verification:** every page was checked visually and functionally in a
real browser against a real API — first a local docker-compose
Postgres/Redis-backed instance, then the live Railway deployment — not
just `next build` succeeding. The full green→amber(+cooling-off, "Break
the ledger")→red(+"Approve as merchant") flow was driven end to end on
`/live` with real signed requests and a live-updating ledger stream in
both environments; `/approvals` was exercised for both Approve and
Decline; `/dispute/[orderId]` was checked against a real captured order's
full pack; `/metrics` and the homepage counter were checked against real
seeded data; mobile-viewport screenshots confirmed the nav and `/live`'s
three-column grid both reflow correctly below 768px.

## Post-Phase-7 — a real Twilio finding from live phone testing

**Finding (confirmed via `curl` against Twilio's own API, not just
observed in-app):** this project's Twilio account, being on the Trial
tier, cannot fetch `Message`/`Media` REST resources at all —
`401 code 20003: "This feature is not available on a Trial account.
Please upgrade your account to gain access."` This is a *second*, more
fundamental limitation than the already-documented "outbound needs an
approved Content Template" one: it blocks downloading an inbound photo's
media, on any number (the classic shared Sandbox included, not just the
newer self-service trial number `whatsapp/routes_whatsapp.py` was first
tested against). Switching `TWILIO_WHATSAPP_FROM` to the classic Sandbox
(`+14155238886`) — done in response to the user finding a "Connect to
WhatsApp Sandbox" page with a join code — did not fix this, because the
restriction lives at the account tier, not the number.

**Decision (a real bug this surfaced and fixed):** `routes_whatsapp.py`'s
media-fetch loop previously let `fetch_media`'s exception propagate
unhandled, which crashed the entire webhook handler with a 500 — meaning
a message with an undownloadable photo never even reached the state
machine; no ledger event, no merchant creation, nothing. Wrapped in a
try/except per media item, logging a warning and skipping just that item
— the same "one bad photo shouldn't sink the batch" principle already
applied to a bad *extraction* in `onboarding.py`'s loop, just one step
earlier in the pipeline. With this fix, a photo that can't be downloaded
now degrades to the state machine seeing zero usable media (and replying
"I need at least one photo…", same as if none were attached) rather than
the request failing outright.

**Not fixed, because it can't be from our side:** the actual ability to
download and extract a real vendor's photo end-to-end over live WhatsApp
remains blocked until the Twilio account is upgraded with billing. This
is disclosed plainly in the README rather than worked around — there is
no code-level workaround for an account-tier API restriction.
