"""Tests for command-line argument parsing.

Global options (auth, backup dir, log level, IMAP settings) must be accepted
either *before* or *after* the subcommand, and must fall back to the documented
defaults when omitted.
"""
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from gmail_archiver.cli import (
    EXIT_FAILURE,
    _run_restore,
    parse_args,
    resolve_app_password,
)


class TestCliArgOrdering(unittest.TestCase):
    """Global options work before, after, or mixed around the subcommand."""

    def test_options_after_subcommand(self):
        args = parse_args([
            'backup', '--auth-method', 'imap',
            '--email', 'user@gmail.com', '--app-password', 'pw',
            '--folder', 'INBOX', '--backup-dir', '/tmp/b', '--max-results', '5',
        ])
        self.assertEqual(args.command, 'backup')
        self.assertEqual(args.auth_method, 'imap')
        self.assertEqual(args.email, 'user@gmail.com')
        self.assertEqual(args.app_password, 'pw')
        self.assertEqual(args.folder, 'INBOX')
        self.assertEqual(args.backup_dir, '/tmp/b')
        self.assertEqual(args.max_results, 5)

    def test_options_before_subcommand(self):
        args = parse_args([
            '--auth-method', 'imap', '--email', 'user@gmail.com',
            '--app-password', 'pw', '--backup-dir', '/tmp/b',
            'backup', '--max-results', '5',
        ])
        self.assertEqual(args.command, 'backup')
        self.assertEqual(args.auth_method, 'imap')
        self.assertEqual(args.email, 'user@gmail.com')
        self.assertEqual(args.app_password, 'pw')
        self.assertEqual(args.backup_dir, '/tmp/b')
        self.assertEqual(args.max_results, 5)

    def test_mixed_ordering(self):
        # Some global options before the subcommand, some after.
        args = parse_args([
            '--backup-dir', '/tmp/b',
            'backup', '--auth-method', 'imap', '--email', 'user@gmail.com',
        ])
        self.assertEqual(args.command, 'backup')
        self.assertEqual(args.backup_dir, '/tmp/b')
        self.assertEqual(args.auth_method, 'imap')
        self.assertEqual(args.email, 'user@gmail.com')

    def test_after_overrides_before(self):
        # If the same option appears both before and after, the later wins.
        args = parse_args(['--backup-dir', '/before', 'backup', '--backup-dir', '/after'])
        self.assertEqual(args.backup_dir, '/after')

    def test_defaults_when_omitted(self):
        args = parse_args(['backup'])
        # IMAP is the default: it is the supported backup path and the only one
        # that works without the user creating a Google Cloud OAuth client.
        self.assertEqual(args.auth_method, 'imap')
        self.assertEqual(args.backup_dir, '~/gmail-backup')
        self.assertEqual(args.folder, '[Gmail]/All Mail')
        self.assertEqual(args.imap_server, 'imap.gmail.com')
        self.assertEqual(args.log_level, 'INFO')
        self.assertIsNone(args.email)
        self.assertIsNone(args.app_password)
        # Subcommand-specific default.
        self.assertEqual(args.batch_size, 100)

    def test_restore_both_orderings(self):
        after = parse_args(['restore', '--auth-method', 'browser', '--backup-dir', '/tmp/b'])
        before = parse_args(['--auth-method', 'browser', '--backup-dir', '/tmp/b', 'restore'])
        for args in (after, before):
            self.assertEqual(args.command, 'restore')
            self.assertEqual(args.auth_method, 'browser')
            self.assertEqual(args.backup_dir, '/tmp/b')
        # Restore has its own batch-size default (10).
        self.assertEqual(after.batch_size, 10)

    def test_restore_defaults_to_imap_like_every_command(self):
        """The shared default applies to restore too; _run_restore redirects it."""
        self.assertEqual(parse_args(['restore']).auth_method, 'imap')

    def test_log_level_before_subcommand(self):
        args = parse_args(['--log-level', 'DEBUG', 'backup'])
        self.assertEqual(args.log_level, 'DEBUG')


class TestAppPasswordResolution(unittest.TestCase):
    """The app password must not be prompted for more than once per run."""

    def setUp(self):
        self.env_patcher = patch.dict(os.environ, {}, clear=False)
        self.env_patcher.start()
        os.environ.pop('GMAIL_ARCHIVER_APP_PASSWORD', None)

    def tearDown(self):
        self.env_patcher.stop()

    def test_environment_variable_wins(self):
        os.environ['GMAIL_ARCHIVER_APP_PASSWORD'] = 'from-env'
        args = parse_args(['backup', '--app-password', 'from-argv'])

        self.assertEqual(resolve_app_password(args), 'from-env')

    def test_falls_back_to_flag(self):
        args = parse_args(['backup', '--app-password', 'from-argv'])

        self.assertEqual(resolve_app_password(args), 'from-argv')

    def test_prompts_only_once(self):
        """A second call reuses the answer instead of asking again."""
        args = parse_args(['backup'])

        with patch('gmail_archiver.cli.sys.stdin.isatty', return_value=True), \
             patch('gmail_archiver.cli.getpass.getpass', return_value='typed') as prompt:
            first = resolve_app_password(args)
            second = resolve_app_password(args)

        self.assertEqual((first, second), ('typed', 'typed'))
        prompt.assert_called_once()

    def test_returns_none_when_non_interactive(self):
        args = parse_args(['backup'])

        with patch('gmail_archiver.cli.sys.stdin.isatty', return_value=False):
            self.assertIsNone(resolve_app_password(args))


class TestRestoreRejectsImap(unittest.TestCase):
    """Restore writes to the mailbox, which Gmail's IMAP cannot do."""

    def test_imap_restore_fails_with_actionable_message(self):
        """Now that imap is the default, the error must name the fix."""
        args = parse_args(['restore'])

        with patch('gmail_archiver.cli.logger') as mock_logger:
            code = _run_restore(args, Path('/tmp/does-not-matter'))

        self.assertEqual(code, EXIT_FAILURE)
        message = mock_logger.error.call_args[0][0]
        self.assertIn('--auth-method oauth', message)
        self.assertIn('Gmail API', message)


if __name__ == '__main__':
    unittest.main()
