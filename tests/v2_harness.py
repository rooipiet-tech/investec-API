"""Offline harness for the v2 money-path tests (S11): fake inbox / SMTP / Investec client, an injected clock and an
``Env`` that drives real cycles. Nothing here sends mail or touches a network. New modules are imported lazily so the
red stage can collect the tests before the modules exist."""
from __future__ import annotations

import email.policy
import importlib
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from email.message import EmailMessage
from types import SimpleNamespace

T0 = datetime(2026, 10, 7, 10, 0, tzinfo=timezone.utc)
SENDER = "bot@example.com"
OWNER = "piet@example.com"
GOOD_AUTH = ("mx.google.com; dkim=pass header.i=@example.com; spf=pass smtp.mailfrom=piet@example.com; "
             "dmarc=pass header.from=example.com")
BAD_AUTH = "mx.google.com; dkim=fail header.i=@example.com; spf=pass smtp.mailfrom=piet@example.com"
KEY = "test-fingerprint-key"
ACME_RAW = {"beneficiaryId": "ben-acme", "beneficiaryName": "Acme Trading", "accountNumber": "1234567890", "code": "250655"}
BETA_RAW = {"beneficiaryId": "ben-beta", "beneficiaryName": "Beta Supplies", "accountNumber": "9876543210", "code": "632005"}
ACCOUNT = {"accountId": "acc-1", "accountNumber": "10012345123", "profileId": "prof-1", "accountName": "Current"}
QUOTE = ("\n\nOn Wed, 7 Oct 2026 at 10:00, Invespend <bot@example.com> wrote:\n> Invespend automated notice.\n"
         "> 1. Acme Trading | ZAR 100.00\n")
SUCCESS_BODY = {"data": {"TransferResponses": [{"PaymentReferenceNumber": "PR1", "Status": "Processed"}],
                         "ErrorMessage": None}}
INSTRUCTION = "pay 123\nPayee: Acme Trading\nAmount: R100.00\nReference: INV7\n"


def mod(name: str):
    return importlib.import_module(f"invespend.payments.{name}")


# ------------------------------------------------------------------ fakes
class FakeInbox:
    def __init__(self) -> None:
        self.pending: list = []
        self.fetch_calls = 0

    def queue(self, *messages) -> None:
        self.pending.extend(messages)

    def fetch_messages(self) -> list:
        self.fetch_calls += 1
        out, self.pending = self.pending, []      # IMAP marks them read
        return out


class FakeSMTP:
    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []
        self.fail_with: BaseException | None = None
        self.fail_times = 0

    def __call__(self, msg: EmailMessage) -> None:
        if self.fail_with is not None and (self.fail_times != 0):
            if self.fail_times > 0:
                self.fail_times -= 1
            raise self.fail_with
        self.sent.append(msg)

    def subjects(self) -> list[str]:
        return [str(m["Subject"]) for m in self.sent]

    def body_text(self) -> str:
        return "\n".join(str(m.get_content()) for m in self.sent)


class FakeClient:
    def __init__(self) -> None:
        self.beneficiaries: list[dict] = [dict(ACME_RAW), dict(BETA_RAW)]
        self.accounts: list[dict] = [dict(ACCOUNT)]
        self.balance: object = {"availableBalance": "100000.00", "currentBalance": "100000.00"}
        self.fail_beneficiaries: BaseException | None = None
        self.fail_accounts: BaseException | None = None
        self.payment_calls: list[tuple] = []
        self.token_fetches = 0
        self.beneficiary_calls = 0
        self.responder = lambda: SUCCESS_BODY          # callable returning a body or raising

    def get_beneficiaries(self):
        self.beneficiary_calls += 1
        if self.fail_beneficiaries:
            raise self.fail_beneficiaries
        return [dict(b) for b in self.beneficiaries]

    def get_accounts(self):
        if self.fail_accounts:
            raise self.fail_accounts
        return [dict(a) for a in self.accounts]

    def get_balance(self, account_id):
        if isinstance(self.balance, BaseException):
            raise self.balance
        return dict(self.balance)

    def create_payment(self, source_account_id, beneficiary_id, amount, reference="", my_reference="", *, fresh_token=False):
        assert fresh_token is True, "v2 must always use the hardened write path"
        self.token_fetches += 1
        self.payment_calls.append((source_account_id, beneficiary_id, str(amount), reference, my_reference))
        return self.responder()


