from __future__ import annotations

import re
from typing import Any

from backend.models import ClarificationOption, DraftItem, ExtractedItem
from backend.resolver import (
    _as_option,
    normalize_products,
    pick_cheapest_variant,
    pick_median_option,
    resolve_search_results,
    _option_from_variant,
)

CATEGORY_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\batta\b|\baata\b|\bflour\b|\bmaida\b", re.I), "atta"),
    (re.compile(r"\bmilk\b|\bdoodh\b|\bdahi\b|\bcurd\b|\byogurt\b", re.I), "milk"),
    (re.compile(r"\brice\b|\bchawal\b|\bchaval\b", re.I), "rice"),
    (re.compile(r"\blays\b|\bchips\b|\bkurkure\b|\bsnack", re.I), "snacks"),
    (re.compile(r"\bbread\b|\bpav\b", re.I), "bread"),
    (re.compile(r"\begg\b|\bande\b", re.I), "eggs"),
    (re.compile(r"\bpaneer\b", re.I), "paneer"),
    (re.compile(r"\boil\b|\brefined\b", re.I), "oil"),
    (re.compile(r"\bdal\b|\blentil\b|\bmoong\b|\btoor\b", re.I), "dal"),
]

CHEAP_WORDS = {"cheap", "cheapest", "sasta", "budget", "economy", "low", "kam"}
PREMIUM_WORDS = {
    "organic",
    "premium",
    "best",
    "quality",
    "sharbati",
    "biryani",
    "basmati",
    "imported",
}
GREEN_WORDS = {"green", "magic", "masala", "hari"}
RED_WORDS = {"red", "american", "cream", "classic"}


def category_key(*texts: str | None) -> str | None:
    combined = " ".join(t for t in texts if t).lower()
    if not combined:
        return None
    for pattern, key in CATEGORY_PATTERNS:
        if pattern.search(combined):
            return key
    return None


def preference_tags(text: str) -> set[str]:
    tokens = set(re.findall(r"[a-z]+", text.lower()))
    tags: set[str] = set()
    if tokens & CHEAP_WORDS:
        tags.add("cheap")
    if tokens & PREMIUM_WORDS:
        tags.add("premium")
    if tokens & GREEN_WORDS:
        tags.add("green")
    if tokens & RED_WORDS:
        tags.add("red")
    return tags


def preferences_conflict(a: set[str], b: set[str]) -> bool:
    if not a or not b:
        return False
    if a & b:
        return False
    if "cheap" in a and "premium" in b:
        return True
    if "premium" in a and "cheap" in b:
        return True
    if "green" in a and "red" in b:
        return True
    if "red" in a and "green" in b:
        return True
    return False


def queries_diverge(a: str, b: str) -> bool:
    ta = set(re.findall(r"[a-z0-9]+", a.lower())) - {"kardo", "krdo", "vale", "wale", "kg"}
    tb = set(re.findall(r"[a-z0-9]+", b.lower())) - {"kardo", "krdo", "vale", "wale", "kg"}
    if not ta or not tb:
        return False
    overlap = len(ta & tb) / max(len(ta | tb), 1)
    return overlap < 0.45


def find_conflicting_draft(
    drafts: list[DraftItem], new_item: ExtractedItem
) -> DraftItem | None:
    new_cat = category_key(new_item.raw_query, new_item.search_query)
    if not new_cat:
        return None

    new_prefs = preference_tags(
        f"{new_item.raw_query} {new_item.search_query or ''}"
    )

    for draft in drafts:
        if draft.excluded or draft.status in ("checked_out", "needs_poll"):
            continue
        if new_item.sender in draft.requested_by:
            continue

        draft_cat = category_key(draft.raw_query, draft.resolved_name)
        if draft_cat != new_cat:
            continue

        draft_prefs = preference_tags(f"{draft.raw_query} {draft.resolved_name or ''}")
        if preferences_conflict(new_prefs, draft_prefs):
            return draft
        if queries_diverge(
            new_item.search_query or new_item.raw_query,
            draft.raw_query,
        ):
            return draft

    return None


def _option_label(opt: ClarificationOption, letter: str) -> str:
    price = f" — ₹{opt.unit_price:.0f}" if opt.unit_price is not None else ""
    name = opt.name[:70]
    return f"{letter}) {name}{price}"[:100]


