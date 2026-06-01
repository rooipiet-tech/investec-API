"""Database backup (ARCHITECTURE §8).

The free Supabase tier has no managed backups, so the weekly job runs pg_dump
and produces a gzipped (optionally encrypted) artifact. Set BACKUP_PASSPHRASE to
encrypt the dump with openssl AES-256 before it leaves the runner; without it the
dump is gzip-only and a warning is logged.
"""
from __future__ import annotations

import gzip
import logging
import shutil
import subprocess
from datetime import date
from pathlib import Path

from .config import Settings

log = logging.getLogger(__name__)


def run_backup(settings: Settings, out_dir: Path | None = None) -> Path:
    """Dump the database to ``out_dir`` and return the artifact path."""
    if shutil.which("pg_dump") is None:
        raise RuntimeError("pg_dump not found on PATH; install the postgresql client.")

    out_dir = out_dir or Path("backups")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = date.today().isoformat()

    # Stream pg_dump → gzip so the plaintext never lands on disk uncompressed.
    gz_path = out_dir / f"backup_{stamp}.sql.gz"
    dump = subprocess.run(
        ["pg_dump", "--no-owner", "--no-privileges", settings.database_url],
        stdout=subprocess.PIPE,
        check=True,
    )
    with gzip.open(gz_path, "wb") as fh:
        fh.write(dump.stdout)

    if not settings.backup_passphrase:
        log.warning(
            "BACKUP_PASSPHRASE not set — backup is gzip-only, not encrypted. "
            "Set it to encrypt the dump at rest."
        )
        log.info("Wrote backup %s (%d bytes)", gz_path, gz_path.stat().st_size)
        return gz_path

    enc_path = out_dir / f"backup_{stamp}.sql.gz.enc"
    subprocess.run(
        [
            "openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-salt",
            "-in", str(gz_path), "-out", str(enc_path),
            "-pass", f"pass:{settings.backup_passphrase}",
        ],
        check=True,
    )
    gz_path.unlink()  # keep only the encrypted artifact
    log.info("Wrote encrypted backup %s (%d bytes)", enc_path, enc_path.stat().st_size)
    return enc_path
