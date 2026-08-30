"""IMAP (app-password) backup functionality for Gmail Archiver.

This backend downloads mail over IMAP using a Gmail **app password** (no OAuth)
and writes it in the **exact same on-disk format** the OAuth/API backend
(:class:`gmail_archiver.backup.GmailBackup`) produces, so an IMAP-made backup is
indistinguishable on disk from an API-made one and restorable by the existing
restore.

It reuses ``GmailBackup``'s shared writer/state helpers (``_write_email`` /
``_record_backup`` / ``BackupState``) to guarantee the two backends can't drift
in format.

Why this drops to low-level imaplib: ``imap-tools`` does not expose Gmail's IMAP
extensions (``X-GM-MSGID``, ``X-GM-THRID``, ``X-GM-LABELS``), the server
``INTERNALDATE``, or byte-for-byte raw bytes (``MailMessage.obj.as_bytes()`` is
re-serialized). All of those are required to match the API format, so this module
issues a single ``UID FETCH`` against the underlying imaplib connection
(``mailbox.client``) per message, requesting
``(X-GM-MSGID X-GM-THRID X-GM-LABELS INTERNALDATE FLAGS BODY.PEEK[])``.

Label preservation caveats (unavoidable IMAP differences vs the API path):
    * System labels are normalized to the same IDs the Gmail API uses
      (``\\Inbox`` -> ``INBOX``, ``\\Sent`` -> ``SENT``, ``\\Important`` ->
      ``IMPORTANT``, ``\\Starred`` -> ``STARRED``, ``\\Draft`` -> ``DRAFT``,
      ``\\Trash`` -> ``TRASH``, ``\\Junk``/``\\Spam`` -> ``SPAM``).
    * ``UNREAD`` is synthesized from the IMAP ``\\Seen`` flag to mirror the API's
      ``UNREAD`` label (IMAP read state is a flag, not a label).
    * User labels are stored by **display name** because IMAP exposes names, not
      the API's internal ``Label_NNN`` ids. Consequently, restoring user labels
      is best-effort: Gmail's import expects label ids, so user-labeled messages
      may not have those labels re-applied (system labels and message content
      restore as usual).
    * Gmail categories (``CATEGORY_*``) are not exposed via ``X-GM-LABELS`` and so
      are absent.
"""
import email
import email.utils
import imaplib
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from imap_tools import MailboxFolderSelectError

from .backup import GmailBackup
from .utils import format_size, parse_email_message

try:  # imap-tools ships an IMAP modified-UTF-7 codec used for folder/label names
    from imap_tools.imap_utf7 import utf7_decode
except Exception:  # pragma: no cover - defensive fallback only
    def utf7_decode(value: bytes) -> str:
        """Fallback decoder if imap-tools' modified-UTF-7 codec is unavailable."""
        return value.decode('utf-8', errors='replace')

logger = logging.getLogger(__name__)

# Default Gmail folder that holds every archived message. The exact name varies
# by account language, so it is configurable via ImapBackup(folder=...).
DEFAULT_FOLDER = '[Gmail]/All Mail'

# IMAP FETCH items: Gmail permanent id + thread id + labels + server internal
# date + flags + the raw message body (PEEK so we never set the \Seen flag).
_FETCH_PARTS = '(X-GM-MSGID X-GM-THRID X-GM-LABELS INTERNALDATE FLAGS BODY.PEEK[])'

# Map Gmail's IMAP system-label tokens to the label ids the Gmail API uses, so
# IMAP backups share the API path's label vocabulary.
_SYSTEM_LABEL_MAP = {
    '\\Inbox': 'INBOX',
    '\\Sent': 'SENT',
    '\\Draft': 'DRAFT',
    '\\Drafts': 'DRAFT',
    '\\Important': 'IMPORTANT',
    '\\Starred': 'STARRED',
    '\\Trash': 'TRASH',
    '\\Junk': 'SPAM',
    '\\Spam': 'SPAM',
}


def _parse_fetch_response(data: list) -> Tuple[bytes, Optional[bytes]]:
    """Split a single-message ``UID FETCH`` response into attributes and body.

    imaplib returns a literal (the ``BODY[]`` payload) as the second element of a
    ``(prefix, payload)`` tuple, with the requested attributes appearing in the
    ``prefix`` (and possibly trailing standalone byte strings). This collects all
    attribute byte fragments and the raw body.

    Args:
        data: The ``data`` list returned by ``imaplib.IMAP4.uid('FETCH', ...)``.

    Returns:
        A tuple of ``(attrs_bytes, raw_bytes)``; ``raw_bytes`` is None if the
        response contained no message body.
    """
    raw_bytes: Optional[bytes] = None
    attrs_parts: List[bytes] = []
    for item in data:
        if isinstance(item, tuple) and len(item) == 2:
            prefix, payload = item
            if isinstance(prefix, (bytes, bytearray)):
                attrs_parts.append(bytes(prefix))
            if isinstance(payload, (bytes, bytearray)):
                raw_bytes = bytes(payload)
        elif isinstance(item, (bytes, bytearray)):
            attrs_parts.append(bytes(item))
    return b' '.join(p for p in attrs_parts if p), raw_bytes


