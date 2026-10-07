"""S7: text_view (safe before auth) and gather_payloads (after auth + trigger + age)."""
from __future__ import annotations

import importlib
import io
from pathlib import Path

import pytest

from tests.mail_helpers import make_msg


IMAGES = Path(__file__).parent / "fixtures" / "payments_v2" / "images"


class _Lazy:
    def __init__(self, name):
        self._name = name

    def __getattr__(self, attr):
        return getattr(importlib.import_module(self._name), attr)


content = _Lazy("invespend.payments.content")
extract = _Lazy("invespend.payments.extract")
trigger = _Lazy("invespend.payments.trigger")
imgmod = _Lazy("invespend.payments.images")


def view(**kw):
    return content.text_view(make_msg(**kw))


def no_trigger(v):
    assert trigger.parse_trigger(v.typed_text).status == "none"


# ---------------------------------------------------------------- plain text
def test_plain_reply_with_quoted_history_typed_excludes_quote():
    v = view(plain="approve\n\nOn Tue, 7 Oct 2026 at 10:00, Bot <bot@example.com> wrote:\n> Payments awaiting approval\n> 1. Acme pay 123 Ltd\n",
             subject="Re: Payments awaiting approval [BATCH B-1007-ab12]")
    assert v.typed_text == "approve" and v.confident is True
    assert "Acme" in v.quoted_text and "approve" not in v.quoted_text.splitlines()[0]
    assert v.indicators


def test_plain_wrapped_on_wrote_line():
    v = view(plain="cancel 2\n\nOn Tue, 7 Oct 2026 at 10:00, Bot Notifications\n<bot@example.com> wrote:\n> x\n", subject="Re: x")
    assert v.typed_text == "cancel 2" and v.confident


def test_gmail_forward_block_plain():
    v = view(plain="FYI\n\n---------- Forwarded message ---------\nFrom: Bob <bob@example.com>\nSubject: inv\n\npay 123\nR100\n",
             subject="Fwd: inv")
    assert v.typed_text == "FYI"
    assert "pay 123" in v.forwarded_text and "pay 123" not in v.typed_text
    no_trigger(v)


def test_outlook_forward_block_plain():
    v = view(plain="Please see below\n\nFrom: Bob <bob@example.com>\nSent: Monday, 6 October 2026 09:00\nTo: Piet\nSubject: inv\n\npay 123\n",
             subject="FW: inv")
    assert v.typed_text == "Please see below" and v.confident
    no_trigger(v)


def test_original_message_and_localised_markers():
    for marker in ("-----Original Message-----", "-----Oorspronkelijk bericht-----", "-----Origineel bericht-----",
                   "-----Oorspronklike boodskap-----", "Begin forwarded message:"):
        v = view(plain=f"hello\n{marker}\nVan: x\nVerzonden: y\n\npay 123\n", subject="Hello")
        assert v.typed_text == "hello", marker
        assert v.confident is True and v.indicators, marker


def test_localised_header_block_van_verzonden():
    v = view(plain="Hallo\nVan: Bob <bob@example.com>\nVerzonden: maandag 6 oktober 2026\nAan: Piet\nOnderwerp: x\n\npay 123\n",
             subject="Hello")
    assert v.typed_text == "Hallo"
    no_trigger(v)


def test_quote_lines_start_boundary_and_bottom_posting_is_not_typed():
    v = view(plain="> pay 123 old text\n\npay 456", subject="Re: x")
    assert v.typed_text == ""
    v2 = view(plain="approve\n> older\n> older\nthanks later", subject="Re: x")
    assert v2.typed_text == "approve"


def test_html_only_body_without_indicators_is_wholly_typed_and_confident():
    v = view(html="<html><body><p>Please</p><p>pay 123</p><p>R100</p></body></html>")
    assert v.confident is True and v.indicators == ()
    assert v.typed_text.splitlines() == ["Please", "pay 123", "R100"]
    assert trigger.parse_trigger(v.typed_text).last3 == "123"


