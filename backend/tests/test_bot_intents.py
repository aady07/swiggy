from backend.bot_intents import ChatIntentKind, detect_chat_intent, is_place_order_message


def test_show_cart_intents():
    for phrase in [
        "show me the cart",
        "show cart",
        "what's in the cart",
        "what is in my cart",
        "cart dikhao",
        "dikhao cart",
        "my cart",
        "view cart",
        "cart kya hai",
    ]:
        assert detect_chat_intent(phrase).kind == ChatIntentKind.SHOW_CART, phrase


def test_clear_cart_intents():
    for phrase in ["clear cart", "empty the cart", "cart khali karo", "cart clear karo"]:
        assert detect_chat_intent(phrase).kind == ChatIntentKind.CLEAR_CART, phrase


def test_place_order_intents():
    for phrase in ["checkout", "order this cart", "place order", "order cart kar do"]:
        assert detect_chat_intent(phrase).kind == ChatIntentKind.PLACE_ORDER, phrase


def test_remove_intent():
    intent = detect_chat_intent("remove 3")
    assert intent.kind == ChatIntentKind.REMOVE_LINE
    assert intent.line_number == 3
    assert detect_chat_intent("hatao 2").line_number == 2


def test_help_and_connect():
    assert detect_chat_intent("help").kind == ChatIntentKind.HELP
    assert detect_chat_intent("connect swiggy").kind == ChatIntentKind.CONNECT


def test_grocery_not_misrouted():
    for phrase in [
        "show me atta 5kg",
        "milk 2L",
        "white tshirt medium",
        "order milk",
    ]:
        assert detect_chat_intent(phrase).kind == ChatIntentKind.ADD_ITEMS, phrase


def test_is_place_order_message_compat():
    assert is_place_order_message("checkout")
    assert not is_place_order_message("show cart")
