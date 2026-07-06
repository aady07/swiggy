from backend.bot_labels import option_button_label


def test_whatsapp_label_keeps_full_price():
    label = option_button_label("Supreme Harvest Rajma Chitra (1 kg)", 167, max_len=20)
    assert label.endswith("₹167")
    assert len(label) <= 20


def test_mangat_ram_price():
    label = option_button_label("Mangat Ram Rajma Chitra (1 kg)", 205, max_len=20)
    assert "₹205" in label
    assert "₹20" != label[-3:]  # not truncated to 2 digits


def test_telegram_label_keeps_full_price():
    long_name = "Supreme Harvest Rajma Kashmiri (1 kg) extra"
    label = option_button_label(long_name, 210, max_len=40)
    assert label.endswith("₹210")


def test_no_price():
    assert option_button_label("Milk 1L", None, max_len=20) == "Milk 1L"
