# PRAMAN

A kirana owner sends photos of his price list over WhatsApp; minutes later
his shop is a signed, agent-readable storefront that AI shopping agents can
transact with — gated by a reversibility-scaled policy engine and backed by
a hash-chained, exportable dispute ledger.

This README fills in as each phase lands (see the eventual final structure
in the project's own notes); for now it tracks what's built and what's
still mocked. (The build spec this project follows, `CLAUDE.md`, is an
AI-agent instruction file kept locally and intentionally not part of this
repo.)

**Live API:** [https://praman-production.up.railway.app](https://praman-production.up.railway.app)
(`/health`, `/.well-known/agent-commerce.json`) — Railway, backed by Neon
Postgres and Redis Cloud. No frontend yet (Phase 7).

## Status

- **Phase 0 (done):** Razorpay TEST MODE spike. Order creation and webhook
  signature verification run against the real API/verification code.
  Driving an order to `captured` falls back to `FakeRazorpayClient` — see
  "What's real vs mocked" below and [ARCHITECTURE.md](ARCHITECTURE.md).
- **Phase 1 (done):** foundation — config, DB models, Alembic migrations,
  CI (ruff/mypy/pytest/gitleaks), Ed25519 keys + `did:key` identifiers, RFC
  8785 (JCS) canonicalization, the SSE event bus, and the hash-chained
  ledger (`append_event` / `verify_chain`).
- **Phase 2 (done):** catalog ingest via a **live** VLM call (Gemini, via a
  swappable `LLMClient` adapter — see below), deterministic normalisation
  (unit parsing, category mapping, dedupe), and the confidence gate
  (`needs_review`). Both seed catalogs (`catalog_grocery.json`,
  `catalog_jewellery.json`, 40 SKUs each) were built by running master CSVs
  through the real pipeline against the real Gemini API — not hand-authored.
- **Phase 3 (backend done; inbound proven live, outbound blocked by Twilio
  account tier):** the WhatsApp vendor onboarding state machine (`NEW →
  AWAITING_MEDIA → EXTRACTING → CONFIRMING_ITEMS → SETTING_POLICY → LIVE`),
  the `POST /wa/webhook` inbound handler, and a swappable `WhatsAppClient`
  adapter. Tested live against a real Twilio trial account over an ngrok
  tunnel from a real phone: **inbound fully works** — real signature
  verification, merchant creation, and state transitions all ran correctly
  against Twilio's actual infrastructure. **Outbound replies are blocked**,
  not by our code but by Twilio's own account tier — see "What's real vs
  mocked" below.
- **Phase 4 (done):** the Agent Registry (`AgentRegistry` Protocol +
  `LocalRegistry`, shaped for NPCI's forthcoming UAP), detached Ed25519
  request-signature verification with Redis-backed nonce replay protection
  and clock-skew rejection, signed/TTL'd Quotes with a Redis soft stock
  hold, and **the most important function in the repo**:
  `verify_cart_within_envelope` — pure, no I/O, `now` injected, the six
  ordered envelope checks from the spec. 53 new tests (24 for the envelope
  function alone, including a Hypothesis property test that no `ALLOW`ed
  cart can ever push spend above the ceiling), all passing without a real
  Redis server (`fakeredis`).
- **Phase 5 (done):** the Reversibility Ladder (`reversibility_score_detailed`
  — five weighted factors, hard-zero on any personalised item, MIN across
  items, `f_value` cart-level) and the full **R01-R12 Policy Gate**
  (`run_gate`): signature → registration → envelope existence → envelope
  policy → quote freshness/price/stock → reversibility escalation →
  amber cooling-off → velocity → trust tier/daily cap → idempotency, first
  non-`ALLOW` wins, every outcome (`ALLOW` included) persists and ledgers,
  fail-closed on any unhandled exception. 41 new tests (21 for the
  reversibility function, 20 for the gate — one per rule firing in
  isolation, two ordering tests, two fail-closed tests). `harness/labels.json`
  — 60 hand-reasoned carts committed now, untouched until Phase 9 measures
  the formula against them — see the honesty note in ARCHITECTURE.md.
- **Phase 6 (done; deployed to Railway/Neon/Redis Cloud):**
  `core/checkout.py` orchestrates what happens after the gate decides — an
  order is created only after ALLOW or HOLD, never on BLOCK/SUBSTITUTE, and
  ESCALATE creates a payment-free `pending_approval` order. Cooling-off
  (`core/cooling_off.py`), the merchant WhatsApp approval inbox
  (`whatsapp/approvals.py` — Approve re-runs the gate from R01 against the
  *original* signed request and updates the same order in place; Decline
  closes cleanly; a timeout sweep denies after 15 minutes), the buyer's
  WhatsApp cooling-off cancel (`whatsapp/cooling_off_notify.py`, sharing
  `core/checkout.py::cancel_order` with the REST `order_undo` route so the
  two can't drift), and substitution (`core/substitution.py` — deterministic
  filter first, LLM ranks only the already-safe filtered set, cheapest-first
  on any LLM failure) are all built and tested.
  The full REST surface is live — `catalog_search`/`catalog_get`/`policy_get`
  (read-only), `quote_request` (idempotent, agent-signed), `envelope_submit`,
  `cart_confirm`, **`checkout_execute`** (the sole money-path route —
  `destructiveHint: true`), `substitution_accept`, `order_status`,
  `order_undo` — plus a Razorpay webhook (signature-verified reconciliation),
  an APScheduler sweep (cooling-off dispatch + approval timeouts), a FastMCP
  server (`mcp/server.py`, thin `httpx` wrappers over the same REST routes,
  mounted at `/mcp`, with `readOnlyHint`/`idempotentHint`/`destructiveHint`
  set per CLAUDE.md's table), and `/.well-known/agent-commerce.json`.
  Agent-signed routes carry `timestamp`/`nonce`/`signature` as
  `X-Praman-*` headers rather than body fields — putting a signature inside
  the very body it signs the hash of is self-referential, a real bug this
  build's first integration test caught before it shipped (see
  ARCHITECTURE.md). 21 new tests; 235/235 total passing.

## What's real vs mocked (so far)

- **Razorpay:** TEST MODE only, using real API credentials. Order creation,
  webhook signature verification, and refunds (`RazorpayClient.refund_payment`,
  used by cooling-off cancellation) are real. Driving an order to `captured`
  uses `FakeRazorpayClient.simulate_payment` because Razorpay's
  server-to-server (S2S) test-card API isn't enabled on this test account by
  default (confirmed: 404). See `scripts/spike_razorpay.py`.
- **Infra:** Postgres is [Neon](https://neon.tech) (free tier) and Redis is
  [Redis Cloud](https://redis.io/cloud) (free tier) rather than Railway's
  bundled addons; the API itself deploys to Railway
  (`https://praman-production.up.railway.app`, live) — real, not mocked.
  Local dev uses `docker-compose.yml` with local Postgres/Redis containers
  (also exercised for real: the full green/amber/red reversibility ladder,
  including the Twilio-signature-verified WhatsApp buyer-undo and
  merchant-approval webhooks, was run end to end against a local
  docker-compose Postgres+Redis stack before deploying, and again against
  the live Railway/Neon/Redis Cloud stack after — see ARCHITECTURE.md's
  Phase 6 section). Every automated test runs against `fakeredis` (an
  in-memory Redis emulator, not a mock) rather than a real Redis instance —
  nonce replay protection, nonce/hold TTLs, and quote stock holds are
  exercised for real, just without a server to run.
- **Catalog extraction (VLM):** live, real API calls to Gemini
  (`gemini-2.5-flash`) via `adapters/llm.py`'s `LLMClient` Protocol —
  `GeminiLLMClient` is one implementation; `FakeLLMClient` (used by every
  automated test, so CI needs no network or API key) and a clear
  `UnimplementedLLMClient` placeholder for swapping in another provider are
  the others. **Known limitation:** the free-tier Gemini API key used here
  is capped at roughly 20 requests/day for `gemini-2.5-flash` — comfortably
  enough to build both seed catalogs once, but `make ingest` against the
  full `raw/` folder can hit `429 RESOURCE_EXHAUSTED` if run repeatedly in a
  short window. The pipeline handles this correctly (each file's error is
  reported individually; the batch doesn't crash), but a live demo should
  either use a key with billing enabled or avoid re-running ingestion
  right before presenting.
- **Seed images:** `printed_price_list.png` and `handwritten_price_list.png`
  in `api/praman/seed/raw/` are synthetically generated (`scripts/gen_seed_images.py`,
  Pillow) stand-ins for a real vendor's phone photos, not actual photographs —
  disclosed here rather than passed off as real.
- **WhatsApp:** Twilio's newer self-service WhatsApp trial — needs a
  one-time join code texted from a real phone (a genuine limitation, not a
  shortcut; the standard way anyone tries Twilio's WhatsApp integration
  without a business-verified sender). Confirmed live, end to end, over an
  ngrok tunnel from a real phone:
  - **Inbound works fully and for real** — Twilio's actual signature was
    verified (not faked), a real `Merchant` was created, and the state
    machine correctly transitioned `NEW → AWAITING_MEDIA`.
  - **Outbound is blocked at the Twilio account level, not in our code.**
    This account type requires every outbound WhatsApp message — even a
    same-session reply, not just a business-initiated one outside the
    24-hour window — to carry a pre-approved Content Template `ContentSid`.
    Freeform `body` sends fail with `400 ContentSid Required`. Managing
    templates needs the Content API, which itself returned `401 This
    feature is not available on a Trial account. Please upgrade your
    account to gain access.` A generic public example template SID from
    Twilio's own quickstart docs was also tried and rejected (`400 The
    ContentSid is Invalid` — it isn't provisioned on this account). This is
    a real, verified platform constraint of this specific Twilio trial
    tier — the classic long-standing WhatsApp Sandbox (what the build spec
    assumed) allows freeform sandbox replies; this newer self-service trial
    flow does not, until the account is upgraded with billing.
  - `RealTwilioClient.send_text` is implemented correctly and would work
    immediately once the account is upgraded — nothing in the adapter needs
    to change. Every automated test uses `FakeWhatsAppClient`, so this
    limitation doesn't affect CI or the ability to demo the full state
    machine (confirming items, setting policy, reaching `LIVE`) — it's
    specifically the "watch a real WhatsApp reply arrive" moment that's
    blocked pending an account upgrade.
  - A vendor whose account is send-blocked can still upload photos and have
    them genuinely extracted into the catalog — a failed reply no longer
    stops the state machine's actual work (a real bug this discovery
    surfaced and fixed; see `ARCHITECTURE.md`). They just won't see a bot
    reply confirming it.

  The Sandbox/trial also has no native interactive buttons without an
  approved content template, so every "[Yes] [No]" / "[₹500] [₹2,000]
  [₹5,000]" in the onboarding script is sent as plain text with an explicit
  reply instruction instead. See `whatsapp/client.py`,
  `whatsapp/onboarding.py`, and `ARCHITECTURE.md`'s Phase 3 section for the
  rest of the tradeoffs.
- **`harness/labels.json`'s 60 "hand" labels:** AI-reasoned by this
  assistant, not reviewed by a human. `scripts/gen_labels.py` never calls
  `reversibility_score_detailed`, so the labels aren't secretly derived
  from the formula they're meant to validate — but treat the eventual
  Phase 9 accuracy number accordingly: it's validated against an
  AI-labeled set, a materially weaker claim than a human-reviewed one.

## Quickstart (local dev)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # fill in your own TEST MODE Razorpay keys
alembic upgrade head
uvicorn praman.main:app --reload
```

Run tests:

```bash
pytest -q
```
