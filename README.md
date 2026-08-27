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
