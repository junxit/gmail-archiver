"""Tests for the authentication module."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, mock_open

from gmail_archiver.auth import (
    BACKUP_SCOPES,
    RESTORE_SCOPES,
    get_gmail_credentials,
    get_gmail_credentials_browser,
    get_imap_credentials,
    save_token,
    SCOPES,
)


class TestSaveToken(unittest.TestCase):
    """The token file holds a refresh token, so it must never be world-readable."""

    def _creds(self, payload='{"refresh_token": "secret"}'):
        creds = MagicMock()
        creds.to_json.return_value = payload
        return creds

    def test_token_written_owner_only(self):
        """The saved token is mode 0600."""
        with tempfile.TemporaryDirectory() as temp_dir:
            token_path = os.path.join(temp_dir, 'token.json')
            save_token(self._creds(), token_path)

            mode = os.stat(token_path).st_mode & 0o777
            self.assertEqual(mode, 0o600, f"expected 0600, got {oct(mode)}")

    def test_token_directory_is_owner_only(self):
        """The directory holding the token is tightened to 0700."""
        with tempfile.TemporaryDirectory() as temp_dir:
            token_path = os.path.join(temp_dir, 'nested', 'token.json')
            save_token(self._creds(), token_path)

            mode = os.stat(os.path.dirname(token_path)).st_mode & 0o777
            self.assertEqual(mode, 0o700, f"expected 0700, got {oct(mode)}")

    def test_creates_parent_directories_and_round_trips(self):
        """save_token creates missing parents and writes the credential JSON."""
        with tempfile.TemporaryDirectory() as temp_dir:
            token_path = os.path.join(temp_dir, 'a', 'b', 'token.json')
            save_token(self._creds('{"refresh_token": "abc"}'), token_path)

            self.assertTrue(os.path.exists(token_path))
            self.assertEqual(json.loads(Path(token_path).read_text()),
                             {"refresh_token": "abc"})

    def test_overwrites_existing_token_atomically(self):
        """Re-saving replaces the old token and leaves no temp file behind."""
        with tempfile.TemporaryDirectory() as temp_dir:
            token_path = os.path.join(temp_dir, 'token.json')
            save_token(self._creds('{"v": 1}'), token_path)
            save_token(self._creds('{"v": 2}'), token_path)

            self.assertEqual(json.loads(Path(token_path).read_text()), {"v": 2})
            leftovers = [p for p in os.listdir(temp_dir) if p.endswith('.tmp')]
            self.assertEqual(leftovers, [])


class TestGetGmailCredentials(unittest.TestCase):
    """Test cases for get_gmail_credentials function."""
    
    def test_credentials_file_not_found(self):
        """Test error when credentials file doesn't exist."""
        with tempfile.TemporaryDirectory() as temp_dir:
            token_path = os.path.join(temp_dir, 'token.json')
            creds_path = os.path.join(temp_dir, 'nonexistent.json')
            
            with self.assertRaises(FileNotFoundError) as context:
                get_gmail_credentials(token_path, creds_path)
            
            self.assertIn('Credentials file not found', str(context.exception))
    
    @patch('gmail_archiver.auth.Credentials')
    def test_load_existing_valid_token(self, mock_credentials):
        """Test loading existing valid token."""
        mock_creds = MagicMock()
        mock_creds.valid = True
        mock_credentials.from_authorized_user_file.return_value = mock_creds
        
        with tempfile.TemporaryDirectory() as temp_dir:
            token_path = os.path.join(temp_dir, 'token.json')
            creds_path = os.path.join(temp_dir, 'credentials.json')
            
            # Create dummy files
            Path(token_path).write_text('{}')
            Path(creds_path).write_text('{}')
            
            result = get_gmail_credentials(token_path, creds_path)
            
            self.assertEqual(result, mock_creds)
            mock_credentials.from_authorized_user_file.assert_called_once()
    
    @patch('gmail_archiver.auth.InstalledAppFlow')
    @patch('gmail_archiver.auth.Request')
    @patch('gmail_archiver.auth.Credentials')
    def test_refresh_expired_token(self, mock_credentials, mock_request, mock_flow):
        """Test refreshing expired token with refresh_token."""
        mock_creds = MagicMock()
        mock_creds.valid = False
        mock_creds.expired = True
        mock_creds.refresh_token = 'refresh_token'
        mock_creds.to_json.return_value = '{}'  # Mock the to_json method
        mock_credentials.from_authorized_user_file.return_value = mock_creds
        
        # After refresh, token should be valid
        def mark_valid(*args, **kwargs):
            mock_creds.valid = True
        mock_creds.refresh.side_effect = mark_valid
        
        with tempfile.TemporaryDirectory() as temp_dir:
            token_path = os.path.join(temp_dir, 'token.json')
            creds_path = os.path.join(temp_dir, 'credentials.json')
            
            Path(token_path).write_text('{}')
            Path(creds_path).write_text('{}')
            
            result = get_gmail_credentials(token_path, creds_path)
            
            mock_creds.refresh.assert_called_once()


