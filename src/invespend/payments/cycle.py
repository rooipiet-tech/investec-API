"""The v2 instruction cycle (S11, plan sections 2a/S11): preflight, snapshot, message handling, sweeps, execution, offer.

Binding order inside one cycle: (1) recover stale ``submitting``; (1b) snapshot of the ``accepted`` rows (this is what
makes "approved items execute in the NEXT cycle" structural); fetch beneficiaries once; fetch messages; identity
pre-pass for ALL messages; per-message handling (each in its own try/except) which comes BEFORE (2) expiry-first,
(3) beneficiary -> held -> awaiting_approval, (4) execution of the snapshot, (5) batch offer, (6) notice and
stuck-message sweeps; then cleanup. Per message: loop guard first, then text_view-only steps (no attachment or image
access), sender authentication, commands, trigger, age gate, ``gather_payloads`` only after auth + trigger + age.
"""
from __future__ import annotations

import email
import functools
import hashlib
import secrets
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal

from . import accounts as accounts_mod
from . import (
    approval, bankdetails, batch, beneficiaries as bene, commands, content, execute, fingerprints, images,
    instructions, loopguard, notify_v2, refs, routing, sender_auth, trigger,
)
from .amounts import has_foreign_currency_token, labelled_payee_candidates, marked_amount_candidates
from .mode import v2_settings
from .notices import safe_send, sender_of

_RECONCILE_IGNORED_ATTACHMENT_REASONS = ("no amount found", "empty body")


class PreflightError(RuntimeError):
    """A preflight check failed BEFORE any mail was fetched; the message is the reason code."""


class _Ctx:
    def __init__(self, settings, cfg, store, audit, client, smtp_send, extractor, when, beneficiaries, raw, observations):
        self.settings, self.cfg, self.store, self.audit, self.client = settings, cfg, store, audit, client
        self.smtp_send, self.extractor, self.when = smtp_send, extractor, when
        self.beneficiaries, self.raw, self.observations = beneficiaries, raw, observations
        self.raw_index = fingerprints.raw_by_id(raw)
        self.notice_counter: dict = {}
        self.counters: dict = {}
        self._accounts: list | None = None
        self._accounts_failed = False
        self.image_extras: tuple[str | None, str | None] = (None, None)

    def accounts(self) -> list | None:
        if self._accounts is None and not self._accounts_failed:
            try:
                data = self.client.get_accounts()
                self._accounts = list(data) if isinstance(data, list) else None
            except Exception:  # noqa: BLE001
                self._accounts = None
            self._accounts_failed = self._accounts is None
        return self._accounts

    def send(self, msg) -> bool:
        return safe_send(self.smtp_send, msg, self.audit)


def _utc(moment: datetime | None) -> datetime | None:
    if moment is not None and moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


def received_at_of(v2) -> datetime | None:
    """Trusted receipt time: the OLDER of the IMAP INTERNALDATE and the topmost Received timestamp; the Date header
    is never used; None when neither exists (the caller fails closed)."""
    times = [t for t in (_utc(v2.internaldate), _utc(v2.received_header_time)) if t is not None]
    return min(times) if times else None


def _fresh(received: datetime | None, when: datetime, cfg) -> bool:
    return received is not None and cfg.max_age_hours > 0 and when - received <= cfg.max_age


def _normalised_mailbox(name: object) -> str:
    """RS3A-5: lower-cased mailbox name without surrounding whitespace/quotes and trailing ``.`` / ``/`` delimiters,
    so ``INBOX.``, ``INBOX/`` and ``"INBOX"`` equal ``inbox``. A dedicated sub-label (``INBOX.Payments``) is NOT
    stripped to ``inbox`` and stays allowed (the SAFER reading: only exact inbox spellings are the shared inbox)."""
    text = str(name or "")
    while True:
        stripped = text.strip().strip("\"'").rstrip("./").strip()
        if stripped == text:
            return text.lower()
        text = stripped


