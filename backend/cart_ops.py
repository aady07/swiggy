from __future__ import annotations

from backend.db import db
from backend.models import CheckoutResult
from backend.resolver import build_draft_snapshot

from backend.bot_intents import is_place_order_message


def format_order_placed_text(result: CheckoutResult, *, base_url: str) -> str:
    if not result.success:
        return result.error or "Could not place order."
    lines = [f"✅ {result.swiggy_message}"]
    if result.settlement:
        lines.append("")
        lines.append("Settlement:")
        for row in result.settlement:
            names = ", ".join(row.items[:3])
            if len(row.items) > 3:
                names += f" +{len(row.items) - 3} more"
            lines.append(f"  {row.sender}: ~₹{row.estimated_share:.0f} ({names})")
    lines.append("")
    lines.append(f"Tracked in admin → Orders: {base_url.rstrip('/')}/")
    return "\n".join(lines)


def _resolved_lines(snapshot: dict) -> list[dict]:
    return [
        i
        for i in snapshot.get("items") or []
        if i.get("status") == "resolved" and not i.get("excluded")
    ]


def build_numbered_cart(snapshot: dict) -> list[dict]:
    lines = []
    for n, item in enumerate(_resolved_lines(snapshot), start=1):
        lines.append({**item, "line_number": n})
    return lines


async def get_cart_view(household_id: str) -> dict:
    items = await db.list_draft_items(household_id)
    snapshot = build_draft_snapshot(items)
    numbered = build_numbered_cart(snapshot)
    return {
        **snapshot,
        "numbered_items": numbered,
    }


async def remove_cart_line(household_id: str, line_number: int) -> dict:
    view = await get_cart_view(household_id)
    numbered = view["numbered_items"]
    if line_number < 1 or line_number > len(numbered):
        raise ValueError(f"No item #{line_number} in cart")
    item_id = numbered[line_number - 1]["id"]
    name = (
        numbered[line_number - 1].get("resolved_name")
        or numbered[line_number - 1].get("raw_query")
    )
    deleted = await db.delete_draft_item(item_id, household_id)
    if not deleted:
        raise ValueError("Item not found")
    updated = await get_cart_view(household_id)
    return {
        "removed_line": line_number,
        "removed_name": name,
        "removed_id": item_id,
        "cart": updated,
    }


async def remove_cart_item(household_id: str, draft_item_id: int) -> dict:
    item = await db.get_draft_item(draft_item_id)
    if not item or item.household_id != household_id:
        raise ValueError("Item not found")
    name = item.resolved_name or item.raw_query
    deleted = await db.delete_draft_item(draft_item_id, household_id)
    if not deleted:
        raise ValueError("Item not found")
    return {
        "removed_name": name,
        "removed_id": draft_item_id,
        "cart": await get_cart_view(household_id),
    }


def _format_requesters(requested_by: list | None) -> str:
    if not requested_by:
        return ""
    return f" — {', '.join(requested_by)}"


def format_cart_text(view: dict, *, header: str | None = None) -> str:
    numbered = view.get("numbered_items") or []
    if not numbered:
        body = (
            "Cart khali hai.\n\n"
            "Jo chahiye likho, jaise:\n"
            "  atta 5kg, milk, lays"
        )
        return f"{header}\n\n{body}" if header else body

    subtotal = view.get("estimated_subtotal") or 0
    lines = [
        header or "🛒 Ghar ka cart",
        f"Total ~₹{subtotal:.0f} · {len(numbered)} item",
        "",
    ]
    for row in numbered[:20]:
        n = row["line_number"]
        name = row.get("resolved_name") or row.get("raw_query")
        qty = row.get("merged_qty") or 1
        price = row.get("unit_price")
        line = f"{n}. {name} ×{qty}"
        if price is not None:
            line += f" — ₹{price * qty:.0f}"
        line += _format_requesters(row.get("requested_by"))
        pick = row.get("note") or ""
        if row.get("auto_picked") and "Auto-picked" in str(pick):
            line += " ✓"
        lines.append(line)

    lines.append("")
    if subtotal >= 1000:
        lines.append("⚠️ ₹1000 se zyada — kuch hatao (/remove 2)")
    else:
        lines.append("Hataane ke liye ❌ dabao · Order ke liye ✅ Place order")
    return "\n".join(lines)


def cart_inline_keyboard(view: dict) -> dict:
    numbered = view.get("numbered_items") or []
    keyboard: list[list[dict]] = []
    for row in numbered[:8]:
        n = row["line_number"]
        name = (row.get("resolved_name") or row.get("raw_query") or "")[:28]
        keyboard.append(
            [
                {
                    "text": f"❌ {n}. {name}",
                    "callback_data": f"rm:{row['id']}",
                }
            ]
        )
    keyboard.append(
        [
            {"text": "🛒 Cart dekho", "callback_data": "cmd:cart"},
            {"text": "🗑 Sab hatao", "callback_data": "cmd:clear"},
        ]
    )
    keyboard.append(
        [
            {"text": "🤖 Help", "callback_data": "cmd:assistants"},
            {"text": "✅ Order karo", "callback_data": "cmd:checkout"},
        ]
    )
    return {"inline_keyboard": keyboard}


def assistants_inline_keyboard() -> dict:
    from backend.assistants import list_assistants

    rows = []
    for a in list_assistants():
        rows.append(
            [
                {
                    "text": f"{a['emoji']} {a['name']}",
                    "callback_data": f"asst:{a['id']}",
                }
            ]
        )
    rows.append([{"text": "« Back to cart", "callback_data": "cmd:cart"}])
    return {"inline_keyboard": rows}
