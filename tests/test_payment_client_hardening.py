"""S3: hardened write mode of InvestecClient, reached ONLY via create_payment(fresh_token=True).

No network: both sessions are replaced by fakes. Real money never moves.
"""
from __future__ import annotations

import inspect
import json

import pytest
import requests

from invespend.investec_client import InvestecClient



def _o():
    import invespend.payments.outcome as outcome
    return outcome


def mk_resp(status, body=None, raw=None):
    r = requests.Response()
    r.status_code = status
    r._content = raw if raw is not None else json.dumps(body).encode()
    r.headers["Content-Type"] = "application/json"
    return r


OK_BODY = {"data": {"TransferResponses": [{"PaymentReferenceNumber": "REF1",
                                           "AuthorisationRequired": False}],
                    "ErrorMessage": None}}


class Rig:
    """A client whose token session and write session are fakes."""

    def __init__(self, monkeypatch, write=None):
        self.client = InvestecClient("id", "sec", "key", "https://example.invalid")
        self.token_posts = 0
        self.token_error: Exception | None = None
        self.write_calls: list[dict] = []
        self.write = write or (lambda: mk_resp(200, OK_BODY))
        self.session_posts: list[tuple] = []

        def token_post(url, **kw):
            self.session_posts.append((url, kw))
            if url.endswith("/oauth2/token"):
                self.token_posts += 1
                if self.token_error:
                    raise self.token_error
                return mk_resp(200, {"access_token": f"T{self.token_posts}", "expires_in": 1800})
            return mk_resp(200, {"ok": True})

        def write_post(url, **kw):
            self.write_calls.append({"url": url, **kw})
            out = self.write()
            if isinstance(out, Exception):
                raise out
            return out

        monkeypatch.setattr(self.client._session, "post", token_post)
        monkeypatch.setattr(self.client._write_session, "post", write_post)

    def pay(self, **kw):
        return self.client.create_payment("ACC1", "BEN1", "100.00", reference="rent",
                                          my_reference="me", fresh_token=True, **kw)


# ---- sessions ---------------------------------------------------------------
def test_write_session_has_no_retry_adapter():
    c = InvestecClient("id", "sec", "key", "https://example.invalid")
    assert c._write_session.get_adapter("https://x").max_retries.total == 0
    # the GET/token session still retries
    assert c._session.get_adapter("https://x").max_retries.total == 4
    assert c._write_session is not c._session


# ---- unknown outcome mapping --------------------------------------------------
def test_v2_post_timeout_called_once_raises_unknown(monkeypatch):
    rig = Rig(monkeypatch, write=lambda: requests.exceptions.Timeout("slow"))
    with pytest.raises(_o().PaymentUnknownOutcome):
        rig.pay()
    assert len(rig.write_calls) == 1


def test_chunked_encoding_error_after_send_is_unknown_outcome(monkeypatch):
    rig = Rig(monkeypatch, write=lambda: requests.exceptions.ChunkedEncodingError("x"))
    with pytest.raises(_o().PaymentUnknownOutcome):
        rig.pay()
    assert len(rig.write_calls) == 1


def test_content_decoding_error_is_unknown_outcome(monkeypatch):
    rig = Rig(monkeypatch, write=lambda: requests.exceptions.ContentDecodingError("x"))
    with pytest.raises(_o().PaymentUnknownOutcome):
        rig.pay()


@pytest.mark.parametrize("exc", [
    requests.exceptions.ConnectionError, requests.exceptions.Timeout,
    requests.exceptions.ChunkedEncodingError, requests.exceptions.ContentDecodingError,
    requests.exceptions.TooManyRedirects, requests.exceptions.SSLError,
    requests.exceptions.RequestException,
])
def test_any_requestexception_except_4xx_httperror_is_unknown_outcome(monkeypatch, exc):
    rig = Rig(monkeypatch, write=lambda: exc("boom"))
    with pytest.raises(_o().PaymentUnknownOutcome):
        rig.pay()
    assert len(rig.write_calls) == 1


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_http_429_and_5xx_are_unknown_outcome(monkeypatch, status):
    rig = Rig(monkeypatch, write=lambda: mk_resp(status, {"message": "x"}))
    with pytest.raises(_o().PaymentUnknownOutcome):
        rig.pay()
    assert len(rig.write_calls) == 1


