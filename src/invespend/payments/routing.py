"""Reconciliation of figure candidates and routing (S11). Pure, stdlib only.

``reconcile`` takes every candidate the message produced (typed / forwarded / quoted text, attachments, images)
and either reconciles them to ONE amount, ONE currency (which must be ZAR) and ONE payee, or refuses. An image
is untrusted: it never supplies a trigger, a source account or a command, any disagreement involving an image
parks, and when a figure has no text/attachment support the item is flagged ``image_derived`` so the batch email
says so. ``route`` decides where a reconciled instruction goes; no route can execute and none can skip approval.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from . import beneficiaries as bene
from .caps import check_per_payment

TEXT_ORIGINS = ("typed", "forwarded", "quoted")
NON_IMAGE_ORIGINS = TEXT_ORIGINS + ("attachment",)


@dataclass(frozen=True)
class Candidate:
    amount: str | None
    currency: str | None
    payee: str | None
    origin: str          # "typed" | "forwarded" | "quoted" | "attachment" | "image"


@dataclass(frozen=True)
class Reconciled:
    status: str          # "ok" | "conflict" | "none"
    amount: str | None
    currency: str | None
    payee: str | None
    image_derived: bool
    figures_source: str  # "typed" | "attachment" | "image"
    reason: str


@dataclass(frozen=True)
class Route:
    kind: str            # "ready" | "held" | "new_payee" | "park"
    reason: str
    path: str            # "registered" | "new_payee" (classifies the beneficiary resolution, not the route)
    beneficiary: bene.Beneficiary | None = None
    eligible_at: datetime | None = None
    recent: bool = False
    first_seen_at: datetime | None = None


def normalise_payee(name: object) -> str:
    return " ".join(str(name or "").split()).casefold()


def _amount_key(value: str) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number.quantize(Decimal("0.01")) if number.is_finite() else None


def _stop(status: str, reason: str) -> Reconciled:
    return Reconciled(status, None, None, None, False, "typed", reason)


def reconcile(cands: Sequence[Candidate]) -> Reconciled:
    with_amount = [c for c in cands if c.amount]
    if not with_amount:
        return _stop("none", "no_amount")
    amounts = {_amount_key(c.amount) for c in with_amount}
    if None in amounts or len(amounts) != 1:
        return _stop("conflict", "amount_conflict")
    currencies = {str(c.currency).strip().upper() for c in cands if c.currency}
    if len(currencies) > 1:
        return _stop("conflict", "currency_conflict")
    if currencies != {"ZAR"}:
        return _stop("conflict", "currency_not_zar")      # a missing currency is never assumed to be ZAR
    payees: dict[str, str] = {}
    for c in cands:
        if c.payee and normalise_payee(c.payee):
            payees.setdefault(normalise_payee(c.payee), c.payee.strip())
    if not payees:
        return _stop("none", "no_payee")
    if len(payees) > 1:
        return _stop("conflict", "payee_conflict")
    payee_key, payee = next(iter(payees.items()))
    amount = format(next(iter(amounts)), "f")
    text_amount = any(c.amount and c.origin in TEXT_ORIGINS for c in with_amount)
    non_image_amount = any(c.amount and c.origin in NON_IMAGE_ORIGINS for c in with_amount)
    non_image_payee = any(c.payee and c.origin in NON_IMAGE_ORIGINS and normalise_payee(c.payee) == payee_key for c in cands)
    image_derived = (not non_image_amount) or (not non_image_payee)
    if image_derived:
        source = "image"
    elif not text_amount:
        source = "attachment"
    else:
        source = "typed"
    return Reconciled("ok", amount, "ZAR", payee, image_derived, source, "")


def beneficiary_path(payee: str, beneficiaries: list[bene.Beneficiary] | None) -> tuple[str, bene.Beneficiary | None, str]:
    """``('registered', b, 'ok')`` iff the strict resolution is exact; every other case (none, ambiguous, list
    unavailable) classifies as ``new_payee`` (R6-T1). The third item is the resolution status."""
    if beneficiaries is None:
        return "new_payee", None, "unavailable"
    found, status = bene.resolve_beneficiary_strict(payee, beneficiaries)
    return ("registered" if status == "ok" else "new_payee"), found, status


def route(
    reconciled: Reconciled,
    beneficiaries: list[bene.Beneficiary] | None,
    observations: Mapping[str, object],
    *,
    hold_recent: bool,
    hold: timedelta,
    per_payment_cap: object,
    now: datetime,
) -> Route:
    path, found, status = beneficiary_path(reconciled.payee or "", beneficiaries)
    if status == "unavailable":
        return Route("park", "beneficiary_list_unavailable", path)
    if status == "ambiguous":
        return Route("park", "beneficiary_ambiguous", path)
    if status == "none":
        return Route("new_payee", "", path)
    assert found is not None
    if not check_per_payment(reconciled.amount, per_payment_cap).ok:
        return Route("park", "over_per_payment_cap", path, found)
    obs = observations.get(found.beneficiary_id)
    if hold > timedelta(0) and hold_recent and obs is not None:
        anchors = []
        if not obs.established and obs.first_seen_at > now - hold:
            anchors.append(obs.first_seen_at)
        if obs.fingerprint_changed_at is not None and obs.fingerprint_changed_at > now - hold:
            anchors.append(obs.fingerprint_changed_at)
        if anchors:
            return Route("held", "", path, found, eligible_at=max(anchors) + hold, recent=True, first_seen_at=obs.first_seen_at)
    return Route("ready", "", path, found, first_seen_at=getattr(obs, "first_seen_at", None))