def _preflight(settings, cfg, store, audit, allow_non_durable: bool) -> None:
    def fail(code: str):
        audit.append("preflight_failed", {"reason": code})
        raise PreflightError(code)

    if not getattr(store, "durable", False) and not allow_non_durable:
        fail("v2_requires_durable_store")
    if _normalised_mailbox(cfg.mailbox) in ("", "inbox"):
        fail("mailbox_not_dedicated")
    if not sender_auth.strict_address_parser_available():
        fail("strict_parser_unavailable")
    store.ping()


# ------------------------------------------------------------- new instruction
def _problem(ctx: _Ctx, to: str, iid: str, code: str) -> None:
    ctx.send(notify_v2.build_problem_email(sender_of(ctx.settings), to, ref=refs.request_ref(iid), kind="parked", detail_code=code))


def _stop(ctx: _Ctx, to: str, iid: str, code: str) -> str:
    """A stop BEFORE a complete record exists: no instruction row, one problem email, message outcome parked_<code>."""
    ctx.audit.append("parked", {"ref": refs.request_ref(iid), "reason": code})
    _problem(ctx, to, iid, code)
    return f"parked_{code}"


def _candidates(ctx: _Ctx, payloads) -> tuple[list[routing.Candidate], bool, str | None, str | None]:
    """(candidates, attachment_blocked, image_account_number, bank_details_text)"""
    cands: list[routing.Candidate] = []
    text_for_details: list[str] = []
    for region in payloads.regions:
        if region.kind not in routing.TEXT_ORIGINS:
            continue                                       # attachments are read from their results, the subject never
        stripped = trigger.strip_trigger(region.text)
        if region.kind in ("typed", "forwarded"):
            text_for_details.append(region.text)
        for amount in marked_amount_candidates(stripped):
            cands.append(routing.Candidate(amount, "ZAR", None, region.kind))
        if has_foreign_currency_token(stripped):                  # RS3A-2: reconcile() then parks currency_conflict
            cands.append(routing.Candidate(None, "FOREIGN", None, region.kind))
        for payee in labelled_payee_candidates(stripped):
            cands.append(routing.Candidate(None, None, payee, region.kind))
    blocked = False
    for _name, result in payloads.extractions:
        if result.status == "ok" and result.amount:
            cands.append(routing.Candidate(result.amount, result.currency or "ZAR", None, "attachment"))
            if result.payee:
                cands.append(routing.Candidate(None, None, result.payee, "attachment"))
        elif result.status == "needs_review" and result.reason not in _RECONCILE_IGNORED_ATTACHMENT_REASONS:
            blocked = True
    image_account = None
    image_bank = None
    image_reference = None
    for ref in payloads.images:
        outcome = images.run_extractor(ctx.extractor, ref)
        if outcome.dropped_count:
            ctx.audit.append("image_extra_fields_dropped", {"count": outcome.dropped_count, "reason": ",".join(outcome.dropped_names)[:200]})
        if outcome.fields is None:
            ctx.audit.append("image_skipped", {"reason": outcome.note or "image_no_fields"})
            continue
        f = outcome.fields
        if f.amount:
            cands.append(routing.Candidate(f.amount, f.currency, None, "image"))
        if f.payee_name:
            cands.append(routing.Candidate(None, None, f.payee_name, "image"))
        image_account = image_account or f.account_number
        image_bank = image_bank or f.bank
        image_reference = image_reference or f.reference
    ctx.image_extras = (image_bank, image_reference)
    return cands, blocked, image_account, "\n".join(text_for_details)