def test_http_409_on_the_write_path_is_unknown_outcome(monkeypatch):
    rig = Rig(monkeypatch, write=lambda: mk_resp(409, {"message": "Duplicate payment"}))
    with pytest.raises(_o().PaymentUnknownOutcome):
        rig.pay()
    assert len(rig.write_calls) == 1


def test_httperror_409_is_unknown_outcome(monkeypatch):
    conflict = requests.exceptions.HTTPError("x", response=mk_resp(409, {"message": "Duplicate payment"}))
    rig = Rig(monkeypatch, write=lambda: conflict)
    with pytest.raises(_o().PaymentUnknownOutcome):
        rig.pay()
    assert len(rig.write_calls) == 1


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_other_4xx_is_payment_rejected_with_status(monkeypatch, status):
    rig = Rig(monkeypatch, write=lambda: mk_resp(status, {"message": "nope"}))
    with pytest.raises(_o().PaymentRejected) as ei:
        rig.pay()
    assert ei.value.status_code == status
    assert len(rig.write_calls) == 1


def test_httperror_4xx_is_rejected_other_httperror_is_unknown(monkeypatch):
    rejected = requests.exceptions.HTTPError("x", response=mk_resp(403, {"message": "stop"}))
    rig = Rig(monkeypatch, write=lambda: rejected)
    with pytest.raises(_o().PaymentRejected) as ei:
        rig.pay()
    assert ei.value.status_code == 403 and ei.value.message == "stop"
    for err in (requests.exceptions.HTTPError("x", response=mk_resp(429, {})),
                requests.exceptions.HTTPError("x", response=mk_resp(502, {})),
                requests.exceptions.HTTPError("x")):
        rig = Rig(monkeypatch, write=lambda err=err: err)
        with pytest.raises(_o().PaymentUnknownOutcome):
            rig.pay()


# ---- PaymentNotSent -----------------------------------------------------------
def test_token_fetch_failure_raises_PaymentNotSent_and_write_not_called(monkeypatch):
    rig = Rig(monkeypatch)
    rig.token_error = requests.exceptions.ConnectionError("secret-host-detail")
    with pytest.raises(_o().PaymentNotSent) as ei:
        rig.pay()
    assert rig.write_calls == []
    assert "secret-host-detail" not in str(ei.value)  # underlying text not echoed
    assert not isinstance(ei.value, _o().PaymentUnknownOutcome)


def test_token_fetch_failure_of_any_kind_is_not_sent(monkeypatch):
    rig = Rig(monkeypatch)
    rig.token_error = RuntimeError("weird")
    with pytest.raises(_o().PaymentNotSent):
        rig.pay()
    assert rig.write_calls == []


# ---- token handling -------------------------------------------------------------
def test_force_fresh_token_fetches_new_token(monkeypatch):
    rig = Rig(monkeypatch)
    assert rig.client._get_token() == "T1"
    assert rig.client._get_token() == "T1"           # default: cached
    assert rig.token_posts == 1
    rig.pay()
    assert rig.token_posts == 2                       # forced, although the cache was valid
    assert rig.write_calls[0]["headers"]["Authorization"] == "Bearer T2"
    assert rig.client._get_token() == "T2"


def test_post_once_never_fetches_a_token(monkeypatch):
    rig = Rig(monkeypatch)
    monkeypatch.setattr(rig.client, "_get_token",
                        lambda *a, **k: pytest.fail("_post_once fetched a token"))
    out = rig.client._post_once("/p", {"a": 1}, "TOK")
    assert out == OK_BODY
    assert rig.write_calls[0]["headers"]["Authorization"] == "Bearer TOK"
    assert list(inspect.signature(rig.client._post_once).parameters) == ["path", "payload", "token"]


def test_create_payment_passes_the_force_fetched_token_to_post_once(monkeypatch):
    c = InvestecClient("id", "sec", "key", "https://example.invalid")
    seen = {}
    monkeypatch.setattr(c, "_get_token", lambda force=False: seen.setdefault("force", force) and "FRESH")
    monkeypatch.setattr(c, "_post_once", lambda path, payload, token: seen.update(token=token) or {"ok": 1})
    assert c.create_payment("A", "B", "1.00", fresh_token=True) == {"ok": 1}
    assert seen == {"force": True, "token": "FRESH"}


