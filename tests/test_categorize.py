from invespend.categorize import categorize


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