# ---------------------------------------------------------------------- html
GMAIL_REPLY = (
    '<div dir="ltr">approve</div><br><div class="gmail_quote"><div dir="ltr" class="gmail_attr">'
    'On Tue, 7 Oct 2026 at 10:00 Bot &lt;bot@example.com&gt; wrote:<br></div>'
    '<blockquote class="gmail_quote" style="margin:0px 0px 0px 0.8ex"><div>Payments awaiting approval</div>'
    '<div>1. Pay 123 Ltd | ZAR 100.00</div></blockquote></div>'
)


def test_reply_with_in_reply_to_and_gmail_quote_boundary_is_confident():
    v = view(html=GMAIL_REPLY, subject="Re: Payments awaiting approval [BATCH B-1007-ab12]",
             headers={"In-Reply-To": "<x.invespend-notification.B-1007-ab12@example.com>"})
    assert v.confident is True and v.typed_text == "approve"
    assert "Pay 123 Ltd" in v.quoted_text and "Pay 123 Ltd" not in v.typed_text
    assert "in_reply_to" in v.indicators and "subject_prefix" in v.indicators


def test_owner_typed_trigger_above_blockquote_is_typed():
    v = view(html='<div>pay 123</div><div>R500</div><blockquote>pay 999 older</blockquote>', subject="Re: x")
    assert v.confident and trigger.parse_trigger(v.typed_text).last3 == "123"
    assert "R500" in v.typed_text


def test_html_only_gmail_forward_gmail_quote_fails_closed():
    v = view(html='<div class="gmail_quote"><div class="gmail_attr">---------- Forwarded message ---------<br>From: Bob</div>'
                  '<div>pay 123</div></div>', subject="Fwd: inv")
    assert v.typed_text == ""
    no_trigger(v)


def test_outlook_divRplyFwdMsg_header_block_with_sibling_forwarded_body_pay_123_not_typed():
    v = view(html='<div>Thanks</div><div id="divRplyFwdMsg" dir="ltr"><font><b>From:</b> Bob<br><b>Sent:</b> Monday</font></div>'
                  '<div><div>pay 123</div><div>R100</div></div>', subject="FW: inv")
    assert v.typed_text == "Thanks" and v.confident
    assert "pay 123" in v.forwarded_text or "pay 123" in v.quoted_text
    no_trigger(v)
    v2 = view(html='<div id="divRplyFwdMsg"><b>From:</b> Bob</div><div>pay 123</div>', subject="FW: inv")
    assert v2.typed_text == ""
    no_trigger(v2)


def test_stray_close_blockquote_before_third_party_pay_123_ignored():
    v = view(html='<div>hello</div></blockquote><div>pay 123</div>', subject="Re: x")
    assert v.typed_text == "hello" and v.confident
    no_trigger(v)


def test_text_below_gmail_quote_not_typed():
    v = view(html='<div class="gmail_quote"><blockquote>old</blockquote></div><div>pay 123</div>', subject="Re: x")
    assert v.typed_text == ""
    no_trigger(v)


def test_text_after_first_boundary_never_typed_even_if_nested_container_closes():
    v = view(html='<div>ok</div><blockquote>a<blockquote>b</blockquote>c</blockquote><div>pay 123</div>', subject="Re: x")
    assert v.typed_text == "ok"
    no_trigger(v)


def test_moz_forward_container():
    v = view(html='<div>FYI</div><div class="moz-forward-container">-------- Forwarded Message --------<br>pay 123</div>',
             subject="Fwd: x")
    assert v.typed_text == "FYI"
    no_trigger(v)
    v2 = view(html='<div class="moz-forward-container">-------- Forwarded Message --------<br>pay 123</div>', subject="Fwd: x")
    assert v2.typed_text == "" and trigger.parse_trigger(v2.typed_text).status == "none"
    v3 = view(html='<div class="moz-cite-prefix">On x wrote:</div><blockquote type="cite">pay 123</blockquote>', subject="Re: x")
    assert v3.typed_text == ""


def test_hidden_script_style_and_comments_dropped():
    v = view(html='<style>.a{}</style><!-- pay 999 --><script>pay 888</script><div style="display:none">pay 777</div>'
                  '<div hidden>pay 666</div><div>visible</div>')
    assert v.typed_text == "visible"


