# PRAMAN

**Evidence by construction for agentic commerce.** A merchant-side authorization layer
that lets AI shopping agents transact on Razorpay rails — and lets the merchant prove
afterwards exactly what the agent was allowed to do.

[![Demo](https://img.shields.io/badge/demo-praman--jet.vercel.app-141A22)](https://praman-jet.vercel.app)
[![API](https://img.shields.io/badge/API-praman--production.up.railway.app-141A22)](https://praman-production.up.railway.app)


**In 60 seconds:** [`/live`](https://praman-jet.vercel.app/live) runs a real MCP agent
against a seeded shop, streaming gate decisions into a hash-chained ledger. Break a ledger
row from that page and watch the chain proof fail.

---

### Contents

1. [The problem](#1-the-problem)
2. [What it does](#2-what-it-does)
3. [Quickstart](#3-quickstart)
4. [Results](#4-results)
5. [The Reversibility Ladder](#5-the-reversibility-ladder)
6. [Threat model](#6-threat-model)
7. [Design decisions](#7-design-decisions)
8. [What's real vs mocked](#8-whats-real-vs-mocked)
9. [Limitations](#9-limitations)
10. [Repo map](#10-repo-map)

---

## 1. The problem

AI shopping agents can now pay. Razorpay has said publicly that agentic shopping doesn't
rewrite commercial liability — when an agent buys the wrong thing, **the merchant handles
the dispute and the refund**. Nobody made the merchant able to *prove* what the agent was
authorized to do. A kirana or jewellery merchant has no artifact that an arbitrator,
platform, or customer will accept as "I never approved this."

PRAMAN gives that merchant three things:

- a **signed storefront** any agent can transact against
- a **policy gate** that scales autonomy inversely with how reversible a purchase is
- a **hash-chained ledger** that exports as a one-click dispute pack — evidence captured
  at authorization time, not reconstructed afterwards

## 2. What it does

A kirana owner sends photos of a handwritten price list over WhatsApp. Minutes later the
shop is a signed, agent-readable storefront. An agent registers, receives a one-time-
consent **Intent Envelope** (modelled on UPI Reserve Pay, not a card-on-file), requests
signed quotes, and checks out through a 12-rule gate that **never runs an LLM in the money
path**.

A **Reversibility Ladder** score — five deterministic weighted factors — decides how much
friction the purchase gets:

- 🟢 **Green** — full autonomy, zero friction
- 🟡 **Amber** — buyer cooling-off window, one-tap undo
- 🔴 **Red** — merchant Approve/Decline required

Every decision, ALLOW included, is appended to a ledger that verifies end to end.

```
WhatsApp photos → VLM extraction → confidence-gated catalog → LIVE storefront
                                                                     │
Agent → Registry → Envelope → Quote → Cart → ┌── R01–R12 Gate ──┐   │
                                             │ Reversibility     │◄──┘
                                             │ Ladder            │
                                             └───┬───────────┬───┘
                                    ALLOW/HOLD ──┘           └── ESCALATE
                                         │                        │
                            Razorpay TEST MODE order      Merchant Approve/Decline
                                         │                        │
                                         ▼                   gate re-runs from R01
                            Hash-chained ledger ──► Dispute Pack (JSON/PDF)
```

Full system design and every design decision's tradeoff: [ARCHITECTURE.md](ARCHITECTURE.md).

## 3. Quickstart

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env          # your own TEST MODE Razorpay keys
alembic upgrade head
uvicorn praman.main:app --reload

pytest -q                     # 312 tests
python -m harness.run         # 200 sessions, two arms → harness/RESULTS.md
```

A real external MCP client buying against the deployed storefront — the same signed
protocol any third-party agent must use, not a bespoke integration:

```bash
python scripts/mcp_agent_demo.py --merchant-id <id> --goal "1kg toor dal" --budget-paise 50000
```

It prints a `/pay/{order_id}` link when checkout needs a real Razorpay Checkout.js
round-trip to capture payment, and a `/dispute/{order_id}` link once the order exists.

Frontend setup in [`web/README.md`](web/README.md).

## 4. Results

200 sessions across seven classes (benign, stale-quote race, envelope escape, prompt
injection, replay, identity spoof), run through **Arm A** (naive: unsigned reads, no
envelope, no gate) and **Arm B** (full R01–R12 gate) against the same seeded catalog.
Methodology and caveats: [harness/RESULTS.md](harness/RESULTS.md).

| Metric | Arm A (naive) | Arm B (PRAMAN) |
|---|---|---|
| Net GMV captured | ₹18,63,159 | ₹6,93,645 |
| Bad-transaction value exposed | ₹10,02,514 | **₹0** |
| Bad transactions stopped | 0 / 65 | 65 / 65 |
| Legitimate GMV **wrongly blocked** | — | **₹0** |
| Legitimate GMV **pending human approval** at measurement | — | ₹1,67,000 (2 sessions) |
| Gate precision / recall (adversarial split) | — | 100% / 100% |
| Injection-invariance | — | 15/15 |
| Dispute-pack completeness | — | 132/132 |
| Gate latency p50 / p95 | — | 3.60ms / 3.76ms |
| Reversibility band accuracy | — | 62/64 (96.9%) |

**Read the perfect scores narrowly.** The adversarial sessions are generated by this
repository, so each attack class is constructed to be exactly what a rule checks for — a
replay session reuses the same nonce on both attempts by construction, and R12 catches it
by construction. **This measures correct implementation against a known threat model, not
detection capability against novel attacks.** A real evaluation would need adversarial
sessions authored independently of the gate. Treating 100%/100% as evidence of robustness
would be wrong, and it isn't claimed.

**The "pending approval" number is two real carts, not a category of friction.** Arm B
captured no legitimate GMV wrongly — but two benign red-band jewellery carts (₹85,000 and
₹82,000) correctly escalated under R08 and were still `pending_approval` when the harness
measured GMV. The harness simulates a buyer cancelling a *held* amber order afterward, but
never simulates a merchant actually resolving an *escalated* red one — so those two carts'
value is never added back in, even though nothing was blocked. That's the Reversibility
Ladder working as designed, not an error, but it's also not ₹0 sitting in the balance at
that moment, so it gets its own line rather than folding into "wrongly blocked."

**A number that used to be embarrassing.** Band accuracy started at **39/64 (60.9%)** — no
grocery SKU could score green under the original formula, because it used
`return_window_days` as a proxy for reversibility and perishables structurally can't have
long return windows. Researching why real merchants handle cheap-item returns exposed the
actual bug: *return window isn't reversibility*. The fix (`f_unwind`, §5) was re-measured
against the **same 64 labels, unchanged** → 62/64 (96.9%). The two remaining misses are
large-quantity grocery carts that correctly no longer qualify for cheap-item leniency. The
point isn't that the number rose — it's that it rose without touching the labels.

## 5. The Reversibility Ladder

`core/reversibility.py::reversibility_score_detailed` — pure, deterministic, `[0,1]`, no
I/O, no LLM. Personalised/bespoke items are a hard zero. Multi-item carts take the
**minimum** per factor: a cart is only as reversible as its least reversible item.

| Factor | Weight | Measures |
|---|---|---|
| `f_unwind` | 0.35 | perishable/consumable/digital: `1 − min(total / ₹1,000, 1)` — returnless-refund economics. durable/service/bespoke: `min(return_window_days / 14, 1)` |
| `f_class` | 0.25 | perishable .95 · consumable .90 · digital .70 · durable .55 · service .35 · bespoke .05 |
| `f_speed` | 0.15 | `1 − min(fulfilment_hours / 336, 1)` |
| `f_restock` | 0.10 | `1 − min(restocking_cost_pct / 0.30, 1)` |
| `f_value` | 0.15 | `1 − min(total / envelope_ceiling, 1)` (cart-level) |

**Why `f_unwind` branches.** Retailers routinely refund a cheap perishable without asking
for it back — processing a real return commonly costs 20–65% of a cheap item's value once
shipping, labour and restocking are counted. Return window measures whether a *formal*
return exists, not whether a purchase is hard to unwind. Durable goods don't get that
leniency: a real return actually happens there, so the window really is the signal.

- `≥0.75` → 🟢 **green** (full autonomy)
- `≥0.40` → 🟡 **amber** (dispatch withheld, one-tap undo)
- below that → 🔴 **red** (merchant approval required)

The 64 hand labels in `harness/labels.json` were committed **before** the harness ever ran
against them; weights were never adjusted after seeing accuracy.

## 6. Threat model

| Attack | Defense |
|---|---|
| Prompt injection in catalog text | Catalog text is data; the gate never reads free text. Injection-invariance asserted (15/15 byte-identical) |
| Prompt injection via WhatsApp | Vendor messages route through a state machine, never the gate. Media goes to extraction only |
| Quote replay | Single-use nonce + TTL + `consumed_at` |
| Price/stock drift at checkout | R05/R06 hard block |
| Cart tampering post-signature | Signature over JCS canonical form |
| Envelope escape | `verify_cart_within_envelope()` (R04) + Hypothesis property test: no ALLOWed cart can push spend past the ceiling |
| Irreversibility exploitation | Reversibility Ladder — R08/R09 |
| Agent identity spoofing | Registry + detached Ed25519 signature + nonce/clock-skew rejection |
| Approval spoofing | Channel signature verification + single-use token bound to `cart_id` |
| Runaway retry loop | Idempotency + rolling-window velocity (R10/R12) |
| Hallucination dispute | Dispute Pack — evidence captured at authorization, merchant-signed |
| Salami-slicing the envelope | Running `spent_paise` + rolling velocity window |
| Bad extraction reaching agents | Confidence threshold + `needs_review` gate |
| Client claims an unverified payment succeeded | `confirm_real_payment` verifies Checkout.js signature and re-fetches the payment before dispatch — a browser callback is never trusted alone |

## 7. Design decisions

Full log in [ARCHITECTURE.md](ARCHITECTURE.md). The load-bearing ones:

- **No LLM in `gate.py`, `envelope.py`, `reversibility.py`.** The LLM only extracts catalog
  data and ranks substitution candidates (post-filter, cheapest-first on failure). A design
  that seemed to need a model call in the money path was read as a wrong design.
- **Weights were never tuned after seeing accuracy — but one factor's *definition* was
  corrected once, for cause.** See §4. The labels never moved, the reasoning came from
  external research before re-running, and the conservative branch was kept where the
  original signal was correct.
- **Real Razorpay Orders; a real Payment needs a browser.** Razorpay's S2S test-card capture
  is opt-in and returns `404` on this account. Rather than fabricate a payment under a real
  order id (an earlier version of this code did exactly that), unusable orders stay
  `awaiting_payment` and `/live` completes them through Razorpay's actual Checkout.js
  widget, verified server-side before dispatch.
- **Agent Registry is a `Protocol`, not an integration.** Shaped for NPCI's forthcoming UAP.
  When UAP ships it becomes a second implementation; nothing above it changes.
- **Intent Envelope follows UPI Reserve Pay, not AP2's card-centric flow** — one-time
  consent, a ceiling, instant revocability — while keeping AP2 vocabulary in code.

## 8. What's real vs mocked

- **Razorpay** — TEST MODE, real credentials. Order creation, webhook signature
  verification, and refunds are real. Capture has two real paths: S2S auto-capture where
  enabled (proven in `scripts/spike_razorpay.py`), otherwise a genuine Checkout.js
  round-trip producing a real Payment in the test dashboard. Use Razorpay's **domestic**
  Mastercard test card `5267 3181 8797 5449` — international cards are disabled on this
  account, so `4111 1111 1111 1111` fails (a live account setting, not a bug).
  `FakeRazorpayClient` backs all tests and the harness, so CI needs neither Razorpay nor a
  browser.
- **Infra** — Neon Postgres + Redis Cloud (free tiers), API on Railway. Real. Tests run
  against `fakeredis` (an in-memory emulator, not a mock): nonce replay, TTLs, and stock
  holds are genuinely exercised.
- **Catalog extraction** — live Gemini (`gemini-2.5-flash`) calls. Free-tier key caps around
  20 req/day, so repeated `make ingest` runs can hit `429`. Degrades per-file, not as a crash.
- **Seed images** — synthetically generated (`scripts/gen_seed_images.py`) stand-ins for real
  vendor photos. Disclosed, not passed off as photographs.
- **WhatsApp / Telegram** — Twilio's trial delivers inbound webhooks for real (real signature
  verification, real state transitions, confirmed live from a phone), but the *account tier*
  blocks inbound media fetch (`401`) and outbound freeform replies (`400 ContentSid
  Required`). Rather than fake those, both API and scheduler dispatch through
  `MultiChannelClient`, routing by `telegram:`/`whatsapp:` prefix behind one Protocol — so no
  business logic branches on channel. Merchant approvals and buyer undo are demoed over
  Telegram for that reason, not because the WhatsApp path doesn't work.
  `RealTwilioClient.send_text` works as written and starts functioning on account upgrade.
- **`harness/labels.json`** — AI-reasoned, not human-reviewed. `scripts/gen_labels.py` never
  calls `reversibility_score_detailed`, so labels aren't circularly derived from the formula
  they validate — but §4's accuracy is validated against an AI-labeled set, a weaker claim
  than human review.

## 9. Limitations

- **The harness measures implementation correctness, not robustness** (§4). Independently
  authored adversarial sessions are the missing piece.
- The 2 residual band misses are large-quantity grocery carts crossing a flat ₹1,000
  unwind-free ceiling. A better model would scale that per category or merchant margin
  rather than one constant for every kirana SKU.
- Substitution *acceptance* (fresh quote → second `checkout_execute` after R07) isn't in the
  200-session harness — covered by `core/substitution.py` unit tests only.
- Twilio outbound/media is a trial-tier block (§8), not a code limitation.
- Fully autonomous checkout with a genuinely captured payment needs S2S enabled or a method
  without a hosted card form (UPI AutoPay/Reserve Pay in test mode).
- The Agent Registry is a local Protocol implementation; NPCI's UAP hasn't shipped.

## 10. Repo map

```
api/praman/
  core/         gate.py · envelope.py · reversibility.py · checkout.py · ledger.py — the money path
  adapters/     razorpay_client.py · llm.py — Real/Fake pairs behind a Protocol
  ingest/       extract.py (VLM) · normalise.py (deterministic)
  whatsapp/     onboarding · approvals · cooling_off_notify · client (MultiChannel) · telegram_client
  api/          routes_*.py — REST surface
  mcp/          server.py — thin wrappers over the same routes
  crypto/       canonical.py (JCS) · keys.py (Ed25519) · did.py
web/app/        / · /onboard · /live · /catalog · /approvals · /dispute/[id] · /pay/[id] · /metrics
harness/        simulator · sessions · injection_corpus · report · run
scripts/        spike_razorpay.py · gen_labels.py · gen_seed_images.py · mcp_agent_demo.py
tests/          312 tests, heaviest on gate/envelope/reversibility/checkout
ARCHITECTURE.md full design + every decision's tradeoff
```
