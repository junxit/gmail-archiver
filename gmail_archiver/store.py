"""SQLite-backed archive index for Gmail Archiver.

This replaces the previous single-JSON-blob state file. That design held every
message's metadata in memory and rewrote the whole file after each batch, which
is O(n^2) I/O across a full mailbox and — worse — leaves the archive with one
unreplicated copy of its dedup index: a single interrupted write corrupted it,
and the loader then silently treated the corrupt file as "nothing archived yet".

The index here is authoritative for *dedup* only. It is deliberately
reconstructible: every field it holds is also present in the on-disk
``metadata/*.json`` sidecars, so :func:`rebuild_from_metadata` can rebuild it
from the archive itself. Losing the database is an inconvenience, not data loss.
"""
import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Union

from .utils import get_email_hash

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

DEFAULT_DB_NAME = 'index.db'

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    msg_id        TEXT PRIMARY KEY,
    thread_id     TEXT,
    internal_date INTEGER,
    size          INTEGER NOT NULL,
    sha256        TEXT NOT NULL,
    rel_path      TEXT NOT NULL,
    labels        TEXT,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    vanished_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_vanished ON messages(vanished_at);
CREATE INDEX IF NOT EXISTS idx_messages_last_seen ON messages(last_seen);

CREATE TABLE IF NOT EXISTS failures (
    msg_id     TEXT PRIMARY KEY,
    reason     TEXT,
    attempts   INTEGER NOT NULL DEFAULT 1,
    first_error TEXT NOT NULL,
    last_error  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def _utcnow() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


class ArchiveStore:
    """The archive's dedup index and tombstone record.

    Opens (creating if needed) a SQLite database in WAL mode. Writes are
    committed in batches by the callers via :meth:`commit`; ``synchronous=FULL``
    means a committed batch is durable, so an interrupted run loses at most the
    messages downloaded since the last commit — and those are re-downloaded on
    the next run rather than lost.
    """

    def __init__(self, db_path: Union[str, Path]) -> None:
        """Open or create the index database.

        Args:
            db_path: Path to the SQLite file. Parent directories are created.
        """
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(_SCHEMA)
        existing = self.get_meta('schema_version')
        if existing is None:
            self.set_meta('schema_version', str(SCHEMA_VERSION))
        elif int(existing) > SCHEMA_VERSION:
            raise ValueError(
                f"Index at {self.db_path} has schema version {existing}, but this "
                f"build only understands {SCHEMA_VERSION}. Upgrade gmail-archiver."
            )
        self.conn.commit()

    def __enter__(self) -> 'ArchiveStore':
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        """Commit any pending work and close the database."""
        try:
            self.conn.commit()
        finally:
            self.conn.close()

    def commit(self) -> None:
        """Flush pending writes durably to disk."""
        self.conn.commit()

    # ---------------------------------------------------------------- meta

    def get_meta(self, key: str) -> Optional[str]:
        """Return a value from the ``meta`` table, or None if unset."""
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row['value'] if row else None

    def set_meta(self, key: str, value: str) -> None:
        """Set a value in the ``meta`` table."""
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )

    # --------------------------------------------------------------- dedup

    def is_archived(self, msg_id: str) -> bool:
        """Return True if ``msg_id`` is recorded in the index.

        This is an index-only check. Callers that care whether the bytes are
        still on disk should follow up with :meth:`get` and stat the path — see
        ``GmailBackup._is_intact``.
        """
        row = self.conn.execute(
            "SELECT 1 FROM messages WHERE msg_id = ?", (msg_id,)
        ).fetchone()
        return row is not None

    def get(self, msg_id: str) -> Optional[Dict[str, Any]]:
        """Return the indexed record for ``msg_id``, or None."""
        row = self.conn.execute(
            "SELECT * FROM messages WHERE msg_id = ?", (msg_id,)
        ).fetchone()
        if row is None:
            return None
        record = dict(row)
        record['labels'] = json.loads(record['labels']) if record['labels'] else []
        return record

    def record(
        self,
        msg_id: str,
        thread_id: Optional[str],
        internal_date: Optional[int],
        size: int,
        sha256: str,
        rel_path: str,
        labels: Iterable[str],
    ) -> None:
        """Insert or refresh the index entry for an archived message.

        Upserts rather than inserts so that re-archiving a message (after a
        truncated file is detected and re-downloaded) updates the row in place
        instead of failing or double-counting totals.

        Args:
            msg_id: Stable message identifier; the primary key.
            thread_id: Thread identifier, or None.
            internal_date: Message internal date in Unix milliseconds.
            size: Size of the raw message in bytes.
            sha256: Hex digest of the raw message bytes.
            rel_path: Path to the ``.eml``, relative to the backup directory.
            labels: Labels to record.
        """
        now = _utcnow()
        self.conn.execute(
            """
            INSERT INTO messages
                (msg_id, thread_id, internal_date, size, sha256, rel_path,
                 labels, first_seen, last_seen, vanished_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            ON CONFLICT(msg_id) DO UPDATE SET
                thread_id     = excluded.thread_id,
                internal_date = excluded.internal_date,
                size          = excluded.size,
                sha256        = excluded.sha256,
                rel_path      = excluded.rel_path,
                labels        = excluded.labels,
                last_seen     = excluded.last_seen,
                vanished_at   = NULL
            """,
            (
                msg_id, thread_id, internal_date, size, sha256, rel_path,
                json.dumps(list(labels)), now, now,
            ),
        )

    def mark_seen(self, msg_ids: Iterable[str]) -> None:
        """Record that these messages were present in the current sweep.

        Bumping ``last_seen`` is what distinguishes "still in Gmail" from
        "disappeared" when :meth:`tombstone_missing` runs. Messages skipped as
        already-archived must be marked too, otherwise the next tombstone pass
        would flag the entire archive as vanished.
        """
        now = _utcnow()
        self.conn.executemany(
            "UPDATE messages SET last_seen = ?, vanished_at = NULL WHERE msg_id = ?",
            [(now, m) for m in msg_ids],
        )

    # ----------------------------------------------------------- tombstones

    def tombstone_missing(self, sweep_started_at: str) -> int:
        """Flag archived messages that the just-completed sweep did not see.

        Callers must only invoke this after a sweep that ran to completion with
        no errors. A partial sweep has not seen the messages it never reached,
        and tombstoning on that basis would mark the whole archive as vanished.

        Nothing is deleted: the ``.eml`` and its metadata sidecar stay on disk
        permanently. This only records *when* Gmail stopped listing the message.

        Args:
            sweep_started_at: ISO-8601 timestamp captured before the sweep began.

        Returns:
            The number of messages newly tombstoned.
        """
        cur = self.conn.execute(
            "UPDATE messages SET vanished_at = ? "
            "WHERE vanished_at IS NULL AND last_seen < ?",
            (_utcnow(), sweep_started_at),
        )
        return cur.rowcount

    def list_vanished(self) -> List[Dict[str, Any]]:
        """Return every message flagged as no longer present in Gmail."""
        rows = self.conn.execute(
            "SELECT msg_id, rel_path, internal_date, vanished_at FROM messages "
            "WHERE vanished_at IS NOT NULL ORDER BY vanished_at"
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ failures

    def record_failure(self, msg_id: str, reason: str) -> None:
        """Record (or increment) a download failure so it can be retried later."""
        now = _utcnow()
        self.conn.execute(
            """
            INSERT INTO failures(msg_id, reason, attempts, first_error, last_error)
            VALUES(?, ?, 1, ?, ?)
            ON CONFLICT(msg_id) DO UPDATE SET
                reason     = excluded.reason,
                attempts   = failures.attempts + 1,
                last_error = excluded.last_error
            """,
            (msg_id, reason[:500], now, now),
        )

    def clear_failure(self, msg_id: str) -> None:
        """Drop a message from the retry list after it succeeds."""
        self.conn.execute("DELETE FROM failures WHERE msg_id = ?", (msg_id,))

    def list_failures(self) -> List[Dict[str, Any]]:
        """Return every message that failed to download and is pending retry."""
        rows = self.conn.execute(
            "SELECT msg_id, reason, attempts, first_error, last_error FROM failures "
            "ORDER BY attempts DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    # --------------------------------------------------------------- stats

    def stats(self) -> Dict[str, int]:
        """Return archive counters for the ``status`` command."""
        row = self.conn.execute(
            "SELECT COUNT(*) AS total, COALESCE(SUM(size), 0) AS total_size, "
            "COALESCE(SUM(vanished_at IS NOT NULL), 0) AS vanished FROM messages"
        ).fetchone()
        failures = self.conn.execute("SELECT COUNT(*) AS n FROM failures").fetchone()['n']
        return {
            'total_emails': row['total'],
            'total_size': row['total_size'],
            'vanished': row['vanished'],
            'failures': failures,
        }


def migrate_from_json(json_path: Union[str, Path], store: ArchiveStore) -> int:
    """Import a legacy ``backup_state.json`` into the SQLite index.

    The old format carried no content hash, so ``sha256`` is left empty for
    imported rows; ``rebuild_from_metadata`` or a ``--verify-hashes`` pass fills
    them in. Dedup works regardless, since it keys on ``msg_id``.

    Args:
        json_path: Path to the legacy state file.
        store: The destination index.

    Returns:
        The number of messages imported.
    """
    json_path = Path(json_path)
    try:
        with open(json_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Could not read legacy state %s: %s", json_path, e)
        return 0

    if not isinstance(data, dict):
        return 0

    emails = data.get('emails') or {}
    backed_up = set(data.get('backed_up_message_ids') or [])
    imported = 0
    now = _utcnow()

    for msg_id, meta in emails.items():
        if not isinstance(meta, dict):
            continue
        internal_date = meta.get('internal_date')
        internal_ms: Optional[int] = None
        if internal_date:
            try:
                internal_ms = int(datetime.fromisoformat(internal_date).timestamp() * 1000)
            except (TypeError, ValueError):
                internal_ms = None
        store.conn.execute(
            """
            INSERT INTO messages
                (msg_id, thread_id, internal_date, size, sha256, rel_path,
                 labels, first_seen, last_seen, vanished_at)
            VALUES (?, ?, ?, ?, '', ?, ?, ?, ?, NULL)
            ON CONFLICT(msg_id) DO NOTHING
            """,
            (
                msg_id,
                meta.get('thread_id'),
                internal_ms,
                meta.get('size') or 0,
                meta.get('backup_path') or '',
                json.dumps(meta.get('labels') or []),
                now,
                now,
            ),
        )
        imported += 1

    # Ids present only in the flat list still count as archived; without a path
    # they cannot be verified on disk, so they get an empty rel_path and will be
    # re-downloaded by the self-heal check.
    for msg_id in backed_up - set(emails):
        store.conn.execute(
            "INSERT INTO messages"
            "(msg_id, thread_id, internal_date, size, sha256, rel_path, labels,"
            " first_seen, last_seen, vanished_at)"
            " VALUES(?, NULL, NULL, 0, '', '', '[]', ?, ?, NULL)"
            " ON CONFLICT(msg_id) DO NOTHING",
            (msg_id, now, now),
        )
        imported += 1

    store.commit()
    logger.info("Migrated %d messages from %s", imported, json_path)
    return imported


def rebuild_from_metadata(backup_dir: Union[str, Path], store: ArchiveStore) -> int:
    """Rebuild the index by walking the archive's metadata sidecars.

    This is the recovery path that keeps the database from being a single point
    of failure: every column is re-derived from ``metadata/*.json`` plus the
    ``.eml`` bytes themselves.

    Args:
        backup_dir: Root of the archive.
        store: The index to populate.

    Returns:
        The number of messages indexed.
    """
    backup_dir = Path(backup_dir)
    metadata_dir = backup_dir / 'metadata'
    if not metadata_dir.is_dir():
        raise ValueError(f"No metadata directory at {metadata_dir}")

    indexed = 0
    for meta_file in sorted(metadata_dir.glob('*.json')):
        try:
            with open(meta_file, 'r', encoding='utf-8') as f:
                meta = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("Skipping unreadable metadata %s: %s", meta_file, e)
            continue

        rel_path = meta.get('backup_path') or ''
        eml_path = backup_dir / rel_path if rel_path else None
        if not eml_path or not eml_path.is_file():
            logger.warning("Metadata %s references missing .eml %s", meta_file.name, rel_path)
            continue

        raw = eml_path.read_bytes()
        internal_date = meta.get('internal_date')
        try:
            internal_ms = int(internal_date) if internal_date is not None else None
        except (TypeError, ValueError):
            internal_ms = None

        store.record(
            msg_id=meta.get('message_id') or meta_file.stem,
            thread_id=meta.get('thread_id'),
            internal_date=internal_ms,
            size=len(raw),
            sha256=get_email_hash(raw),
            rel_path=rel_path,
            labels=meta.get('labels') or [],
        )
        indexed += 1
        if indexed % 500 == 0:
            store.commit()
            logger.info("Rebuilt %d entries...", indexed)

    store.commit()
    logger.info("Rebuilt index with %d messages", indexed)
    return indexed