def select_poll_options(
    options: list[ClarificationOption | dict],
    existing: DraftItem,
    new_item: ExtractedItem,
    max_count: int = 4,
) -> list[ClarificationOption]:
    opts = [_as_option(o) for o in options]
    if not opts:
        return []

    seen: set[str] = set()
    picked: list[ClarificationOption] = []

    def add(opt: ClarificationOption | None) -> None:
        if not opt or opt.spin_id in seen:
            return
        seen.add(opt.spin_id)
        picked.append(opt)

    existing_prefs = preference_tags(existing.raw_query)
    new_prefs = preference_tags(new_item.raw_query)

    if "premium" in existing_prefs or "organic" in existing.raw_query.lower():
        premium = max(
            (o for o in opts if "organic" in o.name.lower() or "sharbati" in o.name.lower()),
            key=lambda o: o.unit_price or 0,
            default=None,
        )
        add(premium)
    if "cheap" in new_prefs or "sasta" in new_item.raw_query.lower():
        add(
            min(
                (o for o in opts if o.unit_price is not None),
                key=lambda o: o.unit_price or float("inf"),
                default=None,
            )
        )

    median = pick_median_option(opts)
    add(median)

    for opt in opts:
        if len(picked) >= max_count:
            break
        add(opt)

    return picked[:max_count]


def build_poll_from_options(
    existing: DraftItem,
    new_item: ExtractedItem,
    options: list[ClarificationOption],
) -> dict[str, Any]:
    letters = "ABCDEFGH"
    poll_options = []
    for i, opt in enumerate(options):
        poll_options.append(
            {
                "label": _option_label(opt, letters[i]),
                "spin_id": opt.spin_id,
                "name": opt.name,
                "pack_size": opt.pack_size,
                "unit_price": opt.unit_price,
                "product_id": opt.product_id,
            }
        )

    requesters = list(existing.requested_by)
    if new_item.sender not in requesters:
        requesters.append(new_item.sender)

    cat = category_key(existing.raw_query, new_item.raw_query) or "item"
    question = (
        f"🗳 Vote: {cat.title()} — group pick\n"
        f"{existing.requested_by[-1]}: {existing.raw_query}\n"
        f"{new_item.sender}: {new_item.raw_query}"
    )

    return {
        "draft_item_id": existing.id,
        "question": question[:300],
        "options": poll_options,
        "context": {
            "category": cat,
            "party_a": existing.requested_by[-1],
            "party_b": new_item.sender,
        },
    }


def options_from_search(search_data: dict, used_query: str, qty: int) -> list[ClarificationOption]:
    resolution = resolve_search_results(used_query, search_data, qty)
    raw = resolution.get("clarification_options") or resolution.get("alternatives") or []
    if raw:
        return [_as_option(o) for o in raw]

    if resolution.get("status") == "resolved" and resolution.get("resolved_sku"):
        return [
            ClarificationOption(
                spin_id=resolution["resolved_sku"],
                name=resolution.get("resolved_name") or used_query,
                pack_size=resolution.get("pack_size"),
                unit_price=resolution.get("unit_price"),
                product_id=resolution.get("product_id"),
            )
        ]

    products = normalize_products(search_data)
    options: list[ClarificationOption] = []
    for product in products[:5]:
        variant = pick_cheapest_variant(product)
        if variant:
            options.append(_option_from_variant(product, variant))
    return options


async def prepare_conflict_poll(
    existing: DraftItem,
    new_item: ExtractedItem,
    search_data: dict,
    used_query: str,
    qty: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Returns (draft_update_kwargs, poll_request)."""
    options = options_from_search(search_data, used_query, qty)
    poll_options = select_poll_options(options, existing, new_item)

    if len(poll_options) < 2 and len(options) >= 2:
        poll_options = options[:4]
    if len(poll_options) < 2:
        poll_options = options[: min(4, len(options))]

    poll_request = build_poll_from_options(existing, new_item, poll_options)

    requesters = list(existing.requested_by)
    if new_item.sender not in requesters:
        requesters.append(new_item.sender)

    draft_update = {
        "status": "needs_poll",
        "requested_by": requesters,
        "resolved_sku": None,
        "resolved_name": None,
        "pack_size": None,
        "unit_price": None,
        "product_id": None,
        "auto_picked": False,
        "merged_qty": max(existing.merged_qty, qty),
        "clarification_options": [o.model_dump() for o in poll_options],
        "alternatives": [],
        "note": (
            f"Group vote: {existing.requested_by[-1]} ({existing.raw_query}) "
            f"vs {new_item.sender} ({new_item.raw_query})"
        ),
    }
    return draft_update, poll_request
