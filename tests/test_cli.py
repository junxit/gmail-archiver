"""Tests for command-line argument parsing.

Global options (auth, backup dir, log level, IMAP settings) must be accepted
either *before* or *after* the subcommand, and must fall back to the documented
defaults when omitted.
"""
import unittest

from gmail_archiver.cli import parse_args


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
        self.assertEqual(args.auth_method, 'browser')
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

    def test_log_level_before_subcommand(self):
        args = parse_args(['--log-level', 'DEBUG', 'backup'])
        self.assertEqual(args.log_level, 'DEBUG')


if __name__ == '__main__':
    unittest.main()
