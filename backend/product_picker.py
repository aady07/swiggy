from __future__ import annotations

import json
import re
from typing import Any

from backend.config import gemini_configured, settings
from backend.models import ClarificationOption

PICKER_SYSTEM_PROMPT = """You pick ONE Instamart product variant for an Indian household shopping app.

Instamart sells groceries AND non-grocery items (fashion, electronics, personal care, home).
Match the user's intent whether they asked for atta, t-shirt, charger, or shampoo.

You receive:
- raw_query: what the user typed (English/Hinglish)
- search_query: what was searched on Instamart
- options: numbered list with spin_id, name, pack_size, unit_price (ONLY pick from this list)
- past_orders: items this household ordered before (prefer if same intent)

Rules:
1. Return ONLY valid JSON: {"spin_id": "...", "reason": "5 words max"}
2. spin_id MUST be exactly one of the provided option spin_ids — never invent
3. Match user INTENT, not just cheapest:
   - "biryani wala chawal" / "biryani rice" → basmati biryani rice, NOT regular rice
   - "green lays" / "lays green wale" → Lay's Magic Masala (green pack), NOT American Cream
   - "kinder joy without toy" / "chota without toy" → Kinder Joy Treat / chocolate only variant
   - "hide n seek" → Parle Hide & Seek biscuits
4. Respect pack size in query (5kg → pick 5kg option if available)
5. If past_orders clearly match one option, pick that
6. If unsure between two, pick the one whose name best matches search_query semantics

Examples:
- query "basmati chawal biryani 5kg" → India Gate Basmati Biryani Rice 5kg
- query "lays magic masala" → Lay's Magic Masala
- query "white tshirt medium" → cotton round neck t-shirt medium
- query "type c charger" → USB Type C fast charger
- query "atta 5kg" with no past order → common whole wheat/sharbati atta 5kg matching query"""


def _as_option(raw: ClarificationOption | dict) -> ClarificationOption:
    if isinstance(raw, ClarificationOption):
        return raw
    return ClarificationOption(**raw)


def _extract_pack_hint(query: str) -> str | None:
    match = re.search(r"(\d+\s*(?:kg|g|gm|gram|l|ml|litre|ltr))", query, re.IGNORECASE)
    return match.group(1) if match else None


def _filter_options_by_pack(
    options: list[ClarificationOption], pack_hint: str | None
) -> list[ClarificationOption]:
    if not pack_hint:
        return options
    nums = re.findall(r"[\d.]+", pack_hint)
    if not nums:
        return options
    target = nums[0]
    matched = [
        o
        for o in options
        if o.pack_size and target in re.sub(r"\s+", "", str(o.pack_size).lower())
    ]
    return matched or options


def _format_options_for_prompt(options: list[ClarificationOption]) -> list[dict[str, Any]]:
    rows = []
    for i, opt in enumerate(options[:8], start=1):
        rows.append(
            {
                "n": i,
                "spin_id": opt.spin_id,
                "name": opt.name,
                "pack_size": opt.pack_size,
                "unit_price": opt.unit_price,
            }
        )
    return rows


def _format_preferences_for_prompt(preferences: list[dict], limit: int = 12) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for pref in preferences:
        name = str(pref.get("name") or pref.get("resolved_name") or "").strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        names.append(name)
        if len(names) >= limit:
            break
    return names


def _parse_picker_response(text: str) -> dict[str, str] | None:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    spin_id = str(data.get("spin_id") or "").strip()
    reason = str(data.get("reason") or "AI matched intent").strip()
    if not spin_id:
        return None
    return {"spin_id": spin_id, "reason": reason}


async def rank_options_for_recommendation(
    options: list[ClarificationOption | dict],
    raw_query: str,
    search_query: str | None = None,
) -> list[ClarificationOption]:
    """Order options for display (best match first). Does not select one."""
    opts = [_as_option(o) for o in options]
    if len(opts) <= 1 or not gemini_configured():
        return opts

    payload = {
        "raw_query": raw_query,
        "search_query": search_query or raw_query,
        "options": _format_options_for_prompt(opts),
        "task": "Rank these options best-to-worst for the user's intent. Return JSON: {\"ranked_spin_ids\": [\"id1\", \"id2\", ...]}",
    }

    from google import genai

    client = genai.Client(api_key=settings.gemini_api_key.strip())
    try:
        response = client.models.generate_content(
            model=settings.gemini_model,
            contents=json.dumps(payload, ensure_ascii=False),
            config={
                "system_instruction": (
                    "You rank Instamart product options for an Indian user. "
                    "Return only JSON with ranked_spin_ids — every id must come from the input list."
                ),
                "temperature": 0.1,
                "response_mime_type": "application/json",
            },
        )
        data = json.loads(response.text or "{}")
        ranked_ids = data.get("ranked_spin_ids") or []
    except Exception:
        return opts

    by_spin = {o.spin_id: o for o in opts}
    ranked: list[ClarificationOption] = []
    for spin_id in ranked_ids:
        if spin_id in by_spin and spin_id not in {o.spin_id for o in ranked}:
            ranked.append(by_spin[spin_id])
    for opt in opts:
        if opt.spin_id not in {o.spin_id for o in ranked}:
            ranked.append(opt)
    return ranked


async def pick_option_with_ai(
    options: list[ClarificationOption | dict],
    raw_query: str,
    preferences: list[dict] | None = None,
    search_query: str | None = None,
) -> tuple[ClarificationOption | None, str | None]:
    """Rank Instamart options with Gemini. Returns (option, reason) or (None, None)."""
    if not gemini_configured():
        return None, None

    opts = [_as_option(o) for o in options]
    if len(opts) < 2:
        return (opts[0], "single option") if opts else (None, None)

    pack_hint = _extract_pack_hint(raw_query) or _extract_pack_hint(search_query or "")
    candidates = _filter_options_by_pack(opts, pack_hint)

    payload = {
        "raw_query": raw_query,
        "search_query": search_query or raw_query,
        "options": _format_options_for_prompt(candidates),
        "past_orders": _format_preferences_for_prompt(preferences or []),
    }

    from google import genai

    client = genai.Client(api_key=settings.gemini_api_key.strip())
    try:
        response = client.models.generate_content(
            model=settings.gemini_model,
            contents=json.dumps(payload, ensure_ascii=False),
            config={
                "system_instruction": PICKER_SYSTEM_PROMPT,
                "temperature": 0.1,
                "response_mime_type": "application/json",
            },
        )
        parsed = _parse_picker_response(response.text or "")
    except Exception:
        return None, None

    if not parsed:
        return None, None

    by_spin = {o.spin_id: o for o in candidates}
    chosen = by_spin.get(parsed["spin_id"])
    if not chosen:
        return None, None

    reason = parsed.get("reason") or "AI matched intent"
    return chosen, f"AI: {reason}"
