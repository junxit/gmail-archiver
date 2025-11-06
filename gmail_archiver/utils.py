"""Utility functions for Gmail Archiver."""
import email
import hashlib
import json
import logging
import os
from email.policy import default
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union

from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials

logger = logging.getLogger(__name__)

def setup_logging(log_level: str = 'INFO') -> None:
    """Set up basic logging configuration.
    
    Args:
        log_level: The log level to use (DEBUG, INFO, WARNING, ERROR, CRITICAL).
    """
    logging.basicConfig(
        level=getattr(logging, log_level.upper()),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler(),
        ]
    )

def ensure_directory_exists(directory: Union[str, Path]) -> Path:
    """Ensure that a directory exists, creating it if necessary.
    
    Args:
        directory: The directory path to check/create.
        
    Returns:
        The Path object for the directory.
    """
    path = Path(directory).expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path

def get_email_hash(email_data: bytes) -> str:
    """Generate a hash for an email message.
    
    Args:
        email_data: The raw email message data.
        
    Returns:
        A hexadecimal string representing the SHA-256 hash of the email.
    """
    return hashlib.sha256(email_data).hexdigest()

def parse_email_message(email_data: bytes) -> email.message.Message:
    """Parse raw email data into an email message object.
    
    Args:
        email_data: The raw email data.
        
    Returns:
        An email.message.Message object.
    """
    return email.message_from_bytes(email_data, policy=default)

def get_safe_filename(filename: str, max_length: int = 255) -> str:
    """Convert a string to a safe filename.
    
    Args:
        filename: The original filename.
        max_length: Maximum length of the resulting filename.
        
    Returns:
        A safe version of the filename.
    """
    # Remove any characters that aren't alphanumeric, spaces, dots, or hyphens
    safe = "".join(c if c.isalnum() or c in (' ', '.', '-', '_') else '_' for c in filename)
    
    # Replace multiple spaces with a single underscore
    safe = '_'.join(safe.split())
    
    # Truncate if necessary
    if len(safe) > max_length:
        # Keep the extension if there is one
        name, ext = os.path.splitext(safe)
        ext = ext[:10]  # Limit extension length
        safe = name[:(max_length - len(ext) - 1)] + '_' + ext
    
    return safe

def get_gmail_service(credentials: Credentials):
    """Get a Gmail API service instance.
    
    Args:
        credentials: OAuth2 credentials.
        
    Returns:
        A Gmail API service instance.
    """
    return build('gmail', 'v1', credentials=credentials, cache_discovery=False)

def parse_email_address(email_address: str) -> Tuple[str, str]:
    """Parse an email address string into name and email parts.
    
    Args:
        email_address: The email address string to parse.
        
    Returns:
        A tuple of (name, email).
    """
    from email.utils import parseaddr
    name, addr = parseaddr(email_address)
    return name, addr.lower()

def format_size(size_in_bytes: int) -> str:
    """Format a size in bytes into a human-readable string.
    
    Args:
        size_in_bytes: The size in bytes.
        
    Returns:
        A formatted string (e.g., "1.5 MB").
    """
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size_in_bytes < 1024.0:
            if unit == 'B':
                return f"{int(size_in_bytes)} {unit}"
            return f"{size_in_bytes:.1f} {unit}"
        size_in_bytes /= 1024.0
    return f"{size_in_bytes:.1f} PB"

def load_json_file(file_path: Union[str, Path]) -> Any:
    """Load JSON data from a file.
    
    Args:
        file_path: Path to the JSON file.
        
    Returns:
        The parsed JSON data.
    """
    with open(file_path, 'r', encoding='utf-8') as f:
        return json.load(f)

def save_json_file(data: Any, file_path: Union[str, Path], indent: int = 2) -> None:
    """Save data to a JSON file.
    
    Args:
        data: The data to save.
        file_path: Path to the output file.
        indent: Number of spaces to use for indentation.
    """
    with open(file_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=indent, ensure_ascii=False, default=str)
