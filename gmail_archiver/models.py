"""Data models for Gmail Archiver."""
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set


@dataclass
class EmailMetadata:
    """Metadata for an archived email."""
    message_id: str
    thread_id: str
    labels: Set[str] = field(default_factory=set)
    internal_date: Optional[datetime] = None
    backup_path: Optional[Path] = None
    size: Optional[int] = None


@dataclass
class BackupState:
    """State of the backup process."""
    # Email ID to metadata mapping
    emails: Dict[str, EmailMetadata] = field(default_factory=dict)
    # Set of message IDs that have been backed up
    backed_up_message_ids: Set[str] = field(default_factory=set)
    # Timestamp of the last successful backup
    last_backup_time: Optional[datetime] = None
    # Total number of emails processed in the last backup
    last_backup_count: int = 0
    # Total size of the backup in bytes
    total_backup_size: int = 0
    # Number of emails in the backup
    total_emails: int = 0

    def add_email(self, email_metadata: EmailMetadata) -> None:
        """Add an email to the backup state.
        
        Args:
            email_metadata: The email metadata to add.
        """
        self.emails[email_metadata.message_id] = email_metadata
        self.backed_up_message_ids.add(email_metadata.message_id)
        if email_metadata.size:
            self.total_backup_size += email_metadata.size
        self.total_emails += 1

    def get_email(self, message_id: str) -> Optional[EmailMetadata]:
        """Get email metadata by message ID.
        
        Args:
            message_id: The message ID to look up.
            
        Returns:
            The email metadata if found, None otherwise.
        """
        return self.emails.get(message_id)

    def is_email_backed_up(self, message_id: str) -> bool:
        """Check if an email has been backed up.
        
        Args:
            message_id: The message ID to check.
            
        Returns:
            True if the email has been backed up, False otherwise.
        """
        return message_id in self.backed_up_message_ids

    def to_dict(self) -> dict:
        """Convert the backup state to a dictionary.
        
        Returns:
            A dictionary representation of the backup state.
        """
        return {
            'emails': {
                msg_id: {
                    'message_id': msg_id,
                    'thread_id': meta.thread_id,
                    'labels': list(meta.labels),
                    'internal_date': meta.internal_date.isoformat() if meta.internal_date else None,
                    'backup_path': str(meta.backup_path) if meta.backup_path else None,
                    'size': meta.size,
                }
                for msg_id, meta in self.emails.items()
            },
            'backed_up_message_ids': list(self.backed_up_message_ids),
            'last_backup_time': self.last_backup_time.isoformat() if self.last_backup_time else None,
            'last_backup_count': self.last_backup_count,
            'total_backup_size': self.total_backup_size,
            'total_emails': self.total_emails,
        }

    @classmethod
    def from_dict(cls, data: dict) -> 'BackupState':
        """Create a BackupState from a dictionary.
        
        Args:
            data: Dictionary containing backup state data.
            
        Returns:
            A new BackupState instance.
        """
        state = cls()
        state.last_backup_count = data.get('last_backup_count', 0)
        state.total_backup_size = data.get('total_backup_size', 0)
        state.total_emails = data.get('total_emails', 0)
        
        if 'last_backup_time' in data and data['last_backup_time']:
            state.last_backup_time = datetime.fromisoformat(data['last_backup_time'])
        
        for msg_id, meta_data in data.get('emails', {}).items():
            email_meta = EmailMetadata(
                message_id=msg_id,
                thread_id=meta_data['thread_id'],
                labels=set(meta_data.get('labels', [])),
                internal_date=datetime.fromisoformat(meta_data['internal_date']) if meta_data.get('internal_date') else None,
                backup_path=Path(meta_data['backup_path']) if meta_data.get('backup_path') else None,
                size=meta_data.get('size')
            )
            state.add_email(email_meta)
        
        # Ensure all message IDs are in the backed_up_message_ids set
        state.backed_up_message_ids = set(data.get('backed_up_message_ids', []))
        
        return state
