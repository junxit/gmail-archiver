"""Command-line interface for Gmail Archiver."""
import argparse
import getpass
import logging
import os
import sys
from pathlib import Path
from typing import Optional, Union

from google.oauth2.credentials import Credentials

from . import __version__
from .auth import (
    BACKUP_SCOPES,
    RESTORE_SCOPES,
    get_gmail_credentials,
    get_gmail_credentials_browser,
    get_imap_credentials,
)
from .backup import GmailBackup
from .imap_backup import ImapBackup
from .restore import GmailRestore
from .store import ArchiveStore, DEFAULT_DB_NAME, rebuild_from_metadata
from .utils import format_size, get_gmail_service, setup_logging

logger = logging.getLogger(__name__)

# Restore needs write access, so it authenticates separately and keeps its
# broader grant in its own file. The token the archiver uses for its regular
# backups stays read-only.
DEFAULT_RESTORE_TOKEN = '~/.gmail-archiver/token-restore.json'

# Exit codes. Distinguishing "finished but some messages failed" from a clean run
# matters when this is driven from a script: a run that could not download
# everything should not look like a success.
EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_COMPLETED_WITH_ERRORS = 2
EXIT_INTERRUPTED = 130


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
        help='Path to the OAuth client secrets file (for oauth method). '
             'Env: GMAIL_ARCHIVER_CLIENT_SECRETS'
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
        help='Email address for IMAP authentication. Env: GMAIL_ARCHIVER_EMAIL'
    )
    imap_group.add_argument(
        '--app-password',
        type=str,
        default=default(None),
        help='App password for IMAP authentication. DEPRECATED on the command '
             'line: the value is visible to other users via `ps` and is saved in '
             'your shell history. Prefer GMAIL_ARCHIVER_APP_PASSWORD or the '
             'interactive prompt.'
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
  # Back up over IMAP with an app password (password read from the environment)
  export GMAIL_ARCHIVER_APP_PASSWORD='xxxx xxxx xxxx xxxx'
  gmail-archiver backup --auth-method imap --email you@gmail.com

  # Backup using OAuth with your own Google Cloud credentials
  gmail-archiver backup --auth-method oauth --client-secrets ~/client_secrets.json

  # Show what the archive currently holds
  gmail-archiver status --backup-dir ~/gmail-backup

  # Rebuild the dedup index from the archive itself
  gmail-archiver rebuild-index --backup-dir ~/gmail-backup

  # Restore emails from backup (authenticates separately; needs write access)
  gmail-archiver restore --backup-dir ~/gmail-backup
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
        help='Maximum number of new emails to download.'
    )

    backup_parser.add_argument(
        '--batch-size',
        type=int,
        default=100,
        help='Number of emails to process between index commits.'
    )

    backup_parser.add_argument(
        '--index-db',
        type=str,
        help=f'Path to the SQLite archive index (default: <backup-dir>/{DEFAULT_DB_NAME}).'
    )

    backup_parser.add_argument(
        '--no-verify-existing',
        action='store_true',
        help='Skip the on-disk size check for already-indexed messages. Faster, '
             'but a truncated or deleted .eml will not be noticed or repaired.'
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

    restore_parser.add_argument(
        '--restore-token',
        type=str,
        default=DEFAULT_RESTORE_TOKEN,
        help='Path to the separate OAuth token used for restore (write access).'
    )

    # Status command
    status_parser = subparsers.add_parser(
        'status',
        parents=[common],
        help='Show what the archive currently holds.'
    )

    status_parser.add_argument(
        '--index-db',
        type=str,
        help=f'Path to the SQLite archive index (default: <backup-dir>/{DEFAULT_DB_NAME}).'
    )

    status_parser.add_argument(
        '--list-vanished',
        action='store_true',
        help='List every message that is archived but no longer present in Gmail.'
    )

    status_parser.add_argument(
        '--list-failures',
        action='store_true',
        help='List messages that failed to download and are pending retry.'
    )

    # Rebuild-index command
    rebuild_parser = subparsers.add_parser(
        'rebuild-index',
        parents=[common],
        help='Rebuild the dedup index from the archive on disk.'
    )

    rebuild_parser.add_argument(
        '--index-db',
        type=str,
        help=f'Path to the SQLite archive index (default: <backup-dir>/{DEFAULT_DB_NAME}).'
    )

    return parser.parse_args(argv)


def resolve_app_password(args) -> Optional[str]:
    """Resolve the IMAP app password without putting it on the command line.

    Order of preference: the ``GMAIL_ARCHIVER_APP_PASSWORD`` environment
    variable, then ``--app-password``, then an interactive prompt. The flag is
    still accepted for compatibility but warns, because an argv value is readable
    by any local user through ``ps`` and is written to shell history.

    Args:
        args: Parsed command-line arguments.

    Returns:
        The password, or None if no value could be obtained.
    """
    env_password = os.environ.get('GMAIL_ARCHIVER_APP_PASSWORD')
    if env_password:
        return env_password
    if args.app_password:
        logger.warning(
            "--app-password is visible to other users via `ps` and is stored in "
            "your shell history. Prefer GMAIL_ARCHIVER_APP_PASSWORD."
        )
        return args.app_password
    if sys.stdin.isatty():
        return getpass.getpass('Gmail app password: ')
    return None


def get_credentials(args, scopes=None, token_path: Optional[str] = None) -> Union[Credentials, object]:
    """Get Gmail API credentials based on the authentication method.

    Args:
        args: Parsed command-line arguments.
        scopes: OAuth scopes to request. Defaults to the read-only backup scopes.
        token_path: Override for where the token is read from and written to.

    Returns:
        Credentials object for Gmail API or MailBox for IMAP.

    Raises:
        SystemExit: If authentication fails.
    """
    scopes = scopes or BACKUP_SCOPES
    try:
        if args.auth_method in ('oauth', 'browser'):
            client_secrets = os.environ.get(
                'GMAIL_ARCHIVER_CLIENT_SECRETS', args.client_secrets
            )
            resolved_token = os.path.expanduser(token_path or args.token)

            if args.auth_method == 'oauth':
                return get_gmail_credentials(
                    resolved_token, os.path.expanduser(client_secrets), scopes=scopes
                )

            # Browser flow: prefer the user's own client secrets when present,
            # since the bundled config ships as placeholders.
            client_secrets_path = Path(client_secrets).expanduser()
            if client_secrets_path.exists():
                logger.info(f"Using custom credentials from {client_secrets_path}")
                return get_gmail_credentials(
                    resolved_token, str(client_secrets_path), scopes=scopes
                )
            return get_gmail_credentials_browser(
                token_path=resolved_token, scopes=scopes
            )

        # imap
        email = os.environ.get('GMAIL_ARCHIVER_EMAIL') or args.email
        if not email:
            logger.error(
                "Email address is required for IMAP authentication. "
                "Use --email or set GMAIL_ARCHIVER_EMAIL."
            )
            sys.exit(EXIT_FAILURE)

        app_password = resolve_app_password(args)
        if not app_password:
            logger.error(
                "App password is required for IMAP authentication. "
                "Set GMAIL_ARCHIVER_APP_PASSWORD or run interactively."
            )
            sys.exit(EXIT_FAILURE)

        return get_imap_credentials(
            email=email,
            app_password=app_password,
            imap_server=args.imap_server
        )

    except (FileNotFoundError, ValueError) as e:
        logger.error(str(e))
        sys.exit(EXIT_FAILURE)
    except Exception as e:
        logger.error(f"Authentication failed: {e}")
        if args.log_level == 'DEBUG':
            logger.exception("Full traceback:")
        sys.exit(EXIT_FAILURE)


def get_index_path(args, backup_dir: Path) -> Path:
    """Resolve the archive index path for the current invocation."""
    if getattr(args, 'index_db', None):
        return Path(args.index_db).expanduser().resolve()
    return backup_dir / DEFAULT_DB_NAME


def get_state_file(args, command: str) -> Path:
    """Get the restore state file path, creating a default if not specified.

    Args:
        args: Parsed command-line arguments.
        command: The command being executed.

    Returns:
        Path to the state file.
    """
    if getattr(args, 'state_file', None):
        return Path(args.state_file).expanduser().resolve()

    backup_dir = Path(args.backup_dir).expanduser().resolve()
    return backup_dir / f'{command}_state.json'


def _run_backup(args, backup_dir: Path) -> int:
    """Run a backup and return the process exit code."""
    index_path = get_index_path(args, backup_dir)
    verify_existing = not args.no_verify_existing

    if args.auth_method == 'imap':
        # IMAP (app password) backup - no OAuth. Writes the identical on-disk
        # format as the API path so the existing restore can read IMAP-made
        # backups.
        mailbox = get_credentials(args)
        email = os.environ.get('GMAIL_ARCHIVER_EMAIL') or args.email
        password = resolve_app_password(args)

        def reconnect():
            """Re-establish the mailbox after the server drops the connection."""
            return get_imap_credentials(
                email=email, app_password=password, imap_server=args.imap_server
            )

        backup = ImapBackup(
            mailbox=mailbox,
            backup_dir=backup_dir,
            db_path=index_path,
            folder=args.folder,
            batch_size=args.batch_size,
            verify_existing=verify_existing,
            reconnect=reconnect,
        )
        logger.info(f"Starting IMAP backup to {backup_dir}")
        try:
            result = backup.backup_emails(max_results=args.max_results)
        finally:
            backup.close()
            try:
                backup.mailbox.logout()
            except Exception as e:
                logger.debug("IMAP logout failed: %s", e)
    else:
        # OAuth / browser flow.
        credentials = get_credentials(args)
        service = get_gmail_service(credentials)

        backup = GmailBackup(
            gmail_service=service,
            backup_dir=backup_dir,
            db_path=index_path,
            batch_size=args.batch_size,
            verify_existing=verify_existing,
        )

        logger.info(f"Starting backup to {backup_dir}")
        try:
            result = backup.backup_emails(max_results=args.max_results)
        finally:
            backup.close()

    print("\n" + "=" * 60)
    print("BACKUP COMPLETE" if not result['total_errors'] else "BACKUP FINISHED WITH ERRORS")
    print("=" * 60)
    print(f"  New emails downloaded: {result['total_processed']}")
    print(f"  Already archived:      {result.get('total_skipped', 0)}")
    print(f"  Errors:                {result['total_errors']}")
    print(f"  Newly vanished:        {result.get('tombstoned', 0)}")
    print(f"  Total in archive:      {result.get('archived_total', 0)}")
    print(f"  Backup location:       {backup_dir}")
    if result['total_errors']:
        print()
        print("  Some messages could not be downloaded. They are recorded in the")
        print("  index and will be retried on the next run; see `status --list-failures`.")
    print("=" * 60)

    return EXIT_COMPLETED_WITH_ERRORS if result['total_errors'] else EXIT_OK


def _run_restore(args, backup_dir: Path) -> int:
    """Run a restore and return the process exit code."""
    if args.auth_method == 'imap':
        logger.error("IMAP is supported for backup only. Please use oauth or browser for restore.")
        return EXIT_FAILURE

    # Restore writes to the mailbox, so it consents separately and stores its
    # wider grant in its own token file rather than escalating the backup token.
    print("\nRestore needs permission to write messages into your mailbox.")
    print(f"It authenticates separately from backup and stores that grant in "
          f"{args.restore_token}.\n")
    credentials = get_credentials(args, scopes=RESTORE_SCOPES, token_path=args.restore_token)
    service = get_gmail_service(credentials)

    restore = GmailRestore(
        gmail_service=service,
        backup_dir=str(backup_dir),
        state_file=str(get_state_file(args, 'restore')),
        batch_size=args.batch_size,
        log_level=args.log_level
    )

    logger.info(f"Starting restore from {backup_dir}")
    result = restore.restore_emails(max_results=args.max_results)

    print("\n" + "=" * 60)
    print("RESTORE COMPLETE" if not result['total_errors'] else "RESTORE FINISHED WITH ERRORS")
    print("=" * 60)
    print(f"  Emails restored: {result['total_restored']}")
    print(f"  Errors: {result['total_errors']}")
    print("=" * 60)

    return EXIT_COMPLETED_WITH_ERRORS if result['total_errors'] else EXIT_OK


def _run_status(args, backup_dir: Path) -> int:
    """Print archive statistics and return the process exit code."""
    index_path = get_index_path(args, backup_dir)
    if not index_path.exists():
        logger.error(
            "No archive index at %s. Run a backup first, or `rebuild-index` if "
            "you have an archive but lost the index.", index_path
        )
        return EXIT_FAILURE

    with ArchiveStore(index_path) as store:
        stats = store.stats()
        print("\n" + "=" * 60)
        print("ARCHIVE STATUS")
        print("=" * 60)
        print(f"  Location:        {backup_dir}")
        print(f"  Index:           {index_path}")
        print(f"  Messages:        {stats['total_emails']}")
        print(f"  Total size:      {format_size(stats['total_size'])}")
        print(f"  Vanished:        {stats['vanished']}  (deleted in Gmail, kept here)")
        print(f"  Pending retries: {stats['failures']}")
        print("=" * 60)

        if args.list_vanished:
            vanished = store.list_vanished()
            print(f"\nVanished messages ({len(vanished)}):")
            for row in vanished:
                print(f"  {row['vanished_at']}  {row['msg_id']}  {row['rel_path']}")

        if args.list_failures:
            failures = store.list_failures()
            print(f"\nPending retries ({len(failures)}):")
            for row in failures:
                print(f"  {row['msg_id']}  attempts={row['attempts']}  {row['reason']}")

    return EXIT_OK


def _run_rebuild_index(args, backup_dir: Path) -> int:
    """Rebuild the index from on-disk metadata and return the exit code."""
    index_path = get_index_path(args, backup_dir)
    logger.info("Rebuilding index at %s from %s", index_path, backup_dir / 'metadata')
    with ArchiveStore(index_path) as store:
        indexed = rebuild_from_metadata(backup_dir, store)
        stats = store.stats()

    print("\n" + "=" * 60)
    print("INDEX REBUILT")
    print("=" * 60)
    print(f"  Messages indexed: {indexed}")
    print(f"  Total size:       {format_size(stats['total_size'])}")
    print(f"  Index:            {index_path}")
    print("=" * 60)
    return EXIT_OK


def main() -> None:
    """Main entry point for the CLI."""
    args = parse_args()
    setup_logging(args.log_level)

    logger.debug(f"Gmail Archiver {__version__}")
    logger.debug(f"Command: {args.command}")

    try:
        backup_dir = Path(args.backup_dir).expanduser().resolve()

        if args.command == 'backup':
            sys.exit(_run_backup(args, backup_dir))
        elif args.command == 'restore':
            sys.exit(_run_restore(args, backup_dir))
        elif args.command == 'status':
            sys.exit(_run_status(args, backup_dir))
        elif args.command == 'rebuild-index':
            sys.exit(_run_rebuild_index(args, backup_dir))

    except KeyboardInterrupt:
        print("\n")
        logger.info("Operation cancelled by user. Progress so far has been saved; "
                    "re-run to continue.")
        sys.exit(EXIT_INTERRUPTED)
    except SystemExit:
        raise
    except Exception as e:
        logger.error(f"An error occurred: {e}")
        if args.log_level == 'DEBUG':
            logger.exception("Full traceback:")
        sys.exit(EXIT_FAILURE)


if __name__ == '__main__':
    main()