def test_html_to_text_basics():
    assert content.html_to_text("<p>a</p><p>b<br>c</p>&amp;&lt;").splitlines() == ["a", "b", "c", "&<"]
    assert "x" not in content.html_to_text("<script>x</script>")


# ----------------------------------------------------------- fail-closed set
def test_unknown_locale_forward_subject_fails_closed():
    # a listed locale prefix with no recognised marker, and an unlisted one with a reply header
    v = view(plain="pay 123\nR100 to Acme\n", subject="ENC: factura")
    assert v.typed_text == "" and v.confident is False and "subject_prefix" in v.indicators
    no_trigger(v)
    v2 = view(plain="pay 123\nR100\n", subject="VL: Faktura", headers={"In-Reply-To": "<a@example.com>"})
    assert v2.typed_text == "" and v2.confident is False
    no_trigger(v2)


def test_no_marker_forward_with_in_reply_to_fails_closed():
    v = view(plain="third party says pay 123 and R50\n", subject="Hello", headers={"In-Reply-To": "<a@example.com>"})
    assert v.typed_text == "" and v.confident is False and "in_reply_to" in v.indicators
    assert "pay 123" in v.quoted_text
    no_trigger(v)


def test_mobile_inline_forward_without_marker_with_references_header():
    v = view(plain="Hi\npay 123\n", subject="Hello", headers={"References": "<a@example.com> <b@example.com>"})
    assert v.typed_text == "" and v.confident is False and "references" in v.indicators
    no_trigger(v)


def test_subject_trigger_ignored():
    v = view(plain="hello there", subject="pay 123")
    assert v.typed_text == "hello there" and v.confident
    no_trigger(v)


def test_no_indicator_whole_body_typed_is_confident():
    v = view(plain="pay 123\nR100\n", subject="Hello")
    assert v.confident is True and v.indicators == () and v.typed_text == "pay 123\nR100"


def test_indicator_without_boundary_is_not_confident_and_typed_empty():
    v = view(plain="no markers at all\n", subject="Fwd: x")
    assert v.confident is False and v.typed_text == "" and "no markers at all" in v.quoted_text
    assert v.indicators == ("subject_prefix",)


@pytest.mark.parametrize("prefix", ["Re:", "RE:", "Fwd:", "FW:", "WG:", "TR:", "RV:", "I:", "ENC:", "Doorst.:", "VS:", "AW:", "SV:", "Antw:", "Odp:"])
def test_every_subject_prefix_is_an_indicator(prefix):
    v = view(plain="hello", subject=f"{prefix} x")
    assert "subject_prefix" in v.indicators and v.typed_text == ""


# ------------------------------------------------------------ raw head lines
def test_raw_head_lines_skip_blank_lines_and_stop_at_three():
    v = view(plain="\n\n  first \n\n second\nthird\nfourth\n")
    assert v.raw_head_lines == ("first", "second", "third")


def test_raw_head_lines_taken_before_the_split_and_independent_of_confident():
    v = view(plain="> quoted\nlater\n", subject="Re: x")
    assert v.typed_text == "" and v.raw_head_lines[0] == "> quoted"
    v2 = view(html="<div>cancel 2</div>", subject="Fwd: x")
    assert v2.confident is False and v2.raw_head_lines == ("cancel 2",)


def test_raw_head_lines_prefer_plain_over_html():
    v = view(plain="plain first\n", html="<div>html first</div>")
    assert v.raw_head_lines == ("plain first",)


def test_text_view_has_no_raw_first_line_attribute():
    v = view(plain="x")
    assert not hasattr(v, "raw_first_line")


# ---------------------------------------------------- alternative / both parts
def test_alternative_parts_use_the_more_conservative_split():
    plain = "approve\n\nOn Tue, Bot wrote:\n> pay 123\n"
    v = view(plain=plain, html=GMAIL_REPLY, subject="Re: x")
    assert v.typed_text == "approve" and v.confident
    # html claims a longer typed region than plain: the shorter one wins
    v2 = view(plain="approve\n> old\n", html="<div>approve</div><div>pay 123 extra</div><blockquote>old</blockquote>", subject="Re: x")
    assert v2.typed_text == "approve"


