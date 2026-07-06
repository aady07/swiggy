from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Query, Request, Response

from backend.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/whatsapp", tags=["whatsapp"])

_bot = None


def init_whatsapp_bot(bot) -> None:
    global _bot
    _bot = bot


@router.get("/webhook")
async def verify_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
):
    if hub_mode == "subscribe" and hub_verify_token == settings.whatsapp_verify_token:
        return Response(content=hub_challenge or "", media_type="text/plain")
    raise HTTPException(status_code=403, detail="Verification failed")


@router.post("/webhook")
async def receive_webhook(request: Request):
    payload = await request.json()
    fields = []
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            fields.append(change.get("field", "?"))
    logger.info("WhatsApp webhook POST fields=%s", fields or "none")

    if not _bot or not _bot.enabled:
        logger.warning("WhatsApp webhook ignored — bot not enabled (check WHATSAPP_* in .env)")
        return {"ok": True}

    asyncio.create_task(_process_webhook(payload))
    return {"ok": True}


async def _process_webhook(payload: dict) -> None:
    try:
        await _bot.handle_incoming(payload)
    except Exception:
        logger.exception("WhatsApp webhook processing failed")
