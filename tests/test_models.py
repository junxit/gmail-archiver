"""Tests for the models module."""
import unittest
from datetime import datetime, timezone
from pathlib import Path

from gmail_archiver.models import EmailMetadata, BackupState


class TestEmailMetadata(unittest.TestCase):
    """Test cases for EmailMetadata dataclass."""
    
    def test_create_minimal(self):
        """Test creating EmailMetadata with minimal fields."""
        meta = EmailMetadata(
            message_id='msg123',
            thread_id='thread456'
        )
        
        self.assertEqual(meta.message_id, 'msg123')
        self.assertEqual(meta.thread_id, 'thread456')
        self.assertEqual(meta.labels, set())
        self.assertIsNone(meta.internal_date)
        self.assertIsNone(meta.backup_path)
        self.assertIsNone(meta.size)
    
    def test_create_full(self):
        """Test creating EmailMetadata with all fields."""
        now = datetime.now(timezone.utc)
        path = Path('/backup/email.eml')
        
        meta = EmailMetadata(
            message_id='msg123',
            thread_id='thread456',
            labels={'INBOX', 'IMPORTANT'},
            internal_date=now,
            backup_path=path,
            size=1024
        )
        
        self.assertEqual(meta.message_id, 'msg123')
        self.assertEqual(meta.thread_id, 'thread456')
        self.assertEqual(meta.labels, {'INBOX', 'IMPORTANT'})
        self.assertEqual(meta.internal_date, now)
        self.assertEqual(meta.backup_path, path)
        self.assertEqual(meta.size, 1024)
    
    def test_labels_are_set(self):
        """Test that labels are a set type."""
        meta = EmailMetadata(
            message_id='msg123',
            thread_id='thread456',
            labels={'A', 'B', 'A'}  # Duplicate should be removed
        )
        
        self.assertEqual(len(meta.labels), 2)
        self.assertIn('A', meta.labels)
        self.assertIn('B', meta.labels)


class TestBackupState(unittest.TestCase):
    """Test cases for BackupState dataclass."""
    
    def test_create_empty(self):
        """Test creating empty BackupState."""
        state = BackupState()
        
        self.assertEqual(state.emails, {})
        self.assertEqual(state.backed_up_message_ids, set())
        self.assertIsNone(state.last_backup_time)
        self.assertEqual(state.last_backup_count, 0)
        self.assertEqual(state.total_backup_size, 0)
        self.assertEqual(state.total_emails, 0)
    
    def test_add_email(self):
        """Test adding an email to state."""
        state = BackupState()
        email_meta = EmailMetadata(
            message_id='msg123',
            thread_id='thread456',
            labels={'INBOX'},
            size=1024
        )
        
        state.add_email(email_meta)
        
        self.assertEqual(state.total_emails, 1)
        self.assertEqual(state.total_backup_size, 1024)
        self.assertTrue(state.is_email_backed_up('msg123'))
        self.assertIn('msg123', state.emails)
    
    def test_add_multiple_emails(self):
        """Test adding multiple emails."""
        state = BackupState()
        
        for i in range(5):
            email_meta = EmailMetadata(
                message_id=f'msg{i}',
                thread_id=f'thread{i}',
                size=100
            )
            state.add_email(email_meta)
        
        self.assertEqual(state.total_emails, 5)
        self.assertEqual(state.total_backup_size, 500)
    
    def test_get_email(self):
        """Test getting email by message ID."""
        state = BackupState()
        email_meta = EmailMetadata(
            message_id='msg123',
            thread_id='thread456'
        )
        state.add_email(email_meta)
        
        result = state.get_email('msg123')
        
        self.assertEqual(result, email_meta)
    
    def test_get_nonexistent_email(self):
        """Test getting non-existent email returns None."""
        state = BackupState()
        
        result = state.get_email('nonexistent')
        
        self.assertIsNone(result)
    
    def test_is_email_backed_up_true(self):
        """Test is_email_backed_up returns True for backed up email."""
        state = BackupState()
        email_meta = EmailMetadata(
            message_id='msg123',
            thread_id='thread456'
        )
        state.add_email(email_meta)
        
        self.assertTrue(state.is_email_backed_up('msg123'))
    
    def test_is_email_backed_up_false(self):
        """Test is_email_backed_up returns False for unknown email."""
        state = BackupState()
        
        self.assertFalse(state.is_email_backed_up('unknown'))


