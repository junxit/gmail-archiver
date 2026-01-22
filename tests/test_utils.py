"""Tests for the utils module."""
import email
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from gmail_archiver.utils import (
    setup_logging,
    ensure_directory_exists,
    get_email_hash,
    parse_email_message,
    get_safe_filename,
    get_gmail_service,
    parse_email_address,
    format_size,
    load_json_file,
    save_json_file,
)


class TestSetupLogging(unittest.TestCase):
    """Test cases for setup_logging function."""
    
    def test_setup_logging_info(self):
        """Test setting up INFO level logging."""
        # Should not raise any exceptions
        setup_logging('INFO')
    
    def test_setup_logging_debug(self):
        """Test setting up DEBUG level logging."""
        setup_logging('DEBUG')
    
    def test_setup_logging_case_insensitive(self):
        """Test that log level is case insensitive."""
        setup_logging('debug')
        setup_logging('INFO')
        setup_logging('Warning')


class TestEnsureDirectoryExists(unittest.TestCase):
    """Test cases for ensure_directory_exists function."""
    
    def test_creates_directory(self):
        """Test creating a new directory."""
        with tempfile.TemporaryDirectory() as temp_dir:
            new_dir = os.path.join(temp_dir, 'new', 'nested', 'dir')
            
            result = ensure_directory_exists(new_dir)
            
            self.assertTrue(result.exists())
            self.assertTrue(result.is_dir())
    
    def test_existing_directory(self):
        """Test with existing directory."""
        with tempfile.TemporaryDirectory() as temp_dir:
            result = ensure_directory_exists(temp_dir)
            
            self.assertTrue(result.exists())
    
    def test_expands_user_path(self):
        """Test that ~ is expanded."""
        result = ensure_directory_exists('~/test_gmail_archiver_temp')
        
        self.assertTrue(str(result).startswith(str(Path.home())))
        
        # Cleanup
        if result.exists():
            result.rmdir()
    
    def test_returns_path_object(self):
        """Test that function returns Path object."""
        with tempfile.TemporaryDirectory() as temp_dir:
            result = ensure_directory_exists(temp_dir)
            
            self.assertIsInstance(result, Path)


class TestGetEmailHash(unittest.TestCase):
    """Test cases for get_email_hash function."""
    
    def test_returns_hex_string(self):
        """Test that hash is a hex string."""
        result = get_email_hash(b'test email content')
        
        self.assertIsInstance(result, str)
        # SHA-256 hex digest is 64 characters
        self.assertEqual(len(result), 64)
        # Should only contain hex characters
        self.assertTrue(all(c in '0123456789abcdef' for c in result))
    
    def test_same_input_same_hash(self):
        """Test that same input produces same hash."""
        content = b'consistent email content'
        
        hash1 = get_email_hash(content)
        hash2 = get_email_hash(content)
        
        self.assertEqual(hash1, hash2)
    
    def test_different_input_different_hash(self):
        """Test that different inputs produce different hashes."""
        hash1 = get_email_hash(b'content 1')
        hash2 = get_email_hash(b'content 2')
        
        self.assertNotEqual(hash1, hash2)


class TestParseEmailMessage(unittest.TestCase):
    """Test cases for parse_email_message function."""
    
    def test_parses_simple_email(self):
        """Test parsing a simple email."""
        email_data = b"""From: sender@example.com
To: recipient@example.com
Subject: Test Subject
Date: Mon, 1 Jan 2024 12:00:00 +0000

This is the body.
"""
        result = parse_email_message(email_data)
        
        self.assertEqual(result['From'], 'sender@example.com')
        self.assertEqual(result['To'], 'recipient@example.com')
        self.assertEqual(result['Subject'], 'Test Subject')
    
    def test_returns_message_object(self):
        """Test that function returns email.message.Message."""
        email_data = b"From: test@test.com\n\nBody"
        
        result = parse_email_message(email_data)
        
        self.assertIsInstance(result, email.message.Message)


class TestGetSafeFilename(unittest.TestCase):
    """Test cases for get_safe_filename function."""
    
    def test_removes_special_characters(self):
        """Test removing special characters."""
        result = get_safe_filename('file/with\\special:chars?')
        
        self.assertNotIn('/', result)
        self.assertNotIn('\\', result)
        self.assertNotIn(':', result)
        self.assertNotIn('?', result)
    
    def test_preserves_alphanumeric(self):
        """Test that alphanumeric characters are preserved."""
        result = get_safe_filename('SimpleFileName123')
        
        self.assertEqual(result, 'SimpleFileName123')
    
    def test_replaces_spaces_with_underscores(self):
        """Test that multiple spaces are replaced with single underscore."""
        result = get_safe_filename('file with   multiple spaces')
        
        self.assertNotIn('  ', result)
        self.assertIn('_', result)
    
    def test_respects_max_length(self):
        """Test that result respects max_length."""
        long_name = 'a' * 300
        
        result = get_safe_filename(long_name, max_length=50)
        
        self.assertLessEqual(len(result), 50)
    
    def test_preserves_extension(self):
        """Test that file extension is preserved when truncating."""
        long_name = 'a' * 300 + '.txt'
        
        result = get_safe_filename(long_name, max_length=50)
        
        self.assertTrue(result.endswith('.txt'))
    
    def test_allows_dots_and_hyphens(self):
        """Test that dots and hyphens are allowed."""
        result = get_safe_filename('file-name.with.dots')
        
        self.assertIn('-', result)
        self.assertIn('.', result)