# ---------------------------------------------------------------- mail builder
_counter = {"n": 0}


def make_mail(plain: str, *, subject: str = "Payment", frm: str = f"Piet <{OWNER}>", auth: str | None = GOOD_AUTH,
              message_id: str | None = None, in_reply_to: str = "", references: str = "", headers: dict | None = None,
              attachments=(), inline=(), internaldate: datetime | None = T0 - timedelta(minutes=5), received_header=None,
              html: str | None = None):
    _counter["n"] += 1
    # real clients fold long ids at whitespace; never RFC 2047-encode them (our batch Message-ID is > 78 chars)
    msg = EmailMessage(policy=email.policy.default.clone(max_line_length=998))
    msg["From"] = frm
    msg["To"] = SENDER
    msg["Subject"] = subject
    msg["Message-ID"] = message_id if message_id is not None else f"<test{_counter['n']}.{id(msg)}@mail.example.com>"
    if message_id == "":
        del msg["Message-ID"]
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    if auth:
        msg["Authentication-Results"] = auth
    if received_header:
        msg["Received"] = f"from mx.example.com by mx.google.com; {received_header}"
    for k, v in (headers or {}).items():
        msg[k] = v
    if html is not None:
        msg.set_content(plain)
        msg.add_alternative(html, subtype="html")
    else:
        msg.set_content(plain)
    for fn, mt, st, data in attachments:
        msg.add_attachment(data, maintype=mt, subtype=st, filename=fn)
    for fn, mt, st, data in inline:
        msg.add_attachment(data, maintype=mt, subtype=st, filename=fn, disposition="inline")
    return mod("v2_inbox").parse_v2_message(msg.as_bytes(), internaldate)


class MutableClock:
    def __init__(self, start: datetime = T0) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now


def make_settings(**over):
    values = dict(
        payments_allowed_senders=(OWNER,), payments_authserv_ids=("mx.google.com",), payments_authserv_id="mx.google.com",
        imap_mailbox="PAYMENTS", report_sender=SENDER, smtp_user=SENDER, per_payment_cap=20000.0,
        daily_aggregate_cap=50000.0, payments_fingerprint_key=KEY, retention_days=90, payments_state_backend="postgres",
        payments_mode="v2", payments_dry_run=True, payments_live_enable=False, payments_hold_hours=0.0,
        payments_notify_recipients=(),
    )
    live = over.pop("live", False)
    values.update(over)
    ns = SimpleNamespace(**values)
    ns.live_enabled = lambda: live
    ns._has_write_trio = lambda: False
    ns.payment_credentials = lambda: ("write-id", "write-secret", "write-key")
    return ns


