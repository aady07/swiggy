from backend.resolver import normalize_products


def test_normalize_swiggy_variations_format():
    swiggy = {
        "products": [
            {
                "displayName": "Aashirvaad Shudh Chakki Atta",
                "productId": "WBZDJ53X5C",
                "inStock": True,
                "variations": [
                    {
                        "spinId": "3ABVY116ZJ",
                        "quantityDescription": "5 kg",
                        "price": {"mrp": 262, "offerPrice": 236},
                "imageUrl": "https://media-assets.swiggy.com/swiggy/image/upload/test.jpg",
                    }
                ],
            }
        ]
    }
    products = normalize_products(swiggy)
    assert len(products) == 1
    assert products[0]["variants"][0]["spin_id"] == "3ABVY116ZJ"
    assert products[0]["variants"][0]["unit_price"] == 236.0
    assert "imageUrl" in swiggy["products"][0]["variations"][0] or products[0]["variants"][0].get("image_url")
