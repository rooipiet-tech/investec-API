"""F5/F11/F18: extraction is deterministic and fails closed on ambiguity."""
import pytest

from invespend.payments import extract


def test_clean_text_single_candidate():
    r = extract.extract_from_text("Payee: Acme\nAmount: R 1234.56")
    assert r.status == "ok"
    assert r.amount == "1234.56"
    assert r.currency == "ZAR"
    assert r.payee == "Acme"


def test_multiple_amounts_needs_review():
    r = extract.extract_from_text("Pay R 100.00 and R 200.00")
    assert r.status == "needs_review"


def test_multiple_payees_needs_review():
    r = extract.extract_from_text("Payee: Acme\nPayee: Beta\nAmount: R 100.00")
    assert r.status == "needs_review"


def test_currency_mismatch_needs_review():
    r = extract.extract_from_text("Amount: USD 50.00", expected_currency="ZAR")
    assert r.status == "needs_review"


def test_no_amount_needs_review():
    r = extract.extract_from_text("hello there, please approve")
    assert r.status == "needs_review"


def test_csv_clean():
    r = extract.extract_from_csv("Payee,Acme\nAmount,R 250.00")
    assert r.status == "ok"
    assert r.amount == "250.00"
    assert r.payee == "Acme"


def test_corrupt_pdf_needs_review():
    r = extract.extract_from_pdf(b"%PDF-not-really")
    assert r.status == "needs_review"


def test_image_skipped_no_network(monkeypatch):
    # F11: image -> skipped, and assert NO network egress is attempted.
    import socket

    def boom(*a, **k):
        raise AssertionError("network egress attempted for image OCR")

    monkeypatch.setattr(socket.socket, "connect", boom)
    r = extract.extract_attachment("receipt.png", b"\x89PNG\r\n")
    assert r.status == "skipped"
    assert "OCR" in r.reason


def test_xlsx_clean_when_pandas_present():
    pd = pytest.importorskip("pandas")
    import io
    buf = io.BytesIO()
    df = pd.DataFrame([["Payee", "Acme"], ["Amount", "R 777.00"]])
    df.to_excel(buf, index=False, header=False)
    r = extract.extract_from_xlsx(buf.getvalue())
    assert r.status == "ok"
    assert r.amount == "777.00"


def test_corrupt_xlsx_needs_review():
    r = extract.extract_from_xlsx(b"not an excel file")
    assert r.status == "needs_review"


# ── RS2/F11/F18: oversized attachment fails closed WITHOUT invoking the parser ─
def test_oversized_attachment_needs_review_parser_not_invoked(monkeypatch):
    called = {"pdf": False}

    def boom_pdf(data, expected_currency="ZAR"):
        called["pdf"] = True
        raise AssertionError("parser must not be invoked for oversized payload")

    monkeypatch.setattr(extract, "extract_from_pdf", boom_pdf)
    payload = b"%PDF-1.4" + b"\x00" * (extract.MAX_ATTACHMENT_BYTES + 1)
    r = extract.extract_attachment("statement.pdf", payload)
    assert r.status == "needs_review"
    assert r.reason == "attachment too large"
    assert called["pdf"] is False


def test_at_cap_attachment_still_parsed(monkeypatch):
    seen = {}

    def fake_pdf(data, expected_currency="ZAR"):
        seen["len"] = len(data)
        return extract.ExtractResult("needs_review", reason="empty pdf text")

    monkeypatch.setattr(extract, "extract_from_pdf", fake_pdf)
    payload = b"%PDF" + b"\x00" * (extract.MAX_ATTACHMENT_BYTES - 4)
    extract.extract_attachment("statement.pdf", payload)
    assert seen["len"] == extract.MAX_ATTACHMENT_BYTES
