"""Smoke tests for eBay condition mapping (app.ebay_client.map_condition_for_category).

This file previously contained a handful of newline-separated tokens that weren't
valid Python (each word of a one-off debug script on its own line) and broke
`pytest` collection for the whole backend. Replaced with an actual runnable test.
"""
from unittest.mock import patch

from app.ebay_client import map_condition_for_category


def test_generic_category_uses_standard_enums():
    # No category-specific condition policy (metadata lookup returns nothing) -
    # falls back to the plain, semantically obvious enum for each tier.
    with patch("app.ebay_client.get_category_allowed_condition_ids", return_value=[]):
        assert map_condition_for_category("12345", "New without tags") == "NEW_OTHER"
        assert map_condition_for_category("12345", "Very good") == "USED_VERY_GOOD"
        assert map_condition_for_category("12345", "Good") == "USED_GOOD"
        assert map_condition_for_category("12345", "Fair") == "USED_ACCEPTABLE"
        assert map_condition_for_category(None, None) == "USED_GOOD"


def test_trainers_category_only_allows_its_own_condition_subset():
    # eBay's real Trainers (95672) item condition policy: {1000, 1500, 1750, 2990,
    # 3000, 3010} -> {NEW, NEW_OTHER, NEW_WITH_DEFECTS, PRE_OWNED_EXCELLENT,
    # USED_EXCELLENT, PRE_OWNED_FAIR}. USED_GOOD/USED_VERY_GOOD/USED_ACCEPTABLE are
    # real enum values but NOT in that set - sending one of those passes inventory
    # item PUT (which only checks it's a real enum name) but fails offer publish
    # with errorId 25021 ("condition id is invalid for the selected primary
    # category id") - confirmed live. Every value returned here must be one this
    # category actually accepts.
    with patch(
        "app.ebay_client.get_category_allowed_condition_ids",
        return_value=["1000", "1500", "1750", "2990", "3000", "3010"],
    ):
        assert map_condition_for_category("95672", "New without tags") == "NEW_OTHER"
        assert map_condition_for_category("95672", "Very good") == "PRE_OWNED_EXCELLENT"
        assert map_condition_for_category("95672", "Good") == "USED_EXCELLENT"
        assert map_condition_for_category("95672", "Fair") == "PRE_OWNED_FAIR"