class TestGetGmailCredentialsBrowser(unittest.TestCase):
    """Test cases for get_gmail_credentials_browser function."""
    
    def test_bundled_credentials_placeholder_error(self):
        """Test error when bundled credentials are placeholders."""
        with tempfile.TemporaryDirectory() as temp_dir:
            token_path = os.path.join(temp_dir, 'token.json')
            
            with self.assertRaises(ValueError) as context:
                get_gmail_credentials_browser(token_path=token_path)
            
            self.assertIn('Browser authentication requires valid OAuth credentials', 
                         str(context.exception))
    
    @patch('gmail_archiver.auth.Credentials')
    def test_load_existing_valid_token(self, mock_credentials):
        """Test loading existing valid token for browser auth."""
        mock_creds = MagicMock()
        mock_creds.valid = True
        mock_credentials.from_authorized_user_file.return_value = mock_creds
        
        with tempfile.TemporaryDirectory() as temp_dir:
            token_path = os.path.join(temp_dir, 'token.json')
            Path(token_path).write_text('{}')
            
            result = get_gmail_credentials_browser(token_path=token_path)
            
            self.assertEqual(result, mock_creds)


class TestGetImapCredentials(unittest.TestCase):
    """Test cases for get_imap_credentials function."""
    
    def test_empty_email_raises_error(self):
        """Test error when email is empty."""
        with self.assertRaises(ValueError) as context:
            get_imap_credentials('', 'password')
        
        self.assertIn('Email address is required', str(context.exception))
    
    def test_empty_password_raises_error(self):
        """Test error when password is empty."""
        with self.assertRaises(ValueError) as context:
            get_imap_credentials('user@gmail.com', '')
        
        self.assertIn('App password is required', str(context.exception))
    
    def test_whitespace_only_email_raises_error(self):
        """Test error when email is whitespace only."""
        with self.assertRaises(ValueError):
            get_imap_credentials('   ', 'password')
    
    @patch('gmail_archiver.auth.MailBox')
    def test_successful_imap_login(self, mock_mailbox_class):
        """Test successful IMAP login."""
        mock_mailbox = MagicMock()
        mock_mailbox_class.return_value = mock_mailbox
        
        result = get_imap_credentials('user@gmail.com', 'app_password', 'imap.gmail.com')
        
        mock_mailbox_class.assert_called_once_with('imap.gmail.com')
        mock_mailbox.login.assert_called_once_with('user@gmail.com', 'app_password')
        self.assertEqual(result, mock_mailbox)


class TestScopes(unittest.TestCase):
    """Backup must not hold write access; only restore may."""

    def test_backup_scopes_are_readonly(self):
        """The default (backup) scopes grant read access only."""
        self.assertIn('https://www.googleapis.com/auth/gmail.readonly', BACKUP_SCOPES)
        self.assertEqual(SCOPES, BACKUP_SCOPES)

    def test_backup_scopes_exclude_write_access(self):
        """A leaked backup token must not be able to modify or delete mail."""
        for scope in BACKUP_SCOPES:
            self.assertNotIn('gmail.modify', scope)
            self.assertNotIn('gmail.insert', scope)
            self.assertNotIn('mail.google.com', scope)

    def test_restore_scopes_include_modify(self):
        """Restore needs write access to import messages back."""
        self.assertIn('https://www.googleapis.com/auth/gmail.modify', RESTORE_SCOPES)


if __name__ == "__main__":
    unittest.main()
