from backend.cart_ops import is_place_order_message


def test_place_order_phrases():
    assert is_place_order_message("order this cart")
    assert is_place_order_message("order the cart")
    assert is_place_order_message("place order")
    assert is_place_order_message("place the order")
    assert is_place_order_message("checkout")
    assert is_place_order_message("checkout this cart")
    assert is_place_order_message("order cart kar do")


def test_not_place_order_phrases():
    assert not is_place_order_message("order milk")
    assert not is_place_order_message("atta 5kg")
    assert not is_place_order_message("order some atta")
