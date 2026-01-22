"""Authentication module for Gmail Archiver."""
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional, Union

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from imap_tools import MailBox, MailboxLoginError

logger = logging.getLogger(__name__)

# If modifying these scopes, delete the token.json file.
SCOPES = [
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/gmail.modify',
]

# Bundled OAuth client credentials for browser-based authentication
# Users can use their own credentials by providing a client_secrets.json file
BUNDLED_CLIENT_CONFIG = {
    "installed": {
        "client_id": "YOUR_CLIENT_ID.apps.googleusercontent.com",
        "project_id": "gmail-archiver",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
        "client_secret": "YOUR_CLIENT_SECRET",
        "redirect_uris": ["http://localhost"]
    }
}


def get_gmail_credentials(token_path: str, credentials_path: str) -> Credentials:
    """Get valid user credentials from storage or prompt for login.
    
    Args:
        token_path: Path to the token file.
        credentials_path: Path to the credentials file.
        
    Returns:
        Credentials, the obtained credential.
        
    Raises:
        FileNotFoundError: If credentials file doesn't exist.
        ValueError: If credentials file is invalid.
    """
    creds = None
    token_path = Path(token_path).expanduser().resolve()
    credentials_path = Path(credentials_path).expanduser().resolve()
    
    # Check if credentials file exists
    if not credentials_path.exists():
        raise FileNotFoundError(
            f"Credentials file not found: {credentials_path}\n"
            "Please download OAuth credentials from Google Cloud Console or use --auth-method browser"
        )
    
    if token_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        except Exception as e:
            logger.warning(f"Failed to load existing token: {e}")
            creds = None
    
    # If there are no (valid) credentials available, let the user log in.
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as e:
                logger.warning(f"Failed to refresh token: {e}. Re-authenticating...")
                creds = None
        
        if not creds:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(credentials_path), SCOPES)
            creds = flow.run_local_server(port=0)
        
        # Save the credentials for the next run
        token_path.parent.mkdir(parents=True, exist_ok=True)
        with open(token_path, 'w', encoding='utf-8') as token:
            token.write(creds.to_json())
        logger.info(f"Credentials saved to {token_path}")
    
    return creds


def get_gmail_credentials_browser(
    token_path: Optional[str] = None,
    client_config: Optional[Dict[str, Any]] = None
) -> Credentials:
    """Get Gmail credentials using browser-based OAuth without custom credentials file.
    
    This method allows users to authenticate without creating their own Google Cloud
    project. It uses bundled OAuth credentials or user-provided config.
    
    Args:
        token_path: Path to store the token file. Defaults to ~/.gmail-archiver/token.json
        client_config: Optional custom OAuth client configuration. If not provided,
                       uses the bundled credentials.
        
    Returns:
        Credentials, the obtained credential.
        
    Raises:
        ValueError: If authentication fails.
    """
    # Default token path
    if token_path is None:
        token_path = Path.home() / '.gmail-archiver' / 'token.json'
    else:
        token_path = Path(token_path).expanduser().resolve()
    
    creds = None
    
    # Try to load existing credentials
    if token_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
            logger.info("Loaded existing credentials from token file")
        except Exception as e:
            logger.warning(f"Failed to load existing token: {e}")
            creds = None
    
    # Refresh or get new credentials
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                logger.info("Successfully refreshed credentials")
            except Exception as e:
                logger.warning(f"Failed to refresh token: {e}. Re-authenticating...")
                creds = None
        
        if not creds:
            # Use provided config or bundled credentials
            config = client_config or BUNDLED_CLIENT_CONFIG
            
            # Check if bundled credentials are placeholder values
            if config == BUNDLED_CLIENT_CONFIG and "YOUR_CLIENT_ID" in config["installed"]["client_id"]:
                raise ValueError(
                    "Browser authentication requires valid OAuth credentials.\n\n"
                    "Options:\n"
                    "1. Use --auth-method oauth with your own client_secrets.json from Google Cloud Console\n"
                    "2. Use --auth-method imap with an app password\n"
                    "3. Set up your own OAuth credentials and provide via --client-secrets\n\n"
                    "See README.md for detailed setup instructions."
                )
            
            flow = InstalledAppFlow.from_client_config(config, SCOPES)
            
            logger.info("Opening browser for authentication...")
            print("\n" + "="*60)
            print("AUTHENTICATION REQUIRED")
            print("="*60)
            print("A browser window will open for you to sign in to Google.")
            print("After signing in, you'll be redirected back to this application.")
            print("="*60 + "\n")
            
            creds = flow.run_local_server(
                port=0,
                prompt='consent',
                success_message='Authentication successful! You can close this window.',
                open_browser=True
            )
        
        # Save credentials for future use
        token_path.parent.mkdir(parents=True, exist_ok=True)
        with open(token_path, 'w', encoding='utf-8') as token:
            token.write(creds.to_json())
        logger.info(f"Credentials saved to {token_path}")
    
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
        ValueError: If email or password is empty.
    """
    if not email or not email.strip():
        raise ValueError("Email address is required for IMAP authentication")
    if not app_password or not app_password.strip():
        raise ValueError("App password is required for IMAP authentication")
    
    try:
        mailbox = MailBox(imap_server)
        mailbox.login(email.strip(), app_password.strip())
        logger.info(f"Successfully connected to {imap_server} as {email}")
        return mailbox
    except MailboxLoginError as e:
        logger.error(f"IMAP login failed for {email}: {e}")
        raise


def save_auth_state(state: Dict[str, Any], state_file: Union[str, Path]) -> None:
    """Save authentication state to a file.
    
    Args:
        state: Dictionary containing state information.
        state_file: Path to the state file.
        
    Raises:
        IOError: If file cannot be written.
    """
    state_path = Path(state_file).expanduser().resolve()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    
    with open(state_path, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    logger.debug(f"Auth state saved to {state_path}")


def load_auth_state(state_file: Union[str, Path]) -> Dict[str, Any]:
    """Load authentication state from a file.
    
    Args:
        state_file: Path to the state file.
        
    Returns:
        Dictionary containing the saved state, or empty dict if file doesn't exist.
    """
    state_path = Path(state_file).expanduser().resolve()
    
    if not state_path.exists():
        return {}
    
    try:
        with open(state_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.warning(f"Failed to load auth state from {state_path}: {e}")
        return {}
