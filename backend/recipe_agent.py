from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any, Literal

from backend.config import gemini_configured, settings
from backend.db import db
from backend.models import ClarificationOption, ExtractedItem, as_clarification_option
from backend.mcp_client import SwiggyInstamartClient
from backend.resolver import (
    fetch_order_preferences,
    merge_into_existing,
    resolve_search_results,
    search_products_with_fallback,
    try_auto_pick_clarification_async,
)

logger = logging.getLogger(__name__)

RecipeLineStatus = Literal["found", "not_found", "needs_pick"]

_RECIPE_ASK = re.compile(
    r"(?:"
    r"kya\s+(?:kya\s+)?(?:lagega|lagta|lagenge|chahiye|chaiye|lgega|lge|lage)"
    r"|what\s+(?:do\s+i\s+need|ingredients|all\s+do\s+i\s+need)"
    r"|ingredients?\s+for"
    r"|banane\s+(?:me|ke\s+liye|hai)"
    r"|bana(?:ne|na)\s+(?:hai|h)"
    r"|recipe\s+for"
    r"|saman\s+(?:chahiye|lgega|lagega)"
    r")",
    re.I,
)

_RECIPE_ADD_ALL = re.compile(
    r"(?:"
    r"sab\s+add|add\s+all|saare\s+add|sab\s+kardo|sab\s+kar\s*do|"
    r"haan\s+add|add\s+kar\s*do|add\s+everything|sab\s+daal\s*do|"
    r"yes\s+add\s+all"
    r")",
    re.I,
)

_RECIPE_ADD_NUMBERS = re.compile(
    r"^(?:add|daal|include)\s+([\d\s,and]+)$",
    re.I,
)

_DISH_PATTERNS = [
    re.compile(
        r"(?:(?:aaj|aj)\s+)?(?:mujhe\s+)?(.+?)\s+banane\s+(?:me|ke|hai)",
        re.I,
    ),
    re.compile(
        r"(?:(?:aaj|aj)\s+)?(?:mujhe\s+)?(.+?)\s+bana(?:na|ne)\s+(?:hai|h)",
        re.I,
    ),
    re.compile(r"(?:make|cook)\s+(.+?)(?:\s+what|\s+ingredients|$)", re.I),
    re.compile(r"ingredients?\s+for\s+(.+)", re.I),
    re.compile(r"(.+?)\s+ke\s+liye\s+kya\s+(?:kya\s+)?lagega", re.I),
    re.compile(r"(.+?)\s+recipe", re.I),
]

_FALLBACK_INGREDIENTS: dict[str, list[dict[str, str]]] = {
    "chole bhature": [
        {"name": "kabuli chana (chole)", "search_query": "kabuli chana"},
        {"name": "atta", "search_query": "atta whole wheat"},
        {"name": "onion", "search_query": "onion 1kg"},
        {"name": "tomato", "search_query": "tomato fresh"},
        {"name": "ginger", "search_query": "ginger adrak"},
        {"name": "refined oil", "search_query": "refined oil 1 litre"},
        {"name": "chole masala", "search_query": "chole masala"},
        {"name": "curd", "search_query": "curd dahi"},
    ],
    "paneer butter masala": [
        {"name": "paneer", "search_query": "paneer fresh"},
        {"name": "butter", "search_query": "amul butter"},
        {"name": "tomato", "search_query": "tomato fresh"},
        {"name": "onion", "search_query": "onion 1kg"},
        {"name": "cream", "search_query": "fresh cream"},
        {"name": "ginger garlic", "search_query": "ginger garlic paste"},
        {"name": "kasuri methi", "search_query": "kasuri methi"},
    ],
}

_INGREDIENT_SYSTEM = """You list Instamart grocery ingredients for Indian home cooking.
Return ONLY JSON:
{"dish": "...", "ingredients": [{"name": "...", "search_query": "..."}, ...]}
Rules:
- 6 to 10 essential ingredients only (not optional garnish unless critical)
- search_query = short Instamart search string (brand optional)
- Indian household portions
- No cooking steps, only shopping list"""


def is_recipe_question(text: str) -> bool:
    return bool(_RECIPE_ASK.search(text.strip()))


def is_recipe_add_all(text: str) -> bool:
    return bool(_RECIPE_ADD_ALL.search(text.strip()))


def parse_recipe_add_numbers(text: str) -> list[int] | None:
    m = _RECIPE_ADD_NUMBERS.match(text.strip())
    if not m:
        return None
    nums = re.findall(r"\d+", m.group(1))
    return [int(n) for n in nums] if nums else None


