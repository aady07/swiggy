from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from backend.admin_auth import extract_bearer_token, router as admin_router, verify_session_token

from backend.bot_copy import format_error_no_address, format_error_no_swiggy
from backend.bot_images import BRAND_PICK_MAX, build_pick_collage
from backend.checkout import run_checkout, run_dummy_checkout
from backend.config import gemini_configured, settings
from backend.db import db
from backend.extractor import ExtractionTrace, extract_items_with_trace, format_extraction_trace, format_search_trace
from backend.mcp_client import MCPError, mcp_client
from backend.models import (
    AssistantChatRequest,
    CheckoutRequest,
    DummyCheckoutRequest,
    as_clarification_option,
    ClarificationOption,
    ExcludeDraftRequest,
    RemoveDraftRequest,
    ResolveDraftRequest,
    SendMessageRequest,
    SetAddressRequest,
    clarification_option_dict,
)
from backend.oauth import auth_status, get_valid_access_token, router as auth_router
from backend.assistants import format_assistants_menu, list_assistants, run_assistant_chat
from backend.cart_ops import (
    format_cart_text,
    get_cart_view,
    remove_cart_item,
    remove_cart_line,
)
from backend.resolver import (
    fetch_order_preferences,
    process_extracted_item,
    resolve_draft_selection,
    search_response_hint,
)
from backend.household_notify import broadcast_add
from backend.models import CheckoutResult
from backend.order_analytics import compute_order_analytics
from backend.telegram_bot import TelegramBot
from backend.whatsapp_bot import WhatsAppBot
from backend.whatsapp_routes import init_whatsapp_bot, router as whatsapp_router

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
telegram_bot: TelegramBot | None = None
whatsapp_bot: WhatsAppBot | None = None


