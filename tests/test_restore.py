"""Tests for the restore module."""
import base64
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from gmail_archiver.restore import GmailRestore


class TestGmailRestoreInitialization(unittest.TestCase):
    """Test cases for GmailRestore class initialization."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.backup_dir = os.path.join(self.temp_dir, 'backup')
        self.mock_service = MagicMock()
        
        # Create required directory structure
        os.makedirs(os.path.join(self.backup_dir, 'emails'))
        os.makedirs(os.path.join(self.backup_dir, 'metadata'))
    
    def tearDown(self):
        """Clean up test fixtures."""
        import shutil
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)
    
    def test_initialization_success(self):
        """Test successful initialization."""
        restore = GmailRestore(
            gmail_service=self.mock_service,
            backup_dir=self.backup_dir,
            batch_size=10
        )
        
        self.assertEqual(restore.batch_size, 10)
        self.assertIsInstance(restore.state, dict)
    
    def test_initialization_creates_default_state_file(self):
        """Test that default state file path is used when not provided."""
        restore = GmailRestore(
            gmail_service=self.mock_service,
            backup_dir=self.backup_dir
        )
        
        expected_state_file = Path(self.backup_dir).resolve() / 'restore_state.json'
        self.assertEqual(restore.state_file, expected_state_file)
    
    def test_initialization_with_custom_state_file(self):
        """Test initialization with custom state file path."""
        custom_state_file = os.path.join(self.temp_dir, 'custom_state.json')
        
        restore = GmailRestore(
            gmail_service=self.mock_service,
            backup_dir=self.backup_dir,
            state_file=custom_state_file
        )
        
        self.assertEqual(restore.state_file, Path(custom_state_file))
    
    def test_initialization_invalid_backup_dir(self):
        """Test error when backup directory structure is invalid."""
        invalid_backup_dir = os.path.join(self.temp_dir, 'invalid')
        os.makedirs(invalid_backup_dir)
        
        with self.assertRaises(ValueError) as context:
            GmailRestore(
                gmail_service=self.mock_service,
                backup_dir=invalid_backup_dir
            )
        
        self.assertIn('Invalid backup directory structure', str(context.exception))
    
    def test_default_batch_size(self):
        """Test default batch size is 10."""
        restore = GmailRestore(
            gmail_service=self.mock_service,
            backup_dir=self.backup_dir
        )
        
        self.assertEqual(restore.batch_size, 10)


class TestGmailRestoreState(unittest.TestCase):
    """Test cases for GmailRestore state management."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.backup_dir = os.path.join(self.temp_dir, 'backup')
        self.mock_service = MagicMock()
        
        os.makedirs(os.path.join(self.backup_dir, 'emails'))
        os.makedirs(os.path.join(self.backup_dir, 'metadata'))
    
    def tearDown(self):
        """Clean up test fixtures."""
        import shutil
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)
    
    def test_save_and_load_state(self):
        """Test saving and loading the restore state."""
        restore = GmailRestore(
            gmail_service=self.mock_service,
            backup_dir=self.backup_dir
        )
        
        # Modify state
        restore.state['restored_message_ids'].append('msg123')
        restore.state['total_restored'] = 1
        restore._save_state()
        
        # Create new instance and verify state was loaded
        new_restore = GmailRestore(
            gmail_service=self.mock_service,
            backup_dir=self.backup_dir
        )
        
        self.assertIn('msg123', new_restore.state['restored_message_ids'])
        self.assertEqual(new_restore.state['total_restored'], 1)
    
    def test_load_corrupted_state(self):
        """Test loading corrupted state file returns default state."""
        state_file = os.path.join(self.backup_dir, 'restore_state.json')
        with open(state_file, 'w') as f:
            f.write("invalid json {{{")
        
        restore = GmailRestore(
            gmail_service=self.mock_service,
            backup_dir=self.backup_dir
        )
        
        # Should have default state
        self.assertEqual(restore.state['restored_message_ids'], [])
        self.assertEqual(restore.state['total_restored'], 0)