def _clean_dish(name: str) -> str:
    name = re.sub(
        r"^(?:aaj|mujhe|me|main|please|batao|bata|dikhao|tell)\s+",
        "",
        name.strip(),
        flags=re.I,
    )
    name = re.sub(
        r"\s+(?:banane|banana|bana|ke\s+liye|me|hai|h|kya.*)$",
        "",
        name,
        flags=re.I,
    )
    return re.sub(r"\s+", " ", name).strip(" ?.,!")


def extract_dish_name(text: str) -> str | None:
    t = text.strip()
    if not is_recipe_question(t):
        return None
    for pat in _DISH_PATTERNS:
        m = pat.search(t)
        if m:
            dish = _clean_dish(m.group(1))
            if dish and len(dish) > 2:
                return dish
    # "chole bhature ... kya lagega" — take words before recipe cue
    m = re.match(r"^(.+?)\s+(?:kya|what)\s", t, re.I)
    if m:
        dish = _clean_dish(m.group(1))
        if dish and len(dish) > 2:
            return dish
    return None


async def extract_dish_with_ai(text: str) -> str | None:
    if not gemini_configured():
        return None
    from google import genai

    client = genai.Client(api_key=settings.gemini_api_key.strip())
    try:
        response = client.models.generate_content(
            model=settings.gemini_model,
            contents=text,
            config={
                "system_instruction": (
                    "Extract the dish/meal name from a recipe question in English or Hinglish. "
                    'Return JSON only: {"dish": "chole bhature"}'
                ),
                "temperature": 0.1,
                "response_mime_type": "application/json",
            },
        )
        data = json.loads(response.text or "{}")
        dish = str(data.get("dish") or "").strip()
        return dish or None
    except Exception:
        logger.exception("Dish extraction failed")
        return None


async def generate_ingredients(dish: str) -> list[dict[str, str]]:
    key = dish.lower().strip()
    for known, items in _FALLBACK_INGREDIENTS.items():
        if known in key or key in known:
            return list(items)

    if not gemini_configured():
        return [{"name": dish, "search_query": dish}]

    from google import genai

    client = genai.Client(api_key=settings.gemini_api_key.strip())
    try:
        response = client.models.generate_content(
            model=settings.gemini_model,
            contents=f"Dish: {dish}",
            config={
                "system_instruction": _INGREDIENT_SYSTEM,
                "temperature": 0.2,
                "response_mime_type": "application/json",
            },
        )
        data = json.loads(response.text or "{}")
        rows = data.get("ingredients") or []
        out: list[dict[str, str]] = []
        for row in rows[:10]:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or "").strip()
            sq = str(row.get("search_query") or name).strip()
            if name:
                out.append({"name": name, "search_query": sq})
        if out:
            return out
    except Exception:
        logger.exception("Ingredient generation failed for %s", dish)

    return [{"name": dish, "search_query": dish}]


async def lookup_ingredient(
    client: SwiggyInstamartClient,
    address_id: str,
    sender: str,
    ingredient: dict[str, str],
    preferences: list[dict],
) -> dict[str, Any]:
    name = ingredient["name"]
    search_q = ingredient.get("search_query") or name
    item = ExtractedItem(
        raw_query=name,
        sender=sender,
        search_query=search_q,
        alternate_search_queries=[],
    )
    search_data, used_query, _attempts = await search_products_with_fallback(
        client, address_id, item
    )
    resolution = resolve_search_results(used_query, search_data, 1)
    if resolution["status"] in ("needs_clarification", "out_of_stock") and (
        resolution.get("clarification_options") or resolution.get("alternatives")
    ):
        resolution = await try_auto_pick_clarification_async(
            resolution,
            preferences,
            name,
            1,
            search_query=search_q,
        )

    if resolution["status"] == "resolved":
        return {
            "ingredient": name,
            "search_query": search_q,
            "status": "found",
            "resolved_name": resolution.get("resolved_name"),
            "spin_id": resolution.get("resolved_sku"),
            "unit_price": resolution.get("unit_price"),
            "pack_size": resolution.get("pack_size"),
            "product_id": resolution.get("product_id"),
            "auto_picked": resolution.get("auto_picked", False),
            "note": resolution.get("note"),
            "clarification_options": [],
        }

    options = resolution.get("clarification_options") or resolution.get("alternatives") or []
    if options:
        return {
            "ingredient": name,
            "search_query": search_q,
            "status": "needs_pick",
            "resolved_name": None,
            "spin_id": None,
            "unit_price": None,
            "pack_size": None,
            "product_id": None,
            "clarification_options": options[:5],
            "note": resolution.get("note"),
        }

    return {
        "ingredient": name,
        "search_query": search_q,
        "status": "not_found",
        "resolved_name": None,
        "note": resolution.get("note") or "Not on Instamart at your address",
        "clarification_options": [],
    }