def _new_instruction(ctx: _Ctx, v2, msg, view, iid: str, auth_from: str, received: datetime | None) -> str:
    cfg, store, audit, when = ctx.cfg, ctx.store, ctx.audit, ctx.when
    ref = refs.request_ref(iid)
    trig = trigger.parse_trigger(view.typed_text)
    if trig.status == "none":
        if view.indicators and not view.confident:
            audit.append("typed_text_unsplittable", {"count": len(view.indicators)})
        audit.append("ignored", {"reason": "no_trigger"})
        return "ignored_no_trigger"
    if trig.status != "ok":
        audit.append("trigger_ignored", {"reason": trig.status})
        return f"ignored_trigger_{trig.status}"
    if not _fresh(received, when, cfg):                                   # age gate: AFTER auth AND trigger
        audit.append("expired_age", {"ref": ref})
        _problem(ctx, auth_from, iid, "expired_age")
        return "expired_age"
    accounts = ctx.accounts()
    if accounts is None:
        return _stop(ctx, auth_from, iid, "accounts_unavailable")
    source, status = accounts_mod.resolve_source_unique(accounts, trig.last3)
    if source is None:
        return _stop(ctx, auth_from, iid, f"source_{status}")

    payloads = content.gather_payloads(msg, view)                         # only now: auth + trigger + age all passed
    cands, blocked, image_account, details_text = _candidates(ctx, payloads)
    if blocked:
        return _stop(ctx, auth_from, iid, "attachment_ambiguous")
    rec = routing.reconcile(cands)
    if rec.status != "ok":
        if rec.reason == "no_amount" and any(c.currency == "FOREIGN" for c in cands):
            return _stop(ctx, auth_from, iid, "currency_conflict")        # "$100": never read as rand, say why
        return _stop(ctx, auth_from, iid, rec.reason)

    details = bankdetails.extract_bank_details(details_text)
    number = details.account_number
    if number and image_account and "".join(ch for ch in number if ch.isdigit()) != "".join(ch for ch in image_account if ch.isdigit()):
        return _stop(ctx, auth_from, iid, "account_conflict")
    number = number or image_account
    image_bank, image_reference = ctx.image_extras
    their_reference = (details.reference or image_reference or "")[:100]
    amount = Decimal(rec.amount)
    payee_norm = routing.normalise_payee(rec.payee)
    mac = fingerprints.account_hmac(number, cfg.fingerprint_key) if number and cfg.fingerprint_key else None
    path, found, _resolution = routing.beneficiary_path(rec.payee, ctx.beneficiaries)      # BEFORE the duplicate guard (R6-T1)
    if auth_from not in cfg.allowed_senders:                              # defence in depth (not an assert: -O safe)
        raise RuntimeError("notify_to must be an allowlisted authenticated address")

    base = {
        "instruction_id": iid, "path": path, "amount": amount, "currency": "ZAR",
        "source_account_id": accounts_mod.source_account_id(source),
        "source_profile_id": accounts_mod.source_profile_id(source) or None,
        "source_account_last3": trig.last3, "payee_name_norm": payee_norm, "account_hmac": mac,
        "my_reference": their_reference or None, "their_reference": their_reference or None,
        "figures_source": rec.figures_source,
        "message_id_hash": hashlib.sha256(v2.message_id.strip().lower().encode("utf-8")).hexdigest(),
        "notify_to": auth_from, "received_at": received, "updated_at": when,
    }

    def create(status: str, **extra):
        return store.create({**base, "status": status, **extra})

    def park(reason: str) -> str:
        row, created = create("parked", outcome_code=reason, expires_at=when)
        if created:
            audit.append("parked", {"ref": ref, "reason": reason, "status": "parked"})
            _problem(ctx, auth_from, iid, reason)
        return f"parked_{reason}"

    since = when - cfg.duplicate_window
    if store.find_recent_similar(base["source_account_id"], payee_norm, amount, since) is not None:
        return park("possible_duplicate")
    if path == "registered" and mac is not None:
        raw = ctx.raw_index.get(found.beneficiary_id)
        registered = raw.get("accountNumber") if raw else None
        if not registered or fingerprints.account_hmac(registered, cfg.fingerprint_key) != mac:
            return park("account_number_mismatch")

    route = routing.route(rec, ctx.beneficiaries, ctx.observations, hold_recent=cfg.hold_registered_recent,
                          hold=cfg.hold, per_payment_cap=cfg.per_payment_cap, now=when)
    if route.kind == "park":
        return park(route.reason)
    if route.kind == "new_payee":
        row, created = create("awaiting_beneficiary", expires_at=when + cfg.awaiting_expiry)
        if not created:
            audit.append("instruction_exists", {"ref": ref})
            return "duplicate_existing"
        audit.append("instruction_created", {"ref": ref, "status": "awaiting_beneficiary", "amount": str(amount), "currency": "ZAR",
                                             "source_account_last3": trig.last3})
        shown = bankdetails.BankDetails(
            payee_name=details.payee_name or rec.payee, bank=details.bank or image_bank, account_number=number,
            branch_code=details.branch_code, reference=their_reference or None)
        paste = notify_v2.build_paste_details_email(
            sender_of(ctx.settings), auth_from, ref=ref, details=shown, amount=amount,
            their_reference=their_reference, my_reference=their_reference)
        if ctx.send(paste):
            store.mark_notified(iid, "paste", when)
        return "instruction_awaiting_beneficiary"

    if not cfg.fingerprint_key:
        return park("fingerprint_key_missing")
    raw = ctx.raw_index.get(route.beneficiary.beneficiary_id)
    if raw is None:
        return park("beneficiary_removed")
    fp = fingerprints.beneficiary_fingerprint(raw, cfg.fingerprint_key)
    common = {"beneficiary_id": route.beneficiary.beneficiary_id, "beneficiary_fingerprint": fp}
    if route.kind == "held":
        row, created = create("held", first_seen_at=route.first_seen_at, eligible_at=route.eligible_at,
                              expires_at=route.eligible_at + cfg.held_expiry, recent_beneficiary=True, **common)
        status_name = "held"
    else:
        row, created = create("awaiting_approval", expires_at=when + cfg.approval_window, **common)
        status_name = "awaiting_approval"
    if not created:
        audit.append("instruction_exists", {"ref": ref})
        return "duplicate_existing"
    audit.append("instruction_created", {"ref": ref, "status": status_name, "amount": str(amount), "currency": "ZAR",
                                         "source_account_last3": trig.last3})
    return "instruction"


