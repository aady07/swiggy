from __future__ import annotations

import json
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from backend.config import settings
from backend.oauth import get_valid_access_token, invalidate_token_on_401


class MCPError(Exception):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _parse_tool_result(result: Any) -> dict[str, Any]:
    if getattr(result, "isError", False):
        texts = []
        for block in getattr(result, "content", []) or []:
            if hasattr(block, "text"):
                texts.append(block.text)
        raise MCPError(" ".join(texts) or "MCP tool call failed")

    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        payload = structured
    else:
        content = result.content if hasattr(result, "content") else result
        if isinstance(content, list):
            texts = []
            for block in content:
                if hasattr(block, "text"):
                    texts.append(block.text)
                elif isinstance(block, dict) and block.get("type") == "text":
                    texts.append(block.get("text", ""))
            raw = "".join(texts)
        elif isinstance(content, str):
            raw = content
        else:
            raw = json.dumps(content)

        try:
            payload = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            return {"success": True, "data": {"raw": raw}, "message": raw}

    if isinstance(payload, dict) and "success" in payload:
        if not payload.get("success"):
            err = payload.get("error") or {}
            msg = err.get("message") if isinstance(err, dict) else str(err)
            raise MCPError(msg or "MCP tool call failed")
        return payload

    return {"success": True, "data": payload}


def _extract_address_list(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [a for a in data if isinstance(a, dict)]
    if not isinstance(data, dict):
        return []

    for key in (
        "addresses",
        "savedAddresses",
        "saved_addresses",
        "userAddresses",
        "data",
        "items",
        "results",
    ):
        val = data.get(key)
        if isinstance(val, list):
            return [a for a in val if isinstance(a, dict)]

    return []


def normalize_address(addr: dict[str, Any]) -> dict[str, Any]:
    addr_id = (
        addr.get("addressId")
        or addr.get("id")
        or addr.get("address_id")
        or addr.get("addrId")
    )
    label = (
        addr.get("label")
        or addr.get("tag")
        or addr.get("title")
        or addr.get("name")
        or addr.get("annotation")
        or "Address"
    )
    line = (
        addr.get("displayAddress")
        or addr.get("address")
        or addr.get("fullAddress")
        or addr.get("formattedAddress")
        or addr.get("addressLine")
        or addr.get("displayText")
        or ""
    )
    if not line and isinstance(addr.get("addressDetails"), dict):
        line = addr["addressDetails"].get("formattedAddress") or ""
    return {
        "id": str(addr_id) if addr_id is not None else "",
        "addressId": str(addr_id) if addr_id is not None else "",
        "label": str(label),
        "address": str(line),
        "displayAddress": str(line),
    }


class SwiggyInstamartClient:
    async def _call_tool(self, name: str, arguments: dict | None = None) -> dict[str, Any]:
        token = await get_valid_access_token()
        if not token:
            raise MCPError("Not authenticated", status_code=401)

        arguments = arguments or {}
        try:
            async with streamablehttp_client(
                settings.swiggy_mcp_url,
                headers={"Authorization": f"Bearer {token}"},
            ) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(name, arguments=arguments)
                    return _parse_tool_result(result)
        except MCPError:
            raise
        except Exception as exc:
            msg = str(exc)
            if "401" in msg or "Unauthorized" in msg:
                await invalidate_token_on_401()
                raise MCPError("Authentication expired", status_code=401) from exc
            raise MCPError(msg) from exc

    async def get_addresses(self) -> list[dict[str, Any]]:
        resp = await self._call_tool("get_addresses")
        data = resp.get("data")
        raw_list = _extract_address_list(data)
        if not raw_list and isinstance(data, dict):
            raw_list = _extract_address_list(resp)
        normalized = [normalize_address(a) for a in raw_list]
        return [a for a in normalized if a.get("id")]

    async def search_products(self, address_id: str, query: str, offset: int = 0) -> dict:
        resp = await self._call_tool(
            "search_products",
            {"addressId": address_id, "query": query, "offset": offset},
        )
        return resp.get("data") or {}

    async def your_go_to_items(self, address_id: str) -> dict:
        resp = await self._call_tool("your_go_to_items", {"addressId": address_id})
        return resp.get("data") or {}

    async def get_cart(self) -> dict:
        resp = await self._call_tool("get_cart")
        return resp.get("data") or {}

    async def update_cart(self, selected_address_id: str, items: list[dict]) -> dict:
        resp = await self._call_tool(
            "update_cart",
            {"selectedAddressId": selected_address_id, "items": items},
        )
        return resp.get("data") or {}

    async def clear_cart(self) -> dict:
        resp = await self._call_tool("clear_cart")
        return resp.get("data") or {}

    async def checkout(self, address_id: str, payment_method: str | None = None) -> dict:
        args: dict[str, str] = {"addressId": address_id}
        if payment_method:
            args["paymentMethod"] = payment_method
        resp = await self._call_tool("checkout", args)
        return {"data": resp.get("data") or {}, "message": resp.get("message")}

    async def get_orders(
        self,
        count: int = 20,
        order_type: str = "DASH",
        active_only: bool = False,
    ) -> dict:
        resp = await self._call_tool(
            "get_orders",
            {
                "count": count,
                "orderType": order_type,
                "activeOnly": active_only,
            },
        )
        return resp.get("data") or {}

    async def get_order_details(self, order_id: str) -> dict:
        resp = await self._call_tool("get_order_details", {"orderId": order_id})
        return resp.get("data") or {}

    async def track_order(self, order_id: str) -> dict:
        resp = await self._call_tool("track_order", {"orderId": order_id})
        return resp.get("data") or {}


mcp_client = SwiggyInstamartClient()
