from backend.bot_copy import format_grocery_understood, format_recipe_understood


def test_grocery_understood_single():
    text = format_grocery_understood("Mom", ["atta 5kg"])
    assert "Mom" in text
    assert "samjha" in text.lower()
    assert "atta" in text


def test_grocery_understood_multi():
    text = format_grocery_understood("Dad", ["atta", "milk", "onion"])
    assert "3 cheezein" in text


def test_recipe_understood():
    text = format_recipe_understood("chole bhature")
    assert "Samajh gaya" in text
    assert "Chole Bhature" in text
    assert "order nahi" in text.lower()
