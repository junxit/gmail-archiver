"""Tests for the IMAP backup module.

These tests mock the imap-tools ``MailBox`` and its underlying imaplib
``client`` — no real network, server, or credentials are used. They verify that
the IMAP backend produces the same on-disk format as the OAuth/API backend so
the existing restore can read IMAP-made backups.
"""
import base64
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
    'labels', 'internal_date', 'size', 'backup_path', 'backup_time',
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

    def fake_uid(command, arg, parts):
        if arg == '1:*':
            return sweep_response
        return per_msg.get(str(arg), ('NO', [None]))

    mailbox = MagicMock()
    mailbox.client.uid.side_effect = fake_uid
    return mailbox


class ImapBackupTestBase(unittest.TestCase):
    """Shared temp-dir setup/teardown for IMAP backup tests."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.backup_dir = os.path.join(self.temp_dir, 'backup')
        self.state_file = os.path.join(self.temp_dir, 'backup', 'backup_state.json')

    def tearDown(self):
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    def make_backup(self, mailbox, folder='[Gmail]/All Mail'):
        return ImapBackup(
            mailbox=mailbox,
            backup_dir=self.backup_dir,
            state_file=self.state_file,
            folder=folder,
            batch_size=10,
        )


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
        self.assertTrue(backup.state.is_email_backed_up('100'))

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
        self.assertTrue(backup.state.is_email_backed_up('100'))
        self.assertTrue(backup.state.is_email_backed_up('101'))


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
        fetched_args = [call.args[1] for call in mailbox2.client.uid.call_args_list]
        self.assertTrue(fetched_args)  # the sweep happened
        self.assertTrue(all(arg == '1:*' for arg in fetched_args))


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
        self.assertTrue(backup.state.is_email_backed_up('<abc123@example.com>'))


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


if __name__ == '__main__':
    unittest.main()
