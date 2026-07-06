from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from backend.config import settings
from backend.db import db

if TYPE_CHECKING:
    from backend.telegram_bot import TelegramBot
    from backend.whatsapp_bot import WhatsAppBot

logger = logging.getLogger(__name__)


def _norm_phone(phone: str) -> str:
    return re.sub(r"\D", "", phone)


def _phone_map() -> dict[str, str]:
    out: dict[str, str] = {}
    for part in settings.whatsapp_phone_map.split(","):
        part = part.strip()
        if ":" not in part:
            continue
        phone, name = part.split(":", 1)
        digits = _norm_phone(phone)
        if digits:
            out[digits] = name.strip()
    return out


async def all_whatsapp_recipients() -> dict[str, str]:
    """Normalized phone digits -> household member name."""
    phones: dict[str, str] = dict(_phone_map())
    for key, name in (await db.list_settings_with_prefix("whatsapp_name:")).items():
        digits = _norm_phone(key.removeprefix("whatsapp_name:"))
        if digits:
            phones[digits] = name.strip()
    for key, name in (await db.list_settings_with_prefix("whatsapp_contact:")).items():
        digits = _norm_phone(key.removeprefix("whatsapp_contact:"))
        if digits and digits not in phones:
            phones[digits] = name.strip()
    return phones


async def all_member_names() -> dict[str, dict[str, str]]:
    by_name: dict[str, dict[str, str]] = {}
    for phone, name in (await all_whatsapp_recipients()).items():
        by_name.setdefault(name, {})["whatsapp"] = phone
    for key, name in (await db.list_settings_with_prefix("telegram_name:")).items():
        by_name.setdefault(name, {})["telegram"] = key.removeprefix("telegram_name:")
    return by_name


async def resolve_telegram_name(user_id: str, fallback: str) -> str:
    stored = await db.get_setting(f"telegram_name:{user_id}")
    return stored or fallback


async def record_whatsapp_contact(phone: str, name: str) -> None:
    """Remember every phone that has texted the bot (for notify recipient list)."""
    digits = _norm_phone(phone)
    if not digits or not name:
        return
    await db.set_setting(f"whatsapp_contact:{digits}", name)
    await db.set_setting(f"whatsapp_name:{digits}", name)


async def save_telegram_member(user_id: str, chat_id: str, name: str) -> None:
    await db.set_setting(f"telegram_name:{user_id}", name)
    await db.set_setting(f"telegram_chat:{user_id}", chat_id)
    if str(chat_id).startswith("-"):
        await db.set_setting("telegram_household_chat_id", str(chat_id))
    else:
        try:
            if int(chat_id) < 0:
                await db.set_setting("telegram_household_chat_id", str(chat_id))
        except (TypeError, ValueError):
            pass


async def save_whatsapp_member(phone: str, name: str) -> None:
    await record_whatsapp_contact(phone, name)


async def broadcast_add(
    sender: str,
    added: list[str],
    *,
    source: str,
    source_whatsapp_phone: str | None = None,
    source_telegram_chat: str | None = None,
    whatsapp_bot: WhatsAppBot | None = None,
    telegram_bot: TelegramBot | None = None,
) -> str:
    if not added:
        return ""

    items = "\n".join(f"• {a}" for a in added[:8])
    msg = f"📢 {sender} added to shared cart:\n{items}\n\n/cart to see all."
    tg_msg = f"📢 {sender} added ({source}):\n{items}"
    notified: list[str] = []
    failed: list[str] = []
    fail_reasons: list[str] = []
    sender_digits = _norm_phone(source_whatsapp_phone or "")

    recipients = await all_whatsapp_recipients()
    others = {
        phone: name
        for phone, name in recipients.items()
        if phone != sender_digits
    }

    logger.info(
        "broadcast_add sender=%s source=%s items=%s recipients=%s others=%s",
        sender,
        source,
        added,
        list(recipients.keys()),
        list(others.keys()),
    )

    if whatsapp_bot and whatsapp_bot.enabled:
        if not others:
            logger.info(
                "broadcast_add: no other WhatsApp phones — add WHATSAPP_PHONE_MAP "
                "or have each family member text the bot once"
            )
        for phone, name in others.items():
            ok, reason = await whatsapp_bot.send_text_with_reason(phone, msg)
            if ok:
                notified.append(name)
                logger.info("broadcast_add: notified %s (%s)", name, phone)
            else:
                failed.append(name)
                fail_reasons.append(f"{name}: {reason}")
                logger.warning(
                    "broadcast_add: failed %s (%s): %s", name, phone, reason
                )
    elif not whatsapp_bot or not whatsapp_bot.enabled:
        logger.info("broadcast_add: WhatsApp bot not enabled")

    group_chat = await db.get_setting("telegram_household_chat_id")
    if telegram_bot and telegram_bot.enabled and group_chat:
        from_group = (
            source == "telegram" and str(source_telegram_chat) == str(group_chat)
        )
        if not from_group:
            if await telegram_bot.send_message(group_chat, tg_msg):
                notified.append("Telegram group")
                logger.info("broadcast_add: notified Telegram group %s", group_chat)
            else:
                logger.warning("broadcast_add: Telegram group notify failed")
    elif source != "telegram":
        logger.info(
            "broadcast_add: no telegram_household_chat_id — add bot to family group"
        )

    if notified:
        return f"\n\n📢 Notified: {', '.join(notified)}"
    if failed:
        hint = fail_reasons[0] if len(fail_reasons) == 1 else "see server logs"
        return (
            f"\n\n⚠️ Couldn't notify {', '.join(failed)} ({hint}). "
            "Each person must message the bot once, and be in Meta recipient list."
        )
    if len(recipients) <= 1:
        return (
            "\n\nℹ️ Add all family +91 numbers to WHATSAPP_PHONE_MAP in .env "
            "(or have them text the bot once)."
        )
    return ""
