"""The 100-icon set for categories AND goals (v23, owner-approved 2026-11-15): genuine Lucide names, byte for byte as
the approved design pack's icons.json, in eight groups, plus the 16-icon "common" sets the picker shows closed.

One list, one picker: web.ICONS carries every name's drawing (the 68 new ones were added from the pack's SVGs; the other
32 reuse the sprite's own markup, with aliases where its name differs), /api/categories/icons serves these lists to the
browser, goals.ICONS is ALL, and catops validates a saved icon against ALL. static/iconpick.js is the one picker both
Categories and Goals open; nothing else carries a copy of these names.
"""

from __future__ import annotations

GROUPS = [
    ("Money", ["piggy-bank", "wallet", "banknote", "coins", "credit-card", "landmark", "trending-up", "hand-coins",
               "handshake", "receipt-text", "ticket-percent", "bitcoin"]),
    ("Home", ["house", "sofa", "washing-machine", "lightbulb", "plug-zap", "droplets", "flame", "wifi", "key-round",
              "hammer", "paintbrush", "wrench"]),
    ("Food and drink", ["utensils", "coffee", "wine", "beer", "pizza", "apple", "cake", "sandwich", "ice-cream-cone",
                        "cooking-pot", "salad", "shopping-cart"]),
    ("Travel", ["plane", "plane-takeoff", "car", "bus", "bike", "ship", "fuel", "tent", "mountain", "umbrella",
                "luggage", "map-pin"]),
    ("Health", ["heart-pulse", "stethoscope", "syringe", "pill", "glasses", "dumbbell", "heart", "sparkle"]),
    ("Fun", ["gamepad-2", "music", "film", "popcorn", "headphones", "camera", "ticket", "trophy", "party", "book-open"]),
    ("Family and pets", ["baby", "dog", "cat", "fish", "bone", "flower-2", "trees", "leaf", "users", "hand-heart",
                         "heart-handshake", "graduation-cap", "backpack", "school"]),
    ("Work and other", ["briefcase-business", "building-2", "store", "truck", "package", "smartphone", "tv-minimal",
                        "shirt", "scissors", "gift", "star", "crown", "medal", "rocket", "sprout", "target",
                        "shield-check", "zap", "newspaper", "church"]),
]

# The two rows the picker shows while closed (16 each; a phone shows the first PHONE_ROWS of each).
QUICK_CAT = ["utensils", "shopping-cart", "house", "car", "coffee", "plane", "heart-pulse", "gamepad-2",
             "gift", "shirt", "dog", "briefcase-business", "wifi", "fuel", "dumbbell", "graduation-cap"]
QUICK_GOAL = ["tent", "piggy-bank", "house", "car", "plane", "graduation-cap", "gift", "trophy",
              "baby", "rocket", "shield-check", "heart", "briefcase-business", "camera", "bike", "star"]
PHONE_ROWS = 12

ALL = [name for _, names in GROUPS for name in names]
assert len(GROUPS) == 8, len(GROUPS)
assert len(ALL) == 100, len(ALL)
assert len(set(ALL)) == len(ALL)
assert len(QUICK_CAT) == len(QUICK_GOAL) == 16
assert set(QUICK_CAT) <= set(ALL) and set(QUICK_GOAL) <= set(ALL)


def payload() -> dict:
    """GET /api/categories/icons: the taxonomy groups, the two common sets and the count (one read for the picker)."""
    return {"groups": [{"name": name, "icons": list(names)} for name, names in GROUPS],
            "common": {"categories": list(QUICK_CAT), "goals": list(QUICK_GOAL)}, "count": len(ALL)}
