from __future__ import annotations

from backend.models import ClarificationOption
from backend.product_picker import (
    _parse_picker_response,
    pick_option_with_ai,
)


RICE_OPTIONS = [
    ClarificationOption(
        spin_id="spin_regular",
        name="Fortune Rice 5 kg",
        pack_size="5 kg",
        unit_price=250,
        product_id="p1",
    ),
    ClarificationOption(
        spin_id="spin_biryani",
        name="India Gate Basmati Rice Biryani Special 5 kg",
        pack_size="5 kg",
        unit_price=620,
        product_id="p2",
    ),
    ClarificationOption(
        spin_id="spin_sona",
        name="India Gate Sona Masoori 5 kg",
        pack_size="5 kg",
        unit_price=400,
        product_id="p3",
    ),
]


def test_parse_picker_response():
    assert _parse_picker_response('{"spin_id": "abc", "reason": "biryani rice"}') == {
        "spin_id": "abc",
        "reason": "biryani rice",
    }
    assert _parse_picker_response("not json") is None


async def test_pick_option_with_ai_no_gemini(monkeypatch):
    monkeypatch.setattr("backend.product_picker.gemini_configured", lambda: False)
    chosen, reason = await pick_option_with_ai(RICE_OPTIONS, "biryani chawal 5kg")
    assert chosen is None
    assert reason is None
