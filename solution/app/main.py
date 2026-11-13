from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .config import AppConfig
from .database import Database
from .dispatcher import Dispatcher
from .provider import ProviderClient
from .repository import Repository
from .webhooks import verify_signature

logger = logging.getLogger("campaign_dispatcher")


def create_app(
    *,
    config: AppConfig,
    database_url: str,
    provider_url: str,
    callback_url: str,
    output_path: str | Path,
    database_schema: str | None = None,
) -> FastAPI:
    database = Database(database_url, schema=database_schema)
    try:
        repository = Repository(database, config)
        repository.initialize()
        dispatcher_lock_acquired = database.try_acquire_dispatcher_lock()
        provider = ProviderClient(provider_url)
    except Exception:
        database.close()
        raise
    dispatcher = Dispatcher(
        config=config,
        repository=repository,
        provider=provider,
        callback_url=callback_url,
        output_path=str(output_path),
    )
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if dispatcher_lock_acquired:
            app.state.dispatcher_task = asyncio.create_task(
                run_dispatcher(), name="campaign-dispatcher"
            )
        else:
            # Another process already owns the scheduler lock for this DB schema.
            # This process can still serve authenticated webhook deliveries.
            app.state.dispatcher_task = None
            logger.warning("dispatcher scheduler lock is held by another process")
        try:
            yield
        finally:
            dispatcher.stop()
            task = app.state.dispatcher_task
            if task is not None and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            await provider.close()
            await asyncio.to_thread(database.close)

    app = FastAPI(title="Durable Campaign Dispatcher", version="1.0.0", lifespan=lifespan)
    app.state.repository = repository
    app.state.provider = provider
    app.state.dispatcher = dispatcher
    app.state.dispatcher_task = None
    app.state.dispatcher_error = None

    async def run_dispatcher() -> None:
        try:
            await dispatcher.run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # surfaced through /status and process logs
            app.state.dispatcher_error = f"{type(exc).__name__}: {exc}"
            logger.exception("dispatcher stopped after an unrecoverable error")

    @app.get("/health")
    async def health() -> JSONResponse:
        try:
            await asyncio.to_thread(database.health_check)
        except Exception as exc:
            logger.warning("database health check failed: %s", exc)
            return JSONResponse(status_code=503, content={"status": "unavailable", "database": "down"})
        return JSONResponse(status_code=200, content={"status": "ok", "database": "up"})

    @app.get("/status")
    async def status() -> dict[str, Any]:
        try:
            clock = await provider.get_clock()
            snapshot = await asyncio.to_thread(repository.update_status_snapshot, clock.now)
        except Exception as exc:
            snapshot = {"clock_error": str(exc)}
        task = app.state.dispatcher_task
        snapshot.update(
            {
                "dispatcher_done": bool(task and task.done()),
                "dispatcher_error": app.state.dispatcher_error,
                "scheduler_lock_acquired": dispatcher_lock_acquired,
            }
        )
        return snapshot

    @app.post("/webhooks/telephony")
    async def receive_telephony_webhook(request: Request) -> JSONResponse:
        raw_body = await request.body()
        supplied = request.headers.get("X-AU-Signature")
        if not verify_signature(raw_body, config.webhook_secret, supplied):
            raise HTTPException(status_code=401, detail="Invalid webhook signature")
        try:
            result = json.loads(raw_body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise HTTPException(status_code=400, detail="Invalid JSON webhook")
        required = {
            "event", "request_id", "call_sid", "tenant_id", "campaign_id", "lead_id",
            "phone", "status", "ended_at",
        }
        if not isinstance(result, dict) or not required.issubset(result):
            raise HTTPException(status_code=400, detail="Webhook is missing required fields")
        if any(not isinstance(result[field], str) or not result[field].strip() for field in required):
            raise HTTPException(status_code=400, detail="Webhook contains invalid required fields")
        if result["event"] != "call.completed":
            raise HTTPException(status_code=400, detail="Unsupported webhook event")
        if result["status"] not in {"completed", "no_answer", "busy", "failed"}:
            raise HTTPException(status_code=400, detail="Unsupported provider call status")
        delivery_id = request.headers.get("X-AU-Delivery-Id") or hashlib.sha256(raw_body).hexdigest()
        try:
            accepted = await asyncio.to_thread(
                repository.apply_webhook,
                delivery_id=delivery_id,
                raw_body=raw_body,
                result=result,
                received_at=datetime.now().astimezone(),
            )
        except KeyError as exc:
            # A valid event can race the HTTP response. Ask the provider to redeliver;
            # the durable request row should already exist in a correct dispatcher run.
            logger.warning("webhook arrived for an unknown request: %s", exc)
            raise HTTPException(status_code=503, detail="Unknown request_id; retry delivery")
        except (ValueError, TypeError) as exc:
            logger.error("invalid provider webhook: %s", exc)
            raise HTTPException(status_code=400, detail="Invalid provider webhook")
        return JSONResponse(
            status_code=200,
            content={"received": True, "duplicate": not accepted},
        )

    return app
