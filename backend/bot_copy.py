from __future__ import annotations

from typing import Any


def format_help_short() -> str:
    return (
        "🏠 Ghar ka shared Instamart cart\n\n"
        "Jo chahiye bas likh do:\n"
        "  atta 5kg, milk, lays\n\n"
        "Recipe ke liye:\n"
        "  chole bhature banane me kya lagega\n"
        "  phir ✅ Sab cart mein daalo dabao\n\n"
        "Cart dekho: /cart\n"
        "Order karo: /checkout\n"
        "Sab hatao: /clear"
    )


def format_grocery_understood(sender: str, items: list[str]) -> str:
    if not items:
        return (
            "Thoda clear likho 🙏\n"
            "Jaise: atta 5kg, milk, onion"
        )
    if len(items) == 1:
        return (
            f"Theek hai {sender}! 👍\n\n"
            f"Maine samjha: *{items[0]}*\n\n"
            "Instamart pe dhoondh ke cart mein daal raha hoon…"
        )
    bullets = "\n".join(f"  • {name}" for name in items[:10])
    extra = ""
    if len(items) > 10:
        extra = f"\n  …aur {len(items) - 10} aur"
    return (
        f"Theek hai {sender}! 👍\n\n"
        f"Maine {len(items)} cheezein samjhi:\n{bullets}{extra}\n\n"
        "Instamart pe dhoondh ke cart mein daal raha hoon…"
    )


def format_recipe_understood(dish: str) -> str:
    title = dish.strip().title()
    return (
        f"Samajh gaya 👍\n\n"
        f"Aap *{title}* banane ka saman puch rahe ho.\n"
        "Main order nahi kar raha — pehle list dikhaunga.\n\n"
        "Instamart pe kya-kya milta hai, dhoondh raha hoon…"
    )


def format_recipe_list(session: dict[str, Any]) -> str:
    dish = (session.get("dish") or "yeh dish").strip().title()
    rows = session.get("lines") or []
    ready = [ln for ln in rows if ln.get("status") == "found"]
    pick = [ln for ln in rows if ln.get("status") == "needs_pick"]
    missing = [ln for ln in rows if ln.get("status") == "not_found"]

    out = [
        f"🍳 *{dish}* — yeh saman chahiye",
        "",
    ]
    if ready:
        out.append(f"*Mil gaya Instamart pe ({len(ready)}):*")
        for i, ln in enumerate(rows, start=1):
            if ln.get("status") != "found":
                continue
            price = ln.get("unit_price")
            price_s = f" — ₹{price:.0f}" if price is not None else ""
            name = ln.get("resolved_name") or ln.get("ingredient")
            out.append(f"  {i}. {name}{price_s}")
        out.append("")

    if pick:
        out.append(f"*Brand chunna padega ({len(pick)}):*")
        for i, ln in enumerate(rows, start=1):
            if ln.get("status") != "needs_pick":
                continue
            out.append(f"  {i}. {ln.get('ingredient')} — neeche ?{i} dabao")
        out.append("")

    if missing:
        out.append(f"*Nahi mila ({len(missing)}):*")
        for i, ln in enumerate(rows, start=1):
            if ln.get("status") != "not_found":
                continue
            out.append(f"  {i}. {ln.get('ingredient')}")
        out.append("")

    if ready:
        out.append("Cart mein daalne ke liye neeche button dabao 👇")
        out.append("Sirf list chahiye thi? *Bas itna* dabao.")
    elif pick:
        out.append("Pehle brand chuno (? buttons), phir cart mein daal sakte ho.")
    else:
        out.append("Kuch bhi nahi mila — dish ka naam alag se likh ke try karo.")

    return "\n".join(out)


def recipe_list_keyboard(session_id: str, session: dict[str, Any]) -> dict:
    rows_data = session.get("lines") or []
    rows: list[list[dict]] = []
    ready_count = sum(1 for ln in rows_data if ln.get("status") == "found")

    if ready_count:
        rows.append(
            [
                {
                    "text": f"✅ Sab cart mein daalo ({ready_count})",
                    "callback_data": f"recipe:all:{session_id}",
                }
            ]
        )

    add_row: list[dict] = []
    pick_row: list[dict] = []
    for i, ln in enumerate(rows_data):
        n = i + 1
        if ln.get("status") == "found":
            short = (ln.get("ingredient") or f"item {n}")[:12]
            add_row.append(
                {
                    "text": f"+ {short}",
                    "callback_data": f"recipe:one:{session_id}:{i}",
                }
            )
        elif ln.get("status") == "needs_pick":
            short = (ln.get("ingredient") or f"item {n}")[:12]
            pick_row.append(
                {
                    "text": f"? {short}",
                    "callback_data": f"recipe:pick:{session_id}:{i}",
                }
            )
        if len(add_row) == 3:
            rows.append(add_row)
            add_row = []
        if len(pick_row) == 3:
            rows.append(pick_row)
            pick_row = []
    if add_row:
        rows.append(add_row)
    if pick_row:
        rows.append(pick_row)

    rows.append([{"text": "Bas itna (list only)", "callback_data": f"recipe:skip:{session_id}"}])
    return {"inline_keyboard": rows}


def format_add_summary(sender: str, details: list[dict[str, Any]]) -> str:
    if not details:
        return ""
    lines = [f"✅ *{sender}* — cart mein daal diya:"]
    for d in details:
        name = d.get("resolved_name") or d.get("raw_query") or "item"
        if d.get("usual"):
            lines.append(f"  • {name} _(pehle bhi yahi order karte ho)_")
        elif d.get("ai"):
            lines.append(f"  • {name} _(sahi product chuna)_")
        else:
            lines.append(f"  • {name}")
    return "\n".join(lines)


def format_pick_brand_prompt(raw_query: str) -> str:
    return (
        f"*{raw_query}* — kaunsa brand?\n"
        "Neeche se chuno 👇"
    )


def format_item_failed(raw_query: str, note: str | None = None) -> str:
    msg = f"✗ *{raw_query}* — Instamart pe nahi mila."
    if note and "tried" not in note.lower():
        return f"{msg}\n{note}"
    return f"{msg}\nAlag naam try karo, jaise brand + size."


def format_cart_header_added(sender: str) -> str:
    return f"🛒 Abhi ka cart ({sender} ne add kiya)"


def format_recipe_added(count: int) -> str:
    if count == 1:
        return "✅ 1 cheez recipe se cart mein daal di."
    return f"✅ {count} cheezein recipe se cart mein daal di."


def format_recipe_whatsapp_followup() -> str:
    return (
        "Cart mein daalne ke liye likho:\n"
        "  *add all* — sab jo mila\n"
        "  *add 1 3 5* — sirf woh number"
    )


def format_nothing_understood() -> str:
    return (
        "Samajh nahi aaya 🙏\n\n"
        "Aise likho:\n"
        "  • atta 5kg, milk\n"
        "  • chole bhature banane me kya lagega"
    )


def format_error_no_address(url: str) -> str:
    return (
        "Pehle delivery address chuno 🏠\n\n"
        f"Yahan kholo, address select karo:\n{url}"
    )


def format_error_no_swiggy(url: str) -> str:
    return (
        "Pehle Swiggy connect karo 🔗\n\n"
        f"Yahan kholo:\n{url}\n\n"
        "Ya Telegram pe /connect bhejo."
    )


def format_collage_caption(raw_query: str, count: int) -> str:
    return (
        f"*{raw_query}* — {count} options\n"
        "Bada dekhne ke liye 🔍 · Cart mein daalne ke liye ✅"
    )


def format_working(sender: str) -> str:
    return f"Ek minute {sender}… 👍"
