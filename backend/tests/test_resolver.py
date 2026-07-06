from __future__ import annotations

import pytest

from backend.models import ClarificationOption
from backend.resolver import (
    match_option_from_preferences,
    normalize_products,
    pick_best_variant_for_qty,
    pick_median_option,
    resolve_search_results,
    try_auto_pick_clarification,
    try_auto_pick_clarification_async,
)


SAMPLE_SEARCH = {
    "products": [
        {
            "productId": "p1",
            "displayName": "Amul Taaza Milk",
            "variants": [
                {
                    "spinId": "spin_500",
                    "quantity": "500 ml",
                    "offerPrice": 30,
                    "inStock": True,
                },
                {
                    "spinId": "spin_1l",
                    "quantity": "1 L",
                    "offerPrice": 58,
                    "inStock": True,
                },
            ],
        },
        {
            "productId": "p2",
            "displayName": "Mother Dairy Milk",
            "variants": [
                {
                    "spinId": "spin_md",
                    "quantity": "500 ml",
                    "offerPrice": 32,
                    "inStock": True,
                }
            ],
        },
    ]
}


def test_normalize_products():
    products = normalize_products(SAMPLE_SEARCH)
    assert len(products) == 2
    assert products[0]["variants"][0]["spin_id"] == "spin_500"


def test_single_product_one_variant_auto_resolves():
    single = {
        "products": [
            {
                "productId": "p1",
                "displayName": "Amul Taaza Milk",
                "variants": [
                    {
                        "spinId": "spin_500",
                        "quantity": "500 ml",
                        "offerPrice": 30,
                        "inStock": True,
                    }
                ],
            }
        ]
    }
    result = resolve_search_results("milk", single, requested_qty=1)
    assert result["status"] == "resolved"
    assert result["resolved_sku"] == "spin_500"


def test_single_product_multi_variant_needs_pick():
    single = {"products": [SAMPLE_SEARCH["products"][0]]}
    result = resolve_search_results("milk", single, requested_qty=1)
    assert result["status"] == "needs_clarification"
    assert len(result["clarification_options"]) == 2


def test_multiple_products_need_clarification():
    result = resolve_search_results("milk", SAMPLE_SEARCH, requested_qty=1)
    assert result["status"] == "needs_clarification"
    assert len(result["clarification_options"]) >= 2


def test_out_of_stock():
    oos = {
        "products": [
            {
                "productId": "p3",
                "displayName": "Kurkure",
                "variants": [{"spinId": "k1", "offerPrice": 20, "inStock": False}],
            }
        ]
    }
    result = resolve_search_results("kurkure", oos)
    assert result["status"] == "out_of_stock"


def test_right_size_picks_larger_pack_for_qty():
    single = {"products": [SAMPLE_SEARCH["products"][0]]}
    variant, _ = pick_best_variant_for_qty(normalize_products(single)[0], qty=3)
    assert variant is not None
    assert variant["spin_id"] in {"spin_500", "spin_1l"}


ATTA_OPTIONS = [
    {
        "spin_id": "spin_mg",
        "name": "Aashirvaad Multigrains Atta (5 kg)",
        "pack_size": "5 kg",
        "unit_price": 311,
        "product_id": "p_mg",
    },
    {
        "spin_id": "spin_org",
        "name": "Aashirvaad Organic Whole Wheat Atta (5 kg)",
        "pack_size": "5 kg",
        "unit_price": 275,
        "product_id": "p_org",
    },
    {
        "spin_id": "spin_sharbati",
        "name": "Aashirvaad Select Sharbati Atta (5 kg)",
        "pack_size": "5 kg",
        "unit_price": 313,
        "product_id": "p_sharbati",
    },
    {
        "spin_id": "spin_protein",
        "name": "Aashirvaad Meri Chakki Protein Atta (5 kg)",
        "pack_size": "5 kg",
        "unit_price": 532,
        "product_id": "p_protein",
    },
    {
        "spin_id": "spin_1kg",
        "name": "Aashirvaad Atta High Protein (1 kg)",
        "pack_size": "1 kg",
        "unit_price": 49,
        "product_id": "p_1kg",
    },
]


def test_pick_median_option_prefers_matching_pack():
    chosen = pick_median_option(ATTA_OPTIONS, pack_hint="5kg")
    assert chosen is not None
    assert chosen.spin_id == "spin_sharbati"
    assert chosen.unit_price == 313


def test_match_option_from_past_order_by_spin():
    prefs = [{"name": "Aashirvaad Organic Whole Wheat Atta", "spin_id": "spin_org"}]
    chosen = match_option_from_preferences(ATTA_OPTIONS, prefs, raw_query="atta 5kg")
    assert chosen is not None
    assert chosen.spin_id == "spin_org"


async def test_try_auto_pick_keeps_recommendations_without_past_order(monkeypatch):
    async def _no_ai(*_args, **_kwargs):
        return None, None

    monkeypatch.setattr(
        "backend.product_picker.pick_option_with_ai",
        _no_ai,
    )

    resolution = {
        "status": "needs_clarification",
        "clarification_options": ATTA_OPTIONS,
        "alternatives": [],
    }
    result = await try_auto_pick_clarification_async(
        resolution, [], "biryani rice 5kg", requested_qty=1
    )
    assert result["status"] == "needs_clarification"
    assert len(result["clarification_options"]) >= 2
    assert "pick a brand" in result["note"].lower()


async def test_try_auto_pick_uses_ai_picker(monkeypatch):
    ai_option = ClarificationOption(
        spin_id="spin_biryani",
        name="India Gate Basmati Rice Biryani Special 5 kg",
        pack_size="5 kg",
        unit_price=620,
        product_id="p2",
    )

    async def _ai_pick(*_args, **_kwargs):
        return ai_option, "AI: biryani rice"

    monkeypatch.setattr(
        "backend.product_picker.pick_option_with_ai",
        _ai_pick,
    )

    resolution = {
        "status": "needs_clarification",
        "clarification_options": ATTA_OPTIONS,
        "alternatives": [],
    }
    result = await try_auto_pick_clarification_async(
        resolution, [], "biryani chawal 5kg", requested_qty=1
    )
    assert result["status"] == "resolved"
    assert result["resolved_sku"] == "spin_biryani"
    assert "AI" in result["note"]


async def test_try_auto_pick_uses_past_order(monkeypatch):
    async def _should_not_run(*_args, **_kwargs):
        raise AssertionError("AI picker should not run when past order matches")

    monkeypatch.setattr(
        "backend.product_picker.pick_option_with_ai",
        _should_not_run,
    )

    resolution = {
        "status": "needs_clarification",
        "clarification_options": ATTA_OPTIONS,
        "alternatives": [],
    }
    prefs = [{"name": "Aashirvaad Organic Whole Wheat Atta", "spin_id": "spin_org"}]
    result = await try_auto_pick_clarification_async(
        resolution, prefs, "atta 5kg", requested_qty=1
    )
    assert result["status"] == "resolved"
    assert result["resolved_sku"] == "spin_org"
    assert "past order" in result["note"]