class TestBackupStateToDict(unittest.TestCase):
    """Test cases for BackupState.to_dict method."""
    
    def test_empty_state_to_dict(self):
        """Test converting empty state to dict."""
        state = BackupState()
        
        result = state.to_dict()
        
        self.assertEqual(result['emails'], {})
        self.assertEqual(result['backed_up_message_ids'], [])
        self.assertIsNone(result['last_backup_time'])
        self.assertEqual(result['last_backup_count'], 0)
        self.assertEqual(result['total_backup_size'], 0)
        self.assertEqual(result['total_emails'], 0)
    
    def test_state_with_email_to_dict(self):
        """Test converting state with emails to dict."""
        now = datetime.now(timezone.utc)
        state = BackupState()
        state.last_backup_time = now
        
        email_meta = EmailMetadata(
            message_id='msg123',
            thread_id='thread456',
            labels={'INBOX', 'IMPORTANT'},
            internal_date=now,
            backup_path=Path('/backup/email.eml'),
            size=1024
        )
        state.add_email(email_meta)
        
        result = state.to_dict()
        
        self.assertIn('msg123', result['emails'])
        self.assertEqual(result['emails']['msg123']['message_id'], 'msg123')
        self.assertEqual(result['emails']['msg123']['thread_id'], 'thread456')
        self.assertIn('INBOX', result['emails']['msg123']['labels'])
        self.assertEqual(result['emails']['msg123']['size'], 1024)
        self.assertEqual(result['last_backup_time'], now.isoformat())
    
    def test_to_dict_labels_are_list(self):
        """Test that labels are converted to list in dict."""
        state = BackupState()
        email_meta = EmailMetadata(
            message_id='msg123',
            thread_id='thread456',
            labels={'INBOX', 'IMPORTANT'}
        )
        state.add_email(email_meta)
        
        result = state.to_dict()
        
        self.assertIsInstance(result['emails']['msg123']['labels'], list)


class TestBackupStateFromDict(unittest.TestCase):
    """Test cases for BackupState.from_dict method."""
    
    def test_from_empty_dict(self):
        """Test creating state from empty dict."""
        state = BackupState.from_dict({})
        
        self.assertEqual(state.total_emails, 0)
        self.assertEqual(state.backed_up_message_ids, set())
    
    def test_from_dict_with_emails(self):
        """Test creating state from dict with emails."""
        data = {
            'emails': {
                'msg123': {
                    'message_id': 'msg123',
                    'thread_id': 'thread456',
                    'labels': ['INBOX', 'IMPORTANT'],
                    'internal_date': '2024-01-01T12:00:00+00:00',
                    'backup_path': '/backup/email.eml',
                    'size': 1024
                }
            },
            'backed_up_message_ids': ['msg123'],
            'last_backup_time': '2024-01-01T12:00:00+00:00',
            'last_backup_count': 1,
            'total_backup_size': 1024,
            'total_emails': 1
        }
        
        state = BackupState.from_dict(data)
        
        self.assertTrue(state.is_email_backed_up('msg123'))
        self.assertIsNotNone(state.last_backup_time)
        email = state.get_email('msg123')
        self.assertEqual(email.thread_id, 'thread456')
        self.assertIn('INBOX', email.labels)
    
    def test_roundtrip(self):
        """Test to_dict and from_dict roundtrip."""
        original = BackupState()
        original.last_backup_time = datetime.now(timezone.utc)
        
        email_meta = EmailMetadata(
            message_id='msg123',
            thread_id='thread456',
            labels={'INBOX'},
            size=512
        )
        original.add_email(email_meta)
        
        # Convert to dict and back
        data = original.to_dict()
        restored = BackupState.from_dict(data)
        
        self.assertEqual(restored.total_emails, original.total_emails)
        self.assertTrue(restored.is_email_backed_up('msg123'))
        self.assertEqual(restored.get_email('msg123').thread_id, 'thread456')
    
    def test_from_dict_handles_missing_fields(self):
        """Test from_dict handles missing optional fields."""
        data = {
            'emails': {
                'msg123': {
                    'message_id': 'msg123',
                    'thread_id': 'thread456'
                }
            }
        }
        
        state = BackupState.from_dict(data)
        
        email = state.get_email('msg123')
        self.assertIsNotNone(email)
        self.assertEqual(email.labels, set())
        self.assertIsNone(email.internal_date)


if __name__ == "__main__":
    unittest.main()
