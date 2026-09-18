"""Smoke tests for eBay condition-ID mapping (app.ebay_client.map_condition_for_category).

This file previously contained a handful of newline-separated tokens that weren't
valid Python (each word of a one-off debug script on its own line) and broke
`pytest` collection for the whole backend. Replaced with an actual runnable test.
"""
from unittest.mock import patch

from app.ebay_client import map_condition_for_category


def test_books_category_maps_condition_ids():
    # eBay's Books category (261186) uses numeric conditionIds, not the usual
    # USED_GOOD/USED_EXCELLENT enum values.
    with patch("app.ebay_client.get_category_allowed_condition_ids", return_value=["1000", "2750", "4000", "5000", "6000"]):
        assert map_condition_for_category("261186", "Very good") == "4000"
        assert map_condition_for_category("261186", "Good") == "5000"
        assert map_condition_for_category("261186", "Fair") == "6000"
        assert map_condition_for_category("261186", "New") == "1000"


def test_category_without_numeric_ids_falls_back_to_enum():
    with patch("app.ebay_client.get_category_allowed_condition_ids", return_value=[]):
        assert map_condition_for_category("12345", "Very good") == "USED_EXCELLENT"
        assert map_condition_for_category("12345", "Good") == "USED_GOOD"
        assert map_condition_for_category(None, None) == "USED_GOOD"
