from __future__ import annotations

import json
import logging
import re
from typing import Any, Awaitable, Callable

import httpx

from backend.bot_copy import (
    format_add_summary,
    format_cart_header_added,
    format_collage_caption,
    format_grocery_understood,
    format_help_short,
    format_item_failed,
    format_nothing_understood,
    format_pick_brand_prompt,
    format_recipe_added,
    format_recipe_list,
    format_recipe_understood,
    format_recipe_whatsapp_followup,
)
from backend.bot_images import BRAND_PICK_MAX, collage_public_url
from backend.bot_intents import ChatIntent, ChatIntentKind, detect_chat_intent
from backend.bot_labels import option_button_label
from backend.cart_ops import (
    format_cart_text,
    format_order_placed_text,
    get_cart_view,
    remove_cart_line,
)
from backend.checkout import run_dummy_checkout
from backend.config import settings
from backend.db import db
from backend.household_notify import broadcast_add, record_whatsapp_contact, save_whatsapp_member
from backend.models import CheckoutResult, as_clarification_option
from backend.extractor import extract_items_with_trace
from backend.mcp_client import mcp_client
from backend.oauth import get_valid_access_token
from backend.recipe_agent import (
    add_recipe_lines,
    delete_recipe_session,
    get_latest_session_for_sender,
    is_recipe_add_all,
    is_recipe_question,
    parse_recipe_add_numbers,
    resolve_dish_from_text,
    run_recipe_lookup,
)

logger = logging.getLogger(__name__)

ProcessFn = Callable[..., Awaitable[dict[str, Any]]]
BroadcastDraftFn = Callable[[], Awaitable[None]]
CheckoutBroadcastFn = Callable[[CheckoutResult], Awaitable[None]]


