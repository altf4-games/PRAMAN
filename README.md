# PRAMAN

A kirana owner sends photos of his price list over WhatsApp; minutes later
his shop is a signed, agent-readable storefront that AI shopping agents can
transact with — gated by a reversibility-scaled policy engine and backed by
a hash-chained, exportable dispute ledger.

Full build spec: [CLAUDE.md](CLAUDE.md). This README fills in as each phase
lands (see §7 of the spec for its eventual final structure); for now it
tracks what's built and what's still mocked.

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

## What's real vs mocked (so far)

- **Razorpay:** TEST MODE only, using real API credentials. Order creation
  and webhook signature verification are real. Driving an order to
  `captured` uses `FakeRazorpayClient.simulate_payment` because Razorpay's
  server-to-server (S2S) test-card API isn't enabled on this test account by
  default (confirmed: 404). See `scripts/spike_razorpay.py`.
- **Infra:** Postgres is [Neon](https://neon.tech) (free tier) and Redis is
  [Redis Cloud](https://redis.io/cloud) (free tier) rather than Railway's
  bundled addons; the API itself still deploys to Railway. Local dev uses
  `docker-compose.yml` with local Postgres/Redis containers.
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
