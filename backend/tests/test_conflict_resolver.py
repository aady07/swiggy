from __future__ import annotations

from backend.conflict_resolver import (
    find_conflicting_draft,
    preferences_conflict,
    category_key,
)
from backend.models import DraftItem, ExtractedItem


def _draft(**kwargs) -> DraftItem:
    defaults = {
        "id": 1,
        "household_id": "default",
        "raw_query": "organic atta 5kg",
        "merged_qty": 1,
        "requested_by": ["Priya"],
        "status": "resolved",
        "resolved_name": "Aashirvaad Organic Atta 5kg",
        "alternatives": [],
        "clarification_options": [],
        "auto_picked": True,
        "excluded": False,
    }
    defaults.update(kwargs)
    return DraftItem(**defaults)


def test_preferences_conflict_cheap_vs_organic():
    assert preferences_conflict({"cheap"}, {"premium"}) is True
    assert preferences_conflict({"cheap"}, {"cheap"}) is False


def test_find_conflict_atta():
    existing = _draft()
    bob = ExtractedItem(
        raw_query="cheapest atta 5kg",
        search_query="aashirvaad atta 5 kg",
        sender="Bob",
    )
    hit = find_conflicting_draft([existing], bob)
    assert hit is not None
    assert hit.id == 1


def test_no_conflict_same_sender():
    existing = _draft(requested_by=["Bob"])
    bob = ExtractedItem(raw_query="cheapest atta", search_query="atta", sender="Bob")
    assert find_conflicting_draft([existing], bob) is None


def test_category_key():
    assert category_key("basmati chawal 5kg") == "rice"
    assert category_key("lays magic masala") == "snacks"
