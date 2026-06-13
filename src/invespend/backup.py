"""Database backup (ARCHITECTURE §3, free-tier constraints).

The free Supabase tier has no managed backups, so the weekly job runs pg_dump
and produces a gzipped (optionally encrypted) artifact. Set BACKUP_PASSPHRASE to
encrypt the dump with openssl AES-256 before it leaves the runner; without it the
dump is gzip-only and a warning is logged.

The dump is produced as a streamed ``pg_dump | gzip [| openssl]`` pipeline:
plaintext never lands on disk and the whole dump never buffers in RAM. Secrets
are kept off the process command line — the database password is passed via the
``PG*`` libpq environment variables (not the connection URI on argv) and the
encryption passphrase via ``-pass env:`` — so neither shows up in ``ps`` /
``/proc`` while the job runs.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from datetime import date
from pathlib import Path

from psycopg.conninfo import conninfo_to_dict

from .config import Settings

log = logging.getLogger(__name__)

# Guard against a leaked dump session wedging the database: if this pg_dump's
# connection is ever left idle in a transaction (e.g. the runner is killed
# mid-dump), the server reaps it after this timeout instead of letting it hold
# AccessShare locks on every table for days — which would otherwise block the
# nightly ingest's DDL. pg_dump is only briefly idle between COPYs on this small
# DB, so a healthy run is never affected.
_DUMP_PGOPTIONS = "-c idle_in_transaction_session_timeout=300000"

# libpq honours these env vars in place of a connection URI on argv, keeping the
# password out of the process command line.
_CONNINFO_TO_PGENV = {
    "host": "PGHOST",
    "hostaddr": "PGHOSTADDR",
    "port": "PGPORT",
    "user": "PGUSER",
    "password": "PGPASSWORD",
    "dbname": "PGDATABASE",
    "sslmode": "PGSSLMODE",
}


def _pg_env(database_url: str) -> dict[str, str]:
    """Base subprocess env with the connection passed via PG* vars, not argv."""
    info = conninfo_to_dict(database_url)
    env = {**os.environ, "PGOPTIONS": _DUMP_PGOPTIONS}
    for key, env_name in _CONNINFO_TO_PGENV.items():
        value = info.get(key)
        if value:
            env[env_name] = str(value)
    return env


def _run_pipeline(stages: list[list[str]], out_path: Path, env: dict[str, str]) -> None:
    """Run ``stages`` as a shell-less pipeline, final stdout → ``out_path``.

    Each stage's stdout feeds the next stage's stdin; the last writes to the
    file. No shell (so nothing is word-split or injectable) and no buffering of
    the whole dump in Python — data streams stage to stage.
    """
    procs: list[subprocess.Popen] = []
    prev_stdout = None
    with out_path.open("wb") as out:
        for i, cmd in enumerate(stages):
            is_last = i == len(stages) - 1
            proc = subprocess.Popen(
                cmd,
                stdin=prev_stdout,
                stdout=(out if is_last else subprocess.PIPE),
                env=env,
            )
            # Close our copy of the upstream pipe so only this stage holds the
            # read end — that way an upstream failure delivers SIGPIPE here.
            if prev_stdout is not None:
                prev_stdout.close()
            prev_stdout = proc.stdout
            procs.append(proc)
        for proc in procs:
            proc.wait()
    for cmd, proc in zip(stages, procs):
        if proc.returncode:
            raise subprocess.CalledProcessError(proc.returncode, cmd)


def run_backup(settings: Settings, out_dir: Path | None = None) -> Path:
    """Dump the database to ``out_dir`` and return the artifact path."""
    if shutil.which("pg_dump") is None:
        raise RuntimeError("pg_dump not found on PATH; install the postgresql client.")
    if shutil.which("gzip") is None:
        raise RuntimeError("gzip not found on PATH.")

    out_dir = out_dir or Path("backups")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = date.today().isoformat()
    env = _pg_env(settings.database_url)

    # Connection comes from the PG* env (set above), so no URI on argv.
    dump = ["pg_dump", "--no-owner", "--no-privileges"]
    gzip_stage = ["gzip", "-c"]

    if not settings.backup_passphrase:
        log.warning(
            "BACKUP_PASSPHRASE not set — backup is gzip-only, not encrypted. "
            "Set it to encrypt the dump at rest."
        )
        gz_path = out_dir / f"backup_{stamp}.sql.gz"
        _run_pipeline([dump, gzip_stage], gz_path, env)
        log.info("Wrote backup %s (%d bytes)", gz_path, gz_path.stat().st_size)
        return gz_path

    if shutil.which("openssl") is None:
        raise RuntimeError("openssl not found on PATH; cannot encrypt the backup.")

    # Pass the passphrase via the environment (-pass env:), never on argv.
    env["BACKUP_PASSPHRASE"] = settings.backup_passphrase
    encrypt = [
        "openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-salt",
        "-pass", "env:BACKUP_PASSPHRASE",
    ]
    enc_path = out_dir / f"backup_{stamp}.sql.gz.enc"
    _run_pipeline([dump, gzip_stage, encrypt], enc_path, env)
    log.info("Wrote encrypted backup %s (%d bytes)", enc_path, enc_path.stat().st_size)
    return enc_path
