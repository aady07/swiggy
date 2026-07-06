from __future__ import annotations

import json
from collections import defaultdict

from backend.config import settings
from backend.db import db
from backend.models import CheckoutResult, ClarificationOption, SettlementLine
from backend.mcp_client import MCPError, SwiggyInstamartClient
from backend.resolver import normalize_products, resolve_search_results


MIN_ORDER = 99.0
MAX_ORDER = 1000.0


def _cart_total(cart_data: dict) -> float:
    bill = cart_data.get("bill") or cart_data.get("billDetails") or cart_data
    for key in ("toPay", "total", "finalTotal", "payableAmount", "cartTotal"):
        val = bill.get(key) if isinstance(bill, dict) else None
        if val is not None:
            if isinstance(val, dict):
                return float(val.get("value") or val.get("amount") or 0)
            return float(val)
    items = cart_data.get("items") or []
    total = 0.0
    for item in items:
        price = item.get("price") or item.get("offerPrice") or 0
        qty = item.get("quantity") or 1
        if isinstance(price, dict):
            price = price.get("value") or price.get("amount") or 0
        total += float(price) * int(qty)
    return total


def _payment_methods(cart_data: dict) -> list[str]:
    methods = cart_data.get("availablePaymentMethods") or []
    if isinstance(methods, list):
        out = []
        for m in methods:
            if isinstance(m, str):
                out.append(m)
            elif isinstance(m, dict):
                out.append(str(m.get("type") or m.get("name") or m.get("id")))
        return [m for m in out if m]
    return []


def build_settlement(items: list[dict], total: float) -> list[SettlementLine]:
    by_sender: dict[str, list[str]] = defaultdict(list)
    for item in items:
        name = item.get("resolved_name") or item.get("raw_query")
        for sender in item.get("requested_by") or []:
            by_sender[sender].append(name)

    if not by_sender:
        return []

    per_sender = total / len(by_sender)
    settlement = []
    for sender, names in sorted(by_sender.items()):
        settlement.append(
            SettlementLine(
                sender=sender,
                items=names,
                estimated_share=round(per_sender, 2),
            )
        )
    return settlement


async def run_checkout(
    client: SwiggyInstamartClient,
    household_id: str,
    address_id: str,
) -> CheckoutResult:
    draft_items = await db.list_draft_items(household_id)
    resolved = [
        i
        for i in draft_items
        if i.status == "resolved" and not i.excluded and i.resolved_sku
    ]
    if not resolved:
        return CheckoutResult(success=False, error="No resolved items to checkout")

    verified_items = []
    for item in resolved:
        search_data = await client.search_products(address_id, item.raw_query)
        check = resolve_search_results(item.raw_query, search_data, item.merged_qty)
        if check.get("status") != "resolved":
            return CheckoutResult(
                success=False,
                error=f"'{item.resolved_name or item.raw_query}' is no longer available. Refresh draft.",
            )
        verified_items.append(
            {
                "spinId": item.resolved_sku,
                "quantity": item.merged_qty,
                "resolved_name": item.resolved_name,
                "raw_query": item.raw_query,
                "requested_by": item.requested_by,
                "unit_price": item.unit_price,
            }
        )

    cart_payload = [
        {"spinId": i["spinId"], "quantity": i["quantity"]} for i in verified_items
    ]

    try:
        await client.update_cart(address_id, cart_payload)
        cart = await client.get_cart()
    except MCPError as exc:
        return CheckoutResult(success=False, error=str(exc))

    total = _cart_total(cart)
    if total >= MAX_ORDER:
        return CheckoutResult(
            success=False,
            error=(
                f"Cart total ₹{total:.0f} exceeds ₹1000 MCP beta limit. "
                "Please complete this order in the Swiggy app."
            ),
        )
    if total < MIN_ORDER:
        return CheckoutResult(
            success=False,
            error=f"Cart total ₹{total:.0f} is below Instamart minimum of ₹99.",
        )

    payment_methods = _payment_methods(cart)
    payment_method = payment_methods[0] if payment_methods else None

    try:
        checkout_resp = await client.checkout(address_id, payment_method)
    except MCPError as exc:
        return CheckoutResult(success=False, error=str(exc))

    swiggy_message = checkout_resp.get("message") or "Instamart order placed successfully."
    settlement = build_settlement(verified_items, total)

    order_id = await db.save_order_history(
        household_id,
        json.dumps(verified_items),
        total,
        order_type="instamart",
        settlement_json=json.dumps([s.model_dump() for s in settlement]),
    )
    await db.mark_draft_checked_out(household_id)

    return CheckoutResult(
        success=True,
        swiggy_message=swiggy_message,
        settlement=settlement,
        total=total,
        order_id=order_id,
        order_type="instamart",
    )


async def run_dummy_checkout(
    household_id: str,
    *,
    placed_by: str = "Admin",
) -> CheckoutResult:
    """Archive current cart as a placed order without calling Swiggy."""
    draft_items = await db.list_draft_items(household_id)
    resolved = [
        i
        for i in draft_items
        if i.status == "resolved" and not i.excluded
    ]
    if not resolved:
        return CheckoutResult(success=False, error="No items in cart to place")

    line_items = []
    total = 0.0
    for item in resolved:
        qty = item.merged_qty or 1
        unit = float(item.unit_price or 0)
        line_total = unit * qty
        total += line_total
        line_items.append(
            {
                "spinId": item.resolved_sku,
                "quantity": qty,
                "resolved_name": item.resolved_name,
                "raw_query": item.raw_query,
                "requested_by": item.requested_by,
                "unit_price": item.unit_price,
                "pack_size": item.pack_size,
            }
        )

    settlement = build_settlement(line_items, total)
    order_id = await db.save_order_history(
        household_id,
        json.dumps(line_items),
        total,
        order_type="dummy",
        settlement_json=json.dumps([s.model_dump() for s in settlement]),
        placed_by=placed_by,
    )
    await db.clear_draft(household_id)

    names = [i.get("resolved_name") or i.get("raw_query") for i in line_items[:4]]
    preview = ", ".join(names)
    if len(line_items) > 4:
        preview += f" +{len(line_items) - 4} more"

    return CheckoutResult(
        success=True,
        swiggy_message=f"Order #{order_id} placed (dummy). Cart cleared. {preview}",
        settlement=settlement,
        total=round(total, 2),
        order_id=order_id,
        order_type="dummy",
    )
