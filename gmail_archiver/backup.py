"""Backup functionality for Gmail Archiver."""
import base64
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from googleapiclient.errors import HttpError

from .store import ArchiveStore, DEFAULT_DB_NAME, migrate_from_json
from .utils import (
    build_email_filename,
    ensure_directory_exists,
    ensure_within,
    format_size,
    get_email_hash,
    parse_email_message,
    safe_key,
    write_atomic,
)

logger = logging.getLogger(__name__)

# Passed to googleapiclient's execute(); it applies exponential backoff with
# jitter to 429/500/503, which is exactly the class of error that used to be
# recorded as a permanent failure.
API_NUM_RETRIES = 5


class GmailBackup:
    """Class to handle Gmail backup operations."""

    def __init__(
        self,
        gmail_service: Any,
        backup_dir: Union[str, Path],
        db_path: Optional[Union[str, Path]] = None,
        batch_size: int = 100,
        verify_existing: bool = True,
    ) -> None:
        """Initialize the GmailBackup instance.

        Args:
            gmail_service: Authenticated Gmail API service instance.
            backup_dir: Directory to store the backup files.
            db_path: Path to the SQLite index. Defaults to ``index.db`` inside
                ``backup_dir``.
            batch_size: Number of emails to process between index commits.
            verify_existing: When True, stat each already-indexed message before
                skipping it and re-download if the file is missing or the wrong
                size. This is what lets the archive heal after a truncated write.
        """
        self.gmail = gmail_service
        self.backup_dir = Path(backup_dir).expanduser().resolve()
        self.batch_size = batch_size
        self.verify_existing = verify_existing

        # Ensure backup directory exists
        self.backup_dir.mkdir(parents=True, exist_ok=True)

        # Create necessary directories
        self.emails_dir = self.backup_dir / 'emails'
        self.metadata_dir = self.backup_dir / 'metadata'
        self.emails_dir.mkdir(parents=True, exist_ok=True)
        self.metadata_dir.mkdir(parents=True, exist_ok=True)

        self.db_path = Path(db_path) if db_path else self.backup_dir / DEFAULT_DB_NAME
        is_new_index = not self.db_path.exists()
        self.store = ArchiveStore(self.db_path)

        # One-time import of the pre-SQLite state file, if this archive predates it.
        if is_new_index:
            legacy = self.backup_dir / 'backup_state.json'
            if legacy.is_file():
                logger.info("Migrating legacy state file %s into %s", legacy, self.db_path)
                migrate_from_json(legacy, self.store)
                legacy.rename(legacy.with_suffix('.json.migrated'))

    def close(self) -> None:
        """Commit and close the underlying index."""
        self.store.close()

    def __enter__(self) -> 'GmailBackup':
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def _get_email(self, msg_id: str) -> Optional[Dict]:
        """Get a single email by message ID.

        Args:
            msg_id: The message ID to retrieve.

        Returns:
            The email message as a dictionary, or None if not found.
        """
        try:
            message = self.gmail.users().messages().get(
                userId='me',
                id=msg_id,
                format='raw'
            ).execute(num_retries=API_NUM_RETRIES)
            return message
        except HttpError as e:
            if e.resp.status == 404:  # Not found
                logger.warning("Message %s not found: %s", msg_id, e)
            else:
                logger.error("Error retrieving message %s: %s", msg_id, e)
            return None

    def _save_email(self, email_data: Dict, labels: List[str]) -> Optional[Tuple[Path, str]]:
        """Save an email from the Gmail API to disk.

        Decodes the API's base64 ``raw`` payload and delegates the actual write
        to :meth:`_write_email`, the shared writer used by both the API and IMAP
        backup paths.

        Args:
            email_data: The email data from the Gmail API.
            labels: List of labels for the email.

        Returns:
            A ``(path, sha256)`` tuple, or None if saving failed.
        """
        try:
            # Decode the raw email data
            msg_str = base64.urlsafe_b64decode(email_data['raw'].encode('ASCII'))

            return self._write_email(
                raw_bytes=msg_str,
                msg_id=email_data['id'],
                thread_id=email_data.get('threadId'),
                internal_date_ms=email_data.get('internalDate'),
                labels=labels,
            )
        except Exception as e:
            logger.error("Error saving email %s: %s", email_data.get('id', 'unknown'), e)
            return None

    def _write_email(
        self,
        raw_bytes: bytes,
        msg_id: str,
        thread_id: Optional[str],
        internal_date_ms: Optional[str],
        labels: List[str],
    ) -> Optional[Tuple[Path, str]]:
        """Write raw RFC822 bytes to disk as ``.eml`` plus a metadata sidecar.

        This is the shared, transport-agnostic writer used by both the Gmail API
        backup path (via :meth:`_save_email`) and the IMAP backup path, so the
        two backends always produce an identical on-disk format.

        Both files are written atomically (temp file, fsync, rename), so an
        interrupted run can never leave a truncated ``.eml`` behind. The message
        id is sanitized before it reaches a path and the result is checked for
        containment, because on the IMAP path the id can fall back to a
        sender-controlled ``Message-ID`` header.

        Args:
            raw_bytes: The raw RFC822 message bytes.
            msg_id: Stable message identifier used as the on-disk key (the Gmail
                API message id for the API path; X-GM-MSGID or the RFC822
                Message-ID for the IMAP path).
            thread_id: Thread identifier, or None.
            internal_date_ms: Message internal date as a Unix-millisecond string,
                used both for the ``YYYY/MM`` directory and the ``internal_date``
                metadata field.
            labels: Labels to record in the metadata sidecar.

        Returns:
            A ``(path, sha256)`` tuple, or None if writing failed.
        """
        try:
            # Parse the email to get header metadata
            msg = parse_email_message(raw_bytes)

            email_hash = get_email_hash(raw_bytes)
            # Never let an untrusted id become a path component.
            key = safe_key(msg_id, fallback=email_hash[:32])
            filename = build_email_filename(key, email_hash, msg.get('subject'))

            # Bucket by date in UTC so the layout does not depend on the
            # machine's timezone.
            try:
                stamp = int(internal_date_ms) / 1000 if internal_date_ms else 0
            except (TypeError, ValueError):
                stamp = 0
            date = datetime.fromtimestamp(stamp, tz=timezone.utc)
            email_dir = self.emails_dir / str(date.year) / f"{date.month:02d}"
            ensure_directory_exists(email_dir)

            # Save the email
            email_path = ensure_within(email_dir / filename, self.backup_dir)
            write_atomic(email_path, raw_bytes)

            # Save metadata
            metadata = {
                'message_id': msg_id,
                'thread_id': thread_id,
                'subject': msg.get('Subject', 'no-subject'),
                'from': msg.get('From', ''),
                'to': msg.get('To', ''),
                'date': msg.get('Date', ''),
                'labels': labels,
                'internal_date': internal_date_ms,
                'size': len(raw_bytes),
                'sha256': email_hash,
                'backup_path': str(email_path.relative_to(self.backup_dir)),
                'backup_time': datetime.now(timezone.utc).isoformat(),
            }

            metadata_path = ensure_within(self.metadata_dir / f"{key}.json", self.backup_dir)
            write_atomic(
                metadata_path,
                json.dumps(metadata, indent=2, ensure_ascii=False, default=str).encode('utf-8'),
            )

            return email_path, email_hash

        except Exception as e:
            logger.error("Error saving email %s: %s", msg_id, e)
            return None

    def _record_backup(
        self,
        email_path: Path,
        msg_id: str,
        thread_id: Optional[str],
        internal_date_ms: Optional[str],
        labels: List[str],
        sha256: str = '',
    ) -> None:
        """Record a freshly written email in the archive index.

        Shared by the API and IMAP backup paths so both update the index
        identically (keyed by ``msg_id``), which is what makes re-runs skip
        already-backed-up messages. Called only after :meth:`_write_email` has
        durably written the bytes, so the index never claims a message the disk
        does not have.

        Args:
            email_path: Path to the saved ``.eml`` file.
            msg_id: Stable message identifier (matches the on-disk key).
            thread_id: Thread identifier, or None.
            internal_date_ms: Message internal date as a Unix-millisecond string.
            labels: Labels for the message.
            sha256: Hex digest of the raw bytes, from :meth:`_write_email`.
        """
        try:
            internal_ms = int(internal_date_ms) if internal_date_ms else None
        except (TypeError, ValueError):
            internal_ms = None

        self.store.record(
            msg_id=msg_id,
            thread_id=thread_id,
            internal_date=internal_ms,
            size=os.path.getsize(email_path),
            sha256=sha256,
            rel_path=str(Path(email_path).relative_to(self.backup_dir)),
            labels=labels,
        )
        self.store.clear_failure(msg_id)

    def _is_intact(self, msg_id: str) -> bool:
        """Return True if an indexed message is still correctly on disk.

        The index alone is not proof the bytes survived: a crash between the
        ``.eml`` write and the commit, a partial restore, or manual tampering can
        leave a missing or short file that an index-only check would skip
        forever. With ``verify_existing`` on, a mismatch causes a re-download.

        Args:
            msg_id: The indexed message to check.

        Returns:
            True if the file exists with the recorded size, False otherwise.
        """
        record = self.store.get(msg_id)
        if record is None:
            return False
        if not self.verify_existing:
            return True
        rel_path = record.get('rel_path')
        if not rel_path:
            return False
        path = self.backup_dir / rel_path
        try:
            return path.stat().st_size == record['size']
        except OSError:
            return False

    def _archive(
        self,
        raw_bytes: bytes,
        msg_id: str,
        thread_id: Optional[str],
        internal_date_ms: Optional[str],
        labels: List[str],
    ) -> Optional[Path]:
        """Write a message to disk and index it, in that order.

        Returns:
            The path written, or None if the write failed.
        """
        written = self._write_email(
            raw_bytes=raw_bytes,
            msg_id=msg_id,
            thread_id=thread_id,
            internal_date_ms=internal_date_ms,
            labels=labels,
        )
        if not written:
            return None
        email_path, sha256 = written
        self._record_backup(email_path, msg_id, thread_id, internal_date_ms, labels, sha256)
        return email_path

    def _process_email_batch(self, message_ids: List[str]) -> Tuple[int, int, int]:
        """Process a batch of email messages.

        Args:
            message_ids: List of message IDs to process.

        Returns:
            A tuple of (processed_count, error_count, skipped_count).
        """
        processed = 0
        errors = 0
        skipped = 0

        for msg_id in message_ids:
            if self.store.is_archived(msg_id) and self._is_intact(msg_id):
                logger.debug("Skipping already backed up message: %s", msg_id)
                skipped += 1
                continue

            try:
                # Get the email with full content
                email_data = self._get_email(msg_id)
                if not email_data:
                    self.store.record_failure(msg_id, "download returned no data")
                    errors += 1
                    continue

                # Get the labels for this email
                labels = email_data.get('labelIds', [])

                # Save the email to disk and index it
                if not self._archive(
                    base64.urlsafe_b64decode(email_data['raw'].encode('ASCII')),
                    msg_id,
                    email_data.get('threadId'),
                    email_data.get('internalDate'),
                    labels,
                ):
                    self.store.record_failure(msg_id, "write failed")
                    errors += 1
                    continue

                processed += 1
                if processed % 10 == 0:
                    logger.info("Processed %d emails...", processed)

            except Exception as e:
                logger.error("Error processing message %s: %s", msg_id, e)
                self.store.record_failure(msg_id, str(e))
                errors += 1

        return processed, errors, skipped

    def backup_emails(self, max_results: Optional[int] = None) -> Dict[str, Any]:
        """Back up emails from Gmail.

        Enumerates the full mailbox on every run and relies on the index to skip
        what is already archived. It deliberately does *not* filter by a
        "last backup" timestamp: doing so meant any message that errored once —
        a rate limit, a transient network fault — fell outside every subsequent
        query and was never retried. Listing ids is cheap relative to fetching
        bodies, so a full enumeration costs little and cannot silently skip mail.

        Args:
            max_results: Maximum number of *new* emails to download. If None,
                download all.

        Returns:
            A dictionary with statistics about the backup operation.
        """
        logger.info("Starting Gmail backup to: %s", self.backup_dir)
        sweep_started_at = datetime.now(timezone.utc).isoformat()

        try:
            # Get the list of all messages
            request = self.gmail.users().messages().list(
                userId='me',
                maxResults=self.batch_size,
            )

            total_processed = 0
            total_errors = 0
            total_skipped = 0
            swept_ids: List[str] = []
            completed = True

            while request:
                response = request.execute(num_retries=API_NUM_RETRIES)
                messages = response.get('messages', [])

                if not messages:
                    logger.info("No more messages to process.")
                    break

                # Process a batch of messages
                message_ids = [msg['id'] for msg in messages]
                swept_ids.extend(message_ids)
                processed, errors, skipped = self._process_email_batch(message_ids)

                total_processed += processed
                total_errors += errors
                total_skipped += skipped

                # Everything listed this run is still in Gmail, whether or not we
                # re-downloaded it; this is what the tombstone pass reads.
                self.store.mark_seen(message_ids)
                self.store.commit()

                logger.info(
                    "Processed batch: %d new, %d skipped, %d errors "
                    "(total: %d new, %d skipped, %d errors)",
                    processed, skipped, errors,
                    total_processed, total_skipped, total_errors,
                )

                # Check if we've reached the max results
                if max_results and total_processed >= max_results:
                    logger.info("Reached maximum number of emails to process (%d)", max_results)
                    completed = False
                    break

                # Get the next page of results if available
                request = self.gmail.users().messages().list_next(
                    previous_request=request, previous_response=response
                ) if 'nextPageToken' in response else None

            tombstoned = self._finish_sweep(sweep_started_at, completed, total_errors)
            stats = self.store.stats()
            self.store.commit()

            logger.info(
                "Backup completed. New: %d, skipped: %d, errors: %d, vanished: %d, "
                "archive size: %s",
                total_processed, total_skipped, total_errors, tombstoned,
                format_size(stats['total_size']),
            )

            return {
                'total_processed': total_processed,
                'total_errors': total_errors,
                'total_skipped': total_skipped,
                'emails_processed': total_processed,
                'tombstoned': tombstoned,
                'archived_total': stats['total_emails'],
            }

        except Exception as e:
            logger.error("Error during backup: %s", e, exc_info=True)
            raise

    def _finish_sweep(self, sweep_started_at: str, completed: bool, errors: int) -> int:
        """Run the tombstone pass if this sweep is a trustworthy basis for it.

        A message is only flagged as vanished when the sweep saw the whole
        mailbox cleanly. A run that was capped by ``--max-results``, cut short by
        an error, or interrupted has simply not looked at the messages it never
        reached, and tombstoning on that basis would flag the entire archive.

        Args:
            sweep_started_at: ISO-8601 timestamp taken before enumeration began.
            completed: Whether the sweep enumerated the whole mailbox.
            errors: Number of errors encountered during the sweep.

        Returns:
            The number of messages newly tombstoned (0 if the pass was skipped).
        """
        if not completed:
            logger.info("Partial sweep - skipping the vanished-message check.")
            return 0
        if errors:
            logger.warning(
                "Sweep finished with %d error(s) - skipping the vanished-message "
                "check to avoid mis-flagging messages.", errors
            )
            return 0
        tombstoned = self.store.tombstone_missing(sweep_started_at)
        if tombstoned:
            logger.info(
                "%d message(s) are no longer in Gmail; kept on disk and flagged "
                "as vanished.", tombstoned
            )
        return tombstoned
