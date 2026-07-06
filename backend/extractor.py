from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from backend.config import _ENV_FILE, gemini_configured, settings
from backend.models import ExtractedItem

SYSTEM_PROMPT = """You are the intent extractor for a Household Instamart Shopping app in INDIA.

## What this app does
- Family members send ONE chat message with many items (English/Hinglish, comma-separated).
- YOU split into SEPARATE items — one product per array entry. Never merge two products into one item.
- Each item gets search_query: what to type in Swiggy Instamart search (India).
- Backend calls Instamart MCP search_products with search_query. You never output SKUs/prices.

## Instamart sells MORE than groceries
Users can add ANYTHING sold on Instamart at their address, including:
- Groceries: atta, rice, milk, snacks, biscuits, oil, spices, fruits, vegetables
- Personal care: shampoo, soap, toothpaste, deodorant, face wash
- Home & cleaning: detergent, dishwash, tissues, garbage bags
- Baby & pet: diapers, pet food
- Fashion & apparel: t-shirt, shirt, socks, innerwear, slippers
- Electronics & accessories: phone charger, cable, earphones, power bank, phone case
- Kitchen & home: bottles, containers, bulbs, batteries
Do NOT refuse or relabel non-grocery items as groceries. Extract them normally.

## CRITICAL: split long messages
Comma, "aur", "and", "bhi" often separate products. Example input:
"mere lays vale green vale krdo, atta kardo 5kg, white tshirt medium, iphone charger"
MUST become separate JSON objects with good Instamart search strings.

## Indian Instamart search_query tips
- Fix typos: greeen→green, basamti→basmati, tshirt→t shirt
- Include pack size IN search_query when user says it: "5kg", "500ml", "medium", "large"
- Fashion: include type + size — "men cotton t shirt medium", "women socks pack"
- Electronics: include device when known — "type c charger", "lightning cable", "bluetooth earphones"
- Brands when obvious: Amul, Aashirvaad, Lay's, Samsung, boAt, Nike (if user said it)
- Hinglish fillers to REMOVE from search_query: kardo, krdo, mera, mere, vale, wale, please, chahiye

## Fields per item
- raw_query: user's phrase for one product
- search_query: Instamart search string (product type + brand + size/spec if mentioned)
- alternate_search_queries: 1-3 backups (e.g. t-shirt → "cotton t shirt", "round neck t shirt")
- sender: provided sender name
- needs_clarification: true ONLY if zero product hint ("kuch acha")

Return ONLY JSON array."""


@dataclass
class ExtractionTrace:
    provider: str
    user_message: str
    sender: str
    raw_response: str = ""
    items: list[ExtractedItem] = field(default_factory=list)
    error: str | None = None


def _parse_json_response(text: str, sender: str) -> list[ExtractedItem]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    raw_items = json.loads(text)
    if isinstance(raw_items, dict):
        raw_items = raw_items.get("items") or raw_items.get("products") or [raw_items]
    parsed: list[ExtractedItem] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        item.setdefault("sender", sender)
        if not item.get("search_query"):
            item["search_query"] = _clean_search_query(item.get("raw_query", ""))
        if not item.get("alternate_search_queries"):
            item["alternate_search_queries"] = []
        parsed.append(ExtractedItem(**item))
    return parsed


HINGLISH_STOP = re.compile(
    r"\b("
    r"kardo|krdo|karna|kar do|karke|dalo|de do|dedo|lana|lena|le lo|lelo|"
    r"mera|meri|mere|merer|bhai|please|plz|chahiye|order|"
    r"chota|choti|bada|badi|wala|wale|vale|vala|"
    r"packet|sachet"
    r")\b",
    re.IGNORECASE,
)


def _clean_search_query(text: str) -> str:
    t = text.strip()
    t = HINGLISH_STOP.sub(" ", t)
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"\bn\b(?=\s)", "and ", t, flags=re.IGNORECASE)
    t = re.sub(r"\s+", " ", t).strip()
    return t


