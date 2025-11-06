"""Tests for the authentication module."""
import os
import tempfile
import unittest
from pathlib import Path

from gmail_archiver.auth import get_gmail_credentials, save_auth_state, load_auth_state

class TestAuth(unittest.TestCase):
    """Test cases for authentication functions."""
    
    def test_save_and_load_auth_state(self):
        """Test saving and loading auth state."""
        test_state = {"test_key": "test_value"}
        
        with tempfile.NamedTemporaryFile(delete=False) as temp_file:
            temp_path = temp_file.name
        
        try:
            # Test saving state
            save_auth_state(test_state, temp_path)
            
            # Test loading state
            loaded_state = load_auth_state(temp_path)
            self.assertEqual(loaded_state, test_state)
            
        finally:
            # Clean up
            if os.path.exists(temp_path):
                os.unlink(temp_path)

if __name__ == "__main__":
    unittest.main()