def _search_int(attrs: bytes, key: bytes) -> Optional[int]:
    """Return the integer value of a numeric FETCH attribute (e.g. X-GM-MSGID)."""
    match = re.search(re.escape(key) + rb'\s+(\d+)', attrs)
    return int(match.group(1)) if match else None


def _is_seen(attrs: bytes) -> bool:
    """Return True if the message carries the IMAP ``\\Seen`` flag."""
    match = re.search(rb'FLAGS\s+\(([^)]*)\)', attrs)
    return bool(match) and rb'\Seen' in match.group(1)


def _extract_labels_blob(attrs: bytes) -> Optional[bytes]:
    """Extract the raw bytes inside ``X-GM-LABELS ( ... )``, quote-aware.

    Scans for the matching closing parenthesis while ignoring parentheses that
    appear inside quoted label names.

    Args:
        attrs: The attribute bytes from a FETCH response.

    Returns:
        The bytes between the outer parentheses, or None if absent/malformed.
    """
    marker = b'X-GM-LABELS '
    idx = attrs.find(marker)
    if idx == -1:
        return None
    i = idx + len(marker)
    while i < len(attrs) and attrs[i:i + 1] != b'(':
        i += 1
    if i >= len(attrs):
        return None
    start = i + 1
    depth = 1
    in_quote = False
    escaped = False
    i = start
    while i < len(attrs):
        c = attrs[i:i + 1]
        if escaped:
            escaped = False
        elif in_quote and c == b'\\':
            escaped = True
        elif c == b'"':
            in_quote = not in_quote
        elif not in_quote:
            if c == b'(':
                depth += 1
            elif c == b')':
                depth -= 1
                if depth == 0:
                    return attrs[start:i]
        i += 1
    return None


def _tokenize_labels(blob: bytes) -> List[bytes]:
    """Tokenize an ``X-GM-LABELS`` blob into individual label byte strings.

    Handles space-separated atoms (e.g. ``\\Inbox``) and quoted strings (which
    may contain spaces and ``\\``-escaped characters).
    """
    tokens: List[bytes] = []
    i = 0
    n = len(blob)
    while i < n:
        c = blob[i:i + 1]
        if c in (b' ', b'\t'):
            i += 1
            continue
        if c == b'"':
            i += 1
            buf = bytearray()
            while i < n:
                ch = blob[i:i + 1]
                if ch == b'\\' and i + 1 < n:
                    buf += blob[i + 1:i + 2]
                    i += 2
                elif ch == b'"':
                    i += 1
                    break
                else:
                    buf += ch
                    i += 1
            tokens.append(bytes(buf))
        else:
            buf = bytearray()
            while i < n and blob[i:i + 1] not in (b' ', b'\t'):
                buf += blob[i:i + 1]
                i += 1
            tokens.append(bytes(buf))
    return tokens


def _parse_gm_labels(attrs: bytes) -> List[str]:
    """Parse ``X-GM-LABELS`` from FETCH attributes into decoded label strings."""
    blob = _extract_labels_blob(attrs)
    if blob is None:
        return []
    labels: List[str] = []
    for token in _tokenize_labels(blob):
        try:
            labels.append(utf7_decode(token))
        except Exception:
            labels.append(token.decode('utf-8', errors='replace'))
    return labels


def normalize_labels(raw_labels: List[str], seen: bool) -> List[str]:
    """Normalize IMAP label tokens to the Gmail API's label vocabulary.

    Args:
        raw_labels: Decoded ``X-GM-LABELS`` tokens (e.g. ``\\Inbox``, ``Work``).
        seen: Whether the message has the IMAP ``\\Seen`` flag.

    Returns:
        A de-duplicated, order-preserving list of labels: system tokens mapped to
        API ids, user labels kept by name, plus a synthesized ``UNREAD`` when the
        message is unseen (mirroring the API path).
    """
    labels: List[str] = []
    for token in raw_labels:
        if token in _SYSTEM_LABEL_MAP:
            value = _SYSTEM_LABEL_MAP[token]
        elif token.startswith('\\'):
            # Unknown system token (e.g. \Muted) — keep a readable form.
            value = token.lstrip('\\')
        else:
            value = token
        if value and value not in labels:
            labels.append(value)
    if not seen and 'UNREAD' not in labels:
        labels.append('UNREAD')
    return labels


