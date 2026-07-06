from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


DraftStatus = Literal[
    "resolved",
    "needs_clarification",
    "needs_poll",
    "out_of_stock",
    "checked_out",
    "excluded",
]


class ExtractedItem(BaseModel):
    raw_query: str
    sender: str
    search_query: str | None = None
    alternate_search_queries: list[str] = Field(default_factory=list)
    note: str | None = None
    needs_clarification: bool = False


class ClarificationOption(BaseModel):
    spin_id: str
    name: str
    pack_size: str | None = None
    unit_price: float | None = None
    product_id: str | None = None
    image_url: str | None = None


def as_clarification_option(raw: ClarificationOption | dict[str, Any]) -> ClarificationOption:
    if isinstance(raw, ClarificationOption):
        return raw
    return ClarificationOption(**raw)


def clarification_option_dict(raw: ClarificationOption | dict[str, Any]) -> dict[str, Any]:
    if isinstance(raw, ClarificationOption):
        return raw.model_dump()
    return raw


class DraftItem(BaseModel):
    id: int
    household_id: str
    raw_query: str
    resolved_sku: str | None = None
    resolved_name: str | None = None
    pack_size: str | None = None
    unit_price: float | None = None
    merged_qty: int = 1
    requested_by: list[str] = Field(default_factory=list)
    status: DraftStatus
    alternatives: list[ClarificationOption] = Field(default_factory=list)
    clarification_options: list[ClarificationOption] = Field(default_factory=list)
    auto_picked: bool = False
    product_id: str | None = None
    excluded: bool = False
    note: str | None = None
    created_at: str | None = None


class ChatMessage(BaseModel):
    id: int
    household_id: str
    sender: str
    text: str
    created_at: str


class WSEvent(BaseModel):
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)


class SendMessageRequest(BaseModel):
    sender: str
    text: str


class AdminLoginRequest(BaseModel):
    username: str
    password: str


class ResolveDraftRequest(BaseModel):
    draft_item_id: int
    spin_id: str


class ExcludeDraftRequest(BaseModel):
    draft_item_id: int
    excluded: bool = True


class RemoveDraftRequest(BaseModel):
    line_number: int | None = None
    draft_item_id: int | None = None


class AssistantChatRequest(BaseModel):
    assistant_id: str
    message: str


class SetAddressRequest(BaseModel):
    address_id: str


class CheckoutRequest(BaseModel):
    confirmed: bool = True


class DummyCheckoutRequest(BaseModel):
    confirmed: bool = True
    placed_by: str = "Admin"


class AuthStatus(BaseModel):
    authenticated: bool
    expires_at: str | None = None


class AddressOption(BaseModel):
    id: str
    label: str
    address_line: str


class DraftSnapshot(BaseModel):
    items: list[DraftItem]
    estimated_subtotal: float
    resolved_count: int
    unresolved_count: int


class SettlementLine(BaseModel):
    sender: str
    items: list[str]
    estimated_share: float


class CheckoutResult(BaseModel):
    success: bool
    swiggy_message: str | None = None
    settlement: list[SettlementLine] = Field(default_factory=list)
    total: float | None = None
    error: str | None = None
    order_id: int | None = None
    order_type: str | None = None


class CartItemInput(BaseModel):
    spin_id: str
    quantity: int
    product_id: str | None = None
    name: str | None = None
    pack_size: str | None = None
    unit_price: float | None = None
