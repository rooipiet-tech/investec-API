from invespend.categorize import CATEGORY_RULES, categorize, category_map_rows


def test_known_merchants_map_to_categories():
    assert categorize("WOOLWORTHS SANDTON") == "Groceries"
    assert categorize("Uber *trip help.uber.com") == "Transport"
    assert categorize("NETFLIX.COM") == "Subscriptions"
    assert categorize("Dis-Chem Pharmacy") == "Health"


def test_falls_back_to_other():
    assert categorize("SOME RANDOM PLACE") == "Other"
    assert categorize(None) == "Other"


def test_uses_transaction_type_too():
    assert categorize("", "FeesAndInterest") == "Fees"


def _map_lookup(haystack: str, rows) -> str:
    """Mirror the SQL view: lowest-priority keyword that is a substring wins."""
    matches = [(pri, cat) for kw, cat, pri in rows if kw in haystack.lower()]
    return min(matches)[1] if matches else "Other"


def test_category_map_projection_agrees_with_categorize():
    # The DB projection and the Python engine must classify identically — that
    # equivalence is the whole point of having one source of truth. Check it
    # holds for every keyword the rules define.
    rows = category_map_rows()
    for keyword, category, _ in rows:
        assert categorize(keyword) == _map_lookup(keyword, rows)
        # The keyword alone resolves to its own category (no earlier rule steals it).
        assert _map_lookup(keyword, rows) == category


def test_category_map_rows_unique_and_priority_ordered():
    rows = category_map_rows()
    keywords = [kw for kw, _, _ in rows]
    assert len(keywords) == len(set(keywords))  # unique (category_map PK is keyword)
    priorities = [pri for _, _, pri in rows]
    assert priorities == sorted(priorities)     # strictly increasing in rule order
    # Every rule's keywords are represented.
    assert len(rows) == sum(len(kws) for _, kws in CATEGORY_RULES)