def test_alternative_where_one_part_has_no_boundary_fails_closed():
    v = view(plain="approve\n", html="<div>approve</div>", subject="Re: x")
    assert v.typed_text == "" and v.confident is False


# --------------------------------------------------------- safety contracts
def test_text_view_never_calls_extract_attachment_gather_payloads_or_image_extractor(monkeypatch):
    from invespend.payments import content as cmod, extract as emod, images as imod
    called = []
    monkeypatch.setattr(emod, "extract_attachment", lambda *a, **k: called.append("extract_attachment"))
    monkeypatch.setattr(cmod, "gather_payloads", lambda *a, **k: called.append("gather_payloads"))
    fake = imod.FakeImageExtractor({})
    png = (IMAGES / "tiny.png").read_bytes()
    inner = make_msg(plain="pay 123 inner")
    msg = make_msg(
        plain="hello", subject="Hello",
        attachments=(("bad.pdf", "application", "pdf", b"%PDF-corrupt"),
                     ("book.xlsx", "application", "vnd.openxmlformats-officedocument.spreadsheetml.sheet", b"PK\x03\x04junk"),
                     ("pic.png", "image", "png", png)),
        rfc822=(inner,),
    )
    v = cmod.text_view(msg)
    assert called == [] and fake.calls == []
    assert v.typed_text == ""  # rfc822 indicator + no boundary: fail closed


def test_text_view_flags_rfc822_part_without_unwrapping_it():
    inner = make_msg(plain="inner third party says pay 123")
    v = content.text_view(make_msg(plain="see attached", rfc822=(inner,)))
    assert "message_rfc822" in v.indicators
    assert "pay 123" not in v.typed_text and "pay 123" not in v.quoted_text and "pay 123" not in v.forwarded_text


def test_text_view_oversize_part_truncated_never_raises():
    big = "x" * (content.MAX_TEXT_BYTES + 5000)
    v = view(plain=big)
    assert len(v.typed_text) <= content.MAX_TEXT_BYTES
    v2 = view(html="<p>" + big + "</p>")
    assert len(v2.typed_text) <= content.MAX_TEXT_BYTES


def test_text_view_is_pure_function_of_text_parts():
    png = (IMAGES / "tiny.png").read_bytes()
    base = dict(plain="pay 123\nR100", subject="Hello")
    a = view(**base)
    b = view(**base, attachments=(("x.csv", "text", "csv", b"a,b\n1,2\n"),), inline=(("p.png", "image", "png", png),))
    assert a == b


def test_text_attachments_are_not_body_text():
    v = view(plain="hello", attachments=(("notes.txt", "text", "plain", b"pay 123 secret"),))
    assert "pay 123" not in v.typed_text and v.typed_text == "hello"


def test_empty_and_odd_messages_never_raise():
    assert view(plain="").typed_text == ""
    m = content.text_view(make_msg(plain="x"))
    assert m.confident
    import email
    weird = email.message_from_bytes(b"Content-Type: text/plain; charset=bogus-charset\n\n\xff\xfepay 123\n")
    assert content.text_view(weird).typed_text.endswith("pay 123")
    nothing = email.message_from_bytes(b"Subject: x\n\n")
    assert content.text_view(nothing).typed_text == ""


def test_deeply_nested_multipart_is_bounded():
    from email.message import Message
    root = Message()
    root["Content-Type"] = "multipart/mixed"
    cur = root
    for _ in range(60):
        child = Message()
        child["Content-Type"] = "multipart/mixed"
        cur.set_payload([child])
        cur = child
    leaf = Message()
    leaf["Content-Type"] = "text/plain"
    leaf.set_payload("pay 123")
    cur.set_payload([leaf])
    v = content.text_view(root)
    assert v.typed_text == "" or "pay 123" in v.typed_text   # no exception, bounded work


# ------------------------------------------------------------ gather_payloads
CSV = b"Payee,Acme\nAmount,R100.00\n"


def gather(**kw):
    msg = make_msg(**kw)
    return content.gather_payloads(msg, content.text_view(msg))