class TestGetGmailService(unittest.TestCase):
    """Test cases for get_gmail_service function."""
    
    @patch('gmail_archiver.utils.build')
    def test_builds_gmail_service(self, mock_build):
        """Test that Gmail service is built correctly."""
        mock_credentials = MagicMock()
        mock_service = MagicMock()
        mock_build.return_value = mock_service
        
        result = get_gmail_service(mock_credentials)
        
        mock_build.assert_called_once_with(
            'gmail', 'v1', 
            credentials=mock_credentials, 
            cache_discovery=False
        )
        self.assertEqual(result, mock_service)


class TestParseEmailAddress(unittest.TestCase):
    """Test cases for parse_email_address function."""
    
    def test_simple_email(self):
        """Test parsing simple email address."""
        name, email_addr = parse_email_address('user@example.com')
        
        self.assertEqual(email_addr, 'user@example.com')
    
    def test_email_with_name(self):
        """Test parsing email with display name."""
        name, email_addr = parse_email_address('John Doe <john@example.com>')
        
        self.assertEqual(name, 'John Doe')
        self.assertEqual(email_addr, 'john@example.com')
    
    def test_returns_lowercase_email(self):
        """Test that email is returned lowercase."""
        name, email_addr = parse_email_address('User@EXAMPLE.COM')
        
        self.assertEqual(email_addr, 'user@example.com')
    
    def test_quoted_name(self):
        """Test email with quoted display name."""
        name, email_addr = parse_email_address('"Doe, John" <john@example.com>')
        
        self.assertEqual(name, 'Doe, John')
        self.assertEqual(email_addr, 'john@example.com')


class TestFormatSize(unittest.TestCase):
    """Test cases for format_size function."""
    
    def test_bytes(self):
        """Test formatting bytes."""
        result = format_size(500)
        
        self.assertEqual(result, '500 B')
    
    def test_kilobytes(self):
        """Test formatting kilobytes."""
        result = format_size(1024)
        
        self.assertEqual(result, '1.0 KB')
    
    def test_megabytes(self):
        """Test formatting megabytes."""
        result = format_size(1024 * 1024)
        
        self.assertEqual(result, '1.0 MB')
    
    def test_gigabytes(self):
        """Test formatting gigabytes."""
        result = format_size(1024 * 1024 * 1024)
        
        self.assertEqual(result, '1.0 GB')
    
    def test_fractional_values(self):
        """Test formatting fractional values."""
        result = format_size(1536)  # 1.5 KB
        
        self.assertEqual(result, '1.5 KB')
    
    def test_zero(self):
        """Test formatting zero bytes."""
        result = format_size(0)
        
        self.assertEqual(result, '0 B')


class TestJsonFileOperations(unittest.TestCase):
    """Test cases for load_json_file and save_json_file functions."""
    
    def test_save_and_load_json(self):
        """Test saving and loading JSON data."""
        test_data = {
            'string': 'value',
            'number': 42,
            'list': [1, 2, 3],
            'nested': {'key': 'value'}
        }
        
        with tempfile.NamedTemporaryFile(delete=False, suffix='.json') as f:
            temp_path = f.name
        
        try:
            save_json_file(test_data, temp_path)
            loaded_data = load_json_file(temp_path)
            
            self.assertEqual(loaded_data, test_data)
        finally:
            os.unlink(temp_path)
    
    def test_save_with_indent(self):
        """Test saving with custom indent."""
        test_data = {'key': 'value'}
        
        with tempfile.NamedTemporaryFile(delete=False, suffix='.json') as f:
            temp_path = f.name
        
        try:
            save_json_file(test_data, temp_path, indent=4)
            
            with open(temp_path, 'r') as f:
                content = f.read()
            
            # Should have indentation
            self.assertIn('    ', content)
        finally:
            os.unlink(temp_path)
    
    def test_save_handles_non_ascii(self):
        """Test saving non-ASCII characters."""
        test_data = {'message': 'Hello 世界 🌍'}
        
        with tempfile.NamedTemporaryFile(delete=False, suffix='.json') as f:
            temp_path = f.name
        
        try:
            save_json_file(test_data, temp_path)
            loaded_data = load_json_file(temp_path)
            
            self.assertEqual(loaded_data['message'], 'Hello 世界 🌍')
        finally:
            os.unlink(temp_path)
    
    def test_load_with_path_object(self):
        """Test loading with Path object."""
        test_data = {'key': 'value'}
        
        with tempfile.NamedTemporaryFile(delete=False, suffix='.json') as f:
            temp_path = Path(f.name)
        
        try:
            save_json_file(test_data, temp_path)
            loaded_data = load_json_file(temp_path)
            
            self.assertEqual(loaded_data, test_data)
        finally:
            temp_path.unlink()


if __name__ == "__main__":
    unittest.main()
