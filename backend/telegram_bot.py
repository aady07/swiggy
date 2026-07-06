from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Awaitable, Callable

import httpx

from backend.assistants import format_assistants_menu, run_assistant_chat
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
    recipe_list_keyboard,
)
from backend.bot_images import (
    BRAND_PICK_MAX,
    collage_public_url,
    product_full_url,
    product_thumb_url,
)
from backend.bot_intents import ChatIntent, ChatIntentKind, detect_chat_intent
from backend.bot_labels import option_button_label
from backend.cart_ops import (
    assistants_inline_keyboard,
    cart_inline_keyboard,
    format_cart_text,
    format_order_placed_text,
    get_cart_view,
    remove_cart_item,
    remove_cart_line,
)
from backend.checkout import run_dummy_checkout
from backend.config import settings
from backend.db import db
from backend.household_notify import broadcast_add, resolve_telegram_name, save_telegram_member
from backend.models import CheckoutResult, ClarificationOption, as_clarification_option
from backend.extractor import extract_items_with_trace
from backend.mcp_client import mcp_client
from backend.oauth import get_valid_access_token
from backend.recipe_agent import (
    add_recipe_lines,
    create_pick_draft_for_line,
    delete_recipe_session,
    get_latest_session_for_sender,
    is_recipe_add_all,
    is_recipe_question,
    load_recipe_session,
    parse_recipe_add_numbers,
    resolve_dish_from_text,
    run_recipe_lookup,
)
from backend.resolver import resolve_draft_selection

logger = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org/bot{token}"

ProcessFn = Callable[..., Awaitable[dict[str, Any]]]
BroadcastDraftFn = Callable[[], Awaitable[None]]
CheckoutBroadcastFn = Callable[[CheckoutResult], Awaitable[None]]


