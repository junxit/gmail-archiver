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
from typing import Dict, List, Optional, Tuple, Union

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

    def __init__(
        self,
        mailbox,
        backup_dir: Union[str, Path],
        state_file: Union[str, Path],
        folder: str = DEFAULT_FOLDER,
        batch_size: int = 100,
    ) -> None:
        """Initialize the IMAP backup.

        Args:
            mailbox: An authenticated ``imap_tools.MailBox`` (see
                :func:`gmail_archiver.auth.get_imap_credentials`).
            backup_dir: Directory to store the backup files.
            state_file: Path to the backup state file.
            folder: IMAP folder to back up. Defaults to ``'[Gmail]/All Mail'`` so
                all archived mail is captured, not just the inbox.
            batch_size: Save backup state every this many newly saved messages.
        """
        super().__init__(
            gmail_service=None,
            backup_dir=backup_dir,
            state_file=state_file,
            batch_size=batch_size,
        )
        self.mailbox = mailbox
        self.folder = folder

    def _sweep_message_ids(self) -> List[Tuple[str, Optional[str]]]:
        """Cheaply list ``(uid, X-GM-MSGID)`` for every message in the folder.

        Fetching only ``X-GM-MSGID`` (no bodies) lets re-runs skip
        already-backed-up messages without downloading them.

        Returns:
            A list of ``(uid, gm_msgid)`` tuples; ``gm_msgid`` is None if the
            server did not return one. Empty list for an empty mailbox.
        """
        typ, data = self.mailbox.client.uid('FETCH', '1:*', '(X-GM-MSGID)')
        if typ != 'OK' or not data:
            return []
        entries: List[Tuple[str, Optional[str]]] = []
        for item in data:
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

    def _download_and_save(self, uid: str, gm_msgid_hint: Optional[str]) -> Optional[bool]:
        """Download one message by UID and write it in the shared format.

        Args:
            uid: The IMAP UID to fetch.
            gm_msgid_hint: X-GM-MSGID from the cheap sweep, if known.

        Returns:
            True if saved, False on error, None if it was already backed up
            (determined only after resolving the final id).
        """
        typ, data = self.mailbox.client.uid('FETCH', uid, _FETCH_PARTS)
        if typ != 'OK' or not data:
            logger.warning("FETCH failed for UID %s: %s", uid, typ)
            return False

        attrs, raw_bytes = _parse_fetch_response(data)
        if not raw_bytes:
            logger.warning("No message body returned for UID %s", uid)
            return False

        # Resolve the stable, cross-run id: prefer X-GM-MSGID, else Message-ID.
        gm_msgid = _search_int(attrs, b'X-GM-MSGID')
        if gm_msgid is not None:
            msg_id: Optional[str] = str(gm_msgid)
        elif gm_msgid_hint:
            msg_id = gm_msgid_hint
        else:
            msg_id = _message_id_fallback(raw_bytes)
        if not msg_id:
            logger.warning("UID %s has no X-GM-MSGID and no Message-ID; skipping", uid)
            return False

        if self.state.is_email_backed_up(msg_id):
            logger.debug("Skipping already backed up message: %s", msg_id)
            return None

        thrid = _search_int(attrs, b'X-GM-THRID')
        thread_id = str(thrid) if thrid is not None else ''
        internal_date_ms = _internal_date_ms(attrs, raw_bytes)
        labels = normalize_labels(_parse_gm_labels(attrs), _is_seen(attrs))

        email_path = self._write_email(
            raw_bytes=raw_bytes,
            msg_id=msg_id,
            thread_id=thread_id,
            internal_date_ms=internal_date_ms,
            labels=labels,
        )
        if not email_path:
            return False

        self._record_backup(email_path, msg_id, thread_id, internal_date_ms, labels)
        return True

    def backup_emails(self, max_results: Optional[int] = None) -> Dict[str, int]:
        """Back up emails from the configured IMAP folder.

        Args:
            max_results: Maximum number of new emails to save. If None, save all.

        Returns:
            A statistics dict with the same shape as
            :meth:`gmail_archiver.backup.GmailBackup.backup_emails`.
        """
        logger.info(
            "Starting Gmail IMAP backup to: %s (folder: %r)", self.backup_dir, self.folder
        )

        def _result(processed: int, errors: int) -> Dict[str, int]:
            return {
                'total_processed': processed,
                'total_errors': errors,
                'emails_processed': processed,
                'last_backup_time': self.state.last_backup_time.isoformat()
                if self.state.last_backup_time else None,
            }

        try:
            # Select the folder read-only so the backup never mutates the mailbox.
            try:
                self.mailbox.folder.set(self.folder, readonly=True)
            except MailboxFolderSelectError as e:
                logger.error("Could not select IMAP folder %r: %s", self.folder, e)
                raise

            entries = self._sweep_message_ids()
            if not entries:
                logger.info("No messages found in folder %r.", self.folder)
                return _result(0, 0)

            logger.info("Found %d messages in %r", len(entries), self.folder)

            total_processed = 0
            total_errors = 0

            for uid, gm_msgid in entries:
                # Cheap skip: already backed up, no body download needed.
                if gm_msgid and self.state.is_email_backed_up(gm_msgid):
                    logger.debug("Skipping already backed up message (X-GM-MSGID %s)", gm_msgid)
                    continue

                try:
                    outcome = self._download_and_save(uid, gm_msgid)
                except Exception as e:
                    logger.error("Error processing IMAP message UID %s: %s", uid, e)
                    outcome = False

                if outcome is True:
                    total_processed += 1
                    if total_processed % 10 == 0:
                        logger.info("Processed %d emails...", total_processed)
                    if total_processed % self.batch_size == 0:
                        self.state.last_backup_time = datetime.now(timezone.utc)
                        self.state.last_backup_count = total_processed
                        self._save_state()
                elif outcome is False:
                    total_errors += 1
                # outcome is None -> already backed up; not counted.

                if max_results and total_processed >= max_results:
                    logger.info(
                        "Reached maximum number of emails to process (%d)", max_results
                    )
                    break

            # Final state flush.
            self.state.last_backup_time = datetime.now(timezone.utc)
            self.state.last_backup_count = total_processed
            self._save_state()

            logger.info(
                "IMAP backup completed. Total processed: %d, errors: %d, total size: %s",
                total_processed, total_errors, format_size(self.state.total_backup_size),
            )
            return _result(total_processed, total_errors)

        except Exception as e:
            logger.error("Error during IMAP backup: %s", e, exc_info=True)
            raise