class ConnectionManager:
    def __init__(self) -> None:
        self.active: list[WebSocket] = []

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self.active.append(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        if websocket in self.active:
            self.active.remove(websocket)

    async def broadcast(self, event_type: str, payload: dict[str, Any]) -> None:
        message = json.dumps({"type": event_type, "payload": payload})
        dead: list[WebSocket] = []
        for ws in self.active:
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


manager = ConnectionManager()

app = FastAPI(title="Household Grocery Merge Agent")


class AdminAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.method == "OPTIONS":
            return await call_next(request)
        path = request.url.path
        if not path.startswith("/api/"):
            return await call_next(request)
        if path == "/api/admin/login":
            return await call_next(request)
        if path.startswith("/api/whatsapp/"):
            return await call_next(request)
        if path.startswith("/api/collage/"):
            return await call_next(request)
        token = extract_bearer_token(request.headers.get("Authorization"))
        if not verify_session_token(token):
            return JSONResponse({"detail": "Not authenticated"}, status_code=401)
        return await call_next(request)


app.add_middleware(AdminAuthMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(whatsapp_router)


@app.on_event("startup")
async def startup() -> None:
    global telegram_bot, whatsapp_bot
    await db.init()
    if not gemini_configured():
        print(
            "\n⚠️  GEMINI_API_KEY not loaded — Gemini will NOT run (using dumb fallback).\n"
            "   Edit .env, paste your key, SAVE the file, restart uvicorn.\n"
        )
    else:
        print("✓ Gemini API key loaded")

    async def broadcast_checkout_result(result: CheckoutResult) -> None:
        await manager.broadcast(
            "checkout_result",
            {
                "success": True,
                "swiggy_message": result.swiggy_message,
                "settlement": [s.model_dump() for s in result.settlement],
                "total": result.total,
                "order_id": result.order_id,
                "order_type": result.order_type,
            },
        )

    telegram_bot = TelegramBot(
        settings.telegram_bot_token,
        process_message,
        broadcast_draft_fn=broadcast_draft,
        checkout_broadcast_fn=broadcast_checkout_result,
    )
    whatsapp_bot = WhatsAppBot(
        process_message,
        broadcast_draft_fn=broadcast_draft,
        telegram_bot=telegram_bot,
        checkout_broadcast_fn=broadcast_checkout_result,
    )
    telegram_bot.whatsapp_bot = whatsapp_bot
    init_whatsapp_bot(whatsapp_bot)
    if telegram_bot.enabled:
        telegram_bot.start_background()
        print("✓ Telegram bot polling started")
    else:
        print("ℹ️  TELEGRAM_BOT_TOKEN not set — Telegram bot disabled")

    if whatsapp_bot.enabled:
        print("✓ WhatsApp webhook ready at /api/whatsapp/webhook")
        if await whatsapp_bot.validate_token():
            print("✓ WhatsApp access token validated")
        else:
            print(
                "⚠️  WhatsApp token invalid — outbound messages will fail (error 190).\n"
                "   Meta Developer Console → WhatsApp → API Setup → generate token,\n"
                "   paste WHATSAPP_ACCESS_TOKEN in server .env, restart swiggy."
            )
    else:
        print("ℹ️  WhatsApp not configured — set WHATSAPP_* in .env")
        missing = []
        if not settings.whatsapp_access_token.strip():
            missing.append("WHATSAPP_ACCESS_TOKEN")
        if not settings.whatsapp_phone_number_id.strip():
            missing.append("WHATSAPP_PHONE_NUMBER_ID")
        if not settings.whatsapp_verify_token.strip():
            missing.append("WHATSAPP_VERIFY_TOKEN")
        if missing:
            print(f"   Missing: {', '.join(missing)}")


@app.on_event("shutdown")
async def shutdown() -> None:
    if telegram_bot:
        telegram_bot.stop()


async def broadcast_auth_status() -> None:
    status = await auth_status()
    await manager.broadcast("auth_status", status)


async def broadcast_draft() -> None:
    snapshot = await get_cart_view(settings.household_id)
    await manager.broadcast("draft_update", snapshot)


async def notify_household_added(
    sender: str, result: dict[str, Any], *, source: str = "web"
) -> None:
    added = result.get("added") or []
    if not added:
        return
    await broadcast_add(
        sender,
        added,
        source=source,
        whatsapp_bot=whatsapp_bot,
        telegram_bot=telegram_bot,
    )


async def process_message(
    sender: str,
    text: str,
    telegram_chat_id: int | None = None,
    whatsapp_phone: str | None = None,
    channel: str = "web",
    *,
    trace: ExtractionTrace | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "error": None,
        "draft": None,
        "polls": [],
        "recommendations": [],
        "added": [],
        "added_details": [],
        "understood": [],
        "failures": [],
    }
    if telegram_chat_id is not None:
        await db.set_setting("telegram_chat_id", str(telegram_chat_id))
    if whatsapp_phone is not None:
        await db.set_setting("whatsapp_phone", whatsapp_phone)
    msg = await db.add_message(settings.household_id, sender, text, channel=channel)
    await manager.broadcast(
        "message",
        {
            "id": msg["id"],
            "sender": msg["sender"],
            "text": msg["text"],
            "channel": msg.get("channel") or channel,
            "created_at": msg["created_at"],
        },
    )

    address_id = await db.get_setting("address_id")
    if not address_id:
        result["error"] = format_error_no_address(
            f"{settings.public_base_url.rstrip('/')}/"
        )
        await manager.broadcast(
            "error",
            {"message": "Select a delivery address before adding items."},
        )
        return result

    token = await get_valid_access_token()
    if not token:
        result["error"] = format_error_no_swiggy(
            f"{settings.public_base_url.rstrip('/')}/auth/login"
        )
        await manager.broadcast(
            "error",
            {"message": "Connect Swiggy account first (Login button)."},
        )
        return result

    try:
        if trace is None:
            trace = await extract_items_with_trace(text, sender)
        result["understood"] = [item.raw_query for item in trace.items]
        if settings.debug_pipeline:
            await db.add_message(
                settings.household_id,
                "Debug",
                format_extraction_trace(trace),
            )
            await manager.broadcast(
                "message",
                {
                    "sender": "Debug",
                    "text": format_extraction_trace(trace),
                    "created_at": None,
                },
            )

        preferences = await fetch_order_preferences(
            mcp_client, settings.household_id, address_id
        )

        for item in trace.items:
            _draft, attempts, search_data, poll_req = await process_extracted_item(
                mcp_client,
                settings.household_id,
                address_id,
                item,
                preferences=preferences,
            )
            if poll_req:
                result["polls"].append(poll_req)
            if _draft.status == "resolved":
                result["added"].append(_draft.raw_query)
                note = _draft.note or ""
                detail = {
                    "raw_query": _draft.raw_query,
                    "resolved_name": _draft.resolved_name,
                    "usual": "past order" in note.lower(),
                    "ai": "AI" in note,
                }
                result["added_details"].append(detail)
                if _draft.auto_picked and _draft.resolved_name:
                    result.setdefault("auto_picked_details", []).append(
                        {
                            "raw_query": _draft.raw_query,
                            "resolved_name": _draft.resolved_name,
                            "note": note,
                        }
                    )
            elif _draft.status in ("needs_clarification", "out_of_stock") and (
                not _draft.clarification_options
            ):
                result["failures"].append(
                    {
                        "raw_query": _draft.raw_query,
                        "note": _draft.note or "No Instamart match — try a clearer product name.",
                    }
                )
            if _draft.status == "needs_clarification" and _draft.clarification_options:
                result.setdefault("recommendations", []).append(
                    {
                        "draft_item_id": _draft.id,
                        "raw_query": _draft.raw_query,
                        "note": _draft.note,
                        "options": [
                            clarification_option_dict(o)
                            for o in _draft.clarification_options
                        ],
                    }
                )
            if settings.debug_pipeline:
                hint = search_response_hint(search_data)
                search_trace = format_search_trace(item.raw_query, attempts, hint)
                await db.add_message(settings.household_id, "Debug", search_trace)
                await manager.broadcast(
                    "message",
                    {"sender": "Debug", "text": search_trace, "created_at": None},
                )
        await broadcast_draft()
        result["draft"] = await get_cart_view(settings.household_id)
        return result
    except MCPError as exc:
        result["error"] = str(exc)
        await manager.broadcast("error", {"message": str(exc)})
        if exc.status_code == 401:
            await broadcast_auth_status()
        return result
    except Exception as exc:
        result["error"] = f"Processing failed: {exc}"
        await manager.broadcast("error", {"message": result["error"]})
        return result


@app.get("/")
async def index():
    return FileResponse(FRONTEND_DIR / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/static/styles.css")
async def styles_css():
    path = FRONTEND_DIR / "styles.css"
    if not path.exists():
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="text/css", headers={"Cache-Control": "no-cache"})


@app.get("/static/app.js")
async def app_js():
    path = FRONTEND_DIR / "app.js"
    if not path.exists():
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="application/javascript", headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


@app.get("/api/messages")
async def list_messages():
    return await db.list_messages(settings.household_id)


@app.post("/api/messages")
async def send_message(body: SendMessageRequest):
    if body.sender not in settings.senders:
        raise HTTPException(status_code=400, detail="Invalid sender")
    if not body.text.strip():
        raise HTTPException(status_code=400, detail="Empty message")
    result = await process_message(body.sender, body.text.strip(), channel="web")
    await notify_household_added(body.sender, result, source="web")
    return {"ok": True}


@app.post("/api/clear")
async def clear_session(body: dict | None = None):
    """Clear chat messages and/or draft cart."""
    body = body or {}
    clear_chat = body.get("chat", True)
    clear_draft_flag = body.get("draft", False)
    if clear_chat:
        await db.clear_messages(settings.household_id)
        await manager.broadcast("chat_cleared", {})
    if clear_draft_flag:
        await db.clear_draft(settings.household_id)
        await broadcast_draft()
    return {"ok": True, "chat_cleared": clear_chat, "draft_cleared": clear_draft_flag}


@app.get("/api/draft")
async def get_draft():
    return await get_cart_view(settings.household_id)


@app.get("/api/collage/{draft_item_id}.png")
async def product_collage(draft_item_id: int):
    """Public compact product grid for WhatsApp/Telegram brand pick (no auth)."""
    item = await db.get_draft_item(draft_item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Draft item not found")
    options = [
        as_clarification_option(o)
        for o in (item.clarification_options + item.alternatives)[:BRAND_PICK_MAX]
    ]
    if not options:
        raise HTTPException(status_code=404, detail="No options")
    try:
        png = await build_pick_collage(options)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return Response(
        content=png,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=300"},
    )


@app.post("/api/draft/remove")
async def remove_draft_item(body: RemoveDraftRequest):
    try:
        if body.line_number is not None:
            result = await remove_cart_line(settings.household_id, body.line_number)
        elif body.draft_item_id is not None:
            result = await remove_cart_item(settings.household_id, body.draft_item_id)
        else:
            raise HTTPException(
                status_code=400, detail="Provide line_number or draft_item_id"
            )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await broadcast_draft()
    return result


@app.get("/api/assistants")
async def api_list_assistants():
    return {"assistants": list_assistants(), "menu": format_assistants_menu()}


@app.post("/api/assistants/chat")
async def api_assistant_chat(body: AssistantChatRequest):
    if not body.message.strip():
        raise HTTPException(status_code=400, detail="Empty message")
    try:
        reply = await run_assistant_chat(
            body.assistant_id, body.message.strip(), settings.household_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"assistant_id": body.assistant_id, "reply": reply}


@app.post("/api/draft/resolve")
async def resolve_draft(body: ResolveDraftRequest):
    item = await db.get_draft_item(body.draft_item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Draft item not found")

    option = None
    for raw in item.clarification_options + item.alternatives:
        opt = as_clarification_option(raw)
        if opt.spin_id == body.spin_id:
            option = opt
            break
    if not option:
        raise HTTPException(status_code=400, detail="Invalid spin_id for this item")

    updated = await resolve_draft_selection(
        settings.household_id, body.draft_item_id, body.spin_id, option
    )
    await broadcast_draft()
    return updated.model_dump() if updated else {"ok": False}


@app.post("/api/draft/exclude")
async def exclude_draft(body: ExcludeDraftRequest):
    updated = await db.set_draft_excluded(body.draft_item_id, body.excluded)
    if not updated:
        raise HTTPException(status_code=404, detail="Draft item not found")
    await broadcast_draft()
    return updated.model_dump()


@app.get("/api/addresses")
async def list_addresses():
    token = await get_valid_access_token()
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        addresses = await mcp_client.get_addresses()
        if not addresses:
            return {
                "addresses": [],
                "message": (
                    "No saved addresses on this Swiggy account. "
                    "Add a delivery address in the Swiggy app, then click Refresh."
                ),
            }
        return {"addresses": addresses}
    except MCPError as exc:
        raise HTTPException(status_code=exc.status_code or 500, detail=str(exc))


@app.post("/api/address")
async def set_address(body: SetAddressRequest):
    await db.set_setting("address_id", body.address_id)
    await manager.broadcast("address_selected", {"address_id": body.address_id})
    return {"ok": True, "address_id": body.address_id}


@app.get("/api/household/dashboard")
async def household_dashboard():
    cart = await get_cart_view(settings.household_id)
    messages = await db.list_messages(settings.household_id, limit=50)
    members: dict[str, dict[str, Any]] = {}
    for item in cart.get("numbered_items") or []:
        for name in item.get("requested_by") or []:
            members.setdefault(name, {"items": [], "channels": []})
            members[name]["items"].append(
                item.get("resolved_name") or item.get("raw_query")
            )
    for msg in messages:
        if msg.get("sender") in ("System", "Debug"):
            continue
        name = msg["sender"]
        members.setdefault(name, {"items": [], "channels": []})
        ch = msg.get("channel") or "web"
        if ch not in members[name]["channels"]:
            members[name]["channels"].append(ch)
    member_list = [
        {
            "name": name,
            "items": data.get("items") or [],
            "channels": data.get("channels") or [],
        }
        for name, data in members.items()
    ]
    return {
        "cart": cart,
        "messages": messages,
        "members": member_list,
        "senders": settings.senders,
        "orders": await db.list_order_history(settings.household_id, limit=20),
    }


@app.get("/api/settings")
async def get_settings():
    return {
        "senders": settings.senders,
        "address_id": await db.get_setting("address_id"),
        "household_id": settings.household_id,
    }


@app.get("/api/auth/status")
async def api_auth_status():
    return await auth_status()


@app.post("/api/checkout")
async def checkout(body: CheckoutRequest):
    if not body.confirmed:
        raise HTTPException(status_code=400, detail="Checkout not confirmed")

    address_id = await db.get_setting("address_id")
    if not address_id:
        raise HTTPException(status_code=400, detail="No delivery address selected")

    token = await get_valid_access_token()
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    result = await run_checkout(mcp_client, settings.household_id, address_id)

    if result.success:
        await db.add_message(
            settings.household_id,
            "System",
            result.swiggy_message or "Order placed.",
        )
        summary_lines = ["--- Settlement summary ---"]
        for line in result.settlement:
            summary_lines.append(
                f"{line.sender}: {', '.join(line.items)} (~₹{line.estimated_share})"
            )
        summary = "\n".join(summary_lines)
        await db.add_message(settings.household_id, "System", summary)
        await manager.broadcast(
            "checkout_result",
            {
                "success": True,
                "swiggy_message": result.swiggy_message,
                "settlement": [s.model_dump() for s in result.settlement],
                "total": result.total,
                "order_id": result.order_id,
                "order_type": "instamart",
            },
        )
        await broadcast_draft()
    else:
        await manager.broadcast(
            "checkout_result",
            {"success": False, "error": result.error},
        )

    return result.model_dump()


@app.get("/api/orders")
async def list_orders():
    orders = await db.list_order_history(settings.household_id, limit=100)
    return {
        "orders": orders,
        "analytics": compute_order_analytics(orders),
    }


@app.post("/api/checkout/dummy")
async def dummy_checkout(body: DummyCheckoutRequest):
    if not body.confirmed:
        raise HTTPException(status_code=400, detail="Order not confirmed")

    result = await run_dummy_checkout(
        settings.household_id,
        placed_by=body.placed_by,
    )

    if result.success:
        await db.add_message(
            settings.household_id,
            "System",
            result.swiggy_message or f"Order #{result.order_id} placed.",
            channel="web",
        )
        summary_lines = [f"--- Order #{result.order_id} settlement ---"]
        for line in result.settlement:
            summary_lines.append(
                f"{line.sender}: {', '.join(line.items)} (~₹{line.estimated_share})"
            )
        await db.add_message(
            settings.household_id,
            "System",
            "\n".join(summary_lines),
            channel="web",
        )
        await manager.broadcast(
            "checkout_result",
            {
                "success": True,
                "swiggy_message": result.swiggy_message,
                "settlement": [s.model_dump() for s in result.settlement],
                "total": result.total,
                "order_id": result.order_id,
                "order_type": "dummy",
            },
        )
        await broadcast_draft()
    else:
        await manager.broadcast(
            "checkout_result",
            {"success": False, "error": result.error},
        )

    return result.model_dump()


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    token = websocket.query_params.get("token")
    if not verify_session_token(token):
        await websocket.close(code=4401)
        return
    await manager.connect(websocket)
    try:
        await broadcast_auth_status()
        snapshot = await get_cart_view(settings.household_id)
        await websocket.send_text(
            json.dumps(
                {
                    "type": "draft_update",
                    "payload": snapshot,
                }
            )
        )
        messages = await db.list_messages(settings.household_id)
        for msg in messages:
            await websocket.send_text(
                json.dumps({"type": "message", "payload": msg})
            )

        while True:
            data = await websocket.receive_text()
            payload = json.loads(data)
            if payload.get("type") == "ping":
                await websocket.send_text(json.dumps({"type": "pong", "payload": {}}))
            elif payload.get("type") == "send_message":
                p = payload.get("payload", {})
                sender = p.get("sender", "Alice")
                result = await process_message(sender, p.get("text", ""), channel="web")
                await notify_household_added(sender, result, source="web")
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception:
        manager.disconnect(websocket)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("backend.main:app", host=settings.host, port=settings.port, reload=True)
