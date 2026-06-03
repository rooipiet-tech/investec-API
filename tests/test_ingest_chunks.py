from datetime import date

from invespend.ingest import _date_chunks


def test_chunks_cover_range_newest_first():
    chunks = list(_date_chunks(date(2024, 1, 1), date(2024, 12, 31), 90))
    # Newest window comes first and ends on to_date.
    assert chunks[0][1] == date(2024, 12, 31)
    # Oldest window starts exactly on from_date.
    assert chunks[-1][0] == date(2024, 1, 1)
    # Windows are contiguous and non-overlapping (each starts the day after the
    # previous one ended).
    for newer, older in zip(chunks, chunks[1:]):
        assert older[1] == newer[0] - __import__("datetime").timedelta(days=1)


def test_single_chunk_when_range_within_window():
    chunks = list(_date_chunks(date(2024, 6, 1), date(2024, 6, 15), 90))
    assert chunks == [(date(2024, 6, 1), date(2024, 6, 15))]


def test_chunk_size_respected():
    chunks = list(_date_chunks(date(2020, 1, 1), date(2024, 12, 31), 90))
    for start, end in chunks:
        assert (end - start).days <= 89  # inclusive 90-day windows
