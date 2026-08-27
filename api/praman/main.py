"""FastAPI application entrypoint."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from praman.api.routes_agents import router as agents_router
from praman.api.routes_approvals import router as approvals_router
from praman.api.routes_cart import router as cart_router
from praman.api.routes_catalog import router as catalog_router
from praman.api.routes_checkout import router as checkout_router
from praman.api.routes_dispute import router as dispute_router
from praman.api.routes_envelope import router as envelope_router
from praman.api.routes_events import router as events_router
from praman.api.routes_merchants import router as merchants_router
from praman.api.routes_metrics import router as metrics_router
from praman.api.routes_quotes import router as quotes_router
from praman.api.routes_razorpay_webhook import router as razorpay_webhook_router
from praman.api.routes_telegram import router as telegram_router
from praman.api.routes_well_known import router as well_known_router
from praman.api.routes_whatsapp import router as whatsapp_router
from praman.config import get_settings
from praman.db import init_models
from praman.mcp.server import mcp
from praman.scheduler import create_scheduler

mcp_app = mcp.http_app(path="/mcp")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    async with AsyncExitStack() as stack:
        if get_settings().demo_mode:
            # Convenience for local/demo runs; real deployments migrate via Alembic.
            await init_models()

        # FastMCP's own ASGI app needs its session-manager lifespan running
        # for `/mcp` to work; nest it inside ours rather than running two
        # independent lifespans FastAPI doesn't know how to combine.
        await stack.enter_async_context(mcp_app.router.lifespan_context(app))

        scheduler = create_scheduler()
        scheduler.start()
        stack.callback(scheduler.shutdown, wait=False)

        yield


app = FastAPI(title="PRAMAN API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(events_router, prefix="/api")
app.include_router(catalog_router)
app.include_router(agents_router)
app.include_router(merchants_router)
app.include_router(envelope_router)
app.include_router(quotes_router)
app.include_router(cart_router)
app.include_router(checkout_router)
app.include_router(approvals_router)
app.include_router(dispute_router)
app.include_router(metrics_router)
app.include_router(razorpay_webhook_router)
app.include_router(well_known_router)
app.include_router(whatsapp_router)
app.include_router(telegram_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


# `mcp_app` already serves at its own internal "/mcp" path (set via
# `mcp.http_app(path="/mcp")`); mount it at the root so the effective route
# is "/mcp", not "/mcp/mcp". Registered last — Starlette checks routes in
# registration order, and a root mount registered earlier would swallow
# every path above it (including "/health") before they ever matched.
app.mount("/", mcp_app)
