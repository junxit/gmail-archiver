"""Tests for the authentication module."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, mock_open

from gmail_archiver.auth import (
    get_gmail_credentials,
    get_gmail_credentials_browser,
    get_imap_credentials,
    save_auth_state,
    load_auth_state,
    SCOPES,
)


class TestSaveLoadAuthState(unittest.TestCase):
    """Test cases for save_auth_state and load_auth_state functions."""
    
    def test_save_and_load_auth_state(self):
        """Test saving and loading auth state."""
        test_state = {"test_key": "test_value", "nested": {"key": 123}}
        
        with tempfile.NamedTemporaryFile(delete=False, suffix='.json') as temp_file:
            temp_path = temp_file.name
        
        try:
            # Test saving state
            save_auth_state(test_state, temp_path)
            self.assertTrue(os.path.exists(temp_path))
            
            # Test loading state
            loaded_state = load_auth_state(temp_path)
            self.assertEqual(loaded_state, test_state)
            
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
    
    def test_load_nonexistent_file(self):
        """Test loading from non-existent file returns empty dict."""
        result = load_auth_state('/nonexistent/path/state.json')
        self.assertEqual(result, {})
    
    def test_load_invalid_json(self):
        """Test loading invalid JSON returns empty dict."""
        with tempfile.NamedTemporaryFile(delete=False, suffix='.json', mode='w') as temp_file:
            temp_file.write("not valid json {{{")
            temp_path = temp_file.name
        
        try:
            result = load_auth_state(temp_path)
            self.assertEqual(result, {})
        finally:
            os.unlink(temp_path)
    
    def test_save_creates_parent_directories(self):
        """Test save_auth_state creates parent directories."""
        with tempfile.TemporaryDirectory() as temp_dir:
            nested_path = os.path.join(temp_dir, 'nested', 'dir', 'state.json')
            save_auth_state({'key': 'value'}, nested_path)
            
            self.assertTrue(os.path.exists(nested_path))
            loaded = load_auth_state(nested_path)
            self.assertEqual(loaded, {'key': 'value'})


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
    """Test cases for OAuth scopes."""
    
    def test_scopes_include_gmail_readonly(self):
        """Test that scopes include readonly access."""
        self.assertIn('https://www.googleapis.com/auth/gmail.readonly', SCOPES)
    
    def test_scopes_include_gmail_modify(self):
        """Test that scopes include modify access."""
        self.assertIn('https://www.googleapis.com/auth/gmail.modify', SCOPES)


if __name__ == "__main__":
    unittest.main()
