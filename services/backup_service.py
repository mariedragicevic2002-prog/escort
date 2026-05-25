"""
PostgreSQL logical backups via pg_dump (gzip). Used by the scheduler and scripts/backup_database.py.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
from datetime import datetime, timezone
from services.database_service import normalize_database_url

logger = logging.getLogger("adella_chatbot.backup")


def prune_old_backups(backup_dir: str, keep: int, prefix: str = "adella_backup_") -> int:
    """Remove oldest compressed backups beyond ``keep``. Returns number of files removed."""
    if keep < 1 or not os.path.isdir(backup_dir):
        return 0
    candidates = []
    for name in os.listdir(backup_dir):
        if not name.startswith(prefix) or not name.endswith(".sql.gz"):
            continue
        path = os.path.join(backup_dir, name)
        if os.path.isfile(path):
            try:
                candidates.append((os.path.getmtime(path), path))
            except OSError as e:
                logger.warning("Skipping backup candidate mtime for %s: %s", path, e)
                continue
    candidates.sort(key=lambda x: x[0], reverse=True)
    removed = 0
    for _mtime, path in candidates[keep:]:
        try:
            os.remove(path)
            removed += 1
            logger.info("Removed old backup: %s", path)
        except OSError as e:
            logger.warning("Could not remove old backup %s: %s", path, e)
    return removed


def run_pg_dump_backup() -> tuple[bool, str]:
    """
    Run pg_dump, gzip output to BACKUP_DIR, prune by BACKUP_RETENTION_COUNT.

    Returns:
        (success, message for logs)
    """
    import config

    if not getattr(config, "AUTO_BACKUP_ENABLED", True):
        return True, "skipped (AUTO_BACKUP_ENABLED=false)"

    url = (config.DATABASE_URL or "").strip()
    if not url:
        return False, "DATABASE_URL not set"

    url = normalize_database_url(url)

    pg_dump = shutil.which("pg_dump")
    if not pg_dump:
        return False, "pg_dump not found in PATH (install PostgreSQL client tools)"

    backup_dir = os.path.abspath(config.BACKUP_DIR)
    os.makedirs(backup_dir, exist_ok=True)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = os.path.join(backup_dir, f"adella_backup_{stamp}.sql.gz")

    cmd = [
        pg_dump,
        "--no-owner",
        "--no-acl",
        "-f",
        "-",
        url,
    ]

    proc = None
    try:
        import gzip

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert proc.stdout is not None
        assert proc.stderr is not None
        stderr_chunks: list[bytes] = []

        def _drain_stderr(pipe) -> None:
            try:
                for chunk in iter(lambda: pipe.read(8192), b""):
                    if chunk:
                        stderr_chunks.append(chunk)
            except Exception as e:
                logger.warning("Backup stderr drain failed: %s", e)

        stderr_thread = threading.Thread(target=_drain_stderr, args=(proc.stderr,), daemon=True)
        stderr_thread.start()
        with gzip.open(out_path, "wb", compresslevel=6) as gz:
            shutil.copyfileobj(proc.stdout, gz)
        proc.stdout.close()
        proc.wait(timeout=600)
        stderr_thread.join(timeout=2)
        err = b"".join(stderr_chunks)
    except Exception as e:
        logger.error("Backup stream failed: %s", e, exc_info=True)
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            if os.path.isfile(out_path):
                os.remove(out_path)
        except OSError as e:
            logger.warning("Could not remove partial backup after stream failure %s: %s", out_path, e)
        return False, str(e)

    if proc.returncode != 0:
        msg = err.decode("utf-8", errors="replace")[:2000] if err else "pg_dump failed"
        logger.error("pg_dump exited %s: %s", proc.returncode, msg)
        try:
            if os.path.isfile(out_path):
                os.remove(out_path)
        except OSError as e:
            logger.warning("Could not remove failed backup %s: %s", out_path, e)
        return False, msg

    size_kb = os.path.getsize(out_path) // 1024
    logger.info("Database backup written: %s (%s KiB)", out_path, size_kb)

    keep = getattr(config, "BACKUP_RETENTION_COUNT", 7)
    prune_old_backups(backup_dir, keep)

    return True, f"ok -> {out_path} ({size_kb} KiB)"
