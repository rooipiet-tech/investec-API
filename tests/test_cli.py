from invespend.cli import build_parser


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