def _internal_date_ms(attrs: bytes, raw_bytes: bytes) -> str:
    """Derive a Unix-millisecond timestamp string for a message.

    Prefers the IMAP ``INTERNALDATE`` (closest analogue to the API's
    ``internalDate``); falls back to the RFC822 ``Date`` header, then to ``'0'``.
    """
    dt_tuple = imaplib.Internaldate2tuple(attrs)
    if dt_tuple is not None:
        try:
            return str(int(time.mktime(dt_tuple) * 1000))
        except (OverflowError, ValueError):
            pass
    try:
        date_hdr = parse_email_message(raw_bytes).get('Date')
        if date_hdr:
            dt = email.utils.parsedate_to_datetime(str(date_hdr))
            if dt is not None:
                return str(int(dt.timestamp() * 1000))
    except Exception:
        pass
    return '0'


def _message_id_fallback(raw_bytes: bytes) -> Optional[str]:
    """Return the RFC822 ``Message-ID`` header, used when X-GM-MSGID is absent."""
    try:
        msg = parse_email_message(raw_bytes)
        mid = msg.get('Message-ID') or msg.get('Message-Id')
        if mid:
            return str(mid).strip()
    except Exception:
        pass
    return None


class ImapBackup(GmailBackup):
    """Back up Gmail over IMAP into the same on-disk format as the API backend.

    Subclasses :class:`gmail_archiver.backup.GmailBackup` purely to reuse its
    directory layout, ``BackupState`` handling, and the shared ``_write_email`` /
    ``_record_backup`` writers. Only :meth:`backup_emails` is overridden; the
    inherited Gmail API methods are unused (``self.gmail`` is None).
    """

    # Number of UIDs per cheap X-GM-MSGID fetch. A single un-chunked
    # ``UID FETCH 1:*`` over a large All Mail folder returns one enormous
    # response, which balloons memory and can trip imaplib's line-length ceiling.
    SWEEP_CHUNK = 2000

    def __init__(
        self,
        mailbox,
        backup_dir: Union[str, Path],
        db_path: Optional[Union[str, Path]] = None,
        folder: str = DEFAULT_FOLDER,
        batch_size: int = 100,
        verify_existing: bool = True,
        reconnect=None,
    ) -> None:
        """Initialize the IMAP backup.

        Args:
            mailbox: An authenticated ``imap_tools.MailBox`` (see
                :func:`gmail_archiver.auth.get_imap_credentials`).
            backup_dir: Directory to store the backup files.
            db_path: Path to the SQLite index. Defaults to ``index.db`` inside
                ``backup_dir``.
            folder: IMAP folder to back up. Defaults to ``'[Gmail]/All Mail'`` so
                all archived mail is captured, not just the inbox.
            batch_size: Commit the index every this many newly saved messages.
            verify_existing: Stat already-indexed messages before skipping them.
            reconnect: Optional zero-argument callable returning a fresh
                authenticated mailbox, used to resume after the server drops the
                connection mid-run. Without it, a dropped connection ends the run
                with an error rather than silently reporting success.
        """
        super().__init__(
            gmail_service=None,
            backup_dir=backup_dir,
            db_path=db_path,
            batch_size=batch_size,
            verify_existing=verify_existing,
        )
        self.mailbox = mailbox
        self.folder = folder
        self._reconnect = reconnect

    def _select_folder(self) -> None:
        """Select the configured folder read-only so backup never mutates mail."""
        try:
            self.mailbox.folder.set(self.folder, readonly=True)
        except MailboxFolderSelectError as e:
            logger.error("Could not select IMAP folder %r: %s", self.folder, e)
            raise

    def _reconnect_mailbox(self) -> bool:
        """Re-establish a dropped connection and re-select the folder.

        Returns:
            True if the connection was restored, False if no reconnect callable
            was supplied or the attempt failed.
        """
        if self._reconnect is None:
            return False
        try:
            self.mailbox = self._reconnect()
            self._select_folder()
            logger.info("Reconnected to IMAP server; resuming.")
            return True
        except Exception as e:
            logger.error("Reconnect failed: %s", e)
            return False

    def _record_uidvalidity(self) -> None:
        """Record the folder's UIDVALIDITY.

        Dedup keys on ``X-GM-MSGID``, not UID, so a change here is harmless — but
        it invalidates any UID the archive has seen before and is worth noting.
        """
        try:
            raw = self.mailbox.client.untagged_responses.get('UIDVALIDITY')
            current = raw[0].decode() if raw else None
        except Exception:  # pragma: no cover - server/library dependent
            current = None
        if not current:
            return
        previous = self.store.get_meta('uidvalidity')
        if previous and previous != current:
            logger.warning(
                "IMAP UIDVALIDITY changed (%s -> %s); UIDs were reassigned by the "
                "server. Dedup is unaffected (it keys on X-GM-MSGID).",
                previous, current,
            )
        self.store.set_meta('uidvalidity', current)

    def _sweep_message_ids(self) -> List[Tuple[str, Optional[str]]]:
        """Cheaply list ``(uid, X-GM-MSGID)`` for every message in the folder.

        Fetching only ``X-GM-MSGID`` (no bodies) lets re-runs skip
        already-backed-up messages without downloading them. The UID list is
        gathered first with ``UID SEARCH ALL`` and then fetched in chunks of
        ``SWEEP_CHUNK`` so the response size stays bounded regardless of mailbox
        size.

        Returns:
            A list of ``(uid, gm_msgid)`` tuples; ``gm_msgid`` is None if the
            server did not return one. Empty list for an empty mailbox.
        """
        typ, data = self.mailbox.client.uid('SEARCH', None, 'ALL')
        if typ != 'OK' or not data:
            return []
        uids = (data[0] or b'').split()
        if not uids:
            return []

        entries: List[Tuple[str, Optional[str]]] = []
        for start in range(0, len(uids), self.SWEEP_CHUNK):
            chunk = uids[start:start + self.SWEEP_CHUNK]
            typ, resp = self.mailbox.client.uid(
                'FETCH', b','.join(chunk).decode(), '(X-GM-MSGID)'
            )
            if typ != 'OK' or not resp:
                logger.warning(
                    "Sweep chunk %d-%d failed (%s); those messages are not "
                    "enumerated this run.", start, start + len(chunk), typ
                )
                raise IOError(f"UID FETCH failed during sweep: {typ}")
            for item in resp:
                line = item[0] if isinstance(item, tuple) else item
                if not isinstance(line, (bytes, bytearray)) or not line:
                    continue
                uid_match = re.search(rb'\bUID\s+(\d+)', line)
                if not uid_match:
                    continue
                gm_match = re.search(rb'X-GM-MSGID\s+(\d+)', line)
                entries.append((
                    uid_match.group(1).decode(),
                    gm_match.group(1).decode() if gm_match else None,
                ))
        return entries

    def _download_and_save(
        self, uid: str, gm_msgid_hint: Optional[str]
    ) -> Tuple[Optional[bool], Optional[str]]:
        """Download one message by UID and write it in the shared format.

        Args:
            uid: The IMAP UID to fetch.
            gm_msgid_hint: X-GM-MSGID from the cheap sweep, if known.

        Returns:
            A ``(outcome, msg_id)`` tuple. ``outcome`` is True if saved, False on
            error, or None if it was already archived (determined only after
            resolving the final id). ``msg_id`` is the resolved identifier, or
            None if it could not be determined.
        """
        typ, data = self.mailbox.client.uid('FETCH', uid, _FETCH_PARTS)
        if typ != 'OK' or not data:
            logger.warning("FETCH failed for UID %s: %s", uid, typ)
            return False, None

        attrs, raw_bytes = _parse_fetch_response(data)
        if not raw_bytes:
            logger.warning("No message body returned for UID %s", uid)
            return False, None

        # Resolve the stable, cross-run id: prefer X-GM-MSGID, else Message-ID.
        gm_msgid = _search_int(attrs, b'X-GM-MSGID')
        if gm_msgid is not None:
            msg_id: Optional[str] = str(gm_msgid)
        elif gm_msgid_hint:
            msg_id = gm_msgid_hint
        else:
            # Sender-controlled; safe_key() sanitizes it before it reaches a path.
            msg_id = _message_id_fallback(raw_bytes)
        if not msg_id:
            logger.warning("UID %s has no X-GM-MSGID and no Message-ID; skipping", uid)
            return False, None

        if self.store.is_archived(msg_id) and self._is_intact(msg_id):
            logger.debug("Skipping already backed up message: %s", msg_id)
            return None, msg_id

        thrid = _search_int(attrs, b'X-GM-THRID')
        thread_id = str(thrid) if thrid is not None else ''
        internal_date_ms = _internal_date_ms(attrs, raw_bytes)
        labels = normalize_labels(_parse_gm_labels(attrs), _is_seen(attrs))

        if not self._archive(
            raw_bytes=raw_bytes,
            msg_id=msg_id,
            thread_id=thread_id,
            internal_date_ms=internal_date_ms,
            labels=labels,
        ):
            return False, msg_id

        return True, msg_id

    def backup_emails(self, max_results: Optional[int] = None) -> Dict[str, Any]:
        """Back up emails from the configured IMAP folder.

        Sweeps the whole folder every run and skips what the index already has,
        so an interrupted run costs only the messages it had not reached — there
        is no watermark that can advance past undownloaded mail.

        Args:
            max_results: Maximum number of new emails to save. If None, save all.

        Returns:
            A statistics dict with the same shape as
            :meth:`gmail_archiver.backup.GmailBackup.backup_emails`.
        """
        logger.info(
            "Starting Gmail IMAP backup to: %s (folder: %r)", self.backup_dir, self.folder
        )
        sweep_started_at = datetime.now(timezone.utc).isoformat()

        def _result(processed: int, errors: int, skipped: int,
                    tombstoned: int = 0) -> Dict[str, Any]:
            return {
                'total_processed': processed,
                'total_errors': errors,
                'total_skipped': skipped,
                'emails_processed': processed,
                'tombstoned': tombstoned,
                'archived_total': self.store.stats()['total_emails'],
            }

        try:
            # Select the folder read-only so the backup never mutates the mailbox.
            self._select_folder()
            self._record_uidvalidity()

            entries = self._sweep_message_ids()
            if not entries:
                logger.info("No messages found in folder %r.", self.folder)
                return _result(0, 0, 0)

            logger.info("Found %d messages in %r", len(entries), self.folder)

            total_processed = 0
            total_errors = 0
            total_skipped = 0
            completed = True
            seen_batch: List[str] = []

            for uid, gm_msgid in entries:
                # Cheap skip: already archived and intact, no body download needed.
                if gm_msgid and self.store.is_archived(gm_msgid) and self._is_intact(gm_msgid):
                    logger.debug("Skipping already backed up message (X-GM-MSGID %s)", gm_msgid)
                    total_skipped += 1
                    seen_batch.append(gm_msgid)
                else:
                    try:
                        outcome, msg_id = self._download_and_save(uid, gm_msgid)
                    except (imaplib.IMAP4.abort, OSError) as e:
                        # The connection dropped. Without a reconnect every
                        # remaining message would "fail" and the run would still
                        # report success, so recover or stop.
                        logger.warning("IMAP connection lost on UID %s: %s", uid, e)
                        if not self._reconnect_mailbox():
                            self.store.commit()
                            raise
                        try:
                            outcome, msg_id = self._download_and_save(uid, gm_msgid)
                        except Exception as retry_error:
                            logger.error(
                                "Error processing IMAP message UID %s after reconnect: %s",
                                uid, retry_error,
                            )
                            outcome, msg_id = False, gm_msgid
                    except Exception as e:
                        logger.error("Error processing IMAP message UID %s: %s", uid, e)
                        outcome, msg_id = False, gm_msgid

                    if msg_id:
                        seen_batch.append(msg_id)

                    if outcome is True:
                        total_processed += 1
                        if total_processed % 10 == 0:
                            logger.info("Processed %d emails...", total_processed)
                    elif outcome is False:
                        total_errors += 1
                        if msg_id:
                            self.store.record_failure(msg_id, f"IMAP fetch/write failed (UID {uid})")
                    else:
                        total_skipped += 1

                if len(seen_batch) >= self.batch_size:
                    self.store.mark_seen(seen_batch)
                    self.store.commit()
                    seen_batch.clear()

                if max_results and total_processed >= max_results:
                    logger.info(
                        "Reached maximum number of emails to process (%d)", max_results
                    )
                    completed = False
                    break

            if seen_batch:
                self.store.mark_seen(seen_batch)
            self.store.commit()

            tombstoned = self._finish_sweep(sweep_started_at, completed, total_errors)
            stats = self.store.stats()
            self.store.commit()

            logger.info(
                "IMAP backup completed. New: %d, skipped: %d, errors: %d, vanished: %d, "
                "archive size: %s",
                total_processed, total_skipped, total_errors, tombstoned,
                format_size(stats['total_size']),
            )
            return _result(total_processed, total_errors, total_skipped, tombstoned)

        except Exception as e:
            logger.error("Error during IMAP backup: %s", e, exc_info=True)
            raise