# --------------------------------------------------------------- per message
def _handle_message(ctx: _Ctx, v2, iid: str, no_message_id: bool) -> str:
    cfg, store, audit = ctx.cfg, ctx.store, ctx.audit
    if no_message_id:
        audit.append("no_message_id", {})
        return "no_message_id"
    audit.append("message_received", {"message_id_hash": hashlib.sha256(v2.message_id.strip().lower().encode("utf-8")).hexdigest()})
    msg = email.message_from_bytes(v2.raw)
    view = content.text_view(msg)                           # text parts only: safe before authentication
    own = loopguard.is_own_notification(
        v2.message_id, v2.auto_submitted, v2.x_invespend_notification, view.raw_head_lines, v2.subject,
        {"precedence": v2.precedence, "x_autoreply": v2.x_autoreply, "x_autorespond": v2.x_autorespond,
         "x_auto_response_suppress": v2.x_auto_response_suppress})
    if own:
        audit.append("own_notification", {"reason": own})
        return "own_notification"
    verdict = sender_auth.authenticate_sender(v2.from_headers, v2.auth_results, cfg.allowed_senders, cfg.authserv_ids)
    if not verdict.ok:
        audit.append("auth_failed", {"reason": verdict.reason})
        return "auth_failed"
    auth_from = verdict.from_addr
    audit.append("auth_ok", {"reason": "ok"})
    store.set_message_outcome(iid, "processing", auth_from=auth_from)
    batch_refs = refs.find_batch_refs(v2.subject, v2.in_reply_to, v2.references)
    cmd = commands.parse_command(view)
    received = received_at_of(v2)
    if batch_refs or cmd is not None:
        return approval.handle_approval_message(
            ctx.settings, store, audit, auth_from=auth_from, batch_refs=batch_refs, cmd=cmd, received_at=received,
            now=ctx.when, smtp_send=ctx.smtp_send, notice_counter=ctx.notice_counter, counters=ctx.counters)
    return _new_instruction(ctx, v2, msg, view, iid, auth_from, received)


# ------------------------------------------------------------------- sweeps
_EXPIRY_KIND = {"awaiting_approval": "unapproved", "accepted": "approved_not_executed",
                "awaiting_beneficiary": "awaiting_beneficiary", "held": "held"}


