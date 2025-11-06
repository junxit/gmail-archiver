"""Tests for the backup module."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from gmail_archiver.backup import GmailBackup

class TestGmailBackup(unittest.TestCase):
    """Test cases for GmailBackup class."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.backup_dir = os.path.join(self.temp_dir, 'backup')
        self.state_file = os.path.join(self.temp_dir, 'state.json')
        
        # Create a mock Gmail service
        self.mock_service = MagicMock()
        
        # Initialize the backup class
        self.backup = GmailBackup(
            gmail_service=self.mock_service,
            backup_dir=self.backup_dir,
            state_file=self.state_file,
            batch_size=10
        )
    
    def test_initialization(self):
        """Test that the backup class initializes correctly."""
        self.assertEqual(str(self.backup.backup_dir), str(Path(self.backup_dir).resolve()))
        self.assertEqual(str(self.backup.state_file), str(Path(self.state_file).resolve()))
        self.assertEqual(self.backup.batch_size, 10)
        self.assertIsInstance(self.backup.state, dict)
        self.assertTrue(os.path.exists(self.backup_dir))
    
    def test_save_and_load_state(self):
        """Test saving and loading the backup state."""
        # Save some test data
        test_data = {"test_key": "test_value"}
        self.backup.state = test_data
        self.backup._save_state()
        
        # Create a new instance to load the state
        new_backup = GmailBackup(
            gmail_service=self.mock_service,
            backup_dir=self.backup_dir,
            state_file=self.state_file,
            batch_size=10
        )
        
        # Verify the state was saved and loaded correctly
        self.assertEqual(new_backup.state, test_data)
        self.assertTrue(os.path.exists(self.state_file))
    
    @patch('gmail_archiver.backup.GmailBackup._process_email_batch')
    @patch('gmail_archiver.backup.GmailBackup._get_email')
    def test_backup_emails(self, mock_get_email, mock_process_batch):
        """Test the backup_emails method."""
        # Set up mock return values
        mock_process_batch.return_value = (5, 0)  # 5 processed, 0 errors
        
        # Mock the Gmail API responses
        mock_list_response = {
            'messages': [{'id': f'msg_{i}'} for i in range(10)],
            'nextPageToken': None  # No more pages
        }
        
        # Mock email data
        mock_email = {
            'id': 'test_id',
            'threadId': 'test_thread',
            'labelIds': ['INBOX'],
            'payload': {'headers': []}
        }
        
        self.mock_service.users().messages().list().execute.return_value = mock_list_response
        mock_get_email.return_value = mock_email
        
        # Run the backup
        result = self.backup.backup_emails(max_results=10)
        
        # Verify the results
        self.assertEqual(result['total_processed'], 10)  # We have 10 messages in the mock
        self.assertEqual(result['total_errors'], 0)
        self.assertIn('last_backup_time', result)
        self.assertEqual(result['emails_processed'], 10)
    
    def tearDown(self):
        """Clean up test fixtures."""
        import shutil
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

if __name__ == "__main__":
    unittest.main()