def test_gather_regions_are_built_from_the_view_without_reparse():
    msg = make_msg(plain="pay 123\nR100", subject="Hello")
    fake_view = content.TextView(typed_text="TYPED", forwarded_text="FWD", quoted_text="QUOTE",
                                 raw_head_lines=(), confident=True, indicators=())
    p = content.gather_payloads(msg, fake_view)
    got = {(r.kind, r.text) for r in p.regions}
    assert ("typed", "TYPED") in got and ("forwarded", "FWD") in got and ("quoted", "QUOTE") in got
    assert any(r.kind == "subject" and r.text == "Hello" for r in p.regions)
    assert all(isinstance(r, content.Region) for r in p.regions)


def test_gather_csv_xlsx_pdf_attachments_each_extracted(monkeypatch):
    pd = pytest.importorskip("pandas")
    buf = io.BytesIO()
    pd.DataFrame([["Payee", "Acme"], ["Amount", "R200.00"]]).to_excel(buf, header=False, index=False)
    monkeypatch.setattr(extract_mod(), "extract_from_pdf",
                        lambda data, expected_currency="ZAR": extract_mod().ExtractResult("ok", "300.00", "ZAR", "Acme", ""))
    p = gather(plain="pay 123", attachments=(
        ("a.csv", "text", "csv", CSV),
        ("b.xlsx", "application", "vnd.openxmlformats-officedocument.spreadsheetml.sheet", buf.getvalue()),
        ("c.pdf", "application", "pdf", b"%PDF-1.4 fake"),
    ))
    by = {name: res for name, res in p.extractions}
    assert by["a.csv"].status == "ok" and by["a.csv"].amount == "100.00"
    assert by["b.xlsx"].status == "ok" and by["b.xlsx"].amount == "200.00"
    assert by["c.pdf"].amount == "300.00"
    assert [n for n, _ in p.attachments] == ["a.csv", "b.xlsx", "c.pdf"]
    assert {r.source for r in p.regions if r.kind == "attachment"} == {"a.csv", "b.xlsx", "c.pdf"}


def extract_mod():
    from invespend.payments import extract
    return extract


def test_gather_calls_the_existing_extract_attachment_helper(monkeypatch):
    seen = []

    def spy(filename, data, expected_currency="ZAR"):
        seen.append((filename, data))
        return extract_mod().ExtractResult("needs_review", reason="spy")

    monkeypatch.setattr(extract_mod(), "extract_attachment", spy)
    gather(plain="x", attachments=(("a.csv", "text", "csv", CSV),))
    assert seen == [("a.csv", CSV)]


def test_gather_corrupt_pdf_is_needs_review_not_a_crash():
    p = gather(plain="x", attachments=(("bad.pdf", "application", "pdf", b"%PDF-not-really"),))
    assert p.extractions[0][1].status == "needs_review"