async def run_recipe_lookup(
    client: SwiggyInstamartClient,
    household_id: str,
    address_id: str,
    dish: str,
    sender: str,
) -> dict[str, Any]:
    preferences = await fetch_order_preferences(client, household_id, address_id)
    ingredients = await generate_ingredients(dish)
    lines: list[dict[str, Any]] = []
    for ing in ingredients:
        line = await lookup_ingredient(client, address_id, sender, ing, preferences)
        lines.append(line)

    session_id = uuid.uuid4().hex[:10]
    session = {
        "id": session_id,
        "dish": dish,
        "sender": sender,
        "household_id": household_id,
        "lines": lines,
    }
    await save_recipe_session(session_id, session)
    return session


def _session_db_key(session_id: str) -> str:
    return f"recipe_session:{session_id}"


async def save_recipe_session(session_id: str, session: dict[str, Any]) -> None:
    await db.set_setting(_session_db_key(session_id), json.dumps(session, ensure_ascii=False))
    sender = session.get("sender")
    if sender:
        await db.set_setting(f"recipe_latest:{sender}", session_id)


async def load_recipe_session(session_id: str) -> dict[str, Any] | None:
    raw = await db.get_setting(_session_db_key(session_id))
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


async def delete_recipe_session(session_id: str) -> None:
    await db.set_setting(_session_db_key(session_id), "")


async def get_latest_session_for_sender(sender: str) -> dict[str, Any] | None:
    sid = await db.get_setting(f"recipe_latest:{sender}")
    if not sid:
        return None
    return await load_recipe_session(sid)


async def resolve_dish_from_text(text: str) -> str | None:
    dish = extract_dish_name(text)
    if dish:
        return dish
    if is_recipe_question(text):
        return await extract_dish_with_ai(text)
    return None


async def commit_recipe_line(
    household_id: str,
    sender: str,
    line: dict[str, Any],
) -> str | None:
    if line.get("status") != "found" or not line.get("spin_id"):
        return None
    product_id = line.get("product_id")
    if product_id:
        merged = await merge_into_existing(household_id, product_id, 1, sender)
        if merged:
            return line.get("resolved_name") or line.get("ingredient")

    await db.insert_draft_item(
        household_id,
        raw_query=line.get("ingredient") or line.get("search_query") or "item",
        requested_by=[sender],
        status="resolved",
        resolved_sku=line.get("spin_id"),
        resolved_name=line.get("resolved_name"),
        pack_size=line.get("pack_size"),
        unit_price=line.get("unit_price"),
        merged_qty=1,
        product_id=product_id,
        auto_picked=bool(line.get("auto_picked")),
        note=f"Recipe: {line.get('ingredient')}",
    )
    return line.get("resolved_name") or line.get("ingredient")


async def add_recipe_lines(
    session: dict[str, Any],
    indices: list[int] | None = None,
) -> list[str]:
    household_id = session.get("household_id") or settings.household_id
    sender = session.get("sender") or "Someone"
    lines = session.get("lines") or []
    added: list[str] = []

    if indices is None:
        target_idx = [i for i, ln in enumerate(lines) if ln.get("status") == "found"]
    else:
        target_idx = [i - 1 for i in indices if 0 < i <= len(lines)]

    for idx in target_idx:
        if idx < 0 or idx >= len(lines):
            continue
        name = await commit_recipe_line(household_id, sender, lines[idx])
        if name:
            added.append(name)
    return added


async def create_pick_draft_for_line(
    session: dict[str, Any],
    line_index: int,
) -> dict[str, Any] | None:
    lines = session.get("lines") or []
    if line_index < 0 or line_index >= len(lines):
        return None
    line = lines[line_index]
    if line.get("status") != "needs_pick":
        return None
    household_id = session.get("household_id") or settings.household_id
    sender = session.get("sender") or "Someone"
    draft = await db.insert_draft_item(
        household_id,
        raw_query=line.get("ingredient") or "item",
        requested_by=[sender],
        status="needs_clarification",
        clarification_options=line.get("clarification_options") or [],
        alternatives=[],
        note=f"Recipe pick: {session.get('dish')}",
    )
    return {
        "draft_item_id": draft.id,
        "raw_query": draft.raw_query,
        "note": draft.note,
        "options": [
            as_clarification_option(o).model_dump()
            for o in draft.clarification_options
        ],
    }


# User-facing copy lives in bot_copy.py
from backend.bot_copy import format_recipe_list, recipe_list_keyboard  # noqa: E402, F401
