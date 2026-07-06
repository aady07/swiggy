from __future__ import annotations

import json
from typing import Any

from backend.config import gemini_configured, settings
from backend.cart_ops import format_cart_text, get_cart_view

ASSISTANTS: dict[str, dict[str, str]] = {
    "cart": {
        "id": "cart",
        "name": "Cart Assistant",
        "emoji": "🛒",
        "description": "Questions about your cart, items, and totals.",
        "system": """You are the Cart Assistant for a household Instamart shopping app in India.
Users add groceries, fashion, electronics, and home essentials to a shared cart.
Answer briefly using the cart JSON provided. Help users understand what's in the cart,
who requested items, and what to remove. Never invent SKUs or prices not in the cart.""",
    },
    "budget": {
        "id": "budget",
        "name": "Budget Advisor",
        "emoji": "💰",
        "description": "Stay under ₹1000 MCP checkout limit.",
        "system": """You are the Budget Advisor for Instamart MCP grocery checkout in India.
MCP checkout is blocked at ₹1000+. Given the cart, suggest which numbered items to remove
to get under ₹1000 while keeping essentials (atta, rice, milk). Be specific: "Remove #4 and #7".""",
    },
    "meal": {
        "id": "meal",
        "name": "Meal Planner",
        "emoji": "🍳",
        "description": "Turn meal ideas into a shopping list.",
        "system": """You are a Meal Planner for Indian households using Swiggy Instamart.
Given a meal request, output a comma-separated grocery list suitable for Instamart search
(e.g. paneer, tomatoes, onion, ginger, amul butter). Keep it short and practical.""",
    },
    "grocery": {
        "id": "grocery",
        "name": "Grocery Agent",
        "emoji": "🥬",
        "description": "Explain how to add items or fix failed searches.",
        "system": """You are the Grocery Agent helper for a household Instamart merge app in India.
Help users write better Hinglish/English requests for ANY Instamart product — groceries, fashion, electronics, personal care. Examples:
'lays magic masala', 'men t shirt medium', 'type c charger'. Mention they can /remove numbered items.""",
    },
}


def list_assistants() -> list[dict[str, str]]:
    return [
        {
            "id": a["id"],
            "name": a["name"],
            "emoji": a["emoji"],
            "description": a["description"],
        }
        for a in ASSISTANTS.values()
    ]


def get_assistant(assistant_id: str) -> dict[str, str] | None:
    return ASSISTANTS.get(assistant_id)


async def run_assistant_chat(
    assistant_id: str,
    message: str,
    household_id: str,
) -> str:
    assistant = get_assistant(assistant_id)
    if not assistant:
        raise ValueError(f"Unknown assistant: {assistant_id}")

    if not gemini_configured():
        return (
            f"{assistant['emoji']} {assistant['name']}: Gemini key not set.\n"
            "Add GEMINI_API_KEY to .env for AI assistants."
        )

    context_parts = [f"User message: {message}"]
    if assistant_id in ("cart", "budget"):
        view = await get_cart_view(household_id)
        context_parts.append(f"Cart JSON:\n{json.dumps(view, ensure_ascii=False)[:6000]}")

    prompt = "\n\n".join(context_parts)

    from google import genai

    client = genai.Client(api_key=settings.gemini_api_key.strip())
    response = client.models.generate_content(
        model=settings.gemini_model,
        contents=prompt,
        config={
            "system_instruction": assistant["system"],
            "temperature": 0.4,
        },
    )
    text = (response.text or "").strip()
    return text or "No response from assistant."


def format_assistants_menu() -> str:
    lines = ["🤖 AI Assistants (Gemini)", ""]
    for a in list_assistants():
        lines.append(f"{a['emoji']} {a['name']}")
        lines.append(f"   {a['description']}")
        lines.append(f"   /ask {a['id']} your question")
        lines.append("")
    return "\n".join(lines).strip()
