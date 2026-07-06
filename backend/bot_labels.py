from __future__ import annotations


def option_button_label(
    name: str,
    price: float | None,
    *,
    max_len: int = 20,
) -> str:
    """Build a button label that always keeps the full price visible."""
    clean = (name or "Item").strip()
    price_part = f" ₹{price:.0f}" if price is not None else ""
    if not price_part:
        return clean[:max_len]
    name_max = max_len - len(price_part)
    if name_max < 1:
        return price_part.strip()[:max_len]
    short_name = clean[:name_max].rstrip()
    return f"{short_name}{price_part}"
