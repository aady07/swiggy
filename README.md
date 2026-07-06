# Household Grocery Agent

One shared Instamart cart for the whole family. Everyone texts what they need — on WhatsApp, Telegram, or the web dashboard — and an agent merges it all into a single order via the [Swiggy Instamart MCP](https://mcp.swiggy.com/builders/).

The problem: Mom sends a grocery list on WhatsApp, Dad adds two more things an hour later, someone types "pulao rice" and nobody knows which of the 40 rice SKUs that means. Three separate orders, three delivery fees, duplicate milk.

This bot fixes that. One household cart, real catalog data, one checkout.

## What it does

- **Multi-channel input** — WhatsApp (Cloud API webhook), Telegram (long polling), and a web chat. All feed the same household cart.
- **Intent extraction** — Gemini (or Anthropic) parses free-form messages like "2 kg atta and something for breakfast" into structured items. Never invents SKUs or prices; everything is verified against the live Instamart catalog via `search_products`.
- **Smart variant picker** — when a query matches many SKUs, an agent ranks them by relevance, pack size, and price instead of blindly taking the first result. Ambiguous cases come back as a clarification with product photos (auto-generated collages).
- **Recipe agent** — "ingredients for chicken biryani" expands a dish into a checklist of real, in-stock products you can add in one tap.
- **Merge & dedupe** — two people ask for milk, the cart gets one line with both requesters tagged. Conflicting requests (1L vs 2L) trigger a household poll.
- **Cross-channel notifications** — Dad adds ghee on Telegram, Mom gets a WhatsApp ping with the updated cart.
- **Settlement summary** — after checkout, everyone sees who owes what.
- **Live dashboard** — household members, activity feed, and the draft cart over WebSocket, plus order analytics (biggest spender, most-ordered items).

## Architecture

```
WhatsApp webhook ─┐
Telegram polling ─┼─► intent extraction (Gemini) ─► search_products (MCP)
Web chat / WS ────┘            │                          │
                               ▼                          ▼
                        conflict resolver ◄──── smart product picker
                               │
                               ▼
                     SQLite draft cart ──► update_cart / get_cart / checkout (MCP)
```

- **Backend**: FastAPI + aiosqlite, WebSocket for live cart updates
- **MCP client**: OAuth against Swiggy, tools: `get_addresses`, `search_products`, `update_cart`, `get_cart`, `checkout`
- **LLM**: pluggable — Gemini, Anthropic, or a no-LLM fallback for simple lists

## Setup

Requires Python 3.11+ and a Swiggy account with a saved delivery address.

```bash
git clone <this-repo> && cd swiggy
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Add a [Gemini API key](https://aistudio.google.com/apikey) to `.env` (free tier is fine):

```
EXTRACTION_PROVIDER=gemini
GEMINI_API_KEY=AIza...
```

Then run:

```bash
uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000, click **Connect Swiggy** (phone + OTP), pick a delivery address, and start typing grocery requests.

### Optional channels

- **Telegram**: set `TELEGRAM_BOT_TOKEN` — the bot starts polling automatically. Supports `/cart`, `/checkout`, `/deals`, `/name`, group chats, and inline polls.
- **WhatsApp**: set the `WHATSAPP_*` variables (Meta Cloud API) and point the webhook at `/api/whatsapp/webhook`. Map family numbers to names with `WHATSAPP_PHONE_MAP`.

## Tests

```bash
pytest backend/tests/ -q
```

48 tests cover the resolver, conflict handling, product picker, recipe agent, bot copy, and analytics — all run offline, no credentials needed.

## Deploy

`deploy/` has everything for a small EC2 box: setup script, systemd unit, nginx config with TLS via certbot, and a production env template. See `deploy/setup-ec2.sh`.

## Notes & limits

- Checkout places a **real Instamart order** with your saved Swiggy payment method.
- MCP beta caps orders at ₹1000; Instamart minimum is ₹99.
- Swiggy access tokens last ~5 days; reconnect when auth expires.

## References

- [Swiggy Builders Club](https://mcp.swiggy.com/builders/)
- [Instamart tool reference](https://mcp.swiggy.com/builders/docs/reference/instamart/)