def _payee_display(ctx: _Ctx, row: dict) -> str:
    for b in ctx.beneficiaries or ():
        if b.beneficiary_id == row.get("beneficiary_id") and b.name:
            return b.name
    return str(row.get("payee_name_norm") or "")


def _expiry_sweep(ctx: _Ctx) -> int:
    grouped: dict[str, list] = {}
    expired = 0
    for row in ctx.store.list_active():
        if row["status"] == "submitting" or row["expires_at"] > ctx.when:
            continue
        if ctx.store.cas_status(row["instruction_id"], {row["status"]}, "expired", now=ctx.when, outcome_code="expired"):
            expired += 1
            grouped.setdefault(row["notify_to"], []).append(notify_v2.ExpiredItem(
                row["batch_ref"], row["item_no"], _payee_display(ctx, row), Decimal(str(row["amount"])), row["currency"],
                _EXPIRY_KIND[row["status"]]))
    for to, items in sorted(grouped.items()):
        ctx.send(notify_v2.build_expiry_email(sender_of(ctx.settings), to, items=items))
    if expired:
        ctx.audit.append("expired", {"count": expired})
    return expired


def _park_row(ctx: _Ctx, row: dict, expected: str, reason: str) -> bool:
    if ctx.store.cas_status(row["instruction_id"], {expected}, "parked", now=ctx.when, outcome_code=reason):
        ref = refs.request_ref(row["instruction_id"])
        ctx.audit.append("parked", {"ref": ref, "reason": reason})
        ctx.send(notify_v2.build_problem_email(sender_of(ctx.settings), row["notify_to"], ref=ref, kind="parked", detail_code=reason))
        ctx.counters["parked_rows"] = ctx.counters.get("parked_rows", 0) + 1
        return True
    return False


def _beneficiary_sweep(ctx: _Ctx) -> None:
    cfg, store = ctx.cfg, ctx.store
    if ctx.beneficiaries is None:
        return                                              # a failed fetch never looks like "new payee" and never holds
    for row in store.list_by_status({"awaiting_beneficiary"}):
        found, status = bene.resolve_beneficiary_strict(row.get("payee_name_norm") or "", ctx.beneficiaries)
        if status == "ambiguous":
            _park_row(ctx, row, "awaiting_beneficiary", "beneficiary_ambiguous")
            continue
        if status != "ok":
            continue
        if not cfg.fingerprint_key:
            _park_row(ctx, row, "awaiting_beneficiary", "fingerprint_key_missing")
            continue
        raw = ctx.raw_index.get(found.beneficiary_id)
        obs = ctx.observations.get(found.beneficiary_id)
        if raw is None or obs is None:
            continue
        if row.get("account_hmac"):
            registered = raw.get("accountNumber")
            if not registered or fingerprints.account_hmac(registered, cfg.fingerprint_key) != row["account_hmac"]:
                _park_row(ctx, row, "awaiting_beneficiary", "account_number_mismatch")
                continue
        eligible = obs.first_seen_at + cfg.hold
        if store.set_held(row["instruction_id"], beneficiary_id=found.beneficiary_id,
                          fingerprint=fingerprints.beneficiary_fingerprint(raw, cfg.fingerprint_key),
                          first_seen_at=obs.first_seen_at, eligible_at=eligible, expires_at=eligible + cfg.held_expiry,
                          now=ctx.when):
            ctx.audit.append("held", {"ref": refs.request_ref(row["instruction_id"]), "status": "held"})
            _observed_notice(ctx, row["instruction_id"], found.name or _payee_display(ctx, row), row, eligible)
    for row in store.list_by_status({"held"}):
        if row["eligible_at"] is None or row["eligible_at"] > ctx.when:
            continue
        reason = execute.reverify_beneficiary(row, ctx.beneficiaries, ctx.raw, cfg.fingerprint_key)
        if reason:
            _park_row(ctx, row, "held", reason)
            continue
        store.make_ready(row["instruction_id"], now=ctx.when, approval_window=cfg.approval_window)


