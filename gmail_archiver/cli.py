"""Command-line interface for Gmail Archiver."""
import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional, Union

from google.oauth2.credentials import Credentials

from . import __version__
from .auth import get_gmail_credentials, get_gmail_credentials_browser, get_imap_credentials
from .backup import GmailBackup
from .imap_backup import ImapBackup
from .restore import GmailRestore
from .utils import get_gmail_service, setup_logging

logger = logging.getLogger(__name__)


def _add_shared_arguments(target, use_suppress_defaults=False):
    """Add the options shared by every subcommand to ``target``.

    These options are registered on both the top-level parser and a parent
    parser inherited by the subcommands, so each can be supplied either before
    or after the subcommand (both ``--backup-dir X backup`` and
    ``backup --backup-dir X`` work).

    Args:
        target: The ``ArgumentParser`` to add the arguments to.
        use_suppress_defaults: When True, every option defaults to
            ``argparse.SUPPRESS`` so that, on the subcommand parser, an absent
            option does not overwrite a value parsed before the subcommand. The
            top-level parser supplies the real defaults instead.
    """
    def default(real_default):
        return argparse.SUPPRESS if use_suppress_defaults else real_default

    target.add_argument(
        '--backup-dir',
        type=str,
        default=default('~/gmail-backup'),
        help='Directory to store backup files.'
    )

    target.add_argument(
        '--log-level',
        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'],
        default=default('INFO'),
        help='Set the logging level.'
    )

    auth_group = target.add_argument_group('Authentication')
    auth_group.add_argument(
        '--auth-method',
        choices=['oauth', 'browser', 'imap'],
        default=default('browser'),
        help='Authentication method: oauth (requires client_secrets.json), '
             'browser (opens browser for Google login), or imap (uses app password).'
    )

    oauth_group = target.add_argument_group('OAuth Authentication')
    oauth_group.add_argument(
        '--client-secrets',
        type=str,
        default=default('client_secrets.json'),
        help='Path to the OAuth client secrets file (for oauth method).'
    )
    oauth_group.add_argument(
        '--token',
        type=str,
        default=default('~/.gmail-archiver/token.json'),
        help='Path to the OAuth token file.'
    )

    imap_group = target.add_argument_group('IMAP Authentication')
    imap_group.add_argument(
        '--email',
        type=str,
        default=default(None),
        help='Email address for IMAP authentication.'
    )
    imap_group.add_argument(
        '--app-password',
        type=str,
        default=default(None),
        help='App password for IMAP authentication.'
    )
    imap_group.add_argument(
        '--imap-server',
        type=str,
        default=default('imap.gmail.com'),
        help='IMAP server address.'
    )
    imap_group.add_argument(
        '--folder',
        type=str,
        default=default('[Gmail]/All Mail'),
        help='IMAP folder to back up (imap method only). Defaults to all '
             'archived mail; the exact name may vary by account language.'
    )


