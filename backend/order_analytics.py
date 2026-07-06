from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import datetime


def _norm_item_name(item: dict) -> str:
    name = (item.get("resolved_name") or item.get("raw_query") or item.get("name") or "").strip()
    return re.sub(r"\s+", " ", name) or "Unknown item"


def compute_order_analytics(orders: list[dict]) -> dict:
    """Aggregate spend, members, and item frequency from order history."""
    if not orders:
        return {
            "total_orders": 0,
            "total_spend": 0.0,
            "avg_order_value": 0.0,
            "dummy_orders": 0,
            "instamart_orders": 0,
            "biggest_spender": None,
            "top_item": None,
            "spend_by_member": [],
            "top_items": [],
            "orders_by_month": [],
            "recent_totals": [],
        }

    total_spend = 0.0
    spend_by_member: dict[str, float] = defaultdict(float)
    orders_by_member: dict[str, int] = defaultdict(int)
    item_counts: Counter[str] = Counter()
    item_qty: Counter[str] = Counter()
    item_last_ordered: dict[str, str] = {}
    dummy_count = 0
    instamart_count = 0
    month_totals: dict[str, float] = defaultdict(float)
    month_counts: dict[str, int] = defaultdict(int)

    for order in orders:
        total = float(order.get("total") or 0)
        total_spend += total
        order_type = order.get("order_type") or "instamart"
        if order_type == "dummy":
            dummy_count += 1
        else:
            instamart_count += 1

        placed_at = order.get("placed_at") or ""
        month_key = _month_key(placed_at)
        if month_key:
            month_totals[month_key] += total
            month_counts[month_key] += 1

        settlement = order.get("settlement") or []
        if settlement:
            for row in settlement:
                sender = row.get("sender") or "Unknown"
                share = float(row.get("estimated_share") or 0)
                spend_by_member[sender] += share
                orders_by_member[sender] += 1
        else:
            for item in order.get("items") or []:
                for sender in item.get("requested_by") or ["Unknown"]:
                    unit = float(item.get("unit_price") or 0)
                    qty = int(item.get("quantity") or item.get("merged_qty") or 1)
                    spend_by_member[sender] += unit * qty
                    orders_by_member[sender] += 1

        for item in order.get("items") or []:
            name = _norm_item_name(item)
            qty = int(item.get("quantity") or item.get("merged_qty") or 1)
            item_counts[name] += 1
            item_qty[name] += qty
            if placed_at and (name not in item_last_ordered or placed_at > item_last_ordered[name]):
                item_last_ordered[name] = placed_at

    spend_rows = [
        {
            "sender": sender,
            "total_spend": round(amount, 2),
            "order_count": orders_by_member.get(sender, 0),
            "share_pct": round((amount / total_spend * 100) if total_spend else 0, 1),
        }
        for sender, amount in spend_by_member.items()
    ]
    spend_rows.sort(key=lambda r: r["total_spend"], reverse=True)

    top_items = [
        {
            "name": name,
            "order_count": count,
            "total_qty": item_qty[name],
            "last_ordered_at": item_last_ordered.get(name),
        }
        for name, count in item_counts.most_common(15)
    ]

    orders_by_month = [
        {
            "month": month,
            "total_spend": round(month_totals[month], 2),
            "order_count": month_counts[month],
        }
        for month in sorted(month_totals.keys(), reverse=True)
    ]

    recent_totals = [
        {"order_id": o.get("id"), "total": float(o.get("total") or 0), "placed_at": o.get("placed_at")}
        for o in reversed(orders[-12:])
    ]

    biggest = spend_rows[0] if spend_rows else None
    top_item = top_items[0] if top_items else None
    order_count = len(orders)

    return {
        "total_orders": order_count,
        "total_spend": round(total_spend, 2),
        "avg_order_value": round(total_spend / order_count, 2) if order_count else 0.0,
        "dummy_orders": dummy_count,
        "instamart_orders": instamart_count,
        "biggest_spender": biggest,
        "top_item": top_item,
        "spend_by_member": spend_rows,
        "top_items": top_items,
        "orders_by_month": orders_by_month,
        "recent_totals": recent_totals,
    }


def _month_key(placed_at: str) -> str:
    if not placed_at:
        return ""
    try:
        raw = placed_at.replace("Z", "+00:00") if placed_at.endswith("Z") else placed_at
        if "T" not in raw and " " in raw:
            raw = raw.replace(" ", "T", 1)
        dt = datetime.fromisoformat(raw[:19])
        return dt.strftime("%Y-%m")
    except (ValueError, TypeError):
        return placed_at[:7] if len(placed_at) >= 7 else ""
