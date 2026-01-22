"""Tests for the backup module."""
import base64
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

from gmail_archiver.backup import GmailBackup
from gmail_archiver.models import BackupState, EmailMetadata


class TestGmailBackupInitialization(unittest.TestCase):
    """Test cases for GmailBackup class initialization."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.backup_dir = os.path.join(self.temp_dir, 'backup')
        self.state_file = os.path.join(self.temp_dir, 'state.json')
        self.mock_service = MagicMock()
    
    def tearDown(self):
        """Clean up test fixtures."""
        import shutil
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)
    
    def test_initialization_creates_directories(self):
        """Test that initialization creates necessary directories."""
        backup = GmailBackup(
            gmail_service=self.mock_service,
            backup_dir=self.backup_dir,
            state_file=self.state_file,
            batch_size=10
        )
        
        self.assertTrue(os.path.exists(self.backup_dir))
        self.assertTrue(os.path.exists(backup.emails_dir))
        self.assertTrue(os.path.exists(backup.metadata_dir))
    
    def test_initialization_with_path_objects(self):
        """Test initialization with Path objects instead of strings."""
        backup = GmailBackup(
            gmail_service=self.mock_service,
            backup_dir=Path(self.backup_dir),
            state_file=Path(self.state_file),
            batch_size=50
        )
        
        self.assertEqual(backup.batch_size, 50)
        self.assertIsInstance(backup.backup_dir, Path)
        self.assertIsInstance(backup.state_file, Path)
    
    def test_default_batch_size(self):
        """Test default batch size is 100."""
        backup = GmailBackup(
            gmail_service=self.mock_service,
            backup_dir=self.backup_dir,
            state_file=self.state_file
        )
        
        self.assertEqual(backup.batch_size, 100)
    
    def test_state_initialized_as_backup_state(self):
        """Test that state is initialized as BackupState object."""
        backup = GmailBackup(
            gmail_service=self.mock_service,
            backup_dir=self.backup_dir,
            state_file=self.state_file
        )
        
        self.assertIsInstance(backup.state, BackupState)


class TestGmailBackupState(unittest.TestCase):
    """Test cases for GmailBackup state management."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.backup_dir = os.path.join(self.temp_dir, 'backup')
        self.state_file = os.path.join(self.temp_dir, 'state.json')
        self.mock_service = MagicMock()
    
    def tearDown(self):
        """Clean up test fixtures."""
        import shutil
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)
    
    def test_save_and_load_state(self):
        """Test saving and loading the backup state."""
        backup = GmailBackup(
            gmail_service=self.mock_service,
            backup_dir=self.backup_dir,
            state_file=self.state_file,
            batch_size=10
        )
        
        # Add an email to the state
        email_meta = EmailMetadata(
            message_id='test123',
            thread_id='thread456',
            labels={'INBOX', 'IMPORTANT'},
            internal_date=datetime.now(timezone.utc),
            backup_path=Path('/test/path.eml'),
            size=1024
        )
        backup.state.add_email(email_meta)
        backup._save_state()
        
        # Verify file was created
        self.assertTrue(os.path.exists(self.state_file))
        
        # Create a new instance to load the state
        new_backup = GmailBackup(
            gmail_service=self.mock_service,
            backup_dir=self.backup_dir,
            state_file=self.state_file,
            batch_size=10
        )
        
        # Verify the state was loaded correctly
        self.assertTrue(new_backup.state.is_email_backed_up('test123'))
        self.assertEqual(new_backup.state.total_emails, 1)
    
    def test_load_corrupted_state_returns_empty(self):
        """Test loading corrupted state file returns empty BackupState."""
        # Create corrupted state file
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        with open(self.state_file, 'w') as f:
            f.write("not valid json {{{")
        
        backup = GmailBackup(
            gmail_service=self.mock_service,
            backup_dir=self.backup_dir,
            state_file=self.state_file,
            batch_size=10
        )
        
        # Should have empty state
        self.assertIsInstance(backup.state, BackupState)
        self.assertEqual(backup.state.total_emails, 0)


