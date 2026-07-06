import invespend.cli as cli
from invespend.cli import build_parser


def _fake_report(monkeypatch, tmp_path, rows):
    path = tmp_path / "r.xlsx"
    path.write_text("x")
    info = {"start": "2026-06-27", "end": "2026-07-03", "rows": rows}
    monkeypatch.setattr(cli, "generate_weekly_report", lambda *a, **k: (path, info))
    monkeypatch.setattr(cli, "_resolve_group_recipients", lambda *a, **k: ["x@y.com"])
    monkeypatch.setattr(cli, "_report_body", lambda info: "body")
    sent = []
    monkeypatch.setattr(cli, "send_report", lambda *a, **k: sent.append(k))
    return sent


def test_report_skips_email_when_no_rows(monkeypatch, tmp_path):
    sent = _fake_report(monkeypatch, tmp_path, rows=0)
    cli._send_group_report(object(), None, send=True)
    assert sent == []  # empty window (e.g. default bucket) is not emailed


def test_report_emails_when_rows_present(monkeypatch, tmp_path):
    sent = _fake_report(monkeypatch, tmp_path, rows=5)
    cli._send_group_report(object(), None, send=True)
    assert len(sent) == 1


def test_ingest_accepts_backfill_dates():
    args = build_parser().parse_args(["ingest", "--from", "2016-01-01", "--to", "2016-12-31"])
    assert args.from_date == "2016-01-01"
    assert args.to_date == "2016-12-31"
    assert args.command == "ingest"


def test_ingest_defaults():
    args = build_parser().parse_args(["ingest"])
    assert args.from_date is None
    assert args.to_date is None
    assert args.days is None


def test_report_send_flag():
    assert build_parser().parse_args(["report", "--send"]).send is True
    assert build_parser().parse_args(["report"]).send is False
