"""Investec Programmable Banking ingestion + weekly spend report."""

__version__ = "0.1.0"

# Shared Excel money format: the *Accounting* number format (no currency symbol).
# Excel renders the ``,`` digit-group placeholder using the viewer's locale, so
# under en-ZA this shows space-grouped thousands — ``2 398 922.05`` — with two
# decimals, a left-aligned minus on negatives (``- 2 351 459.05``) and a dash
# for zero. Used for every numeric cell so all workbooks read identically.
MONEY_FORMAT = r'_-* #,##0.00_-;-* #,##0.00_-;_-* "-"??_-;_-@_-'
