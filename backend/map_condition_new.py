def map_condition_for_category(category_id: str | None, condition_str: str | None) -> str:
    """Maps a user condition string to an Inventory API ConditionEnum or condition ID that is valid
    for the specific category (e.g. Books requires '4000' for Very Good, not 'USED_EXCELLENT')."""
    cond = (condition_str or "").strip().lower()
    allowed_ids = get_category_allowed_condition_ids(category_id) if category_id else []

    # If category has specific condition IDs (like Books: 1000=New, 2750=Like New, 4000=Very Good, etc.)
    if allowed_ids:
        # Check for 'new without tag' / 'new other' / 'new with defect' first
        if "without tag" in cond or "no tag" in cond or "new without" in cond or "new other" in cond:
            if "2990" in allowed_ids:
                return "2990"
            if "NEW_OTHER" in allowed_ids:
                return "NEW_OTHER"
        if "defect" in cond or "imperfection" in cond:
            if "2980" in allowed_ids:
                return "2980"
            if "NEW_WITH_DEFECTS" in allowed_ids:
                return "NEW_WITH_DEFECTS"

        # Check for specific new conditions first
        if "like new" in cond or "new without tag" in cond:
            if "2750" in allowed_ids:
                return "2750"
            if "1000" in allowed_ids:
                return "1000"
        elif "new" in cond and "tag" not in cond:
            if "1000" in allowed_ids:
                return "1000"

        # For used items, try to match the closest condition ID
        if "very good" in cond or "excellent" in cond:
            if "4000" in allowed_ids:
                return "4000"
            if "USED_EXCELLENT" in allowed_ids:
                return "USED_EXCELLENT"
        if "good" in cond and "very" not in cond:
            if "5000" in allowed_ids:
                return "5000"
            if "USED_GOOD" in allowed_ids:
                return "USED_GOOD"
        if "fair" in cond or "acceptable" in cond:
            if "6000" in allowed_ids:
                return "6000"
            if "USED_ACCEPTABLE" in allowed_ids:
                return "USED_ACCEPTABLE"

    # Standard enum mapping for categories that don't require specific IDs
    if not allowed_ids or "5000" in allowed_ids:
        if "fair" in cond or "acceptable" in cond:
            return "USED_ACCEPTABLE"
        if "good" in cond and "very" not in cond:
            return "USED_GOOD"
        if "very good" in cond or "excellent" in cond or "like new" in cond:
            return "USED_EXCELLENT"
        return "USED_GOOD"

    # If category only allows 3000 / 2990 (e.g. Clothing/Shoes which only accept Pre-owned)
    if "3000" in allowed_ids or "2990" in allowed_ids:
        return "USED_EXCELLENT"

    return "USED_EXCELLENT"