def parse_args(argv=None):
    """Parse command-line arguments.

    Args:
        argv: Optional list of argument strings (defaults to ``sys.argv``).
            Exposed primarily for testing.

    Returns:
        The parsed argument namespace.
    """
    parser = argparse.ArgumentParser(
        description="Gmail Archiver - Backup and restore Gmail emails with metadata.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Backup using browser-based authentication (easiest)
  gmail-archiver backup --auth-method browser --backup-dir ~/gmail-backup

  # Backup using OAuth with custom credentials
  gmail-archiver backup --auth-method oauth --client-secrets ~/credentials.json

  # Backup using IMAP with app password
  gmail-archiver backup --auth-method imap --email user@gmail.com --app-password xxxx

  # Restore emails from backup
  gmail-archiver restore --backup-dir ~/gmail-backup

For more information, see: https://github.com/junxit/gmail-archiver
"""
    )
    
    # Top-level only argument.
    parser.add_argument(
        '--version',
        action='version',
        version=f'%(prog)s {__version__}'
    )

    # Shared options are registered on BOTH the top-level parser (with real
    # defaults) and a parent parser inherited by the subcommands (with
    # suppressed defaults, so an option absent after the subcommand does not
    # clobber a value supplied before it). This lets every global option be
    # passed either before or after the subcommand.
    _add_shared_arguments(parser, use_suppress_defaults=False)

    common = argparse.ArgumentParser(add_help=False)
    _add_shared_arguments(common, use_suppress_defaults=True)

    # Subcommands (each inherits the shared options via parents=[common]).
    subparsers = parser.add_subparsers(
        dest='command',
        required=True,
        help='Command to execute.'
    )

    # Backup command
    backup_parser = subparsers.add_parser(
        'backup',
        parents=[common],
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
        parents=[common],
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

    return parser.parse_args(argv)


def get_credentials(args) -> Union[Credentials, object]:
    """Get Gmail API credentials based on the authentication method.
    
    Args:
        args: Parsed command-line arguments.
        
    Returns:
        Credentials object for Gmail API or MailBox for IMAP.
        
    Raises:
        SystemExit: If authentication fails.
    """
    try:
        if args.auth_method == 'oauth':
            # Traditional OAuth with client_secrets.json
            client_secrets = os.path.expanduser(args.client_secrets)
            token_path = os.path.expanduser(args.token)
            return get_gmail_credentials(token_path, client_secrets)
        
        elif args.auth_method == 'browser':
            # Browser-based OAuth (uses bundled or user credentials)
            token_path = os.path.expanduser(args.token)
            
            # Check if user has a client_secrets file
            client_secrets_path = Path(args.client_secrets).expanduser()
            if client_secrets_path.exists():
                logger.info(f"Using custom credentials from {client_secrets_path}")
                return get_gmail_credentials(token_path, str(client_secrets_path))
            else:
                # Use browser flow with bundled credentials
                return get_gmail_credentials_browser(token_path=token_path)
        
        else:  # imap
            if not args.email:
                logger.error("Email address is required for IMAP authentication. Use --email")
                sys.exit(1)
            if not args.app_password:
                logger.error("App password is required for IMAP authentication. Use --app-password")
                sys.exit(1)
            
            return get_imap_credentials(
                email=args.email,
                app_password=args.app_password,
                imap_server=args.imap_server
            )
            
    except FileNotFoundError as e:
        logger.error(str(e))
        sys.exit(1)
    except ValueError as e:
        logger.error(str(e))
        sys.exit(1)
    except Exception as e:
        logger.error(f"Authentication failed: {e}")
        if args.log_level == 'DEBUG':
            logger.exception("Full traceback:")
        sys.exit(1)


def get_state_file(args, command: str) -> Path:
    """Get the state file path, creating default if not specified.
    
    Args:
        args: Parsed command-line arguments.
        command: The command being executed ('backup' or 'restore').
        
    Returns:
        Path to the state file.
    """
    if args.state_file:
        return Path(args.state_file).expanduser().resolve()
    
    # Default state file in backup directory
    backup_dir = Path(args.backup_dir).expanduser().resolve()
    return backup_dir / f'{command}_state.json'


def main() -> None:
    """Main entry point for the CLI."""
    args = parse_args()
    setup_logging(args.log_level)
    
    logger.debug(f"Gmail Archiver {__version__}")
    logger.debug(f"Command: {args.command}")
    logger.debug(f"Auth method: {args.auth_method}")
    
    try:
        # Expand backup directory path
        backup_dir = Path(args.backup_dir).expanduser().resolve()
        
        if args.command == 'backup':
            # State file path is shared across all auth methods.
            state_file = get_state_file(args, 'backup')

            if args.auth_method == 'imap':
                # IMAP (app password) backup — no OAuth. Writes the identical
                # on-disk format as the API path so the existing restore can
                # read IMAP-made backups.
                mailbox = get_credentials(args)
                backup = ImapBackup(
                    mailbox=mailbox,
                    backup_dir=backup_dir,
                    state_file=state_file,
                    folder=args.folder,
                    batch_size=args.batch_size,
                )
                logger.info(f"Starting IMAP backup to {backup_dir}")
                try:
                    result = backup.backup_emails(max_results=args.max_results)
                finally:
                    try:
                        mailbox.logout()
                    except Exception:
                        pass
            else:
                # OAuth / browser flow (unchanged).
                credentials = get_credentials(args)
                service = get_gmail_service(credentials)

                backup = GmailBackup(
                    gmail_service=service,
                    backup_dir=backup_dir,
                    state_file=state_file,
                    batch_size=args.batch_size,
                )

                logger.info(f"Starting backup to {backup_dir}")
                result = backup.backup_emails(max_results=args.max_results)

            print("\n" + "="*60)
            print("BACKUP COMPLETE")
            print("="*60)
            print(f"  Emails processed: {result['total_processed']}")
            print(f"  Errors: {result['total_errors']}")
            print(f"  Backup location: {backup_dir}")
            print("="*60)
            
        elif args.command == 'restore':
            # Get credentials
            if args.auth_method == 'imap':
                logger.error("IMAP is supported for backup only. Please use oauth or browser for restore.")
                sys.exit(1)
            
            credentials = get_credentials(args)
            service = get_gmail_service(credentials)
            
            # Get state file path
            state_file = get_state_file(args, 'restore')
            
            # Initialize restore
            restore = GmailRestore(
                gmail_service=service,
                backup_dir=str(backup_dir),
                state_file=str(state_file),
                batch_size=args.batch_size,
                log_level=args.log_level
            )
            
            # Run restore
            logger.info(f"Starting restore from {backup_dir}")
            result = restore.restore_emails(max_results=args.max_results)
            
            print("\n" + "="*60)
            print("RESTORE COMPLETE")
            print("="*60)
            print(f"  Emails restored: {result['total_restored']}")
            print(f"  Errors: {result['total_errors']}")
            print("="*60)
            
    except KeyboardInterrupt:
        print("\n")
        logger.info("Operation cancelled by user.")
        sys.exit(130)
    except Exception as e:
        logger.error(f"An error occurred: {e}")
        if args.log_level == 'DEBUG':
            logger.exception("Full traceback:")
        sys.exit(1)


if __name__ == '__main__':
    main()