def _observed_notice(ctx: _Ctx, iid: str, payee: str, row: dict, eligible: datetime) -> None:
    msg = notify_v2.build_beneficiary_observed_email(
        sender_of(ctx.settings), row["notify_to"], ref=refs.request_ref(iid), payee_name=payee,
        amount=Decimal(str(row["amount"])), currency=row["currency"], source_last3=row["source_account_last3"],
        hold_hours=int(ctx.cfg.hold_hours), eligible_at=eligible)
    if ctx.send(msg):
        ctx.store.mark_notified(iid, "hold", ctx.when)


def _notice_sweep(ctx: _Ctx) -> None:
    cfg, store = ctx.cfg, ctx.store
    for row in store.list_by_status({"held"}):
        if row["hold_notified_at"] is None and row["updated_at"] < ctx.when - cfg.stuck:
            _observed_notice(ctx, row["instruction_id"], _payee_display(ctx, row), row, row["eligible_at"] or ctx.when)
    batch.resend_unnotified(ctx.settings, store, ctx.audit, beneficiaries=ctx.beneficiaries, now=ctx.when, smtp_send=ctx.smtp_send)
    for stuck in store.sweep_stuck_messages(older_than=cfg.stuck, now=ctx.when):
        msg = notify_v2.build_problem_email(sender_of(ctx.settings), stuck["auth_from"], ref=refs.request_ref(stuck["instruction_id"]),
                                            kind="resend", detail_code="stuck_message")
        if ctx.send(msg):
            store.mark_resend_notified(stuck["instruction_id"], ctx.when)
            ctx.audit.append("resend_notice", {"ref": refs.request_ref(stuck["instruction_id"])})