class Env:
    """A v2 cycle environment with registered payee 'Acme Trading' (established), hold 0, an injected clock."""

    def __init__(self, tmp_path, *, live: bool = False, bootstrap: bool = True, store=None, **settings_over) -> None:
        self.settings = make_settings(live=live, **settings_over)
        self.store = store if store is not None else mod("instructions").MemoryInstructionStore()
        self.audit = mod("audit").AuditLog(tmp_path)
        self.client = FakeClient()
        self.inbox = FakeInbox()
        self.smtp = FakeSMTP()
        self.extractor = mod("images").FakeImageExtractor({})
        self.clock = MutableClock()
        self.cycles = 0
        if bootstrap:
            self.bootstrap()

    # -- state ---------------------------------------------------------
    @property
    def now(self) -> datetime:
        return self.clock.now

    def advance(self, minutes: float = 15) -> None:
        self.clock.now += timedelta(minutes=minutes)

    def bootstrap(self) -> None:
        fp = mod("fingerprints").beneficiary_fingerprint
        for raw in self.client.beneficiaries:
            self.store.observe_beneficiary(raw["beneficiaryId"], T0 - timedelta(days=1), fingerprint=fp(raw, KEY), established=True)
        self.store.mark_bootstrap_done(T0 - timedelta(days=1))

    # -- running -------------------------------------------------------
    def cycle(self, **kw) -> dict:
        args = dict(inbox=self.inbox, client=self.client, store=self.store, audit=self.audit, smtp_send=self.smtp,
                    extractor=self.extractor, now=self.clock, allow_non_durable=True)
        args.update(kw)
        self.cycles += 1
        return mod("cycle").run_instruction_cycle(self.settings, **args)

    def run_cycles(self, n: int) -> list[dict]:
        out = []
        for _ in range(n):
            out.append(self.cycle())
            self.advance(15)
        return out

    # -- mail ----------------------------------------------------------
    def instruct(self, body: str = INSTRUCTION, **kw):
        msg = make_mail(body, internaldate=kw.pop("internaldate", self.now - timedelta(minutes=2)), **kw)
        self.inbox.queue(msg)
        return msg

    def batch_emails(self) -> list[EmailMessage]:
        return [m for m in self.smtp.sent if "[BATCH " in str(m["Subject"]) and "awaiting approval" in str(m["Subject"])]

    def last_batch(self) -> EmailMessage:
        return self.batch_emails()[-1]

    def batch_ref(self, email: EmailMessage | None = None) -> str:
        email = email or self.last_batch()
        return re.search(r"\[BATCH (B-\d{4}-[0-9a-f]{4})\]", str(email["Subject"])).group(1)

    def reply(self, text: str, *, batch: EmailMessage | None = None, subject_ref: bool = True, in_reply_to: bool = True,
              auth: str | None = GOOD_AUTH, frm: str = f"Piet <{OWNER}>", quote: bool = True, **kw):
        batch = batch or self.last_batch()
        subject = f"Re: {batch['Subject']}" if subject_ref else "Re: your payments"
        body = text + (QUOTE if quote else "")
        msg = make_mail(body, subject=subject, frm=frm, auth=auth,
                        in_reply_to=str(batch["Message-ID"]) if in_reply_to else "",
                        references=str(batch["Message-ID"]) if in_reply_to else "",
                        internaldate=kw.pop("internaldate", self.now - timedelta(minutes=1)), **kw)
        self.inbox.queue(msg)
        return msg

    # -- reads -----------------------------------------------------------
    def rows(self, *statuses: str) -> list[dict]:
        wanted = statuses or mod("instructions").STATUSES
        return self.store.list_by_status(wanted)

    def row(self, n: int = 0, status: str | None = None) -> dict:
        rows = self.rows(status) if status else self.rows()
        return rows[n]

    def audit_steps(self) -> list[str]:
        return [e["step"] for e in self.audit.read_entries()]

    def audit_entries(self, step: str) -> list[dict]:
        return [e for e in self.audit.read_entries() if e["step"] == step]

    def payment_calls(self) -> int:
        return len(self.client.payment_calls)

    def everything_text(self) -> str:
        """Audit + stored rows + emails (for secret / digit-run scans)."""
        return "\n".join(
            [str(self.audit.read_entries()), str(self.rows()), self.smtp.body_text(), " ".join(self.smtp.subjects())])


def approve_in_next_cycles(env: Env, text: str = "approve") -> None:
    """cycle 1 must already have produced the batch; this sends the reply (cycle 2) and executes (cycle 3)."""
    env.advance(15)
    env.reply(text)
    env.cycle()
    env.advance(15)
    env.cycle()


def amount(value: str) -> Decimal:
    return Decimal(value)


def body_for(amount: str = "100.00", payee: str = "Acme Trading", last3: str = "123", reference: str = "INV7") -> str:
    return f"pay {last3}\nPayee: {payee}\nAmount: R{amount}\nReference: {reference}\n"


def instruct_many(env: Env, *amounts: str, **kw) -> list:
    """Queue one instruction per amount; received times increase so the batch numbering follows the argument order."""
    out = []
    for i, amount in enumerate(amounts):
        opts = dict(kw)
        opts.setdefault("internaldate", env.now - timedelta(minutes=len(amounts) - i + 1))
        out.append(env.instruct(body_for(amount), **opts))
    return out


def offered(env: Env, *amounts: str, **kw) -> str:
    """Queue instructions, run ONE cycle and return the batch ref (the cycle offers them)."""
    instruct_many(env, *amounts, **kw)
    env.cycle()
    return env.batch_ref()


def second_sender(env: Env, address: str = "anna@example.com") -> dict:
    """Allow a second authenticated sender; returns kwargs for ``make_mail``/``instruct``."""
    env.settings.payments_allowed_senders = (OWNER, address)
    auth = (f"mx.google.com; dkim=pass header.i=@example.com; spf=pass smtp.mailfrom={address}; "
            "dmarc=pass header.from=example.com")
    return {"frm": f"Anna <{address}>", "auth": auth}
