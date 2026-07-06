from backend.bot_copy import format_recipe_list, recipe_list_keyboard
from backend.recipe_agent import (
    extract_dish_name,
    is_recipe_add_all,
    is_recipe_question,
    parse_recipe_add_numbers,
)


def test_recipe_question_detected():
    for phrase in [
        "aj mujhe chole bhature banane hai kya kya lagega",
        "chole bhature banane me kya lagega",
        "paneer butter masala ke liye kya chahiye",
        "what do I need for biryani",
        "ingredients for pasta",
    ]:
        assert is_recipe_question(phrase), phrase


def test_buy_not_recipe():
    for phrase in [
        "atta 5kg",
        "milk and bread",
        "order milk",
        "show me the cart",
    ]:
        assert not is_recipe_question(phrase), phrase


def test_extract_dish_chole_bhature():
    dish = extract_dish_name("aj mujhe chole bhature banane hai kya kya lagega")
    assert dish is not None
    assert "chole" in dish.lower()
    assert "bhature" in dish.lower()


def test_recipe_add_all_phrases():
    assert is_recipe_add_all("sab add karo")
    assert is_recipe_add_all("add all")
    assert is_recipe_add_all("haan add kar do")


def test_parse_add_numbers():
    assert parse_recipe_add_numbers("add 1 3 5") == [1, 3, 5]
    assert parse_recipe_add_numbers("add 2 and 4") == [2, 4]


def test_format_recipe_list():
    session = {
        "dish": "chole bhature",
        "lines": [
            {
                "status": "found",
                "ingredient": "kabuli chana",
                "resolved_name": "Tata Kabuli Chana 500g",
                "unit_price": 89,
            },
            {
                "status": "needs_pick",
                "ingredient": "atta",
            },
            {
                "status": "not_found",
                "ingredient": "fresh coriander",
            },
        ],
    }
    text = format_recipe_list(session)
    assert "chole bhature" in text.lower()
    assert "Tata Kabuli Chana" in text
    assert "Brand chunna" in text or "?" in text
    assert "Nahi mila" in text
    assert "Mil gaya" in text or "cart mein" in text