# --------------------------------------------------------------------- cycle
def run_instruction_cycle(
    settings, *, inbox, client, store, audit, smtp_send=None, extractor=None, now: Callable[[], datetime] | None = None,
    allow_non_durable: bool = False,
) -> dict:
    cfg = v2_settings(settings)
    clock = now or (lambda: datetime.now(timezone.utc))
    when = clock()
    if smtp_send is None:
        from .. import emailer
        smtp_send = functools.partial(emailer._smtp_send, settings)
    if extractor is None:
        extractor = images.get_extractor(settings)
    _preflight(settings, cfg, store, audit, allow_non_durable)           # ALL before any IMAP fetch (fetch marks mail read)

    live = bool(settings.live_enabled())
    mode = "live" if live else "dry-run"
    audit.append("cycle_start", {"execution_mode": mode, "live_enabled": live})
    if cfg.hold_fallback:
        audit.append("hold_setting_fallback", {"reason": "unparsable_hold_hours", "count": int(cfg.hold_hours)})
    for iid in store.recover_stale_submitting(older_than=cfg.stale, now=when):
        row = store.get(iid)
        audit.append("stale_submitting", {"ref": refs.request_ref(iid), "reason": "stale_submitting"})
        if row is not None:
            safe_send(smtp_send, notify_v2.build_problem_email(
                sender_of(settings), row["notify_to"], ref=refs.request_ref(iid), kind="needs_review",
                detail_code="stale_submitting"), audit)
    snapshot = {r["instruction_id"] for r in store.list_by_status({"accepted"})}     # (1b) BEFORE any message is handled

    try:
        raw = client.get_beneficiaries()
        if not isinstance(raw, list):
            raise TypeError("beneficiary list")
        beneficiaries = bene.from_api(raw)
    except Exception:  # noqa: BLE001 - None, NOT [], so a failed fetch never looks like "new payee"
        raw, beneficiaries = None, None
    observations: dict[str, instructions.BeneficiaryObservation] = {}
    if raw is not None:
        bootstrap = not store.bootstrap_done()
        for item in raw:
            if not (isinstance(item, dict) and item.get("beneficiaryId")):
                continue
            fp = fingerprints.beneficiary_fingerprint(item, cfg.fingerprint_key) if cfg.fingerprint_key else None
            bid = str(item["beneficiaryId"]).strip()
            observations[bid] = store.observe_beneficiary(bid, when, fingerprint=fp, established=bootstrap)
        if bootstrap:
            store.mark_bootstrap_done(when)

    messages = inbox.fetch_messages()
    ctx = _Ctx(settings, cfg, store, audit, client, smtp_send, extractor, when, beneficiaries, raw, observations)
    results: Counter = Counter()
    summary_parts = {"batches_sent": 0, "items_offered": 0, "pre_offer_parked": 0, "deferred": 0}
    executions: list[str] = []
    expired = 0
    try:
        entries = []
        for v2 in messages:                                   # (e') identity pre-pass for ALL messages
            iid = refs.instruction_id_for(v2.message_id, v2.from_headers)
            no_mid = iid is None
            if iid is None:
                iid = hashlib.sha256(("nomid|" + "\n".join(v2.from_headers) + "|" + hashlib.sha256(v2.raw).hexdigest()).encode()).hexdigest()
            entries.append((v2, iid, store.mark_message_seen(iid, when), no_mid))
        for v2, iid, fresh, no_mid in entries:
            if not fresh:
                continue
            try:
                outcome = _handle_message(ctx, v2, iid, no_mid)
            except Exception as exc:  # noqa: BLE001 - one bad message never loses the others
                outcome = "error"
                audit.append("message_error", {"reason": type(exc).__name__})
            store.set_message_outcome(iid, outcome)
            results[outcome] += 1

        expired = _expiry_sweep(ctx)                                            # (2) expiry FIRST
        _beneficiary_sweep(ctx)                                                 # (3)
        rows = [store.get(i) for i in snapshot]                                 # (4) only the cycle-start snapshot
        for row in sorted((r for r in rows if r and r["status"] == "accepted"),
                          key=lambda r: (r["approved_at"], r["batch_ref"] or "", r["item_no"] or 0)):
            try:
                code = execute.execute_instruction(settings, store, audit, client, row, mode=mode, now=when, smtp_send=smtp_send)
            except Exception as exc:  # noqa: BLE001
                audit.append("execute_error", {"reason": type(exc).__name__})
                code = "execute_error"
            executions.append(code)
            results[code] += 1
        summary_parts = batch.offer_batches(                                    # (5)
            settings, store, audit, beneficiaries=beneficiaries, raw_beneficiaries=raw, now=when, smtp_send=smtp_send,
            new_ref=lambda: refs.new_batch_ref(when, secrets.token_hex(2)))
        _notice_sweep(ctx)                                                      # (6)
        if cfg.retention_days * 24 >= 4 * max(cfg.max_age_hours, cfg.approval_expiry_hours, cfg.grace_hours):
            store.cleanup(cfg.retention_days, when)
        else:
            audit.append("retention_too_short", {"count": cfg.retention_days})
    except Exception:
        for to in cfg.notify_recipients:                                        # generic notice: no detail (TR3-13a)
            safe_send(smtp_send, notify_v2.build_problem_email(sender_of(settings), to, ref=None, kind="cycle_failed",
                                                              detail_code=""), audit)
        raise

    parked = (sum(n for code, n in results.items() if code.startswith("parked"))
              + ctx.counters.get("parked_rows", 0) + summary_parts["pre_offer_parked"])
    summary = {
        "mode": mode, "live_enabled": live, "messages": len(messages),
        "ignored": sum(n for code, n in results.items() if code.startswith("ignored")),
        "own_notification": results.get("own_notification", 0), "auth_failed": results.get("auth_failed", 0),
        "approved": ctx.counters.get("approved", 0), "cancelled": ctx.counters.get("cancelled", 0),
        "offered": summary_parts["items_offered"], "batches_sent": summary_parts["batches_sent"],
        "executed": sum(1 for code in executions if code.startswith("executed")), "expired": expired,
        "parked": parked, "errors": results.get("error", 0), "deferred": summary_parts["deferred"],
        "results": dict(results),
    }
    audit.append("cycle_end", {"count": len(messages)})
    return summary