class TelegramBot:
    def __init__(
        self,
        token: str,
        process_fn: ProcessFn,
        broadcast_draft_fn: BroadcastDraftFn | None = None,
        whatsapp_bot: Any | None = None,
        checkout_broadcast_fn: CheckoutBroadcastFn | None = None,
    ) -> None:
        self.token = token.strip()
        self.process_fn = process_fn
        self.broadcast_draft_fn = broadcast_draft_fn
        self.checkout_broadcast_fn = checkout_broadcast_fn
        self.whatsapp_bot = whatsapp_bot
        self._offset = 0
        self._task: asyncio.Task | None = None
        self._pending_assistant: dict[int, str] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def _url(self, method: str) -> str:
        return API_BASE.format(token=self.token) + f"/{method}"

    async def _api_post(self, method: str, payload: dict) -> dict:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(self._url(method), json=payload)
            if resp.status_code != 200:
                logger.warning("Telegram %s failed: %s", method, resp.text)
            return resp.json() if resp.content else {}

    async def send_message(
        self,
        chat_id: int | str,
        text: str,
        *,
        reply_markup: dict | None = None,
        parse_mode: str | None = None,
    ) -> bool:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text[:4096]}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        if parse_mode:
            payload["parse_mode"] = parse_mode
        resp = await self._api_post("sendMessage", payload)
        return bool(resp.get("ok"))

    async def send_photo(
        self,
        chat_id: int | str,
        photo_url: str,
        *,
        caption: str | None = None,
        reply_markup: dict | None = None,
    ) -> bool:
        payload: dict[str, Any] = {"chat_id": chat_id, "photo": photo_url}
        if caption:
            payload["caption"] = caption[:1024]
        if reply_markup:
            payload["reply_markup"] = reply_markup
        resp = await self._api_post("sendPhoto", payload)
        return bool(resp.get("ok"))

    async def send_poll(
        self, chat_id: int | str, question: str, options: list[str]
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "question": question[:300],
            "options": [o[:100] for o in options[:10]],
            "is_anonymous": False,
            "allows_multiple_answers": False,
            "open_period": settings.poll_open_seconds,
        }
        resp = await self._api_post("sendPoll", payload)
        return resp.get("result") or {}

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        payload: dict[str, Any] = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text[:200]
        await self._api_post("answerCallbackQuery", payload)

    async def delete_webhook(self) -> None:
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(self._url("deleteWebhook"))

    async def get_updates(self) -> list[dict]:
        params = {"offset": self._offset, "timeout": 25}
        async with httpx.AsyncClient(timeout=35) as client:
            resp = await client.get(self._url("getUpdates"), params=params)
            resp.raise_for_status()
            data = resp.json()
        if not data.get("ok"):
            return []
        return data.get("result") or []

    async def _sender_name(self, message: dict) -> str:
        user = message.get("from") or {}
        user_id = str(user.get("id") or "")
        fallback = user.get("first_name") or user.get("username") or "TelegramUser"
        if user_id:
            return await resolve_telegram_name(user_id, fallback)
        return fallback

    async def _after_cart_change(self) -> None:
        if self.broadcast_draft_fn:
            await self.broadcast_draft_fn()

    async def send_cart_card(
        self, chat_id: int | str, *, header: str | None = None
    ) -> None:
        view = await get_cart_view(settings.household_id)
        text = format_cart_text(view, header=header)
        markup = cart_inline_keyboard(view) if view.get("numbered_items") else None
        await self.send_message(chat_id, text, reply_markup=markup)

    async def _do_remove_line(self, chat_id: int, line_number: int) -> None:
        try:
            result = await remove_cart_line(settings.household_id, line_number)
        except ValueError as exc:
            await self.send_message(chat_id, str(exc))
            return
        await self._after_cart_change()
        await self.send_cart_card(
            chat_id,
            header=f"Removed #{result['removed_line']}: {result['removed_name']}",
        )

    async def _do_remove_id(self, chat_id: int, item_id: int) -> None:
        try:
            result = await remove_cart_item(settings.household_id, item_id)
        except ValueError as exc:
            await self.send_message(chat_id, str(exc))
            return
        await self._after_cart_change()
        await self.send_cart_card(
            chat_id, header=f"Removed: {result['removed_name']}"
        )

    async def _do_clear_cart(self, chat_id: int) -> None:
        await db.clear_draft(settings.household_id)
        await self._after_cart_change()
        await self.send_message(chat_id, "Cart cleared.")

    async def _resolve_pick(
        self,
        chat_id: int,
        draft_item_id: int,
        option_index: int,
        *,
        picker_name: str | None = None,
    ) -> None:
        item = await db.get_draft_item(draft_item_id)
        if not item:
            await self.send_message(chat_id, "Item not found.")
            return
        options = item.clarification_options + item.alternatives
        if option_index < 0 or option_index >= len(options):
            await self.send_message(chat_id, "Invalid option.")
            return
        option = as_clarification_option(options[option_index])
        sender = picker_name or "Someone"
        await resolve_draft_selection(
            settings.household_id, draft_item_id, option.spin_id, option
        )
        await self._after_cart_change()
        label = option.name or item.raw_query
        notify_note = await broadcast_add(
            sender,
            [label],
            source="telegram",
            source_telegram_chat=str(chat_id),
            whatsapp_bot=self.whatsapp_bot,
            telegram_bot=self,
        )
        await self.send_cart_card(
            chat_id, header=f"Picked: {option.name[:60]}{notify_note}"
        )

    def _collage_pick_keyboard(self, draft_id: int, count: int) -> dict:
        rows: list[list[dict]] = []
        enlarge = [
            {"text": f"🔍 {i + 1}", "callback_data": f"img:{draft_id}:{i}"}
            for i in range(count)
        ]
        rows.append(enlarge)
        pick_row: list[dict] = []
        for i in range(count):
            pick_row.append(
                {"text": f"✅ {i + 1}", "callback_data": f"pick:{draft_id}:{i}"}
            )
            if len(pick_row) == 3:
                rows.append(pick_row)
                pick_row = []
        if pick_row:
            rows.append(pick_row)
        return {"inline_keyboard": rows}

    def _brand_recommendation_keyboard(self, rec: dict[str, Any]) -> dict:
        draft_id = rec["draft_item_id"]
        rows = []
        for idx, opt in enumerate((rec.get("options") or [])[:BRAND_PICK_MAX]):
            o = as_clarification_option(opt)
            if idx < BRAND_PICK_MAX and product_thumb_url(o.image_url):
                continue
            label = option_button_label(o.name, o.unit_price, max_len=64)
            rows.append(
                [
                    {
                        "text": label,
                        "callback_data": f"pick:{draft_id}:{idx}",
                    }
                ]
            )
        return {"inline_keyboard": rows}

    async def _handle_recipe_lookup(
        self, chat_id: int, sender: str, dish: str
    ) -> None:
        address_id = await db.get_setting("address_id")
        if not address_id:
            await self.send_message(
                chat_id,
                "Pick a delivery address on the web app first.",
            )
            return
        if not await get_valid_access_token():
            await self.send_message(chat_id, "Connect Swiggy first — /connect")
            return
        await self.send_message(
            chat_id, f"🍳 {format_recipe_understood(dish)}", parse_mode="Markdown"
        )
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
            await self.send_message(chat_id, f"Recipe lookup failed: {exc}")
            return
        await self.send_message(
            chat_id,
            format_recipe_list(session),
            reply_markup=recipe_list_keyboard(session["id"], session),
            parse_mode="Markdown",
        )

    async def _finish_grocery_result(
        self, chat_id: int, sender: str, result: dict[str, Any]
    ) -> None:
        if result.get("error"):
            await self.send_message(chat_id, result["error"])
            return

        for fail in result.get("failures") or []:
            await self.send_message(
                chat_id,
                format_item_failed(
                    fail.get("raw_query", "item"), fail.get("note")
                ),
                parse_mode="Markdown",
            )

        for poll_req in result.get("polls") or []:
            await self.start_conflict_poll(chat_id, poll_req)

        added_details = result.get("added_details") or []
        if added_details:
            summary = format_add_summary(sender, added_details)
            if summary:
                await self.send_message(chat_id, summary, parse_mode="Markdown")

        for rec in result.get("recommendations") or []:
            raw = rec.get("raw_query", "item")
            await self.send_message(
                chat_id, format_pick_brand_prompt(raw), parse_mode="Markdown"
            )
            await self.send_brand_recommendations(chat_id, rec)

        if result.get("added"):
            notify_note = await broadcast_add(
                sender,
                result["added"],
                source="telegram",
                source_telegram_chat=str(chat_id),
                whatsapp_bot=self.whatsapp_bot,
                telegram_bot=self,
            )
            await self.send_cart_card(
                chat_id,
                header=f"{format_cart_header_added(sender)}{notify_note}",
            )
        elif not result.get("failures") and not result.get("polls") and not (
            result.get("recommendations")
        ):
            await self.send_message(chat_id, format_nothing_understood())

    async def _handle_recipe_add(
        self,
        chat_id: int,
        sender: str,
        session: dict[str, Any],
        *,
        indices: list[int] | None,
        clear_session: bool = False,
    ) -> None:
        added = await add_recipe_lines(session, indices=indices)
        if self.broadcast_draft_fn:
            await self.broadcast_draft_fn()
        if clear_session:
            await delete_recipe_session(session["id"])
        if added:
            notify_note = await broadcast_add(
                sender,
                added,
                source="telegram",
                source_telegram_chat=str(chat_id),
                whatsapp_bot=self.whatsapp_bot,
                telegram_bot=self,
            )
            await self.send_cart_card(
                chat_id,
                header=f"{format_recipe_added(len(added))}{notify_note}",
            )
        else:
            await self.send_message(
                chat_id,
                "Kuch add nahi hua — pehle ? buttons se brand chuno.",
            )

    async def send_brand_recommendations(
        self, chat_id: int, rec: dict[str, Any]
    ) -> None:
        raw_query = rec.get("raw_query", "item")
        note = rec.get("note") or "No past order — choose one:"
        options = [as_clarification_option(o) for o in (rec.get("options") or [])[:BRAND_PICK_MAX]]
        draft_id = int(rec["draft_item_id"])
        count = len(options)

        has_images = any(o.image_url for o in options)
        if has_images and count:
            collage_url = collage_public_url(settings.public_base_url, draft_id)
            caption = format_collage_caption(raw_query, count)
            ok = await self.send_photo(
                chat_id,
                collage_url,
                caption=caption,
                reply_markup=self._collage_pick_keyboard(draft_id, count),
            )
            if ok:
                return

        lines = [f"*{raw_query}* — brand chuno:", ""]
        for i, o in enumerate(options, start=1):
            p = f" — ₹{o.unit_price:.0f}" if o.unit_price is not None else ""
            lines.append(f"{i}. {o.name}{p}")

        markup = self._brand_recommendation_keyboard(rec)
        if markup.get("inline_keyboard"):
            lines.append("")
            lines.append("Neeche button dabao 👇")
            await self.send_message(
                chat_id, "\n".join(lines), reply_markup=markup, parse_mode="Markdown"
            )
        else:
            await self.send_message(chat_id, "\n".join(lines))

    async def start_conflict_poll(self, chat_id: int, poll_request: dict[str, Any]) -> None:
        options = poll_request.get("options") or []
        if len(options) < 2:
            await self.send_message(
                chat_id,
                "Conflict detected but not enough options for a poll — pick on web.",
            )
            return

        labels = [o["label"] for o in options]
        result = await self.send_poll(chat_id, poll_request["question"], labels)
        poll_id = str((result.get("poll") or {}).get("id") or "")
        if not poll_id:
            await self.send_message(chat_id, "Could not start poll — use web cart to pick.")
            return

        active = ActivePoll(
            poll_id=poll_id,
            chat_id=int(chat_id),
            draft_item_id=int(poll_request["draft_item_id"]),
            options=options,
        )
        active.timeout_task = asyncio.create_task(
            self._poll_timeout(poll_id, settings.poll_open_seconds)
        )
        poll_manager.register(active)
        await self.send_message(
            chat_id,
            f"⏱ Poll open for {settings.poll_open_seconds}s — everyone vote!",
        )

    async def _poll_timeout(self, poll_id: str, seconds: int) -> None:
        await asyncio.sleep(seconds)
        await self.finalize_poll(poll_id, reason="time's up")

    async def finalize_poll(self, poll_id: str, reason: str = "majority") -> None:
        poll = poll_manager.get(poll_id)
        if not poll or poll.finalized:
            return
        poll.finalized = True

        idx = poll_manager.winner_index(poll)
        if idx is None:
            idx = 0
        if idx >= len(poll.options):
            poll_manager.remove(poll_id)
            return

        opt_data = poll.options[idx]
        option = ClarificationOption(
            spin_id=opt_data["spin_id"],
            name=opt_data.get("name") or opt_data["label"],
            pack_size=opt_data.get("pack_size"),
            unit_price=opt_data.get("unit_price"),
            product_id=opt_data.get("product_id"),
        )
        await resolve_draft_selection(
            settings.household_id,
            poll.draft_item_id,
            option.spin_id,
            option,
        )
        await self._after_cart_change()
        votes = len(poll.votes)
        label = opt_data.get("name") or opt_data.get("label") or "item"
        notify_note = await broadcast_add(
            "Group",
            [label],
            source="telegram",
            source_telegram_chat=str(poll.chat_id),
            whatsapp_bot=self.whatsapp_bot,
            telegram_bot=self,
        )
        await self.send_message(
            poll.chat_id,
            f"🗳 Group picked ({votes} vote(s), {reason}):\n{opt_data['label']}{notify_note}",
        )
        poll_manager.remove(poll_id)
        await self.send_cart_card(poll.chat_id)

    async def handle_poll_answer(self, poll_answer: dict) -> None:
        poll_id = str(poll_answer.get("poll_id") or "")
        user = poll_answer.get("user") or {}
        option_ids = poll_answer.get("option_ids") or []
        if not poll_id or not option_ids:
            return

        poll = poll_manager.record_vote(poll_id, int(user.get("id", 0)), int(option_ids[0]))
        if not poll:
            return

        name = user.get("first_name") or user.get("username") or "Someone"
        label = poll.options[option_ids[0]]["label"] if option_ids[0] < len(poll.options) else "?"
        await self.send_message(poll.chat_id, f"✓ {name} voted {label[:60]}")

        if len(poll.votes) >= 2:
            await self.finalize_poll(poll_id, reason="majority")

    async def _do_assistant_pick(self, chat_id: int, assistant_id: str) -> None:
        self._pending_assistant[chat_id] = assistant_id
        from backend.assistants import get_assistant

        a = get_assistant(assistant_id)
        if not a:
            await self.send_message(chat_id, "Unknown assistant.")
            return
        await self.send_message(
            chat_id,
            f"{a['emoji']} {a['name']} ready.\nSend your question in the next message.",
        )

    async def _do_assistant_chat(
        self, chat_id: int, assistant_id: str, message: str
    ) -> None:
        try:
            reply = await run_assistant_chat(
                assistant_id, message, settings.household_id
            )
        except Exception as exc:
            reply = f"Assistant error: {exc}"
        from backend.assistants import get_assistant

        a = get_assistant(assistant_id) or {"emoji": "🤖", "name": "Assistant"}
        await self.send_message(chat_id, f"{a['emoji']} {a['name']}:\n\n{reply}")

    async def _handle_chat_intent(
        self, chat_id: int, intent: ChatIntent, sender: str | None
    ) -> bool:
        if intent.kind == ChatIntentKind.SHOW_CART:
            await self.send_cart_card(chat_id)
            return True

        if intent.kind == ChatIntentKind.CLEAR_CART:
            await self._do_clear_cart(chat_id)
            return True

        if intent.kind == ChatIntentKind.PLACE_ORDER:
            await self._handle_place_order(chat_id, sender or "Telegram")
            return True

        if intent.kind == ChatIntentKind.INSTAMART_WEB:
            await self._handle_command(chat_id, "/instamart", sender=sender)
            return True

        if intent.kind == ChatIntentKind.REMOVE_LINE and intent.line_number:
            await self._do_remove_line(chat_id, intent.line_number)
            return True

        if intent.kind == ChatIntentKind.CONNECT:
            await self._handle_command(chat_id, "/connect", sender=sender)
            return True

        if intent.kind == ChatIntentKind.HELP:
            await self._handle_command(chat_id, "/help", sender=sender)
            return True

        return False

    async def _handle_place_order(self, chat_id: int | str, sender: str) -> None:
        result = await run_dummy_checkout(settings.household_id, placed_by=sender)
        if result.success:
            await self._after_cart_change()
            if self.checkout_broadcast_fn:
                await self.checkout_broadcast_fn(result)
        msg = format_order_placed_text(
            result, base_url=settings.public_base_url.rstrip("/")
        )
        await self.send_message(chat_id, msg)

    async def _handle_command(
        self, chat_id: int, text: str, *, sender: str | None = None
    ) -> bool:
        parts = text.split(maxsplit=2)
        cmd = parts[0].split("@")[0].lower()
        base = settings.public_base_url.rstrip("/")

        if cmd in ("/start", "/help"):
            await self.send_message(chat_id, format_help_short())
            return True

        if cmd == "/cart":
            await self.send_cart_card(chat_id)
            return True

        if cmd == "/assistants":
            await self.send_message(
                chat_id,
                format_assistants_menu(),
                reply_markup=assistants_inline_keyboard(),
            )
            return True

        if cmd == "/connect":
            await self.send_message(
                chat_id,
                f"Connect Swiggy:\n{base}/auth/login\n\nPick address:\n{base}/",
            )
            return True

        if cmd == "/clear":
            await self._do_clear_cart(chat_id)
            return True

        if cmd in ("/order", "/placeorder", "/checkout"):
            await self._handle_place_order(chat_id, sender or "Telegram")
            return True

        if cmd == "/instamart":
            view = await get_cart_view(settings.household_id)
            subtotal = view.get("estimated_subtotal") or 0
            if subtotal >= 1000:
                msg = (
                    f"Cart ~₹{subtotal:.0f} — over ₹1000 MCP limit.\n"
                    "Use /ask budget for tips, or /remove items."
                )
            elif not view.get("numbered_items"):
                msg = "Cart is empty."
            else:
                msg = (
                    f"Cart ~₹{subtotal:.0f} · {len(view['numbered_items'])} items\n\n"
                    f"Real Swiggy delivery — open web:\n{base}/"
                )
            await self.send_message(chat_id, msg)
            return True

        if cmd == "/remove":
            if len(parts) < 2 or not parts[1].isdigit():
                await self.send_message(chat_id, "Usage: /remove 3")
                return True
            await self._do_remove_line(chat_id, int(parts[1]))
            return True

        if cmd == "/name":
            if len(parts) < 2:
                await self.send_message(chat_id, "Usage: /name Mom")
                return True
            return False

        if cmd == "/ask":
            if len(parts) < 3:
                await self.send_message(
                    chat_id,
                    "Usage: /ask budget what should I remove?\n"
                    "Assistants: cart, budget, meal, grocery",
                )
                return True
            await self._do_assistant_chat(chat_id, parts[1].lower(), parts[2])
            return True

        return False

    async def handle_callback(self, callback: dict) -> None:
        cb_id = callback.get("id", "")
        data = callback.get("data") or ""
        message = callback.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if not chat_id:
            return

        if data.startswith("recipe:"):
            parts = data.split(":")
            action = parts[1]
            session_id = parts[2]
            session = await load_recipe_session(session_id)
            if not session:
                await self.answer_callback(cb_id, "Session expired")
                await self.send_message(chat_id, "Recipe list expired — ask again.")
                return
            user = callback.get("from") or {}
            user_id = str(user.get("id") or "")
            fallback = user.get("first_name") or user.get("username") or "Someone"
            sender = (
                await resolve_telegram_name(user_id, fallback)
                if user_id
                else fallback
            )

            if action == "skip":
                await delete_recipe_session(session_id)
                await self.answer_callback(cb_id, "Theek hai")
                return

            if action == "all":
                await self.answer_callback(cb_id, "Adding…")
                await self._handle_recipe_add(
                    chat_id, sender, session, indices=None, clear_session=True
                )
                return

            if action == "one" and len(parts) >= 4:
                await self.answer_callback(cb_id, "Added")
                idx = int(parts[3])
                await self._handle_recipe_add(
                    chat_id, sender, session, indices=[idx + 1], clear_session=False
                )
                return

            if action == "pick" and len(parts) >= 4:
                await self.answer_callback(cb_id)
                idx = int(parts[3])
                rec = await create_pick_draft_for_line(session, idx)
                if not rec:
                    await self.send_message(chat_id, "Could not open brand pick.")
                    return
                await self.send_brand_recommendations(chat_id, rec)
                return

        if data.startswith("img:"):
            _, draft_id, idx = data.split(":", 2)
            await self.answer_callback(cb_id)
            item = await db.get_draft_item(int(draft_id))
            if not item:
                await self.send_message(chat_id, "Item not found.")
                return
            opts = item.clarification_options + item.alternatives
            opt_idx = int(idx)
            if opt_idx < 0 or opt_idx >= len(opts):
                await self.send_message(chat_id, "Option not found.")
                return
            o = as_clarification_option(opts[opt_idx])
            full = product_full_url(o.image_url) or o.image_url
            if not full:
                await self.send_message(chat_id, "No image for this option.")
                return
            caption = f"{o.name}"
            if o.pack_size:
                caption += f"\n{o.pack_size}"
            if o.unit_price is not None:
                caption += f" · ₹{o.unit_price:.0f}"
            await self.send_photo(chat_id, full, caption=caption)
            return

        if data.startswith("pick:"):
            _, draft_id, idx = data.split(":", 2)
            await self.answer_callback(cb_id, "Added")
            user = callback.get("from") or {}
            user_id = str(user.get("id") or "")
            fallback = user.get("first_name") or user.get("username") or "Someone"
            picker = (
                await resolve_telegram_name(user_id, fallback)
                if user_id
                else fallback
            )
            await self._resolve_pick(
                chat_id, int(draft_id), int(idx), picker_name=picker
            )
            return

        if data.startswith("rm:"):
            item_id = int(data.split(":", 1)[1])
            await self.answer_callback(cb_id, "Removed")
            await self._do_remove_id(chat_id, item_id)
            return

        if data == "cmd:cart":
            await self.answer_callback(cb_id)
            await self.send_cart_card(chat_id)
            return

        if data == "cmd:clear":
            await self.answer_callback(cb_id, "Cart cleared")
            await self._do_clear_cart(chat_id)
            return

        if data == "cmd:assistants":
            await self.answer_callback(cb_id)
            await self.send_message(
                chat_id,
                format_assistants_menu(),
                reply_markup=assistants_inline_keyboard(),
            )
            return

        if data == "cmd:checkout":
            await self.answer_callback(cb_id, "Placing order…")
            user = callback.get("from") or {}
            user_id = str(user.get("id") or "")
            fallback = user.get("first_name") or user.get("username") or "Telegram"
            sender = (
                await resolve_telegram_name(user_id, fallback)
                if user_id
                else fallback
            )
            await self._handle_place_order(chat_id, sender)
            return

        if data.startswith("asst:"):
            assistant_id = data.split(":", 1)[1]
            await self.answer_callback(cb_id)
            await self._do_assistant_pick(chat_id, assistant_id)
            return

        await self.answer_callback(cb_id)

    async def handle_my_chat_member(self, update: dict) -> None:
        mcm = update.get("my_chat_member") or {}
        chat = mcm.get("chat") or {}
        chat_id = chat.get("id")
        if not chat_id:
            return
        new = (mcm.get("new_chat_member") or {}).get("status")
        if new not in ("member", "administrator"):
            return
        chat_id_str = str(chat_id)
        try:
            is_group = int(chat_id) < 0
        except (TypeError, ValueError):
            is_group = chat_id_str.startswith("-")
        if is_group:
            await db.set_setting("telegram_household_chat_id", chat_id_str)
        await self.send_message(
            chat_id,
            "Hi! I'm @singhgrocerybot — shared Instamart cart for this group.\n\n"
            "1. Turn OFF Group Privacy in @BotFather (Bot Settings)\n"
            "2. Connect Swiggy once: /connect\n"
            "3. Send items: atta 5kg, tshirt, charger, milk\n\n"
            "If I don't reply, mention me: @singhgrocerybot atta 5kg",
        )

    async def handle_update(self, update: dict) -> None:
        if "my_chat_member" in update:
            await self.handle_my_chat_member(update)
            return

        if "poll_answer" in update:
            await self.handle_poll_answer(update["poll_answer"])
            return

        if "callback_query" in update:
            await self.handle_callback(update["callback_query"])
            return

        message = update.get("message") or update.get("edited_message")
        if not message:
            return

        text = (message.get("text") or "").strip()
        if not text:
            return

        chat_id = message["chat"]["id"]

        name_match = re.match(r"^/name(?:@\w+)?\s+(\S+)\s*$", text, re.I)
        if name_match:
            user = message.get("from") or {}
            user_id = str(user.get("id") or "")
            name = name_match.group(1).strip()
            if name not in settings.senders:
                await self.send_message(
                    chat_id,
                    f"Pick one of: {', '.join(settings.senders)}",
                )
                return
            if user_id:
                await save_telegram_member(user_id, str(chat_id), name)
            await self.send_message(
                chat_id,
                f"Hi {name}! Your adds will show as “{name}” on the shared cart.",
            )
            return

        if text.startswith("/"):
            sender_name = await self._sender_name(message)
            handled = await self._handle_command(chat_id, text, sender=sender_name)
            if handled:
                return

        intent = detect_chat_intent(text)
        if intent.kind != ChatIntentKind.ADD_ITEMS:
            sender = await self._sender_name(message)
            if await self._handle_chat_intent(chat_id, intent, sender):
                return

        if chat_id in self._pending_assistant:
            assistant_id = self._pending_assistant.pop(chat_id)
            await self._do_assistant_chat(chat_id, assistant_id, text)
            return

        sender = await self._sender_name(message)

        session = await get_latest_session_for_sender(sender)
        if session:
            if is_recipe_add_all(text):
                await self._handle_recipe_add(
                    chat_id, sender, session, indices=None, clear_session=True
                )
                return
            nums = parse_recipe_add_numbers(text)
            if nums:
                await self._handle_recipe_add(
                    chat_id, sender, session, indices=nums, clear_session=True
                )
                return

        if is_recipe_question(text):
            dish = await resolve_dish_from_text(text)
            if not dish:
                await self.send_message(
                    chat_id,
                    "Kaunsi dish? 🍳\nJaise: chole bhature banane me kya lagega",
                )
                return
            await self._handle_recipe_lookup(chat_id, sender, dish)
            return

        trace = await extract_items_with_trace(text, sender)
        if not trace.items:
            await self.send_message(chat_id, format_nothing_understood())
            return

        await self.send_message(
            chat_id,
            format_grocery_understood(sender, [i.raw_query for i in trace.items]),
            parse_mode="Markdown",
        )
        try:
            result = await self.process_fn(
                sender,
                text,
                telegram_chat_id=chat_id,
                channel="telegram",
                trace=trace,
            )
        except Exception as exc:
            logger.exception("Telegram message processing failed")
            await self.send_message(chat_id, f"Kuch gadbad hui — dubara try karo.\n{exc}")
            return

        await self._finish_grocery_result(chat_id, sender, result)

    async def poll_forever(self) -> None:
        await self.delete_webhook()
        logger.info("Telegram bot polling started")
        while True:
            try:
                updates = await self.get_updates()
                for update in updates:
                    self._offset = update["update_id"] + 1
                    try:
                        await self.handle_update(update)
                    except Exception:
                        logger.exception("Failed handling Telegram update")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Telegram poll error")
                await asyncio.sleep(5)

    def start_background(self) -> None:
        if not self.enabled:
            return
        self._task = asyncio.create_task(self.poll_forever())

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
