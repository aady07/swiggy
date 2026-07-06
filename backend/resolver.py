from __future__ import annotations

import re
from typing import Any

from backend.db import db
from backend.extractor import _clean_search_query, build_search_variants, format_search_trace
from backend.models import ClarificationOption, DraftItem, ExtractedItem
from backend.mcp_client import SwiggyInstamartClient


def _first(d: dict, *keys: str, default: Any = None) -> Any:
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def _parse_price(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        for k in ("offerPrice", "price", "mrp", "finalPrice", "value"):
            if k in value and value[k] is not None:
                return _parse_price(value[k])
        return None
    if isinstance(value, str):
        nums = re.findall(r"[\d.]+", value.replace(",", ""))
        return float(nums[0]) if nums else None
    return None


def _variant_in_stock(variant: dict, product: dict | None = None) -> bool:
    for key in (
        "inStock",
        "isAvailable",
        "available",
        "stockAvailable",
        "isInStockAndAvailable",
        "isAvail",
    ):
        if key in variant:
            return bool(variant[key])
    if product:
        for key in ("inStock", "isAvail", "isAvailable"):
            if key in product:
                return bool(product[key])
    status = str(_first(variant, "stockStatus", "availability", default="")).lower()
    if status:
        return status in {"instock", "in_stock", "available", "true"}
    return True


def _extract_variants_list(product: dict) -> list[dict]:
    for key in ("variations", "variants", "variantList", "skus", "items"):
        val = product.get(key)
        if isinstance(val, list) and val:
            return [v for v in val if isinstance(v, dict)]
    if any(k in product for k in ("spinId", "skuId", "spin_id")):
        return [product]
    return []


def _extract_quantity(raw_query: str) -> tuple[str, int]:
    m = re.match(r"^(\d+)\s+(.+)$", raw_query.strip(), re.IGNORECASE)
    if m:
        return m.group(2).strip(), max(1, int(m.group(1)))
    return raw_query.strip(), 1


def normalize_products(search_data: dict | list | Any) -> list[dict]:
    if isinstance(search_data, list):
        products = search_data
    elif isinstance(search_data, dict):
        products = (
            search_data.get("products")
            or search_data.get("items")
            or search_data.get("productList")
            or search_data.get("searchResults")
            or []
        )
        if not products:
            for key in ("data", "result", "catalog", "searchResult"):
                val = search_data.get(key)
                if isinstance(val, list):
                    products = val
                    break
                if isinstance(val, dict):
                    products = (
                        val.get("products")
                        or val.get("items")
                        or val.get("productList")
                        or []
                    )
                    if products:
                        break
    else:
        products = []
    if isinstance(products, dict):
        products = list(products.values())
    normalized: list[dict] = []
    for product in products:
        if not isinstance(product, dict):
            continue
        pid = str(_first(product, "productId", "parentProductId", "id", "spinId", default=""))
        name = _first(product, "displayName", "name", "title", default="Unknown")
        variants = _extract_variants_list(product)
        parsed_variants = []
        for v in variants:
            spin_id = str(_first(v, "spinId", "skuId", "spin_id", "id", default=""))
            if not spin_id:
                continue
            price_obj = v.get("price") if isinstance(v.get("price"), dict) else {}
            unit_price = _parse_price(
                _first(v, "offerPrice", "price", "finalPrice", "mrp")
                or _first(price_obj, "offerPrice", "price", "mrp", "finalPrice")
            )
            parsed_variants.append(
                {
                    "spin_id": spin_id,
                    "name": _first(v, "displayName", "name", "variantName", default=name),
                    "pack_size": _first(
                        v, "quantityDescription", "quantity", "packSize", "unit", "weight"
                    ),
                    "unit_price": unit_price,
                    "in_stock": _variant_in_stock(v, product),
                    "product_id": pid or spin_id,
                    "image_url": _first(v, "imageUrl", "image_url", "image", default=None),
                    "raw": v,
                }
            )
        if parsed_variants:
            normalized.append(
                {
                    "product_id": pid or parsed_variants[0]["product_id"],
                    "name": name,
                    "variants": parsed_variants,
                }
            )
    return normalized


def _option_from_variant(product: dict, variant: dict) -> ClarificationOption:
    label = variant.get("name") or product.get("name")
    pack = variant.get("pack_size")
    if pack:
        label = f"{label} ({pack})"
    return ClarificationOption(
        spin_id=variant["spin_id"],
        name=str(label),
        pack_size=str(pack) if pack else None,
        unit_price=variant.get("unit_price"),
        product_id=str(product.get("product_id")),
        image_url=variant.get("image_url"),
    )


_STOP_TOKENS = {
    "kg",
    "g",
    "gm",
    "ml",
    "l",
    "ltr",
    "litre",
    "the",
    "and",
    "or",
    "packet",
    "vala",
    "vale",
    "kardo",
    "krdo",
    "chota",
    "bada",
}


def _token_set(text: str) -> set[str]:
    return {
        t
        for t in re.findall(r"[a-z0-9]+", text.lower())
        if t not in _STOP_TOKENS and len(t) > 2
    }


def _names_match(a: str, b: str) -> bool:
    na, nb = a.lower().strip(), b.lower().strip()
    if not na or not nb:
        return False
    if na == nb or na in nb or nb in na:
        return True
    ta, tb = _token_set(na), _token_set(nb)
    return len(ta & tb) >= 2


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


def pick_median_option(
    options: list[ClarificationOption | dict],
    pack_hint: str | None = None,
) -> ClarificationOption | None:
    opts = [_as_option(o) for o in options]
    if not opts:
        return None
    if len(opts) == 1:
        return opts[0]

    priced = [o for o in opts if o.unit_price is not None]
    candidates = _filter_options_by_pack(priced or opts, pack_hint)
    candidates.sort(key=lambda o: o.unit_price if o.unit_price is not None else float("inf"))
    return candidates[len(candidates) // 2]


def match_option_from_preferences(
    options: list[ClarificationOption | dict],
    preferences: list[dict],
    raw_query: str = "",
) -> ClarificationOption | None:
    opts = [_as_option(o) for o in options]
    if not opts or not preferences:
        return None

    by_spin = {o.spin_id: o for o in opts}
    query_tokens = _token_set(raw_query)

    for pref in preferences:
        spin = str(pref.get("spin_id") or pref.get("spinId") or pref.get("sku") or "")
        if spin and spin in by_spin:
            return by_spin[spin]

    best: ClarificationOption | None = None
    best_score = 0
    for pref in preferences:
        pref_name = str(pref.get("name") or pref.get("resolved_name") or "")
        if not pref_name:
            continue
        pref_tokens = _token_set(pref_name)
        if query_tokens and not (pref_tokens & query_tokens):
            continue
        for opt in opts:
            if _names_match(pref_name, opt.name):
                return opt
            score = len(_token_set(opt.name) & pref_tokens)
            if score > best_score:
                best_score = score
                best = opt

    return best if best_score >= 2 else None


def _extract_items_from_orders(orders_data: dict | list) -> list[dict]:
    if isinstance(orders_data, list):
        orders = orders_data
    else:
        orders = (
            orders_data.get("orders")
            or orders_data.get("orderList")
            or orders_data.get("data")
            or []
        )
    if isinstance(orders, dict):
        orders = list(orders.values())

    items: list[dict] = []
    for order in orders:
        if not isinstance(order, dict):
            continue
        order_items = (
            order.get("items")
            or order.get("orderItems")
            or order.get("lineItems")
            or order.get("products")
            or []
        )
        for it in order_items:
            if not isinstance(it, dict):
                continue
            spin = str(
                _first(it, "spinId", "skuId", "spin_id", "id", default="") or ""
            )
            name = _first(it, "name", "displayName", "productName", "title")
            if not spin and not name:
                continue
            items.append(
                {
                    "name": name,
                    "spin_id": spin or None,
                    "product_id": str(_first(it, "productId", default="") or "") or None,
                    "source": "swiggy_orders",
                }
            )
    return items


async def fetch_order_preferences(
    client: SwiggyInstamartClient,
    household_id: str,
    address_id: str,
) -> list[dict]:
    """Past ordered items: local history, Swiggy go-to items, then recent orders."""
    prefs: list[dict] = []
    seen_spins: set[str] = set()

    def add_pref(entry: dict) -> None:
        spin = str(entry.get("spin_id") or "")
        if spin and spin in seen_spins:
            return
        if spin:
            seen_spins.add(spin)
        if entry.get("name") or spin:
            prefs.append(entry)

    for item in await db.list_recent_ordered_items(household_id):
        add_pref(item)

    try:
        go_to = await client.your_go_to_items(address_id)
        for product in normalize_products(go_to):
            variant = pick_cheapest_variant(product) or (
                product.get("variants", [None])[0]
            )
            if not variant:
                continue
            add_pref(
                {
                    "name": product.get("name"),
                    "spin_id": variant.get("spin_id"),
                    "product_id": product.get("product_id"),
                    "source": "go_to",
                }
            )
    except Exception:
        pass

    for order_type in ("INSTAMART", "DASH"):
        try:
            orders_data = await client.get_orders(count=20, order_type=order_type)
            for item in _extract_items_from_orders(orders_data):
                add_pref(item)
        except Exception:
            continue

    return prefs


def try_auto_pick_clarification(
    resolution: dict,
    preferences: list[dict],
    raw_query: str,
    requested_qty: int = 1,
    search_query: str | None = None,
) -> dict:
    status = resolution.get("status")
    if status not in ("needs_clarification", "out_of_stock"):
        return resolution

    options = resolution.get("clarification_options") or resolution.get("alternatives") or []
    if not options:
        return resolution

    pack_hint = _extract_pack_hint(raw_query)
    chosen = match_option_from_preferences(options, preferences, raw_query)
    reason = "your past order"
    if not chosen:
        chosen = pick_median_option(options, pack_hint)
        reason = "median price"

    if not chosen:
        return resolution

    return _resolved_from_option(chosen, requested_qty, reason)


async def try_auto_pick_clarification_async(
    resolution: dict,
    preferences: list[dict],
    raw_query: str,
    requested_qty: int = 1,
    search_query: str | None = None,
) -> dict:
    """Past order → AI variant pick → brand recommendations (user picks)."""
    from backend.product_picker import pick_option_with_ai, rank_options_for_recommendation

    status = resolution.get("status")
    if status not in ("needs_clarification", "out_of_stock"):
        return resolution

    options = resolution.get("clarification_options") or resolution.get("alternatives") or []
    if not options:
        return resolution

    chosen = match_option_from_preferences(options, preferences, raw_query)
    if chosen:
        return _resolved_from_option(chosen, requested_qty, "your past order")

    ai_chosen, ai_reason = await pick_option_with_ai(
        options,
        raw_query,
        preferences=preferences,
        search_query=search_query,
    )
    if ai_chosen:
        return _resolved_from_option(ai_chosen, requested_qty, ai_reason or "AI matched intent")

    ranked = await rank_options_for_recommendation(
        options, raw_query, search_query=search_query
    )
    return {
        **resolution,
        "status": "needs_clarification",
        "clarification_options": [o.model_dump() for o in ranked],
        "alternatives": [],
        "note": "New item — pick a brand (no past order for this)",
    }


def _resolved_from_option(
    chosen: ClarificationOption, requested_qty: int, reason: str
) -> dict:
    return {
        "status": "resolved",
        "resolved_sku": chosen.spin_id,
        "resolved_name": chosen.name,
        "pack_size": chosen.pack_size,
        "unit_price": chosen.unit_price,
        "merged_qty": requested_qty,
        "product_id": chosen.product_id,
        "auto_picked": True,
        "clarification_options": [],
        "alternatives": [],
        "note": f"Auto-picked ({reason})",
    }


def pick_cheapest_variant(product: dict) -> dict | None:
    in_stock = [v for v in product.get("variants", []) if v.get("in_stock")]
    if not in_stock:
        return None
    return min(
        in_stock,
        key=lambda v: (
            v.get("unit_price") if v.get("unit_price") is not None else float("inf"),
            str(v.get("pack_size") or ""),
        ),
    )


def pick_best_variant_for_qty(product: dict, qty: int) -> tuple[dict | None, bool]:
    in_stock = [v for v in product.get("variants", []) if v.get("in_stock")]
    if not in_stock:
        return None, False
    if qty <= 1 or len(in_stock) == 1:
        chosen = pick_cheapest_variant(product)
        return chosen, len(in_stock) > 1

    best = None
    best_cost = float("inf")
    for variant in in_stock:
        price = variant.get("unit_price")
        if price is None:
            continue
        pack_num = 1.0
        if variant.get("pack_size"):
            nums = re.findall(r"[\d.]+", str(variant["pack_size"]))
            if nums:
                pack_num = max(float(nums[0]), 1.0)
        units_needed = max(1, round(qty / pack_num)) if pack_num else qty
        total = price * units_needed
        if total < best_cost:
            best_cost = total
            best = {**variant, "_order_qty": units_needed}
    if best:
        return best, True
    chosen = pick_cheapest_variant(product)
    return chosen, len(in_stock) > 1


def resolve_search_results(
    raw_query: str, search_data: dict, requested_qty: int = 1
) -> dict:
    products = normalize_products(search_data)
    if not products:
        return {
            "status": "needs_clarification",
            "note": "No products found",
            "clarification_options": [],
            "alternatives": [],
        }

    in_stock_products = []
    for p in products:
        if any(v.get("in_stock") for v in p.get("variants", [])):
            in_stock_products.append(p)

    if not in_stock_products:
        alts: list[ClarificationOption] = []
        for p in products[:3]:
            for v in p.get("variants", [])[:1]:
                alts.append(_option_from_variant(p, v))
        return {
            "status": "out_of_stock",
            "alternatives": [a.model_dump() for a in alts],
            "clarification_options": [],
        }

    if len(in_stock_products) == 1:
        product = in_stock_products[0]
        in_stock_variants = [v for v in product.get("variants", []) if v.get("in_stock")]
        if len(in_stock_variants) > 1:
            variant_options = [
                _option_from_variant(product, v) for v in in_stock_variants[:8]
            ]
            return {
                "status": "needs_clarification",
                "clarification_options": [o.model_dump() for o in variant_options],
                "alternatives": [],
                "note": "Multiple pack/variants — pick one",
            }
        variant, auto_picked = pick_best_variant_for_qty(product, requested_qty)
        if not variant:
            return {
                "status": "out_of_stock",
                "alternatives": [],
                "clarification_options": [],
            }
        order_qty = variant.get("_order_qty", requested_qty)
        return {
            "status": "resolved",
            "resolved_sku": variant["spin_id"],
            "resolved_name": product["name"],
            "pack_size": str(variant.get("pack_size") or ""),
            "unit_price": variant.get("unit_price"),
            "merged_qty": order_qty,
            "product_id": str(product["product_id"]),
            "auto_picked": auto_picked,
        }

    options: list[ClarificationOption] = []
    for p in in_stock_products[:5]:
        v = pick_cheapest_variant(p)
        if v:
            options.append(_option_from_variant(p, v))

    return {
        "status": "needs_clarification",
        "clarification_options": [o.model_dump() for o in options],
        "alternatives": [],
        "note": "Multiple products match — pick one",
    }


async def merge_into_existing(
    household_id: str,
    product_id: str,
    add_qty: int,
    requester: str,
) -> DraftItem | None:
    existing = await db.find_resolved_by_product(household_id, product_id)
    if not existing:
        return None

    requesters = list(existing.requested_by)
    if requester not in requesters:
        requesters.append(requester)

    new_qty = existing.merged_qty + add_qty
    updated = await db.update_draft_item(
        existing.id,
        merged_qty=new_qty,
        requested_by=requesters,
    )
    return updated


async def search_products_with_fallback(
    client: SwiggyInstamartClient,
    address_id: str,
    item: ExtractedItem,
) -> tuple[dict, str, list[tuple[str, int]]]:
    raw_query, _qty = _extract_quantity(item.raw_query)
    base = item.search_query or _clean_search_query(raw_query) or raw_query
    queries = build_search_variants(base, item.alternate_search_queries)
    last_data: dict = {}
    attempts: list[tuple[str, int]] = []
    matched_query = base

    for q in queries:
        last_data = await client.search_products(address_id, q)
        count = len(normalize_products(last_data))
        attempts.append((q, count))
        if count > 0:
            return last_data, q, attempts

    return last_data, matched_query, attempts


def search_response_hint(data: dict) -> str:
    if not data:
        return "MCP returned empty data — check address selected & Swiggy auth"
    keys = list(data.keys())[:8] if isinstance(data, dict) else ["list"]
    raw_n = len(data.get("products") or []) if isinstance(data, dict) else 0
    parsed = len(normalize_products(data))
    return f"raw_products={raw_n}, parsed={parsed}, response_keys={keys}"


async def process_extracted_item(
    client: SwiggyInstamartClient,
    household_id: str,
    address_id: str,
    item: ExtractedItem,
    preferences: list[dict] | None = None,
) -> tuple[DraftItem, list[tuple[str, int]], dict, dict[str, Any] | None]:
    from backend.conflict_resolver import find_conflicting_draft, prepare_conflict_poll

    raw_query, qty = _extract_quantity(item.raw_query)

    search_data, used_query, attempts = await search_products_with_fallback(
        client, address_id, item
    )

    drafts = await db.list_draft_items(household_id)
    conflict = find_conflicting_draft(drafts, item)
    if conflict:
        draft_update, poll_request = await prepare_conflict_poll(
            conflict, item, search_data, used_query, qty
        )
        updated = await db.update_draft_item(conflict.id, **draft_update)
        assert updated is not None
        return updated, attempts, search_data, poll_request

    resolution = resolve_search_results(used_query, search_data, qty)
    if resolution["status"] in ("needs_clarification", "out_of_stock") and (
        resolution.get("clarification_options") or resolution.get("alternatives")
    ):
        resolution = await try_auto_pick_clarification_async(
            resolution,
            preferences or [],
            raw_query,
            qty,
            search_query=item.search_query,
        )
    elif resolution["status"] == "resolved" and resolution.get("auto_picked"):
        # Single match auto-picked — still honour past order for multi-variant brands
        products = normalize_products(search_data)
        option_dicts: list[dict] = []
        for p in products[:5]:
            for v in p.get("variants", []):
                if v.get("in_stock"):
                    option_dicts.append(_option_from_variant(p, v).model_dump())
        if len(option_dicts) > 1:
            chosen = match_option_from_preferences(
                option_dicts, preferences or [], raw_query
            )
            if not chosen:
                resolution = await try_auto_pick_clarification_async(
                    {
                        "status": "needs_clarification",
                        "clarification_options": option_dicts,
                        "alternatives": [],
                    },
                    preferences or [],
                    raw_query,
                    qty,
                    search_query=item.search_query,
                )
    if resolution["status"] == "needs_clarification" and not resolution.get(
        "clarification_options"
    ):
        tried = ", ".join(f'"{q}"' for q, c in attempts if c == 0) or f'"{used_query}"'
        resolution["note"] = (
            f"No Instamart matches (tried: {tried}). "
            "Retry with clearer name — groceries: 'aashirvaad atta 5kg'; "
            "fashion: 'men t shirt medium'; electronics: 'type c charger'."
        )

    if resolution["status"] == "resolved" and resolution.get("product_id"):
        merged = await merge_into_existing(
            household_id,
            resolution["product_id"],
            qty,
            item.sender,
        )
        if merged:
            return merged, attempts, search_data, None

    draft = await db.insert_draft_item(
        household_id,
        raw_query=raw_query,
        requested_by=[item.sender],
        status=resolution["status"],
        resolved_sku=resolution.get("resolved_sku"),
        resolved_name=resolution.get("resolved_name"),
        pack_size=resolution.get("pack_size"),
        unit_price=resolution.get("unit_price"),
        merged_qty=resolution.get("merged_qty", qty),
        product_id=resolution.get("product_id"),
        auto_picked=resolution.get("auto_picked", False),
        clarification_options=resolution.get("clarification_options", []),
        alternatives=resolution.get("alternatives", []),
        note=resolution.get("note") or item.note,
    )
    return draft, attempts, search_data, None


async def resolve_draft_selection(
    household_id: str,
    draft_item_id: int,
    spin_id: str,
    option: ClarificationOption,
) -> DraftItem | None:
    item = await db.get_draft_item(draft_item_id)
    if not item:
        return None

    if option.product_id:
        merged = await merge_into_existing(
            household_id,
            option.product_id,
            item.merged_qty,
            item.requested_by[0] if item.requested_by else "Unknown",
        )
        if merged and merged.id != draft_item_id:
            await db.update_draft_item(draft_item_id, status="excluded", excluded=True)
            return merged

    return await db.update_draft_item(
        draft_item_id,
        status="resolved",
        resolved_sku=spin_id,
        resolved_name=option.name,
        pack_size=option.pack_size,
        unit_price=option.unit_price,
        product_id=option.product_id,
        clarification_options=[],
        alternatives=[],
        auto_picked=False,
    )


def build_draft_snapshot(items: list[DraftItem]) -> dict:
    active = [i for i in items if i.status != "checked_out"]
    resolved = [i for i in active if i.status == "resolved" and not i.excluded]
    unresolved = [
        i
        for i in active
        if i.status in ("needs_clarification", "needs_poll", "out_of_stock")
        and not i.excluded
    ]
    subtotal = sum(
        (i.unit_price or 0) * i.merged_qty for i in resolved if i.unit_price is not None
    )
    return {
        "items": [i.model_dump() for i in active],
        "estimated_subtotal": round(subtotal, 2),
        "resolved_count": len(resolved),
        "unresolved_count": len(unresolved),
    }
