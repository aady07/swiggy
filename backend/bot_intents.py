from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class ChatIntentKind(str, Enum):
    SHOW_CART = "show_cart"
    CLEAR_CART = "clear_cart"
    PLACE_ORDER = "place_order"
    INSTAMART_WEB = "instamart_web"
    REMOVE_LINE = "remove_line"
    CONNECT = "connect"
    HELP = "help"
    ADD_ITEMS = "add_items"


@dataclass(frozen=True)
class ChatIntent:
    kind: ChatIntentKind
    line_number: int | None = None


_SHOW_CART = re.compile(
    r"(?:"
    r"(?:show|view|see|check|open|display|list)(?:\s+me)?(?:\s+the|\s+my|\s+our)?\s+cart|"
    r"(?:what(?:'s|s|\s+is)\s+in)(?:\s+the|\s+my|\s+our)?\s+cart|"
    r"cart\s+(?:dikhao|dikha\s*do|batao|bata\s*do|show\s*karo|dekho|list|kya\s+hai|dikha\s*do)|"
    r"(?:dikhao|batao|dekho)\s+(?:the\s+|my\s+|poora\s+)?cart|"
    r"(?:my|current|shared|family)\s+cart|"
    r"cart\s+(?:me|mai|main)\s+kya\s+hai"
    r")",
    re.I,
)

_CLEAR_CART = re.compile(
    r"(?:"
    r"(?:clear|empty|reset|wipe)(?:\s+the|\s+my|\s+whole)?\s+cart|"
    r"cart\s+(?:clear|khali|empty)(?:\s+karo|\s+kar\s*do|\s+kr\s*do)?|"
    r"(?:sab|saara)\s+(?:hatao|hata\s*do|clear)(?:\s+kar\s*do)?|"
    r"cart\s+khali\s+karo"
    r")",
    re.I,
)

_PLACE_ORDER = re.compile(
    r"^(?:/)?(?:order|place(?:\s+the)?\s+order|checkout)"
    r"(?:\s+(?:this|the)\s+cart)?\s*$",
    re.I,
)
_PLACE_ORDER_HINGLISH = re.compile(
    r"^(?:order|place)\s+(?:this\s+)?(?:cart|order)(?:\s+kar(?:\s+do)?)?\s*$",
    re.I,
)

_INSTAMART = re.compile(
    r"(?:"
    r"(?:real|actual|swiggy)\s+(?:instamart\s+)?(?:order|delivery|checkout)|"
    r"instamart\s+(?:order|delivery|checkout|se\s+order)|"
    r"place\s+(?:real|actual)\s+order|"
    r"/instamart"
    r")",
    re.I,
)

_REMOVE = re.compile(
    r"^(?:remove|delete|hatao|nikal)(?:\s+(?:item|line|#))?\s*#?(\d+)\s*$",
    re.I,
)

_CONNECT = re.compile(
    r"(?:"
    r"(?:connect|login|link)\s+swiggy|"
    r"swiggy\s+(?:connect|login|link)|"
    r"/connect"
    r")",
    re.I,
)

_HELP = re.compile(
    r"^(?:"
    r"help|commands|menu|"
    r"what\s+can\s+you\s+do|"
    r"kya\s+kar\s+sakte\s+ho|"
    r"bot\s+help"
    r")$",
    re.I,
)


def detect_chat_intent(text: str) -> ChatIntent:
    """Map natural chat text to a bot action before grocery extraction."""
    t = text.strip()
    if not t:
        return ChatIntent(ChatIntentKind.ADD_ITEMS)

    if t.startswith("/"):
        return ChatIntent(ChatIntentKind.ADD_ITEMS)

    remove = _REMOVE.match(t)
    if remove:
        return ChatIntent(ChatIntentKind.REMOVE_LINE, int(remove.group(1)))

    if _HELP.match(t):
        return ChatIntent(ChatIntentKind.HELP)

    if _CONNECT.search(t):
        return ChatIntent(ChatIntentKind.CONNECT)

    if _PLACE_ORDER.match(t) or _PLACE_ORDER_HINGLISH.match(t):
        return ChatIntent(ChatIntentKind.PLACE_ORDER)

    if _INSTAMART.search(t):
        return ChatIntent(ChatIntentKind.INSTAMART_WEB)

    if _CLEAR_CART.search(t):
        return ChatIntent(ChatIntentKind.CLEAR_CART)

    if _SHOW_CART.search(t):
        return ChatIntent(ChatIntentKind.SHOW_CART)

    return ChatIntent(ChatIntentKind.ADD_ITEMS)


def is_place_order_message(text: str) -> bool:
    return detect_chat_intent(text).kind == ChatIntentKind.PLACE_ORDER
