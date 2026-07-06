from backend.order_analytics import compute_order_analytics


def test_empty_analytics():
    data = compute_order_analytics([])
    assert data["total_orders"] == 0
    assert data["biggest_spender"] is None


def test_spend_and_top_items():
    orders = [
        {
            "id": 1,
            "total": 500,
            "placed_at": "2026-07-01T10:00:00",
            "order_type": "dummy",
            "settlement": [
                {"sender": "Priya", "estimated_share": 300, "items": ["Atta"]},
                {"sender": "Adarsh", "estimated_share": 200, "items": ["Milk"]},
            ],
            "items": [
                {"resolved_name": "Aashirvaad Atta 5kg", "quantity": 1, "requested_by": ["Priya"]},
                {"resolved_name": "Amul Milk 1L", "quantity": 2, "requested_by": ["Adarsh"]},
            ],
        },
        {
            "id": 2,
            "total": 300,
            "placed_at": "2026-07-03T12:00:00",
            "order_type": "dummy",
            "settlement": [
                {"sender": "Priya", "estimated_share": 300, "items": ["Atta"]},
            ],
            "items": [
                {"resolved_name": "Aashirvaad Atta 5kg", "quantity": 1, "requested_by": ["Priya"]},
            ],
        },
    ]
    data = compute_order_analytics(orders)
    assert data["total_orders"] == 2
    assert data["total_spend"] == 800
    assert data["biggest_spender"]["sender"] == "Priya"
    assert data["biggest_spender"]["total_spend"] == 600
    assert data["top_item"]["name"] == "Aashirvaad Atta 5kg"
    assert data["top_item"]["order_count"] == 2