class TestGmailRestoreImportEmail(unittest.TestCase):
    """Test cases for _import_email method."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.backup_dir = os.path.join(self.temp_dir, 'backup')
        self.mock_service = MagicMock()
        
        os.makedirs(os.path.join(self.backup_dir, 'emails'))
        os.makedirs(os.path.join(self.backup_dir, 'metadata'))
        
        self.restore = GmailRestore(
            gmail_service=self.mock_service,
            backup_dir=self.backup_dir
        )
    
    def tearDown(self):
        """Clean up test fixtures."""
        import shutil
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)
    
    def test_import_email_success(self):
        """Test successful email import."""
        # Create test email file
        email_path = Path(self.backup_dir) / 'emails' / 'test.eml'
        email_content = b"From: sender@example.com\nTo: recipient@example.com\nSubject: Test\n\nBody"
        email_path.write_bytes(email_content)
        
        # Mock successful import
        self.mock_service.users().messages().import_().execute.return_value = {
            'id': 'new_msg_id'
        }
        
        metadata = {'labels': ['INBOX']}
        result = self.restore._import_email(email_path, metadata)
        
        self.assertEqual(result, 'new_msg_id')
    
    def test_import_email_file_not_found(self):
        """Test import email with non-existent file."""
        email_path = Path(self.backup_dir) / 'emails' / 'nonexistent.eml'
        metadata = {'labels': ['INBOX']}
        
        result = self.restore._import_email(email_path, metadata)
        
        self.assertIsNone(result)
    
    def test_import_email_api_error(self):
        """Test import email handles API errors."""
        from googleapiclient.errors import HttpError
        
        email_path = Path(self.backup_dir) / 'emails' / 'test.eml'
        email_path.write_bytes(b"email content")
        
        mock_response = MagicMock()
        mock_response.status = 500
        self.mock_service.users().messages().import_().execute.side_effect = HttpError(
            resp=mock_response, content=b'Server error'
        )
        
        metadata = {'labels': ['INBOX']}
        result = self.restore._import_email(email_path, metadata)
        
        self.assertIsNone(result)


class TestGmailRestoreEmails(unittest.TestCase):
    """Test cases for restore_emails method."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.backup_dir = os.path.join(self.temp_dir, 'backup')
        self.mock_service = MagicMock()
        
        os.makedirs(os.path.join(self.backup_dir, 'emails', '2024', '01'))
        os.makedirs(os.path.join(self.backup_dir, 'metadata'))
    
    def tearDown(self):
        """Clean up test fixtures."""
        import shutil
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)
    
    def test_restore_empty_backup(self):
        """Test restore with no emails in backup."""
        restore = GmailRestore(
            gmail_service=self.mock_service,
            backup_dir=self.backup_dir
        )
        
        result = restore.restore_emails()
        
        self.assertEqual(result['total_restored'], 0)
        self.assertEqual(result['total_errors'], 0)
    
    @patch.object(GmailRestore, '_process_restore_batch')
    def test_restore_processes_batches(self, mock_process_batch):
        """Test that restore processes emails in batches."""
        mock_process_batch.return_value = (5, 0)
        
        # Create test metadata files
        for i in range(5):
            metadata = {
                'message_id': f'msg{i}',
                'backup_path': f'emails/2024/01/msg{i}.eml',
                'labels': ['INBOX']
            }
            with open(os.path.join(self.backup_dir, 'metadata', f'msg{i}.json'), 'w') as f:
                json.dump(metadata, f)
        
        restore = GmailRestore(
            gmail_service=self.mock_service,
            backup_dir=self.backup_dir,
            batch_size=10
        )
        
        result = restore.restore_emails()
        
        mock_process_batch.assert_called()
    
    @patch.object(GmailRestore, '_process_restore_batch')
    def test_restore_respects_max_results(self, mock_process_batch):
        """Test that restore respects max_results parameter."""
        mock_process_batch.return_value = (3, 0)
        
        # Create test metadata files
        for i in range(10):
            metadata = {
                'message_id': f'msg{i}',
                'backup_path': f'emails/2024/01/msg{i}.eml',
                'labels': ['INBOX']
            }
            with open(os.path.join(self.backup_dir, 'metadata', f'msg{i}.json'), 'w') as f:
                json.dump(metadata, f)
        
        restore = GmailRestore(
            gmail_service=self.mock_service,
            backup_dir=self.backup_dir,
            batch_size=3
        )
        
        result = restore.restore_emails(max_results=3)
        
        # Should stop after first batch
        self.assertEqual(mock_process_batch.call_count, 1)


class TestGmailRestoreProcessBatch(unittest.TestCase):
    """Test cases for _process_restore_batch method."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.backup_dir = os.path.join(self.temp_dir, 'backup')
        self.mock_service = MagicMock()
        
        os.makedirs(os.path.join(self.backup_dir, 'emails', '2024', '01'))
        os.makedirs(os.path.join(self.backup_dir, 'metadata'))
        
        self.restore = GmailRestore(
            gmail_service=self.mock_service,
            backup_dir=self.backup_dir
        )
    
    def tearDown(self):
        """Clean up test fixtures."""
        import shutil
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)
    
    def test_skips_already_restored_emails(self):
        """Test that already restored emails are skipped."""
        # Add to restored list
        self.restore.state['restored_message_ids'].append('msg123')
        
        # Create metadata file
        metadata = {
            'message_id': 'msg123',
            'backup_path': 'emails/2024/01/msg123.eml',
            'labels': ['INBOX']
        }
        metadata_file = Path(self.backup_dir) / 'metadata' / 'msg123.json'
        with open(metadata_file, 'w') as f:
            json.dump(metadata, f)
        
        processed, errors = self.restore._process_restore_batch([metadata_file])
        
        self.assertEqual(processed, 0)
        self.assertEqual(errors, 0)
    
    def test_handles_missing_email_file(self):
        """Test handling of missing email file."""
        # Create metadata without corresponding email file
        metadata = {
            'message_id': 'msg_missing',
            'backup_path': 'emails/2024/01/nonexistent.eml',
            'labels': ['INBOX']
        }
        metadata_file = Path(self.backup_dir) / 'metadata' / 'msg_missing.json'
        with open(metadata_file, 'w') as f:
            json.dump(metadata, f)
        
        processed, errors = self.restore._process_restore_batch([metadata_file])
        
        self.assertEqual(processed, 0)
        self.assertEqual(errors, 1)


if __name__ == "__main__":
    unittest.main()
