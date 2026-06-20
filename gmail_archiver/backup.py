"""Backup functionality for Gmail Archiver."""
import base64
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from googleapiclient.discovery import Resource
from googleapiclient.errors import HttpError

from .models import BackupState, EmailMetadata
from .utils import (
    ensure_directory_exists,
    format_size,
    get_email_hash,
    get_safe_filename,
    parse_email_message,
    setup_logging,
)

logger = logging.getLogger(__name__)

class GmailBackup:
    """Class to handle Gmail backup operations."""
    
    def __init__(
        self,
        gmail_service: Any,
        backup_dir: Union[str, Path],
        state_file: Union[str, Path],
        batch_size: int = 100,
    ) -> None:
        """Initialize the GmailBackup instance.

        Args:
            gmail_service: Authenticated Gmail API service instance.
            backup_dir: Directory to store the backup files.
            state_file: Path to the state file for tracking backup progress.
            batch_size: Number of emails to process in each batch.
        """
        self.gmail = gmail_service
        self.backup_dir = Path(backup_dir).resolve()
        self.state_file = Path(state_file).resolve()
        self.batch_size = batch_size
        self.state = self._load_state()
        
        # Ensure backup directory exists
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        
        # Create necessary directories
        self.emails_dir = self.backup_dir / 'emails'
        self.metadata_dir = self.backup_dir / 'metadata'
        self.emails_dir.mkdir(parents=True, exist_ok=True)
        self.metadata_dir.mkdir(parents=True, exist_ok=True)
    
    def _load_state(self) -> BackupState:
        """Load the backup state from the state file."""
        if not self.state_file.exists():
            return BackupState()
        try:
            with open(self.state_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return BackupState.from_dict(data) if isinstance(data, dict) else BackupState()
        except (IOError, json.JSONDecodeError) as e:
            logger.warning(f"Error loading backup state: {e}")
            return BackupState()
    
    def _save_state(self) -> None:
        """Save the current backup state to the state file."""
        try:
            # Ensure the directory exists
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.state_file, 'w', encoding='utf-8') as f:
                json.dump(self.state.to_dict(), f, indent=2, ensure_ascii=False, default=str)
        except (IOError, TypeError) as e:
            logger.error(f"Error saving backup state: {e}")
            raise
    
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
            ).execute()
            return message
        except HttpError as e:
            if e.resp.status == 404:  # Not found
                logger.warning("Message %s not found: %s", msg_id, e)
            else:
                logger.error("Error retrieving message %s: %s", msg_id, e)
            return None
    
    def _save_email(self, email_data: Dict, labels: List[str]) -> Optional[Path]:
        """Save an email from the Gmail API to disk.

        Decodes the API's base64 ``raw`` payload and delegates the actual write
        to :meth:`_write_email`, the shared writer used by both the API and IMAP
        backup paths.

        Args:
            email_data: The email data from the Gmail API.
            labels: List of labels for the email.

        Returns:
            Path to the saved email file, or None if saving failed.
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
    ) -> Optional[Path]:
        """Write raw RFC822 bytes to disk as ``.eml`` plus a metadata sidecar.

        This is the shared, transport-agnostic writer used by both the Gmail API
        backup path (via :meth:`_save_email`) and the IMAP backup path, so the
        two backends always produce an identical on-disk format.

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
            Path to the saved ``.eml`` file, or None if writing failed.
        """
        try:
            # Parse the email to get header metadata
            msg = parse_email_message(raw_bytes)

            # Generate a unique filename
            email_hash = get_email_hash(raw_bytes)
            safe_subject = get_safe_filename(msg.get('subject', 'no-subject'))
            filename = f"{msg_id}_{email_hash[:8]}_{safe_subject}.eml"

            # Create a directory structure based on the date
            date = datetime.fromtimestamp(int(internal_date_ms) / 1000)
            email_dir = self.emails_dir / str(date.year) / f"{date.month:02d}"
            ensure_directory_exists(email_dir)

            # Save the email
            email_path = email_dir / filename
            with open(email_path, 'wb') as f:
                f.write(raw_bytes)

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
                'backup_path': str(email_path.relative_to(self.backup_dir)),
                'backup_time': datetime.now(timezone.utc).isoformat(),
            }

            metadata_path = self.metadata_dir / f"{msg_id}.json"
            with open(metadata_path, 'w', encoding='utf-8') as f:
                json.dump(metadata, f, indent=2, ensure_ascii=False)

            return email_path

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
    ) -> None:
        """Record a freshly written email in the backup state.

        Shared by the API and IMAP backup paths so both update ``BackupState``
        identically (keyed by ``msg_id``), which is what makes re-runs skip
        already-backed-up messages.

        Args:
            email_path: Path to the saved ``.eml`` file.
            msg_id: Stable message identifier (matches the on-disk key).
            thread_id: Thread identifier, or None.
            internal_date_ms: Message internal date as a Unix-millisecond string.
            labels: Labels for the message.
        """
        email_meta = EmailMetadata(
            message_id=msg_id,
            thread_id=thread_id,
            labels=set(labels),
            internal_date=datetime.fromtimestamp(int(internal_date_ms or '0') / 1000),
            backup_path=email_path,
            size=os.path.getsize(email_path),
        )
        self.state.add_email(email_meta)

    def _process_email_batch(self, message_ids: List[str]) -> Tuple[int, int]:
        """Process a batch of email messages.
        
        Args:
            message_ids: List of message IDs to process.
            
        Returns:
            A tuple of (processed_count, error_count).
        """
        processed = 0
        errors = 0
        
        for msg_id in message_ids:
            if self.state.is_email_backed_up(msg_id):
                logger.debug("Skipping already backed up message: %s", msg_id)
                continue
                
            try:
                # Get the email with full content
                email_data = self._get_email(msg_id)
                if not email_data:
                    errors += 1
                    continue
                
                # Get the labels for this email
                labels = email_data.get('labelIds', [])
                
                # Save the email to disk
                email_path = self._save_email(email_data, labels)
                if not email_path:
                    errors += 1
                    continue
                
                # Update the backup state
                self._record_backup(
                    email_path,
                    msg_id,
                    email_data.get('threadId'),
                    email_data.get('internalDate'),
                    labels,
                )

                processed += 1
                if processed % 10 == 0:
                    logger.info("Processed %d emails...", processed)
                
            except Exception as e:
                logger.error("Error processing message %s: %s", msg_id, e)
                errors += 1
        
        return processed, errors
    
    def backup_emails(self, max_results: Optional[int] = None) -> Dict[str, int]:
        """Back up emails from Gmail.
        
        Args:
            max_results: Maximum number of emails to process. If None, process all.
            
        Returns:
            A dictionary with statistics about the backup operation.
        """
        logger.info("Starting Gmail backup to: %s", self.backup_dir)
        
        try:
            # Build query for incremental backup
            query = None
            if self.state.last_backup_time:
                # Convert datetime to Unix timestamp for Gmail query
                timestamp = int(self.state.last_backup_time.timestamp())
                query = f"after:{timestamp}"
            
            # Get the list of all messages
            request = self.gmail.users().messages().list(
                userId='me',
                maxResults=self.batch_size,
                q=query
            )
            
            total_processed = 0
            total_errors = 0
            
            while request:
                response = request.execute()
                messages = response.get('messages', [])
                
                if not messages:
                    logger.info("No more messages to process.")
                    break
                
                # Process a batch of messages
                message_ids = [msg['id'] for msg in messages]
                processed, errors = self._process_email_batch(message_ids)
                
                total_processed += processed
                total_errors += errors
                
                # Update the state
                self.state.last_backup_time = datetime.now(timezone.utc)
                self.state.last_backup_count = total_processed
                
                # Save the state after each batch
                self._save_state()
                
                logger.info(
                    "Processed batch: %d emails, %d errors (total: %d, errors: %d)",
                    processed, errors, total_processed, total_errors
                )

                # Check if we've reached the max results
                if max_results and total_processed >= max_results:
                    logger.info("Reached maximum number of emails to process (%d)", max_results)
                    break

                # Get the next page of results if available
                request = self.gmail.users().messages().list_next(
                    previous_request=request, previous_response=response
                ) if 'nextPageToken' in response else None
                
            # Calculate total size of all emails
            total_size = self.state.total_backup_size
            
            logger.info(
                f"Backup completed. Total processed: {total_processed}, errors: {total_errors}, total size: {format_size(total_size)}"
            )
            
            return {
                'total_processed': total_processed,
                'total_errors': total_errors,
                'emails_processed': total_processed,
                'last_backup_time': self.state.last_backup_time.isoformat() if self.state.last_backup_time else None
            }
            
        except Exception as e:
            logger.error("Error during backup: %s", e, exc_info=True)
            raise
