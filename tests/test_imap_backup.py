"""Tests for the IMAP backup module.

These tests mock the imap-tools ``MailBox`` and its underlying imaplib
``client`` — no real network, server, or credentials are used. They verify that
the IMAP backend produces the same on-disk format as the OAuth/API backend so
the existing restore can read IMAP-made backups.
"""
import base64
import imaplib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from gmail_archiver.imap_backup import ImapBackup, normalize_labels
from gmail_archiver.restore import GmailRestore

# A small, realistic RFC822 message with CRLF line endings.
RAW_EMAIL = (
    b"From: sender@example.com\r\n"
    b"To: recipient@example.com\r\n"
    b"Subject: Test Email\r\n"
    b"Date: Mon, 1 Jan 2024 12:00:00 +0000\r\n"
    b"Message-ID: <abc123@example.com>\r\n"
    b"\r\n"
    b"This is a test email body.\r\n"
)

# 2024-01-01 12:00:00 UTC expressed as Unix milliseconds (timezone-independent).
JAN_1_2024_NOON_MS = "1704110400000"
INTERNALDATE = b"01-Jan-2024 12:00:00 +0000"

METADATA_KEYS = {
    'message_id', 'thread_id', 'subject', 'from', 'to', 'date',
    'labels', 'internal_date', 'size', 'sha256', 'backup_path', 'backup_time',
}


def _per_message_response(msg):
    """Build an imaplib-style ``UID FETCH`` response for a single message."""
    raw = msg['raw']
    fields = []
    if msg.get('gm_msgid') is not None:
        fields.append(b'X-GM-MSGID ' + str(msg['gm_msgid']).encode())
    if msg.get('gm_thrid') is not None:
        fields.append(b'X-GM-THRID ' + str(msg['gm_thrid']).encode())
    if msg.get('labels') is not None:
        fields.append(b'X-GM-LABELS (' + msg['labels'] + b')')
    fields.append(b'INTERNALDATE "' + msg.get('internaldate', INTERNALDATE) + b'"')
    fields.append(b'FLAGS (' + msg.get('flags', b'') + b')')
    fields.append(b'UID ' + str(msg['uid']).encode())
    prefix = (
        str(msg['uid']).encode() + b' (' + b' '.join(fields)
        + (b' BODY[] {%d}' % len(raw))
    )
    return ('OK', [(prefix, raw), b')'])


def make_mailbox(messages):
    """Return a MagicMock mailbox whose ``client.uid`` serves crafted responses.

    Args:
        messages: list of dicts with keys ``uid``, ``raw`` and optionally
            ``gm_msgid``, ``gm_thrid``, ``labels`` (bytes blob), ``internaldate``
            (bytes) and ``flags`` (bytes).
    """
    sweep_lines = []
    per_msg = {}
    for m in messages:
        uid = str(m['uid'])
        if m.get('gm_msgid') is not None:
            sweep_lines.append(
                f"{uid} (X-GM-MSGID {m['gm_msgid']} UID {uid})".encode()
            )
        else:
            sweep_lines.append(f"{uid} (UID {uid})".encode())
        per_msg[uid] = _per_message_response(m)

    sweep_response = ('OK', sweep_lines if sweep_lines else [None])
    search_response = ('OK', [b' '.join(str(m['uid']).encode() for m in messages)])

    def fake_uid(command, arg, parts=None):
        # The sweep is now UID SEARCH ALL followed by chunked UID FETCH of
        # X-GM-MSGID only; bodies are fetched one UID at a time.
        if command == 'SEARCH':
            return search_response
        if parts == '(X-GM-MSGID)':
            return sweep_response
        return per_msg.get(str(arg), ('NO', [None]))

    mailbox = MagicMock()
    mailbox.client.uid.side_effect = fake_uid
    mailbox.client.untagged_responses = {'UIDVALIDITY': [b'1']}
    return mailbox