# Known Indian product search expansions (used when Gemini alternates miss)
INDIA_SEARCH_ALIASES: list[tuple[re.Pattern[str], list[str]]] = [
    (re.compile(r"hide.*seek", re.I), ["parle hide and seek", "hide and seek biscuits"]),
    (re.compile(r"lays.*green|green.*lays", re.I), ["lays magic masala", "lays chips"]),
    (re.compile(r"\blays\b", re.I), ["lays chips", "lays magic masala"]),
    (re.compile(r"\batta\b", re.I), ["aashirvaad atta", "fortune atta", "atta"]),
    (re.compile(r"basmati|basamti|chawal", re.I), ["india gate basmati rice", "basmati rice biryani"]),
    (re.compile(r"soya.*bean|soya.*beasn", re.I), ["nutrela soya chunks", "soya bean"]),
    (re.compile(r"kinder.*joy", re.I), ["kinder joy treat", "kinder joy chocolate"]),
    (re.compile(r"kurkure", re.I), ["kurkure masala munch"]),
    (re.compile(r"\bmilk\b|doodh", re.I), ["amul taaza milk", "amul milk"]),
    (re.compile(r"t\s*shirt|tshirt|tee\b", re.I), ["cotton t shirt", "round neck t shirt", "men t shirt"]),
    (re.compile(r"\bshirt\b", re.I), ["formal shirt", "cotton shirt"]),
    (re.compile(r"charger|charging", re.I), ["mobile charger", "type c charger", "fast charger"]),
    (re.compile(r"earphone|earbud|headphone", re.I), ["wired earphones", "bluetooth earphones"]),
    (re.compile(r"phone case|cover", re.I), ["mobile back cover", "phone case"]),
    (re.compile(r"power bank|powerbank", re.I), ["power bank 10000mah"]),
    (re.compile(r"\bsocks\b", re.I), ["men socks", "cotton socks"]),
    (re.compile(r"shampoo", re.I), ["shampoo", "dove shampoo", "head and shoulders"]),
    (re.compile(r"soap|sabun", re.I), ["bathing soap", "dove soap"]),
]


def build_search_variants(query: str, alternates: list[str] | None = None) -> list[str]:
    variants: list[str] = []
    seen: set[str] = set()

    def add(q: str) -> None:
        q = q.strip()
        if not q or len(q) < 2:
            return
        if q.lower() in seen:
            return
        # Never search useless fragments
        if q.lower() in {"hide and", "and seek", "soya", "green"}:
            return
        seen.add(q.lower())
        variants.append(q)

    for q in [query, *(alternates or [])]:
        add(q)
    cleaned = _clean_search_query(query)
    if cleaned.lower() != query.lower():
        add(cleaned)

    for pattern, aliases in INDIA_SEARCH_ALIASES:
        if pattern.search(query) or pattern.search(cleaned):
            for a in aliases:
                add(a)

    return variants


def format_extraction_trace(trace: ExtractionTrace) -> str:
    lines = [f"--- Gemini ({trace.provider}) ---", f'Input: "{trace.user_message[:120]}..."' if len(trace.user_message) > 120 else f'Input: "{trace.user_message}"']
    if trace.error:
        lines.append(f"Error: {trace.error}")
        return "\n".join(lines)
    lines.append(f"Split into {len(trace.items)} item(s):")
    for i, item in enumerate(trace.items, 1):
        alts = ", ".join(item.alternate_search_queries[:2]) if item.alternate_search_queries else "-"
        lines.append(f"  {i}. raw=\"{item.raw_query}\" → search=\"{item.search_query}\" (alt: {alts})")
    return "\n".join(lines)