class TestGmailBackupGetEmail(unittest.TestCase):
    """Test cases for _get_email method."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.backup_dir = os.path.join(self.temp_dir, 'backup')
        self.state_file = os.path.join(self.temp_dir, 'state.json')
        self.mock_service = MagicMock()
        
        self.backup = GmailBackup(
            gmail_service=self.mock_service,
            backup_dir=self.backup_dir,
            state_file=self.state_file,
            batch_size=10
        )
    
    def tearDown(self):
        """Clean up test fixtures."""
        import shutil
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)
    
    def test_get_email_success(self):
        """Test successful email retrieval."""
        expected_message = {
            'id': 'msg123',
            'threadId': 'thread456',
            'raw': base64.urlsafe_b64encode(b'email content').decode()
        }
        
        self.mock_service.users().messages().get().execute.return_value = expected_message
        
        result = self.backup._get_email('msg123')
        
        self.assertEqual(result, expected_message)
    
    def test_get_email_not_found(self):
        """Test email not found returns None."""
        from googleapiclient.errors import HttpError
        
        mock_response = MagicMock()
        mock_response.status = 404
        
        self.mock_service.users().messages().get().execute.side_effect = HttpError(
            resp=mock_response, content=b'Not found'
        )
        
        result = self.backup._get_email('nonexistent')
        
        self.assertIsNone(result)


class TestGmailBackupSaveEmail(unittest.TestCase):
    """Test cases for _save_email method."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.backup_dir = os.path.join(self.temp_dir, 'backup')
        self.state_file = os.path.join(self.temp_dir, 'state.json')
        self.mock_service = MagicMock()
        
        self.backup = GmailBackup(
            gmail_service=self.mock_service,
            backup_dir=self.backup_dir,
            state_file=self.state_file,
            batch_size=10
        )
    
    def tearDown(self):
        """Clean up test fixtures."""
        import shutil
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)
    
    def test_save_email_creates_file(self):
        """Test that _save_email creates the email file."""
        # Create a simple email
        email_content = b"""From: sender@example.com
To: recipient@example.com
Subject: Test Email
Date: Mon, 1 Jan 2024 12:00:00 +0000

This is a test email body.
"""
        email_data = {
            'id': 'msg123',
            'threadId': 'thread456',
            'raw': base64.urlsafe_b64encode(email_content).decode(),
            'internalDate': '1704110400000',  # Jan 1, 2024
            'labelIds': ['INBOX']
        }
        
        result = self.backup._save_email(email_data, ['INBOX'])
        
        self.assertIsNotNone(result)
        self.assertTrue(result.exists())
        self.assertTrue(str(result).endswith('.eml'))
        
        # Verify metadata was also saved
        metadata_file = self.backup.metadata_dir / 'msg123.json'
        self.assertTrue(metadata_file.exists())


class TestGmailBackupEmails(unittest.TestCase):
    """Test cases for backup_emails method."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.backup_dir = os.path.join(self.temp_dir, 'backup')
        self.state_file = os.path.join(self.temp_dir, 'state.json')
        self.mock_service = MagicMock()
    
    def tearDown(self):
        """Clean up test fixtures."""
        import shutil
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)
    
    @patch.object(GmailBackup, '_process_email_batch')
    def test_backup_emails_processes_batches(self, mock_process_batch):
        """Test that backup_emails processes messages in batches."""
        mock_process_batch.return_value = (5, 0)
        
        # Mock Gmail API response
        mock_list_response = {
            'messages': [{'id': f'msg_{i}'} for i in range(5)],
        }
        
        self.mock_service.users().messages().list().execute.return_value = mock_list_response
        self.mock_service.users().messages().list_next.return_value = None
        
        backup = GmailBackup(
            gmail_service=self.mock_service,
            backup_dir=self.backup_dir,
            state_file=self.state_file,
            batch_size=10
        )
        
        result = backup.backup_emails()
        
        self.assertIn('total_processed', result)
        self.assertIn('total_errors', result)
        mock_process_batch.assert_called()
    
    @patch.object(GmailBackup, '_process_email_batch')
    def test_backup_emails_respects_max_results(self, mock_process_batch):
        """Test that backup_emails respects max_results parameter."""
        mock_process_batch.return_value = (10, 0)
        
        mock_list_response = {
            'messages': [{'id': f'msg_{i}'} for i in range(20)],
            'nextPageToken': 'next_page'
        }
        
        self.mock_service.users().messages().list().execute.return_value = mock_list_response
        
        backup = GmailBackup(
            gmail_service=self.mock_service,
            backup_dir=self.backup_dir,
            state_file=self.state_file,
            batch_size=10
        )
        
        result = backup.backup_emails(max_results=10)
        
        self.assertEqual(result['total_processed'], 10)
    
    def test_backup_emails_handles_empty_mailbox(self):
        """Test backup_emails handles empty mailbox gracefully."""
        self.mock_service.users().messages().list().execute.return_value = {
            'messages': []
        }
        
        backup = GmailBackup(
            gmail_service=self.mock_service,
            backup_dir=self.backup_dir,
            state_file=self.state_file,
            batch_size=10
        )
        
        result = backup.backup_emails()
        
        self.assertEqual(result['total_processed'], 0)
        self.assertEqual(result['total_errors'], 0)


class TestGmailBackupProcessEmailBatch(unittest.TestCase):
    """Test cases for _process_email_batch method."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.backup_dir = os.path.join(self.temp_dir, 'backup')
        self.state_file = os.path.join(self.temp_dir, 'state.json')
        self.mock_service = MagicMock()
        
        self.backup = GmailBackup(
            gmail_service=self.mock_service,
            backup_dir=self.backup_dir,
            state_file=self.state_file,
            batch_size=10
        )
    
    def tearDown(self):
        """Clean up test fixtures."""
        import shutil
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)
    
    def test_skips_already_backed_up_emails(self):
        """Test that already backed up emails are skipped."""
        # Add email to state
        email_meta = EmailMetadata(
            message_id='already_backed_up',
            thread_id='thread1',
            labels=set(),
            size=100
        )
        self.backup.state.add_email(email_meta)
        
        processed, errors = self.backup._process_email_batch(['already_backed_up'])
        
        self.assertEqual(processed, 0)
        self.assertEqual(errors, 0)
        # Verify no API call was made
        self.mock_service.users().messages().get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
