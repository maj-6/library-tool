"""Optional authenticated drain API for platforms without native jobs."""

from __future__ import annotations

import asyncio
import hmac
import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from . import __version__
from .settings import ConfigurationError, Settings
from .store import StoreError
from .worker import run_batch


class DrainRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    limit: int = Field(default=10, ge=1, le=100)


def create_app(settings: Settings | None = None) -> FastAPI:
    configured = settings
    drain_lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal configured
        if configured is None:
            configured = Settings.from_env()
        app.state.settings = configured
        yield

    app = FastAPI(
        title="World Herb Library image processor",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "whl-image-processor", "version": __version__}

    @app.get("/readyz")
    async def ready() -> dict[str, str]:
        if configured is None:
            raise HTTPException(status_code=503, detail="configuration not loaded")
        return {"status": "ready"}

    @app.post("/v1/drain")
    async def drain(
        request: DrainRequest,
        x_image_processor_token: str = Header(default=""),
    ) -> dict[str, int]:
        if configured is None:
            raise HTTPException(status_code=503, detail="configuration not loaded")
        expected = configured.admin_token
        if not expected:
            raise HTTPException(status_code=503, detail="drain API is disabled")
        if not hmac.compare_digest(x_image_processor_token, expected):
            raise HTTPException(status_code=401, detail="invalid processor token")
        if drain_lock.locked():
            raise HTTPException(status_code=409, detail="a drain is already running")
        async with drain_lock:
            try:
                summary = await asyncio.to_thread(run_batch, configured, request.limit)
            except (StoreError, ValueError) as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
        return summary.as_dict()

    return app


app = create_app()


def main() -> None:
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port, proxy_headers=True, access_log=True)


if __name__ == "__main__":  # pragma: no cover
    try:
        main()
    except ConfigurationError as exc:
        raise SystemExit(str(exc)) from exc