class WhatsAppBot:
    def __init__(
        self,
        process_fn: ProcessFn,
        broadcast_draft_fn: BroadcastDraftFn | None = None,
        telegram_bot: Any | None = None,
        checkout_broadcast_fn: CheckoutBroadcastFn | None = None,
    ) -> None:
        self.process_fn = process_fn
        self.broadcast_draft_fn = broadcast_draft_fn
        self.telegram_bot = telegram_bot
        self.checkout_broadcast_fn = checkout_broadcast_fn
        self._pending_name: set[str] = set()

    @property
    def enabled(self) -> bool:
        return bool(
            settings.whatsapp_access_token.strip()
            and settings.whatsapp_phone_number_id.strip()
            and settings.whatsapp_verify_token.strip()
        )

    def _api_url(self, path: str = "messages") -> str:
        base = (
            f"https://graph.facebook.com/{settings.whatsapp_api_version}"
            f"/{settings.whatsapp_phone_number_id}/{path}"
        )
        return base

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {settings.whatsapp_access_token}",
            "Content-Type": "application/json",
        }

    async def _api_post(self, payload: dict) -> dict:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                self._api_url("messages"),
                headers=self._headers(),
                json=payload,
            )
            if resp.status_code >= 400:
                logger.warning("WhatsApp send failed: %s", resp.text)
            return resp.json() if resp.content else {}

    async def send_text(self, to: str, text: str) -> bool:
        ok, _ = await self.send_text_with_reason(to, text)
        return ok

    async def validate_token(self) -> bool:
        """Return True if Graph API accepts the configured access token."""
        if not self.enabled:
            return False
        url = (
            f"https://graph.facebook.com/{settings.whatsapp_api_version}"
            f"/{settings.whatsapp_phone_number_id}"
        )
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers=self._headers())
        if resp.status_code < 400:
            return True
        err = (resp.json() if resp.content else {}).get("error") or {}
        code = err.get("code")
        if code == 190:
            logger.error(
                "WhatsApp WHATSAPP_ACCESS_TOKEN invalid or expired (error 190). "
                "Regenerate in Meta Developer Console → WhatsApp → API Setup, "
                "update .env on the server, then restart swiggy."
            )
        else:
            logger.warning("WhatsApp token check failed: %s", resp.text[:300])
        return False

    async def send_text_with_reason(self, to: str, text: str) -> tuple[bool, str]:
        to_digits = self._norm_phone(to)
        payload = {
            "messaging_product": "whatsapp",
            "to": to_digits,
            "type": "text",
            "text": {"body": text[:4096]},
        }
        resp = await self._api_post(payload)
        if resp.get("messages"):
            return True, ""
        err_obj = resp.get("error") or {}
        err = err_obj.get("message", "unknown error")
        code = err_obj.get("code", "")
        detail = f"[{code}] {err}" if code else err
        if code == 190:
            logger.error(
                "WhatsApp send blocked — access token expired (190). "
                "Update WHATSAPP_ACCESS_TOKEN in .env and restart."
            )
        logger.warning("WhatsApp send to %s failed: %s", to_digits, detail)
        return False, detail

    async def send_buttons(
        self, to: str, body: str, buttons: list[tuple[str, str]]
    ) -> bool:
        """buttons: list of (id, title) — max 3, title max 20 chars."""
        rows = buttons[:3]
        if not rows:
            return await self.send_text(to, body)
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": body[:1024]},
                "action": {
                    "buttons": [
                        {
                            "type": "reply",
                            "reply": {
                                "id": btn_id[:256],
                                "title": title[:20],
                            },
                        }
                        for btn_id, title in rows
                    ]
                },
            },
        }
        resp = await self._api_post(payload)
        return bool(resp.get("messages"))

    async def send_pick_list(
        self,
        to: str,
        body: str,
        rows: list[tuple[str, str, str]],
    ) -> bool:
        """WhatsApp list picker — up to 10 rows. rows: (id, title, description)."""
        to_digits = self._norm_phone(to)
        list_rows = [
            {
                "id": row_id[:200],
                "title": title[:24],
                "description": desc[:72],
            }
            for row_id, title, desc in rows[:10]
        ]
        if not list_rows:
            return await self.send_text(to, body)
        payload = {
            "messaging_product": "whatsapp",
            "to": to_digits,
            "type": "interactive",
            "interactive": {
                "type": "list",
                "body": {"text": body[:1024]},
                "action": {
                    "button": "Choose product",
                    "sections": [{"title": "Pick one", "rows": list_rows}],
                },
            },
        }
        resp = await self._api_post(payload)
        return bool(resp.get("messages"))

    async def send_image(
        self, to: str, image_url: str, *, caption: str | None = None
    ) -> bool:
        to_digits = self._norm_phone(to)
        image_payload: dict[str, str] = {"link": image_url}
        if caption:
            image_payload["caption"] = caption[:1024]
        payload = {
            "messaging_product": "whatsapp",
            "to": to_digits,
            "type": "image",
            "image": image_payload,
        }
        resp = await self._api_post(payload)
        return bool(resp.get("messages"))

    def _phone_map(self) -> dict[str, str]:
        raw = settings.whatsapp_phone_map.strip()
        out: dict[str, str] = {}
        if not raw:
            return out
        for part in raw.split(","):
            part = part.strip()
            if ":" not in part:
                continue
            phone, name = part.split(":", 1)
            out[self._norm_phone(phone)] = name.strip()
        return out

    @staticmethod
    def _norm_phone(phone: str) -> str:
        return re.sub(r"\D", "", phone)

    async def _sender_name(self, phone: str) -> str | None:
        key = f"whatsapp_name:{self._norm_phone(phone)}"
        stored = await db.get_setting(key)
        if stored:
            return stored
        return self._phone_map().get(self._norm_phone(phone))

    async def _save_sender_name(self, phone: str, name: str) -> None:
        await save_whatsapp_member(phone, name)

    async def _after_cart_change(self) -> None:
        if self.broadcast_draft_fn:
            await self.broadcast_draft_fn()

    async def send_cart_summary(
        self, to: str, *, header: str | None = None, with_buttons: bool = True
    ) -> None:
        view = await get_cart_view(settings.household_id)
        text = format_cart_text(view, header=header)
        base = settings.public_base_url.rstrip("/")
        text += f"\n\nOpen: {base}/"
        if with_buttons and view.get("numbered_items"):
            await self.send_buttons(
                to,
                text,
                [
                    ("cmd:cart", "Cart"),
                    ("cmd:checkout", "Place order"),
                    ("cmd:clear", "Clear cart"),
                ],
            )
        else:
            await self.send_text(to, text)

    async def _resolve_pick(self, to: str, draft_item_id: int, option_index: int) -> None:
        item = await db.get_draft_item(draft_item_id)
        if not item:
            await self.send_text(to, "Item not found.")
            return
        options = item.clarification_options + item.alternatives
        if option_index < 0 or option_index >= len(options):
            await self.send_text(to, "Invalid option.")
            return
        option = as_clarification_option(options[option_index])
        sender = await self._sender_name(to) or "Someone"
        await resolve_draft_selection(
            settings.household_id, draft_item_id, option.spin_id, option
        )
        await self._after_cart_change()
        label = option.name or item.raw_query
        notify_note = await broadcast_add(
            sender,
            [label],
            source="whatsapp",
            source_whatsapp_phone=to,
            whatsapp_bot=self,
            telegram_bot=self.telegram_bot,
        )
        await self.send_cart_summary(
            to, header=f"Picked: {option.name[:60]}{notify_note}"
        )

    async def send_brand_recommendations(self, to: str, rec: dict[str, Any]) -> None:
        raw_query = rec.get("raw_query", "item")
        note = rec.get("note") or "No past order — choose one:"
        options = [as_clarification_option(o) for o in (rec.get("options") or [])[:BRAND_PICK_MAX]]
        draft_id = rec["draft_item_id"]
        has_images = any(o.image_url for o in options)

        list_rows: list[tuple[str, str, str]] = []
        for idx, o in enumerate(options):
            title = option_button_label(o.name, o.unit_price, max_len=24)
            desc_parts = []
            if o.pack_size:
                desc_parts.append(o.pack_size)
            if o.unit_price is not None:
                desc_parts.append(f"₹{o.unit_price:.0f}")
            desc = " · ".join(desc_parts) or o.name[:72]
            list_rows.append((f"pick:{draft_id}:{idx}", title, desc))

        if has_images:
            collage_url = collage_public_url(settings.public_base_url, int(draft_id))
            await self.send_image(
                to,
                collage_url,
                caption=format_collage_caption(raw_query, len(options)),
            )
        else:
            await self.send_text(to, f"Pick a brand: {raw_query}\n{note}")

        await self.send_pick_list(
            to,
            "Tap *Choose product* below to add your pick to the cart.",
            list_rows,
        )

    async def _handle_chat_intent(
        self, to: str, intent: ChatIntent, sender: str | None
    ) -> bool:
        base = settings.public_base_url.rstrip("/")

        if intent.kind == ChatIntentKind.SHOW_CART:
            await self.send_cart_summary(to)
            return True

        if intent.kind == ChatIntentKind.CLEAR_CART:
            await db.clear_draft(settings.household_id)
            await self._after_cart_change()
            await self.send_text(to, "Cart cleared.")
            return True

        if intent.kind == ChatIntentKind.PLACE_ORDER:
            await self._handle_place_order(to, sender or "WhatsApp")
            return True

        if intent.kind == ChatIntentKind.INSTAMART_WEB:
            await self._handle_command(to, "/instamart")
            return True

        if intent.kind == ChatIntentKind.REMOVE_LINE and intent.line_number:
            await self._handle_command(to, f"/remove {intent.line_number}")
            return True

        if intent.kind == ChatIntentKind.CONNECT:
            await self._handle_command(to, "/connect")
            return True

        if intent.kind == ChatIntentKind.HELP:
            await self._handle_command(to, "/help")
            return True

        return False

    async def _handle_command(self, to: str, text: str) -> bool:
        parts = text.split(maxsplit=2)
        cmd = parts[0].lower()
        base = settings.public_base_url.rstrip("/")

        if cmd in ("/start", "/help"):
            await self.send_text(to, format_help_short())
            return True

        if cmd == "/cart":
            await self.send_cart_summary(to)
            return True

        if cmd == "/connect":
            await self.send_text(
                to,
                f"Connect Swiggy:\n{base}/auth/login\n\nPick address:\n{base}/",
            )
            return True

        if cmd == "/clear":
            await db.clear_draft(settings.household_id)
            await self._after_cart_change()
            await self.send_text(to, "Cart cleared.")
            return True

        if cmd in ("/order", "/placeorder", "/checkout"):
            sender = await self._sender_name(to) or "WhatsApp"
            await self._handle_place_order(to, sender)
            return True

        if cmd == "/instamart":
            view = await get_cart_view(settings.household_id)
            subtotal = view.get("estimated_subtotal") or 0
            if subtotal >= 1000:
                msg = f"Cart ~₹{subtotal:.0f} — over ₹1000 limit. Use /remove to trim."
            elif not view.get("numbered_items"):
                msg = "Cart is empty."
            else:
                msg = (
                    f"Cart ~₹{subtotal:.0f} · {len(view['numbered_items'])} items\n"
                    f"Real Swiggy delivery — open web:\n{base}/"
                )
            await self.send_text(to, msg)
            return True

        if cmd == "/remove":
            if len(parts) < 2 or not parts[1].isdigit():
                await self.send_text(to, "Usage: /remove 3")
                return True
            try:
                result = await remove_cart_line(
                    settings.household_id, int(parts[1])
                )
            except ValueError as exc:
                await self.send_text(to, str(exc))
                return True
            await self._after_cart_change()
            await self.send_cart_summary(
                to, header=f"Removed #{result['removed_line']}: {result['removed_name']}"
            )
            return True

        return False

    async def _handle_button(self, to: str, button_id: str) -> None:
        if button_id.startswith("pick:"):
            _, draft_id, idx = button_id.split(":", 2)
            await self._resolve_pick(to, int(draft_id), int(idx))
            return
        if button_id == "cmd:cart":
            await self.send_cart_summary(to)
            return
        if button_id == "cmd:clear":
            await db.clear_draft(settings.household_id)
            await self._after_cart_change()
            await self.send_text(to, "Cart cleared.")
            return
        if button_id == "cmd:checkout":
            sender = await self._sender_name(to) or "WhatsApp"
            await self._handle_place_order(to, sender)
            return

    async def _handle_place_order(self, to: str, sender: str) -> None:
        result = await run_dummy_checkout(settings.household_id, placed_by=sender)
        if result.success:
            await self._after_cart_change()
            if self.checkout_broadcast_fn:
                await self.checkout_broadcast_fn(result)
        msg = format_order_placed_text(
            result, base_url=settings.public_base_url.rstrip("/")
        )
        await self.send_text(to, msg)

    async def _handle_recipe_lookup(self, to: str, sender: str, dish: str) -> None:
        address_id = await db.get_setting("address_id")
        if not address_id:
            await self.send_text(to, "Pick delivery address on web app first.")
            return
        if not await get_valid_access_token():
            await self.send_text(to, "Connect Swiggy first — /connect")
            return
        await self.send_text(to, format_recipe_understood(dish))
        try:
            session = await run_recipe_lookup(
                mcp_client,
                settings.household_id,
                address_id,
                dish,
                sender,
            )
        except Exception as exc:
            logger.exception("Recipe lookup failed")
            await self.send_text(to, f"Recipe lookup failed: {exc}")
            return
        await self.send_text(to, format_recipe_list(session))
        await self.send_text(to, format_recipe_whatsapp_followup())

    async def _handle_recipe_add(
        self,
        to: str,
        sender: str,
        session: dict[str, Any],
        *,
        indices: list[int] | None,
    ) -> None:
        added = await add_recipe_lines(session, indices=indices)
        if self.broadcast_draft_fn:
            await self.broadcast_draft_fn()
        await delete_recipe_session(session["id"])
        if added:
            notify_note = await broadcast_add(
                sender,
                added,
                source="whatsapp",
                source_whatsapp_phone=to,
                whatsapp_bot=self,
                telegram_bot=self.telegram_bot,
            )
            await self.send_cart_summary(
                to, header=f"{format_recipe_added(len(added))}{notify_note}"
            )
        else:
            await self.send_text(to, "Kuch add nahi hua — pehle brand chuno.")

    async def _finish_grocery_result(
        self, to: str, sender: str, result: dict[str, Any]
    ) -> None:
        if result.get("error"):
            await self.send_text(to, result["error"])
            return

        for fail in result.get("failures") or []:
            await self.send_text(
                to,
                format_item_failed(fail.get("raw_query", "item"), fail.get("note")),
            )

        added_details = result.get("added_details") or []
        if added_details:
            summary = format_add_summary(sender, added_details)
            if summary:
                await self.send_text(to, summary)

        for rec in result.get("recommendations") or []:
            await self.send_text(
                to, format_pick_brand_prompt(rec.get("raw_query", "item"))
            )
            await self.send_brand_recommendations(to, rec)

        if result.get("added"):
            notify_note = await broadcast_add(
                sender,
                result["added"],
                source="whatsapp",
                source_whatsapp_phone=to,
                whatsapp_bot=self,
                telegram_bot=self.telegram_bot,
            )
            await self.send_cart_summary(
                to, header=f"{format_cart_header_added(sender)}{notify_note}"
            )
        elif not result.get("failures") and not result.get("recommendations"):
            await self.send_text(to, format_nothing_understood())

    async def _handle_grocery_message(self, to: str, sender: str, text: str) -> None:
        trace = await extract_items_with_trace(text, sender)
        if not trace.items:
            await self.send_text(to, format_nothing_understood())
            return

        await self.send_text(
            to, format_grocery_understood(sender, [i.raw_query for i in trace.items])
        )
        try:
            result = await self.process_fn(
                sender,
                text,
                whatsapp_phone=to,
                channel="whatsapp",
                trace=trace,
            )
        except Exception as exc:
            logger.exception("WhatsApp message processing failed")
            await self.send_text(to, f"Kuch gadbad hui — dubara try karo.\n{exc}")
            return

        await self._finish_grocery_result(to, sender, result)

    async def _try_register_name(self, to: str, text: str) -> bool:
        """First message from unknown phone: 'name Adarsh' or pick from senders list."""
        if to not in self._pending_name:
            return False
        name = text.strip()
        if name.lower().startswith("name "):
            name = name[5:].strip()
        if name in settings.senders:
            await self._save_sender_name(to, name)
            self._pending_name.discard(to)
            await self.send_text(
                to,
                f"Hi {name}! Send groceries anytime.\nExample: atta 5kg, milk 1L",
            )
            return True
        await self.send_text(
            to,
            f"Reply with: name Alice\n(or one of: {', '.join(settings.senders)})",
        )
        return True

    async def handle_incoming(self, payload: dict) -> None:
        count = 0
        for entry in payload.get("entry") or []:
            for change in entry.get("changes") or []:
                field = change.get("field")
                value = change.get("value") or {}
                messages = value.get("messages") or []
                if messages:
                    count += len(messages)
                    logger.info(
                        "WhatsApp incoming field=%s messages=%d from=%s",
                        field,
                        len(messages),
                        [m.get("from") for m in messages],
                    )
                for message in messages:
                    await self._handle_message(message)
        if count == 0:
            logger.info("WhatsApp webhook had no messages (status-only or wrong field)")

    async def _handle_message(self, message: dict) -> None:
        from_id = str(message.get("from") or "")
        if not from_id:
            return

        msg_type = message.get("type")
        text = ""

        if msg_type == "text":
            text = (message.get("text") or {}).get("body") or ""
        elif msg_type == "interactive":
            interactive = message.get("interactive") or {}
            if interactive.get("type") == "button_reply":
                button_id = (interactive.get("button_reply") or {}).get("id") or ""
                if button_id:
                    await self._handle_button(from_id, button_id)
                return
            if interactive.get("type") == "list_reply":
                row_id = (interactive.get("list_reply") or {}).get("id") or ""
                if row_id:
                    await self._handle_button(from_id, row_id)
                return
            text = (interactive.get("button_reply") or {}).get("title") or ""
        else:
            await self.send_text(
                from_id, "Send Instamart items — e.g. atta 5kg, tshirt medium, type c charger"
            )
            return

        text = text.strip()
        if not text:
            return

        if await self._try_register_name(from_id, text):
            return

        if text.startswith("/"):
            if await self._handle_command(from_id, text):
                return

        intent = detect_chat_intent(text)
        if intent.kind != ChatIntentKind.ADD_ITEMS:
            sender = await self._sender_name(from_id)
            if await self._handle_chat_intent(from_id, intent, sender):
                return

        sender = await self._sender_name(from_id)

        if not sender:
            self._pending_name.add(from_id)
            await self.send_text(
                from_id,
                "Welcome to Instamart!\n\n"
                f"Reply: name {settings.senders[0]}\n"
                f"(Options: {', '.join(settings.senders)})\n\n"
                "Then send groceries, clothes, electronics — anything on Instamart.",
            )
            return

        await record_whatsapp_contact(from_id, sender)

        session = await get_latest_session_for_sender(sender)
        if session:
            if is_recipe_add_all(text):
                await self._handle_recipe_add(
                    from_id, sender, session, indices=None
                )
                return
            nums = parse_recipe_add_numbers(text)
            if nums:
                await self._handle_recipe_add(
                    from_id, sender, session, indices=nums
                )
                return

        if is_recipe_question(text):
            dish = await resolve_dish_from_text(text)
            if not dish:
                await self.send_text(
                    from_id,
                    "Kaunsi dish? Jaise: chole bhature banane me kya lagega",
                )
                return
            await self._handle_recipe_lookup(from_id, sender, dish)
            return

        await self._handle_grocery_message(from_id, sender, text)
