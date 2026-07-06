from backend.bot_images import (
    collage_public_url,
    format_pick_caption,
    product_full_url,
    product_thumb_url,
)
from backend.models import ClarificationOption


def test_product_thumb_url():
    url = (
        "https://media-assets.swiggy.com/swiggy/image/upload/"
        "NI_CATALOG/IMAGES/CIW/2026/6/22/foo.jpg"
    )
    thumb = product_thumb_url(url, width=420)
    assert "w_420,h_420,c_fit" in thumb
    assert thumb.endswith("foo.jpg")


def test_collage_public_url():
    url = collage_public_url("https://swiggy.example.com", 42)
    assert url == "https://swiggy.example.com/api/collage/42.png"


def test_format_pick_caption():
    opt = ClarificationOption(
        spin_id="x",
        name="Rajma Chitra",
        pack_size="1 kg",
        unit_price=167,
    )
    text = format_pick_caption(opt, 1, enlarge_hint=True)
    assert "Rajma Chitra" in text
    assert "₹167" in text
    assert "enlarge" in text.lower()


def test_product_full_url_larger():
    url = (
        "https://media-assets.swiggy.com/swiggy/image/upload/"
        "NI_CATALOG/IMAGES/foo.jpg"
    )
    full = product_full_url(url, width=900)
    assert "w_900" in full
