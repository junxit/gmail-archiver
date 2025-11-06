"""Authentication module for Gmail Archiver."""
import json
import os
from pathlib import Path
from typing import Any, Dict

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from imap_tools import MailBox, MailboxLoginError

# If modifying these scopes, delete the token.json file.
SCOPES = [
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/gmail.modify',
]


def get_gmail_credentials(token_path: str, credentials_path: str) -> Credentials:
    """Get valid user credentials from storage or prompt for login.
    
    Args:
        token_path: Path to the token file.
        credentials_path: Path to the credentials file.
        
    Returns:
        Credentials, the obtained credential.
    """
    creds = None
    token_path = Path(token_path)
    
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    
    # If there are no (valid) credentials available, let the user log in.
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                credentials_path, SCOPES)
            creds = flow.run_local_server(port=0)
        
        # Save the credentials for the next run
        with open(token_path, 'w', encoding='utf-8') as token:
            token.write(creds.to_json())
    
    return creds


def get_imap_credentials(
    email: str,
    app_password: str,
    imap_server: str = 'imap.gmail.com'
) -> MailBox:
    """Get IMAP mailbox connection.
    
    Args:
        email: User's email address.
        app_password: App password for IMAP access.
        imap_server: IMAP server address.
        
    Returns:
        Authenticated MailBox instance.
        
    Raises:
        MailboxLoginError: If authentication fails.
    """
    try:
        mailbox = MailBox(imap_server)
        mailbox.login(email, app_password)
        return mailbox
    except MailboxLoginError as e:
        print(f"IMAP login failed: {e}")
        raise


def save_auth_state(state: Dict[str, Any], state_file: str) -> None:
    """Save authentication state to a file.
    
    Args:
        state: Dictionary containing state information.
        state_file: Path to the state file.
    """
    with open(state_file, 'w', encoding='utf-8') as f:
        json.dump(state, f)


def load_auth_state(state_file: str) -> Dict[str, Any]:
    """Load authentication state from a file.
    
    Args:
        state_file: Path to the state file.
        
    Returns:
        Dictionary containing the saved state.
    """
    if not os.path.exists(state_file):
        return {}
    
    with open(state_file, 'r', encoding='utf-8') as f:
        return json.load(f)