def format_search_trace(raw_query: str, attempts: list[tuple[str, int]], hint: str = "") -> str:
    lines = [f"--- Instamart MCP: \"{raw_query}\" ---"]
    for q, count in attempts:
        mark = "✓" if count > 0 else "✗"
        lines.append(f"  {mark} search_products(\"{q}\") → {count} products")
    if hint:
        lines.append(f"  ↳ {hint}")
    return "\n".join(lines)


def _fallback_extract(message: str, sender: str) -> list[ExtractedItem]:
    parts = re.split(r",|\band\b|\baur\b|\bbhi\b|\+|&", message, flags=re.IGNORECASE)
    items = []
    for part in parts:
        q = part.strip().strip(".")
        if not q or len(q) < 2:
            continue
        vague = q.lower() in {"something", "snacks", "stuff", "the usual"} or "something" in q.lower()
        items.append(
            ExtractedItem(
                raw_query=q,
                search_query=_clean_search_query(q),
                sender=sender,
                note="too vague" if vague else None,
                needs_clarification=vague,
            )
        )
    return items or [
        ExtractedItem(
            raw_query=message.strip(),
            search_query=_clean_search_query(message),
            sender=sender,
            needs_clarification=False,
        )
    ]


async def _extract_with_gemini(message: str, sender: str) -> tuple[list[ExtractedItem], str]:
    from google import genai

    client = genai.Client(api_key=settings.gemini_api_key)
    user_prompt = (
        f"Sender: {sender}\n"
        f'Household chat message: "{message}"\n\n'
        "Split into separate Instamart shopping items (groceries, fashion, electronics, home, personal care — anything on Instamart). Return JSON array for Swiggy Instamart India."
    )

    response = await client.aio.models.generate_content(
        model=settings.gemini_model,
        contents=user_prompt,
        config={
            "system_instruction": SYSTEM_PROMPT,
            "response_mime_type": "application/json",
            "max_output_tokens": 2048,
        },
    )
    text = response.text or "[]"
    return _parse_json_response(text, sender), text


async def extract_items_with_trace(message: str, sender: str) -> ExtractionTrace:
    provider = settings.extraction_provider.lower().strip()
    trace = ExtractionTrace(provider=provider, user_message=message, sender=sender)

    try:
        if provider == "gemini":
            if not gemini_configured():
                trace.provider = "fallback"
                trace.error = (
                    "GEMINI_API_KEY missing or empty in .env file on disk. "
                    f"Edit {_ENV_FILE} — save the file — restart uvicorn. "
                    "(Unsaved editor buffer does not count.)"
                )
                trace.items = _fallback_extract(message, sender)
                return trace
            trace.items, trace.raw_response = await _extract_with_gemini(message, sender)
            trace.provider = "gemini"
        elif provider == "anthropic" and settings.anthropic_api_key:
            trace.items = await _extract_with_anthropic(message, sender)
            trace.raw_response = json.dumps([i.model_dump() for i in trace.items])
        else:
            trace.provider = "fallback"
            trace.error = trace.error or f"Provider '{provider}' not configured"
            trace.items = _fallback_extract(message, sender)
            trace.raw_response = json.dumps([i.model_dump() for i in trace.items])
    except Exception as exc:
        trace.error = f"Gemini API error: {exc}"
        trace.provider = "fallback"
        trace.items = _fallback_extract(message, sender)

    return trace


async def _extract_with_anthropic(message: str, sender: str) -> list[ExtractedItem]:
    import anthropic

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    user_prompt = (
        f"Sender: {sender}\n"
        f'Household chat message: "{message}"\n\n'
        "Split into separate Instamart shopping items (groceries, fashion, electronics, home, personal care — anything on Instamart). Return JSON array for Swiggy Instamart India."
    )

    response = await client.messages.create(
        model=settings.extraction_model,
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )

    text = ""
    for block in response.content:
        if block.type == "text":
            text += block.text
    return _parse_json_response(text, sender)


async def extract_items(message: str, sender: str) -> list[ExtractedItem]:
    trace = await extract_items_with_trace(message, sender)
    return trace.items
