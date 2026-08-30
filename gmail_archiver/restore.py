"""Restore functionality for Gmail Archiver."""
import base64
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from googleapiclient.discovery import Resource
from googleapiclient.errors import HttpError

from .utils import ensure_within, setup_logging, write_atomic

logger = logging.getLogger(__name__)

# Matches backup.API_NUM_RETRIES; googleapiclient applies exponential backoff
# with jitter to 429/500/503 responses.
API_NUM_RETRIES = 5

class GmailRestore:
    """Class to handle Gmail restore operations."""
    
    def __init__(
        self,
        gmail_service: Resource,
        backup_dir: str,
        state_file: Optional[str] = None,
        batch_size: int = 10,
        log_level: str = 'INFO'
    ) -> None:
        """Initialize the GmailRestore instance.
        
        Args:
            gmail_service: Authenticated Gmail API service instance.
            backup_dir: Directory containing the backup.
            state_file: Path to the restore state file. If not provided, will use
                a default location in the backup directory.
            batch_size: Number of messages to process in each batch.
            log_level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL).
        """
        setup_logging(log_level)
        self.service = gmail_service
        self.backup_dir = Path(backup_dir).expanduser().resolve()
        self.emails_dir = self.backup_dir / 'emails'
        self.metadata_dir = self.backup_dir / 'metadata'
        self.state_file = Path(state_file) if state_file else self.backup_dir / 'restore_state.json'
        self.batch_size = batch_size
        self.state = self._load_state()
        
        # Verify backup directory structure
        if not self.emails_dir.exists() or not self.metadata_dir.exists():
            raise ValueError("Invalid backup directory structure. Missing 'emails' or 'metadata' directory.")
    
    def _load_state(self) -> Dict:
        """Load the restore state from the state file.

        A corrupt state file is *not* silently treated as a fresh start: losing
        ``restored_message_ids`` would make the next run re-import every message,
        duplicating the user's entire mailbox. The caller must decide.

        Raises:
            ValueError: If the state file exists but cannot be parsed.
        """
        if self.state_file.exists():
            try:
                with open(self.state_file, 'r', encoding='utf-8') as f:
                    state = json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                raise ValueError(
                    f"Restore state at {self.state_file} is unreadable ({e}). "
                    "Continuing would re-import every message and duplicate your "
                    "mailbox. Inspect or delete the file, then re-run."
                ) from e
            # Membership is checked once per message; a set keeps that O(1).
            state['restored_message_ids'] = set(state.get('restored_message_ids') or [])
            return state
        return {
            'restored_message_ids': set(),
            'last_restore_time': None,
            'total_restored': 0,
            'total_errors': 0,
        }

    def _save_state(self) -> None:
        """Save the current restore state to the state file, atomically."""
        try:
            serializable = dict(self.state)
            serializable['restored_message_ids'] = sorted(self.state['restored_message_ids'])
            write_atomic(
                self.state_file,
                json.dumps(serializable, indent=2, ensure_ascii=False).encode('utf-8'),
            )
        except OSError as e:
            logger.error("Failed to save restore state: %s", e)
            raise
    
    def _import_email(self, email_path: Path, metadata: Dict) -> Optional[str]:
        """Import a single email back to Gmail.
        
        Args:
            email_path: Path to the email file.
            metadata: Email metadata.
            
        Returns:
            The message ID of the imported email, or None if import failed.
        """
        try:
            # Read the email file
            with open(email_path, 'rb') as f:
                email_data = f.read()
            
            # Encode the email data
            message = {
                'raw': base64.urlsafe_b64encode(email_data).decode('utf-8'),
                'labelIds': metadata.get('labels', []),
            }
            
            # Import the message
            result = self.service.users().messages().import_(
                userId='me',
                body=message,
                internalDateSource='dateHeader',
            ).execute(num_retries=API_NUM_RETRIES)
            
            logger.debug("Imported message: %s", result['id'])
            return result['id']
            
        except HttpError as e:
            logger.error("Error importing message %s: %s", email_path.name, e)
            return None
        except Exception as e:
            logger.error("Unexpected error importing message %s: %s", email_path.name, e)
            return None
    
    def _process_restore_batch(self, metadata_files: List[Path]) -> Tuple[int, int]:
        """Process a batch of email metadata files for restoration.
        
        Args:
            metadata_files: List of metadata file paths to process.
            
        Returns:
            A tuple of (processed_count, error_count).
        """
        processed = 0
        errors = 0
        
        for meta_file in metadata_files:
            try:
                # Skip if we've already processed this message
                msg_id = meta_file.stem
                if msg_id in self.state['restored_message_ids']:
                    logger.debug("Skipping already restored message: %s", msg_id)
                    continue
                
                # Load the metadata
                with open(meta_file, 'r', encoding='utf-8') as f:
                    metadata = json.load(f)

                # Find the email file. The recorded path is data, not code: an
                # absolute or ..-bearing value would make restore read an
                # arbitrary local file and upload it into the user's mailbox, so
                # it is confined to the backup directory before being opened.
                try:
                    backup_path = ensure_within(
                        self.backup_dir / metadata['backup_path'], self.backup_dir
                    )
                except (ValueError, KeyError) as e:
                    logger.error("Refusing to restore %s: %s", meta_file.name, e)
                    errors += 1
                    continue
                if not backup_path.exists():
                    logger.error("Email file not found: %s", backup_path)
                    errors += 1
                    continue

                # Import the email
                new_msg_id = self._import_email(backup_path, metadata)
                if not new_msg_id:
                    errors += 1
                    continue

                # Update the state
                self.state['restored_message_ids'].add(msg_id)
                processed += 1
                
                if processed % 5 == 0:
                    logger.info("Restored %d emails...", processed)
                
            except Exception as e:
                logger.error("Error processing metadata file %s: %s", meta_file, e)
                errors += 1
        
        return processed, errors
    
    def restore_emails(
        self,
        max_results: Optional[int] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None
    ) -> Dict[str, int]:
        """Restore emails from backup to Gmail.
        
        Args:
            max_results: Maximum number of emails to restore. If None, restore all.
            start_date: Only restore emails after this date.
            end_date: Only restore emails before this date.
            
        Returns:
            A dictionary with statistics about the restore operation.
        """
        logger.info("Starting Gmail restore from: %s", self.backup_dir)
        
        try:
            # Get all metadata files
            metadata_files = list(self.metadata_dir.glob('*.json'))
            
            # Filter by date if specified
            if start_date or end_date:
                filtered_files = []
                for meta_file in metadata_files:
                    with open(meta_file, 'r', encoding='utf-8') as f:
                        try:
                            metadata = json.load(f)
                            msg_date = datetime.fromisoformat(metadata.get('date', ''))
                            
                            if start_date and msg_date < start_date:
                                continue
                            if end_date and msg_date > end_date:
                                continue
                                
                            filtered_files.append(meta_file)
                            
                        except (json.JSONDecodeError, ValueError) as e:
                            logger.warning("Error reading metadata file %s: %s", meta_file, e)
                            continue
                
                metadata_files = filtered_files
                logger.info("Filtered to %d emails based on date range", len(metadata_files))
            
            # Process in batches
            total_processed = 0
            total_errors = 0
            
            # Handle empty case
            if not metadata_files:
                logger.info("No emails to restore")
                return {
                    'total_restored': 0,
                    'total_errors': 0,
                    'last_restore_time': None
                }
            
            batch_size = min(self.batch_size, len(metadata_files)) if metadata_files else 1
            
            for i in range(0, len(metadata_files), batch_size):
                batch = metadata_files[i:i + batch_size]
                processed, errors = self._process_restore_batch(batch)
                
                total_processed += processed
                total_errors += errors
                
                # Update the state
                self.state['last_restore_time'] = datetime.now(timezone.utc).isoformat()
                self.state['total_restored'] = total_processed
                self.state['total_errors'] = total_errors
                
                # Save the state after each batch
                self._save_state()
                
                logger.info(
                    "Processed batch: %d emails, %d errors (total: %d, errors: %d)",
                    processed, errors, total_processed, total_errors
                )
                
                # Check if we've reached the maximum number of results
                if max_results and total_processed + total_errors >= max_results:
                    logger.info("Reached maximum number of results (%d)", max_results)
                    break
            
            logger.info(
                "Restore completed. Total restored: %d, errors: %d",
                total_processed, total_errors
            )
            
            return {
                'total_restored': total_processed,
                'total_errors': total_errors,
                'last_restore_time': self.state['last_restore_time']
            }
            
        except Exception as e:
            logger.error("Error during restore: %s", e, exc_info=True)
            raise
