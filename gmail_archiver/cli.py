"""Command-line interface for Gmail Archiver."""
import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional

from google.oauth2.credentials import Credentials

from . import __version__
from .auth import get_gmail_credentials, get_imap_credentials
from .backup import GmailBackup
from .restore import GmailRestore
from .utils import get_gmail_service, setup_logging

logger = logging.getLogger(__name__)

def parse_args():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Gmail Archiver - Backup and restore Gmail emails with metadata.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    
    # Global arguments
    parser.add_argument(
        '--version',
        action='version',
        version=f'%(prog)s {__version__}'
    )
    
    parser.add_argument(
        '--backup-dir',
        type=str,
        default='~/gmail-backup',
        help='Directory to store backup files.'
    )
    
    parser.add_argument(
        '--log-level',
        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'],
        default='INFO',
        help='Set the logging level.'
    )
    
    # Subcommands
    subparsers = parser.add_subparsers(
        dest='command',
        required=True,
        help='Command to execute.'
    )
    
    # Backup command
    backup_parser = subparsers.add_parser(
        'backup',
        help='Backup Gmail emails.'
    )
    
    backup_parser.add_argument(
        '--max-results',
        type=int,
        help='Maximum number of emails to process.'
    )
    
    backup_parser.add_argument(
        '--batch-size',
        type=int,
        default=100,
        help='Number of emails to process in each batch.'
    )
    
    backup_parser.add_argument(
        '--state-file',
        type=str,
        help='Path to the backup state file.'
    )
    
    # Restore command
    restore_parser = subparsers.add_parser(
        'restore',
        help='Restore Gmail emails from backup.'
    )
    
    restore_parser.add_argument(
        '--max-results',
        type=int,
        help='Maximum number of emails to restore.'
    )
    
    restore_parser.add_argument(
        '--batch-size',
        type=int,
        default=10,
        help='Number of emails to restore in each batch.'
    )
    
    restore_parser.add_argument(
        '--state-file',
        type=str,
        help='Path to the restore state file.'
    )
    
    # Authentication arguments
    auth_group = parser.add_argument_group('Authentication')
    
    auth_group.add_argument(
        '--auth-method',
        choices=['oauth', 'imap'],
        default='oauth',
        help='Authentication method to use.'
    )
    
    # OAuth arguments
    oauth_group = parser.add_argument_group('OAuth Authentication')
    
    oauth_group.add_argument(
        '--client-secrets',
        type=str,
        default='client_secrets.json',
        help='Path to the OAuth client secrets file.'
    )
    
    oauth_group.add_argument(
        '--token',
        type=str,
        default='token.json',
        help='Path to the OAuth token file.'
    )
    
    # IMAP arguments
    imap_group = parser.add_argument_group('IMAP Authentication')
    
    imap_group.add_argument(
        '--email',
        type=str,
        help='Email address for IMAP authentication.'
    )
    
    imap_group.add_argument(
        '--app-password',
        type=str,
        help='App password for IMAP authentication.'
    )
    
    imap_group.add_argument(
        '--imap-server',
        type=str,
        default='imap.gmail.com',
        help='IMAP server address.'
    )
    
    return parser.parse_args()

def get_credentials(args) -> Credentials:
    """Get Gmail API credentials based on the authentication method."""
    if args.auth_method == 'oauth':
        # Expand the paths
        client_secrets = os.path.expanduser(args.client_secrets)
        token_path = os.path.expanduser(args.token)
        
        # Get OAuth credentials
        return get_gmail_credentials(token_path, client_secrets)
    else:
        # Get IMAP credentials
        if not args.email or not args.app_password:
            logger.error("Email and app password are required for IMAP authentication.")
            sys.exit(1)
            
        return get_imap_credentials(
            email=args.email,
            app_password=args.app_password,
            imap_server=args.imap_server
        )

def main() -> None:
    """Main entry point for the CLI."""
    args = parse_args()
    setup_logging(args.log_level)
    
    try:
        if args.command == 'backup':
            # Get Gmail API service
            credentials = get_credentials(args)
            service = get_gmail_service(credentials)
            
            # Initialize backup
            backup = GmailBackup(
                gmail_service=service,
                backup_dir=args.backup_dir,
                state_file=args.state_file,
                batch_size=args.batch_size,
                log_level=args.log_level
            )
            
            # Run backup
            result = backup.backup_emails(max_results=args.max_results)
            logger.info("Backup completed: %s", json.dumps(result, indent=2))
            
        elif args.command == 'restore':
            # Get Gmail API service
            credentials = get_credentials(args)
            service = get_gmail_service(credentials)
            
            # Initialize restore
            restore = GmailRestore(
                gmail_service=service,
                backup_dir=args.backup_dir,
                state_file=args.state_file,
                batch_size=args.batch_size,
                log_level=args.log_level
            )
            
            # Run restore
            result = restore.restore_emails(max_results=args.max_results)
            logger.info("Restore completed: %s", json.dumps(result, indent=2))
            
    except KeyboardInterrupt:
        logger.info("Operation cancelled by user.")
        sys.exit(1)
    except Exception as e:
        logger.error("An error occurred: %s", e, exc_info=args.log_level == 'DEBUG')
        sys.exit(1)

if __name__ == '__main__':
    main()