def test_v2_request_shape(monkeypatch):
    rig = Rig(monkeypatch)
    rig.pay()
    call = rig.write_calls[0]
    assert call["url"] == "https://example.invalid/za/pb/v1/accounts/ACC1/paymultiple"
    assert call["headers"]["x-api-key"] == "key"
    assert call["timeout"] == 30
    item = call["json"]["paymentList"][0]
    assert item == {"beneficiaryId": "BEN1", "amount": "100.00",
                    "myReference": "me", "theirReference": "rent"}


# ---- redirects / bodies ---------------------------------------------------------
def test_v2_post_disables_redirects(monkeypatch):
    rig = Rig(monkeypatch)
    rig.pay()
    assert rig.write_calls[0]["allow_redirects"] is False


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_302_is_unknown_outcome_not_followed(monkeypatch, status):
    rig = Rig(monkeypatch, write=lambda: mk_resp(status, {}))
    with pytest.raises(_o().PaymentUnknownOutcome):
        rig.pay()
    assert len(rig.write_calls) == 1


@pytest.mark.parametrize("raw", [b"<html>ok</html>", b"", b"not json"])
def test_200_non_json_body_is_unknown_outcome(monkeypatch, raw):
    rig = Rig(monkeypatch, write=lambda: mk_resp(200, raw=raw))
    with pytest.raises(_o().PaymentUnknownOutcome):
        rig.pay()
    assert len(rig.write_calls) == 1


def test_200_json_that_is_not_an_object_is_unknown_outcome(monkeypatch):
    rig = Rig(monkeypatch, write=lambda: mk_resp(200, ["x"]))
    with pytest.raises(_o().PaymentUnknownOutcome):
        rig.pay()


# ---- outcome parser -------------------------------------------------------------
def test_parse_success():
    out = _o().parse_payment_response(OK_BODY)
    assert (out.status, out.reference, out.reason, out.message) == ("success", "REF1", "ok", "")


def test_parse_200_error_message_is_unknown_never_failed():
    out = _o().parse_payment_response({"data": {"TransferResponses": [], "ErrorMessage": "Insufficient funds"}})
    assert out.status == "unknown" and out.reason == "error_message"
    assert out.message == "Insufficient funds"


def test_parse_authorisation_required_is_needs_authorisation():
    body = {"data": {"TransferResponses": [{"PaymentReferenceNumber": "R", "AuthorisationRequired": True,
                                            "Status": "Awaiting authorisation"}], "ErrorMessage": None}}
    out = _o().parse_payment_response(body)
    assert out.status == "needs_authorisation" and out.reason == "authorisation_required"
    assert out.message == "Awaiting authorisation"


def test_parse_missing_transferresponses_is_unknown_by_default():
    for body in ({"data": {"ErrorMessage": None}}, {"data": {"TransferResponses": []}},
                 {"data": None}, {}, None, {"data": "x"}):
        out = _o().parse_payment_response(body)
        assert (out.status, out.reason, out.message) == ("unknown", "unrecognised_shape", ""), body


def test_parse_authorisation_required_beats_error_message():
    # fix round 3: authorisation may still be granted online and then moves money -> keep the reservation, never fail
    body = {"data": {"TransferResponses": [{"AuthorisationRequired": True}], "ErrorMessage": "bad"}}
    assert _o().parse_payment_response(body).status == "needs_authorisation"


# ---- F40 provider message ----------------------------------------------------------
def test_rejected_4xx_carries_sanitised_provider_message(monkeypatch):
    rig = Rig(monkeypatch, write=lambda: mk_resp(400, {"ErrorMessage": "Beneficiary must be paid once online first"}))
    with pytest.raises(_o().PaymentRejected) as ei:
        rig.pay()
    assert ei.value.message == "Beneficiary must be paid once online first"


def test_200_error_message_outcome_message_is_sanitised():
    out = _o().parse_payment_response(
        {"data": {"TransferResponses": [], "ErrorMessage": "Acct 123456789012 for a@b.com failed\n\tbadly"}})
    assert out.message == "Acct [redacted] for [redacted] failed badly"


def test_message_redacts_6plus_digit_runs_token_like_strings_and_addresses_and_truncates_to_200():
    s = _o().sanitize_provider_message
    assert s("acct 123456789012 x") == "acct [redacted] x"
    assert s("five 12345 ok") == "five 12345 ok"
    assert s("key abcdefghijklmnopqrstuvwxyz012345 end") == "key [redacted] end"
    assert s("short abcdefghij-klmnopqrstuv ok").count("[redacted]") == 0   # 23 chars
    assert s("mail me at someone@example.com now") == "mail me at [redacted] now"
    long = s("word " * 100)
    assert len(long) <= 200
    assert s("a\x00b\x07c") == "abc"
    assert s(None) == ""
    assert s(12345) == "12345"
    assert s(object()) != ""  # str-coerced, never raises

    class Boom:
        def __str__(self):
            raise RuntimeError("no")
    assert s(Boom()) == ""


