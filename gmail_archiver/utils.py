"""Utility functions for Gmail Archiver."""
import email
import hashlib
import json
import logging
import os
import re
from email.policy import default
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union

from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials

logger = logging.getLogger(__name__)

# Maximum length of a single path component. APFS, ext4 and NTFS all cap at 255
# *bytes*, not characters, so every clamp below works on the UTF-8 encoding.
MAX_FILENAME_BYTES = 255

# Longest sanitized message key allowed in a filename, leaving room for the hash
# fragment, the subject fragment and the '.eml' suffix within MAX_FILENAME_BYTES.
MAX_KEY_BYTES = 128

# Everything outside this set is replaced when a message id is turned into a path
# component. Notably excludes '/', '\' and NUL, which is what makes traversal
# impossible; see safe_key().
_UNSAFE_KEY_CHARS = re.compile(r'[^A-Za-z0-9._@+-]')

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

def truncate_to_bytes(text: str, max_bytes: int) -> str:
    """Truncate a string so its UTF-8 encoding fits within ``max_bytes``.

    Truncation lands on a character boundary: a partial multi-byte sequence at
    the cut point is dropped rather than producing invalid UTF-8.

    Args:
        text: The string to truncate.
        max_bytes: Maximum size of the UTF-8 encoding.

    Returns:
        ``text`` unchanged if it already fits, otherwise the longest prefix that
        does.
    """
    encoded = text.encode('utf-8')
    if len(encoded) <= max_bytes:
        return text
    if max_bytes <= 0:
        return ''
    return encoded[:max_bytes].decode('utf-8', errors='ignore')

def safe_key(raw_key: str, fallback: str) -> str:
    """Sanitize a message identifier for use as a filesystem path component.

    Message ids are not always trustworthy: on the IMAP path a message with no
    ``X-GM-MSGID`` falls back to its RFC822 ``Message-ID`` header, which is
    sender-controlled and may contain path separators or dot segments. Passing
    such a value into a path would let a message escape the backup directory or
    overwrite the archive's own index, so every id is funneled through here
    before it touches the filesystem.

    Args:
        raw_key: The candidate identifier (a Gmail id, X-GM-MSGID, or a raw
            ``Message-ID`` header).
        fallback: Value to return when ``raw_key`` sanitizes to nothing usable —
            callers pass a content hash so the message is still archived.

    Returns:
        A path-safe component containing only ``[A-Za-z0-9._@+-]``, never empty,
        never a dot segment, and never longer than ``MAX_KEY_BYTES`` bytes.
    """
    candidate = (raw_key or '').strip().strip('<>').strip()
    candidate = _UNSAFE_KEY_CHARS.sub('_', candidate)
    # Strip leading dots so the result can never be '.', '..' or a hidden file.
    candidate = candidate.lstrip('.')
    candidate = truncate_to_bytes(candidate, MAX_KEY_BYTES)
    if not candidate or not set(candidate) - {'.', '_'}:
        return fallback
    return candidate

def build_email_filename(msg_key: str, email_hash: str, subject: str) -> str:
    """Assemble the ``.eml`` filename, clamped to ``MAX_FILENAME_BYTES``.

    The subject is the only variable-length part, so it absorbs the clamp; if
    there is no room for it the filename degrades to ``key_hash.eml`` rather
    than raising. Without this, a long non-ASCII subject produces a name over
    the 255-byte limit and the write fails with ``ENAMETOOLONG``.

    Args:
        msg_key: An already-sanitized key from :func:`safe_key`.
        email_hash: Hex digest of the raw message; the first 8 chars are used.
        subject: The message subject, or any falsy value for none.

    Returns:
        A filename whose UTF-8 encoding is at most ``MAX_FILENAME_BYTES`` bytes.
    """
    suffix = '.eml'
    stem = f"{msg_key}_{email_hash[:8]}"
    budget = MAX_FILENAME_BYTES - len(stem.encode('utf-8')) - len(suffix) - 1
    safe_subject = truncate_to_bytes(get_safe_filename(subject or 'no-subject'), budget)
    if safe_subject:
        return f"{stem}_{safe_subject}{suffix}"
    return f"{stem}{suffix}"

def ensure_within(path: Union[str, Path], root: Union[str, Path]) -> Path:
    """Resolve ``path`` and verify it stays inside ``root``.

    ``Path.relative_to`` is purely lexical and happily returns a path containing
    ``..`` segments, so it cannot be used as a containment check. This resolves
    both sides first and compares the real locations.

    Args:
        path: The path to check.
        root: The directory the path must remain inside.

    Returns:
        The resolved path.

    Raises:
        ValueError: If the resolved path falls outside ``root``.
    """
    resolved = Path(path).resolve()
    root_resolved = Path(root).resolve()
    if not resolved.is_relative_to(root_resolved):
        raise ValueError(f"Path escapes the backup directory: {resolved} not under {root_resolved}")
    return resolved

def _fsync_dir(directory: Path) -> None:
    """Flush a directory entry so a rename into it survives a crash."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError as e:  # pragma: no cover - platform dependent
        logger.debug("Could not open %s for fsync: %s", directory, e)
        return
    try:
        os.fsync(fd)
    except OSError as e:  # pragma: no cover - some filesystems refuse this
        logger.debug("Could not fsync %s: %s", directory, e)
    finally:
        os.close(fd)

def write_atomic(path: Union[str, Path], data: bytes, mode: int = 0o644) -> Path:
    """Write ``data`` to ``path`` atomically and durably.

    Writes to a sibling temp file, fsyncs it, then renames over the target and
    fsyncs the directory. A crash therefore leaves either the old file or the
    complete new one — never a truncated ``.eml``. The file is created with
    ``mode`` from the outset (not chmod'd afterwards), so a secret never exists
    world-readable even briefly.

    Args:
        path: Destination path.
        data: Bytes to write.
        mode: Permission bits for the created file.

    Returns:
        The destination path.
    """
    path = Path(path)
    tmp = path.with_name(f"{path.name}.tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)
    _fsync_dir(path.parent)
    return path

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