class ImapBackupTestBase(unittest.TestCase):
    """Shared temp-dir setup/teardown for IMAP backup tests."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.backup_dir = os.path.join(self.temp_dir, 'backup')
        self.db_path = os.path.join(self.temp_dir, 'backup', 'index.db')
        self._backups = []

    def tearDown(self):
        for backup in self._backups:
            try:
                backup.close()
            except Exception:
                pass
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    def make_backup(self, mailbox, folder='[Gmail]/All Mail', **kwargs):
        backup = ImapBackup(
            mailbox=mailbox,
            backup_dir=self.backup_dir,
            db_path=self.db_path,
            folder=folder,
            batch_size=10,
            **kwargs,
        )
        self._backups.append(backup)
        return backup


class TestImapBackupWritesFormat(ImapBackupTestBase):
    """The IMAP backend writes the same .eml + metadata layout as the API path."""

    def test_writes_eml_and_metadata_with_correct_schema(self):
        mailbox = make_mailbox([{
            'uid': '1',
            'gm_msgid': '100',
            'gm_thrid': '200',
            'labels': b'\\Inbox \\Important "Work"',
            'internaldate': INTERNALDATE,
            'flags': b'\\Seen',
            'raw': RAW_EMAIL,
        }])
        backup = self.make_backup(mailbox)

        result = backup.backup_emails()

        self.assertEqual(result['total_processed'], 1)
        self.assertEqual(result['total_errors'], 0)

        # Metadata sidecar keyed by the Gmail permanent id (X-GM-MSGID).
        metadata_file = Path(self.backup_dir) / 'metadata' / '100.json'
        self.assertTrue(metadata_file.exists())
        with open(metadata_file, 'r', encoding='utf-8') as f:
            metadata = json.load(f)

        # Schema must match the API path exactly (no extra/missing keys).
        self.assertEqual(set(metadata.keys()), METADATA_KEYS)
        self.assertEqual(metadata['message_id'], '100')
        self.assertEqual(metadata['thread_id'], '200')
        self.assertEqual(metadata['subject'], 'Test Email')
        self.assertIn('sender@example.com', metadata['from'])
        self.assertIn('recipient@example.com', metadata['to'])
        self.assertEqual(metadata['labels'], ['INBOX', 'IMPORTANT', 'Work'])
        self.assertEqual(metadata['internal_date'], JAN_1_2024_NOON_MS)
        self.assertEqual(metadata['size'], len(RAW_EMAIL))
        self.assertTrue(metadata['backup_path'].startswith('emails/2024/01/'))
        self.assertTrue(metadata['backup_path'].endswith('.eml'))
        self.assertIn('100_', metadata['backup_path'])
        self.assertTrue(metadata['backup_time'])

        # The .eml is the byte-for-byte raw message, in the YYYY/MM tree.
        eml_path = Path(self.backup_dir) / metadata['backup_path']
        self.assertTrue(eml_path.exists())
        with open(eml_path, 'rb') as f:
            self.assertEqual(f.read(), RAW_EMAIL)

        # State records the message under the same id.
        self.assertTrue(backup.store.is_archived('100'))

    def test_selects_configured_folder_readonly(self):
        mailbox = make_mailbox([])
        backup = self.make_backup(mailbox, folder='[Gmail]/All Mail')

        backup.backup_emails()

        mailbox.folder.set.assert_called_once_with('[Gmail]/All Mail', readonly=True)

    def test_processes_multiple_messages(self):
        mailbox = make_mailbox([
            {'uid': '1', 'gm_msgid': '100', 'gm_thrid': '1',
             'labels': b'\\Inbox', 'flags': b'\\Seen', 'raw': RAW_EMAIL},
            {'uid': '2', 'gm_msgid': '101', 'gm_thrid': '2',
             'labels': b'\\Sent', 'flags': b'\\Seen', 'raw': RAW_EMAIL},
        ])
        backup = self.make_backup(mailbox)

        result = backup.backup_emails()

        self.assertEqual(result['total_processed'], 2)
        self.assertTrue(backup.store.is_archived('100'))
        self.assertTrue(backup.store.is_archived('101'))


class TestImapBackupLabels(ImapBackupTestBase):
    """Label capture and normalization."""

    def test_system_labels_mapped_and_user_label_kept(self):
        mailbox = make_mailbox([{
            'uid': '1', 'gm_msgid': '100', 'gm_thrid': '200',
            'labels': b'\\Inbox \\Important "Work"',
            'flags': b'\\Seen', 'raw': RAW_EMAIL,
        }])
        backup = self.make_backup(mailbox)

        backup.backup_emails()

        with open(Path(self.backup_dir) / 'metadata' / '100.json', encoding='utf-8') as f:
            metadata = json.load(f)
        self.assertEqual(metadata['labels'], ['INBOX', 'IMPORTANT', 'Work'])

    def test_unread_synthesized_when_not_seen(self):
        mailbox = make_mailbox([{
            'uid': '1', 'gm_msgid': '100', 'gm_thrid': '200',
            'labels': b'\\Inbox',
            'flags': b'',  # no \Seen flag => unread
            'raw': RAW_EMAIL,
        }])
        backup = self.make_backup(mailbox)

        backup.backup_emails()

        with open(Path(self.backup_dir) / 'metadata' / '100.json', encoding='utf-8') as f:
            metadata = json.load(f)
        self.assertEqual(metadata['labels'], ['INBOX', 'UNREAD'])

    def test_normalize_labels_unit(self):
        # Quoted user label with a space and a nested path are preserved by name.
        self.assertEqual(
            normalize_labels(['\\Inbox', '\\Sent', 'Project X', 'Work/Sub'], seen=True),
            ['INBOX', 'SENT', 'Project X', 'Work/Sub'],
        )
        # Unseen adds UNREAD; duplicates collapse.
        self.assertEqual(
            normalize_labels(['\\Inbox', '\\Inbox'], seen=False),
            ['INBOX', 'UNREAD'],
        )


class TestImapBackupIncremental(ImapBackupTestBase):
    """Re-running skips already-backed-up messages without re-downloading."""

    def test_second_run_skips_backed_up_message(self):
        messages = [{
            'uid': '1', 'gm_msgid': '100', 'gm_thrid': '200',
            'labels': b'\\Inbox', 'flags': b'\\Seen', 'raw': RAW_EMAIL,
        }]

        # First run downloads and saves the message.
        mailbox1 = make_mailbox(messages)
        result1 = self.make_backup(mailbox1).backup_emails()
        self.assertEqual(result1['total_processed'], 1)

        # Second run (fresh instance loads persisted state) skips it.
        mailbox2 = make_mailbox(messages)
        result2 = self.make_backup(mailbox2).backup_emails()
        self.assertEqual(result2['total_processed'], 0)
        self.assertEqual(result2['total_errors'], 0)

        # The body was never fetched on the second run — only the cheap sweep.
        calls = mailbox2.client.uid.call_args_list
        self.assertTrue(calls)  # the sweep happened
        body_fetches = [c for c in calls if 'BODY.PEEK[]' in str(c.args[-1])]
        self.assertEqual(body_fetches, [], "re-run must not re-download bodies")


class TestImapBackupEdgeCases(ImapBackupTestBase):
    """Empty mailbox and Message-ID fallback."""

    def test_empty_mailbox_handled_cleanly(self):
        mailbox = make_mailbox([])
        backup = self.make_backup(mailbox)

        result = backup.backup_emails()

        self.assertEqual(result['total_processed'], 0)
        self.assertEqual(result['total_errors'], 0)
        # No per-message metadata written.
        metadata_dir = Path(self.backup_dir) / 'metadata'
        self.assertEqual(list(metadata_dir.glob('*.json')), [])

    def test_falls_back_to_message_id_when_no_gm_msgid(self):
        # Message with no X-GM-MSGID (e.g. non-Gmail server) uses the RFC822
        # Message-ID header as the stable id.
        mailbox = make_mailbox([{
            'uid': '1', 'gm_msgid': None, 'gm_thrid': None,
            'labels': b'\\Inbox', 'flags': b'\\Seen', 'raw': RAW_EMAIL,
        }])
        backup = self.make_backup(mailbox)

        result = backup.backup_emails()

        self.assertEqual(result['total_processed'], 1)
        self.assertTrue(backup.store.is_archived('<abc123@example.com>'))


class TestImapBackupRestoreCompatibility(ImapBackupTestBase):
    """An IMAP-made backup is restorable by the existing restore, unchanged."""

    def test_imap_backup_restorable_by_existing_restore(self):
        # Produce a backup via the IMAP backend.
        mailbox = make_mailbox([{
            'uid': '1', 'gm_msgid': '100', 'gm_thrid': '200',
            'labels': b'\\Inbox \\Important "Work"',
            'flags': b'\\Seen', 'raw': RAW_EMAIL,
        }])
        self.make_backup(mailbox).backup_emails()

        # Feed it to the unmodified GmailRestore.
        mock_service = MagicMock()
        mock_service.users().messages().import_().execute.return_value = {'id': 'restored_1'}

        restore = GmailRestore(
            gmail_service=mock_service,
            backup_dir=self.backup_dir,
            state_file=os.path.join(self.temp_dir, 'restore_state.json'),
        )
        result = restore.restore_emails()

        self.assertEqual(result['total_restored'], 1)
        self.assertEqual(result['total_errors'], 0)

        # The byte-for-byte .eml and the captured labels reached Gmail's import.
        body = mock_service.users().messages().import_.call_args.kwargs['body']
        self.assertEqual(base64.urlsafe_b64decode(body['raw']), RAW_EMAIL)
        self.assertEqual(body['labelIds'], ['INBOX', 'IMPORTANT', 'Work'])


class TestImapBackupConnectionLoss(ImapBackupTestBase):
    """A dropped connection must not be reported as a successful backup."""

    def _dropping_mailbox(self, messages, drop_on_uid):
        """A mailbox whose body FETCH raises IMAP4.abort for one UID."""
        mailbox = make_mailbox(messages)
        healthy = mailbox.client.uid.side_effect

        def flaky(command, arg, parts=None):
            if parts and 'BODY.PEEK[]' in str(parts) and str(arg) == drop_on_uid:
                raise imaplib.IMAP4.abort('connection reset')
            return healthy(command, arg, parts)

        mailbox.client.uid.side_effect = flaky
        return mailbox

    def test_drop_without_reconnect_raises(self):
        """With no way to recover, the run fails loudly instead of reporting 0 errors."""
        mailbox = self._dropping_mailbox([
            {'uid': '1', 'gm_msgid': '100', 'gm_thrid': '1',
             'labels': b'\\Inbox', 'flags': b'\\Seen', 'raw': RAW_EMAIL},
        ], drop_on_uid='1')
        backup = self.make_backup(mailbox)

        with self.assertRaises(imaplib.IMAP4.abort):
            backup.backup_emails()

    def test_reconnect_resumes_the_run(self):
        """Given a reconnect callable, the message is retried and archived."""
        messages = [{'uid': '1', 'gm_msgid': '100', 'gm_thrid': '1',
                     'labels': b'\\Inbox', 'flags': b'\\Seen', 'raw': RAW_EMAIL}]
        broken = self._dropping_mailbox(messages, drop_on_uid='1')
        healthy = make_mailbox(messages)

        backup = self.make_backup(broken, reconnect=lambda: healthy)
        result = backup.backup_emails()

        self.assertEqual(result['total_processed'], 1)
        self.assertEqual(result['total_errors'], 0)
        self.assertTrue(backup.store.is_archived('100'))

    def test_progress_before_the_drop_is_kept(self):
        """Messages archived before the connection died survive the failure."""
        messages = [
            {'uid': '1', 'gm_msgid': '100', 'gm_thrid': '1',
             'labels': b'\\Inbox', 'flags': b'\\Seen', 'raw': RAW_EMAIL},
            {'uid': '2', 'gm_msgid': '101', 'gm_thrid': '2',
             'labels': b'\\Inbox', 'flags': b'\\Seen', 'raw': RAW_EMAIL},
        ]
        backup = self.make_backup(self._dropping_mailbox(messages, drop_on_uid='2'))

        with self.assertRaises(imaplib.IMAP4.abort):
            backup.backup_emails()

        self.assertTrue(backup.store.is_archived('100'))


class TestImapSweepChunking(ImapBackupTestBase):
    """The sweep enumerates via SEARCH and fetches ids in bounded chunks."""

    def test_uses_uid_search_not_open_ended_fetch(self):
        mailbox = make_mailbox([
            {'uid': '1', 'gm_msgid': '100', 'gm_thrid': '1',
             'labels': b'\\Inbox', 'flags': b'\\Seen', 'raw': RAW_EMAIL},
        ])
        self.make_backup(mailbox).backup_emails()

        commands = [c.args[0] for c in mailbox.client.uid.call_args_list]
        self.assertIn('SEARCH', commands)
        wildcard = [c for c in mailbox.client.uid.call_args_list if c.args[1] == '1:*']
        self.assertEqual(wildcard, [], "sweep must not use an unbounded 1:* FETCH")

    def test_sweep_failure_aborts_rather_than_under_reporting(self):
        """A failed sweep chunk must not look like a smaller mailbox."""
        mailbox = make_mailbox([
            {'uid': '1', 'gm_msgid': '100', 'gm_thrid': '1',
             'labels': b'\\Inbox', 'flags': b'\\Seen', 'raw': RAW_EMAIL},
        ])

        def failing(command, arg, parts=None):
            if command == 'SEARCH':
                return ('OK', [b'1'])
            if parts == '(X-GM-MSGID)':
                return ('NO', [None])
            raise AssertionError('should not reach body fetch')

        mailbox.client.uid.side_effect = failing
        backup = self.make_backup(mailbox)

        with self.assertRaises(IOError):
            backup.backup_emails()


if __name__ == '__main__':
    unittest.main()