def test_non_json_4xx_body_gives_empty_message_and_status_code_only(monkeypatch):
    rig = Rig(monkeypatch, write=lambda: mk_resp(400, raw=b"<html>Bad request 1234567</html>"))
    with pytest.raises(_o().PaymentRejected) as ei:
        rig.pay()
    assert ei.value.status_code == 400 and ei.value.message == ""
    assert "1234567" not in str(ei.value) and "1234567" not in repr(ei.value)


def test_provider_message_field_order_is_documented_and_first_present_wins(monkeypatch):
    cases = [
        ({"ErrorMessage": "E", "message": "m", "error_description": "d", "error": "e"}, "E"),
        ({"message": "m", "error_description": "d", "error": "e"}, "m"),
        ({"error_description": "d", "error": "e"}, "d"),
        ({"error": "e"}, "e"),
        ({"ErrorMessage": None, "message": "m"}, "m"),
        ({"ErrorMessage": "", "message": "m"}, "m"),
        ({"other": "x"}, ""),
    ]
    for body, expected in cases:
        rig = Rig(monkeypatch, write=lambda body=body: mk_resp(400, body))
        with pytest.raises(_o().PaymentRejected) as ei:
            rig.pay()
        assert ei.value.message == expected, body


def test_message_never_appears_in_exception_str_or_repr_beyond_the_sanitised_text(monkeypatch):
    rig = Rig(monkeypatch, write=lambda: mk_resp(400, {"message": "acct 123456789012 token abcdefghijklmnopqrstuvwxyz012345"}))
    with pytest.raises(_o().PaymentRejected) as ei:
        rig.pay()
    for text in (str(ei.value), repr(ei.value), repr(ei.value.args)):
        assert "123456789012" not in text
        assert "abcdefghijklmnopqrstuvwxyz012345" not in text
    assert ei.value.message == "acct [redacted] token [redacted]"


# ---- legacy compatibility -----------------------------------------------------------
def test_default_create_payment_path_unchanged(monkeypatch):
    c = InvestecClient("id", "sec", "key", "https://example.invalid")
    seen = {}

    def fake_post(path, payload):
        seen["args"] = (path, payload)
        return {"ok": True}

    monkeypatch.setattr(c, "_post", fake_post)
    monkeypatch.setattr(c, "_post_once", lambda *a, **k: pytest.fail("hardened path used"))
    monkeypatch.setattr(c, "_get_token", lambda *a, **k: pytest.fail("token fetched by client"))
    assert c.create_payment("ACC1", "BEN1", "100.00", reference="rent") == {"ok": True}
    assert seen["args"][0].endswith("/ACC1/paymultiple")
    assert list(inspect.signature(InvestecClient._post).parameters) == ["self", "path", "payload"]
    assert inspect.signature(InvestecClient.create_payment).parameters["fresh_token"].default is False
    assert inspect.signature(InvestecClient.create_payment).parameters["fresh_token"].kind is inspect.Parameter.KEYWORD_ONLY


def test_default_path_uses_the_retrying_session_not_the_write_session(monkeypatch):
    rig = Rig(monkeypatch)
    out = rig.client.create_payment("ACC1", "BEN1", "100.00", "rent", "me")
    assert out == {"ok": True}
    assert rig.write_calls == []
    urls = [u for u, _ in rig.session_posts]
    assert any(u.endswith("/paymultiple") for u in urls)


def test_existing_signature_accepts_old_positional_calls(monkeypatch):
    c = InvestecClient("id", "sec", "key", "https://example.invalid")
    got = {}
    monkeypatch.setattr(c, "_post", lambda path, payload: got.update(p=payload) or {})
    c.create_payment("A", "B", "1.00", "their", "mine")
    item = got["p"]["paymentList"][0]
    assert item["theirReference"] == "their" and item["myReference"] == "mine"
    c.create_payment("A", "B", "1.00")
    assert got["p"]["paymentList"][0]["myReference"] == ""