def test_gather_extractor_exception_becomes_needs_review(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("parser exploded with secret-ish text")

    monkeypatch.setattr(extract_mod(), "extract_attachment", boom)
    p = gather(plain="x", attachments=(("a.csv", "text", "csv", CSV),))
    res = p.extractions[0][1]
    assert res.status == "needs_review" and "secret" not in res.reason


def test_gather_formula_prefixed_cell_is_data():
    csv = b"Payee,=HYPERLINK(\"http://example.invalid\")\nAmount,R100.00\n"
    p = gather(plain="x", attachments=(("f.csv", "text", "csv", csv),))
    res = p.extractions[0][1]
    assert res.payee is None or res.payee.startswith("=")   # literal text, never evaluated or stripped


def test_gather_oversize_attachment_skipped_before_parsing(monkeypatch):
    called = []
    monkeypatch.setattr(extract_mod(), "extract_from_pdf", lambda *a, **k: called.append(1))
    monkeypatch.setattr(extract_mod(), "MAX_ATTACHMENT_BYTES", 10)
    p = gather(plain="x", attachments=(("big.pdf", "application", "pdf", b"%PDF" + b"0" * 100),))
    assert p.extractions[0][1].status == "needs_review" and called == []


def test_gather_unsupported_attachment_types_noted_not_extracted(monkeypatch):
    called = []
    monkeypatch.setattr(extract_mod(), "extract_attachment", lambda *a, **k: called.append(1))
    p = gather(plain="x", attachments=(("a.docx", "application", "msword", b"doc"), ("n.txt", "text", "plain", b"pay 123")))
    assert called == [] and p.attachments == ()
    assert "attachment_skipped_unsupported" in p.notes


def test_gather_unwraps_rfc822_to_forwarded_and_never_uses_inner_from():
    inner = make_msg(plain="Invoice from Acme\nR100", subject="inv")
    del inner["From"]
    inner["From"] = "Mallory <mallory@example.invalid>"
    p = gather(plain="hello", rfc822=(inner,))
    fwd = [r for r in p.regions if r.kind == "forwarded"]
    assert fwd and "Invoice from Acme" in fwd[0].text
    assert all("mallory" not in r.text.lower() and "mallory" not in r.source.lower() for r in p.regions)


def test_gather_rfc822_inner_attachments_and_images_are_included():
    png = (IMAGES / "tiny.png").read_bytes()
    inner = make_msg(plain="inner", attachments=(("i.csv", "text", "csv", CSV),), inline=(("p.png", "image", "png", png),))
    p = gather(plain="hello", rfc822=(inner,))
    assert [n for n, _ in p.attachments] == ["i.csv"] and len(p.images) == 1


def test_gather_nested_depth_cap():
    deepest = make_msg(plain="DEEPEST text")
    mid = make_msg(plain="MIDDLE text", rfc822=(deepest,))
    top_inner = make_msg(plain="INNER text", rfc822=(mid,))
    p = gather(plain="hello", rfc822=(top_inner,))
    text = " ".join(r.text for r in p.regions)
    assert "INNER text" in text and "MIDDLE text" in text and "DEEPEST text" not in text
    assert "rfc822_depth_exceeded" in p.notes
    msg = make_msg(plain="hello", rfc822=(top_inner,))
    p1 = content.gather_payloads(msg, content.text_view(msg), max_depth=1)
    text1 = " ".join(r.text for r in p1.regions)
    assert "INNER text" in text1 and "MIDDLE text" not in text1


@pytest.mark.parametrize("name,mime", [("tiny.jpg", "image/jpeg"), ("tiny.png", "image/png"),
                                       ("tiny.gif", "image/gif"), ("tiny.webp", "image/webp")])
def test_gather_inline_and_attached_images_each_mime(name, mime):
    data = (IMAGES / name).read_bytes()
    maintype, subtype = mime.split("/")
    inline = gather(plain="x", inline=((name, maintype, subtype, data),))
    attached = gather(plain="x", attachments=((name, maintype, subtype, data),))
    for p in (inline, attached):
        assert len(p.images) == 1 and p.images[0].mime == mime and p.images[0].data == data
        assert p.extractions == () and p.attachments == ()


def test_gather_images_corrupt_oversize_unsupported_are_skipped_with_notes(monkeypatch):
    monkeypatch.setenv("PAYMENTS_MAX_IMAGE_BYTES", "50")
    png = (IMAGES / "tiny.png").read_bytes()
    big = png + b"0" * 100
    p = gather(plain="x", attachments=(("a.png", "image", "png", b"not an image"), ("b.png", "image", "png", big),
                                       ("c.bmp", "image", "bmp", b"BM0000")))
    assert p.images == ()
    assert set(p.notes) >= {"image_skipped_unsupported", "image_skipped_oversize"}


def test_gather_never_calls_an_image_extractor():
    from invespend.payments.images import FakeImageExtractor
    png = (IMAGES / "tiny.png").read_bytes()
    fake = FakeImageExtractor({})
    gather(plain="x", inline=(("p.png", "image", "png", png),))
    assert fake.calls == []


def test_gather_notes_carry_only_reason_codes():
    p = gather(plain="x", attachments=(("secret-name-123456789.docx", "application", "msword", b"d"),))
    assert all(n.replace("_", "").isalnum() for n in p.notes)
