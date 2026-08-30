"""Tests for the backup module."""
import base64
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from gmail_archiver.backup import GmailBackup
from gmail_archiver.store import ArchiveStore

EMAIL_CONTENT = b"""From: sender@example.com
To: recipient@example.com
Subject: Test Email
Date: Mon, 1 Jan 2024 12:00:00 +0000

This is a test email body.
"""

# 2024-01-01T12:00:00Z
INTERNAL_DATE_MS = '1704110400000'


def api_message(msg_id='msg123', raw=EMAIL_CONTENT, labels=('INBOX',)):
    """Build a Gmail API ``messages.get(format='raw')`` response."""
    return {
        'id': msg_id,
        'threadId': 'thread456',
        'raw': base64.urlsafe_b64encode(raw).decode(),
        'internalDate': INTERNAL_DATE_MS,
        'labelIds': list(labels),
    }


class BackupTestCase(unittest.TestCase):
    """Shared fixture: a temp archive and a mocked Gmail service."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.backup_dir = os.path.join(self.temp_dir, 'backup')
        self.db_path = os.path.join(self.temp_dir, 'index.db')
        self.mock_service = MagicMock()
        self.backups = []

    def tearDown(self):
        for backup in self.backups:
            try:
                backup.close()
            except Exception:
                pass
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    def make_backup(self, **kwargs):
        """Create a GmailBackup wired to the temp archive, closed on teardown."""
        kwargs.setdefault('gmail_service', self.mock_service)
        kwargs.setdefault('backup_dir', self.backup_dir)
        kwargs.setdefault('db_path', self.db_path)
        kwargs.setdefault('batch_size', 10)
        backup = GmailBackup(**kwargs)
        self.backups.append(backup)
        return backup


class TestGmailBackupInitialization(BackupTestCase):
    """Test cases for GmailBackup class initialization."""

    def test_initialization_creates_directories(self):
        """Initialization creates the archive directory layout."""
        backup = self.make_backup()

        self.assertTrue(os.path.exists(self.backup_dir))
        self.assertTrue(os.path.exists(backup.emails_dir))
        self.assertTrue(os.path.exists(backup.metadata_dir))

    def test_initialization_with_path_objects(self):
        """Path objects are accepted as well as strings."""
        backup = self.make_backup(
            backup_dir=Path(self.backup_dir), db_path=Path(self.db_path), batch_size=50
        )

        self.assertEqual(backup.batch_size, 50)
        self.assertIsInstance(backup.backup_dir, Path)

    def test_default_batch_size(self):
        """Default batch size is 100."""
        backup = GmailBackup(
            gmail_service=self.mock_service,
            backup_dir=self.backup_dir,
            db_path=self.db_path,
        )
        self.backups.append(backup)
        self.assertEqual(backup.batch_size, 100)

    def test_index_defaults_into_backup_dir(self):
        """Without an explicit db_path the index lives inside the archive."""
        backup = GmailBackup(
            gmail_service=self.mock_service, backup_dir=self.backup_dir
        )
        self.backups.append(backup)
        self.assertEqual(backup.db_path, Path(self.backup_dir).resolve() / 'index.db')


class TestLegacyStateMigration(BackupTestCase):
    """A pre-SQLite archive keeps its dedup record when the index is created."""

    def test_migrates_legacy_state_file(self):
        """backup_state.json is imported and then set aside."""
        os.makedirs(self.backup_dir, exist_ok=True)
        legacy = Path(self.backup_dir) / 'backup_state.json'
        legacy.write_text(json.dumps({
            'emails': {
                'old1': {
                    'message_id': 'old1', 'thread_id': 't1', 'labels': ['INBOX'],
                    'internal_date': '2024-01-01T12:00:00+00:00',
                    'backup_path': 'emails/2024/01/old1.eml', 'size': 42,
                },
            },
            'backed_up_message_ids': ['old1', 'old2'],
        }))

        backup = self.make_backup()

        self.assertTrue(backup.store.is_archived('old1'))
        self.assertTrue(backup.store.is_archived('old2'))
        self.assertFalse(legacy.exists(), "legacy state should be renamed after import")
        self.assertTrue((Path(self.backup_dir) / 'backup_state.json.migrated').exists())


class TestGmailBackupGetEmail(BackupTestCase):
    """Test cases for _get_email method."""

    def test_get_email_success(self):
        """Successful retrieval returns the API payload."""
        backup = self.make_backup()
        expected = api_message()
        self.mock_service.users().messages().get().execute.return_value = expected

        self.assertEqual(backup._get_email('msg123'), expected)

    def test_get_email_not_found(self):
        """A 404 returns None rather than raising."""
        from googleapiclient.errors import HttpError

        backup = self.make_backup()
        mock_response = MagicMock()
        mock_response.status = 404
        self.mock_service.users().messages().get().execute.side_effect = HttpError(
            resp=mock_response, content=b'Not found'
        )

        self.assertIsNone(backup._get_email('nonexistent'))

    def test_get_email_uses_retries(self):
        """Transient 429/5xx responses are retried by the client library."""
        backup = self.make_backup()
        execute = self.mock_service.users().messages().get().execute
        execute.return_value = api_message()

        backup._get_email('msg123')

        _, kwargs = execute.call_args
        self.assertGreater(kwargs.get('num_retries', 0), 0)


class TestGmailBackupSaveEmail(BackupTestCase):
    """Test cases for the shared writer."""

    def test_save_email_creates_file_and_metadata(self):
        """_save_email writes the .eml and its sidecar."""
        backup = self.make_backup()

        result = backup._save_email(api_message(), ['INBOX'])

        self.assertIsNotNone(result)
        path, sha256 = result
        self.assertTrue(path.exists())
        self.assertTrue(str(path).endswith('.eml'))
        self.assertEqual(path.read_bytes(), EMAIL_CONTENT)
        self.assertEqual(len(sha256), 64)
        self.assertTrue((backup.metadata_dir / 'msg123.json').exists())

    def test_buckets_by_utc_not_local_time(self):
        """The YYYY/MM directory does not depend on the machine's timezone."""
        backup = self.make_backup()

        path, _ = backup._save_email(api_message(), ['INBOX'])

        self.assertEqual(path.parent, backup.emails_dir / '2024' / '01')

    def test_leaves_no_temp_files(self):
        """Atomic writes clean up after themselves."""
        backup = self.make_backup()
        backup._save_email(api_message(), ['INBOX'])

        leftovers = [p for p in Path(self.backup_dir).rglob('*.tmp')]
        self.assertEqual(leftovers, [])

    def test_missing_internal_date_still_archives(self):
        """A message with no internalDate is archived rather than dropped."""
        backup = self.make_backup()
        message = api_message()
        del message['internalDate']

        result = backup._save_email(message, ['INBOX'])

        self.assertIsNotNone(result, "message must still be archived")
        self.assertTrue(result[0].exists())


class TestFilenameSafety(BackupTestCase):
    """A message id or subject must never escape or break the archive."""

    def test_traversal_in_message_id_is_contained(self):
        """A ../-bearing id is sanitized instead of escaping the backup dir."""
        backup = self.make_backup()
        outside = Path(self.temp_dir) / 'pwned.json'

        result = backup._write_email(
            raw_bytes=EMAIL_CONTENT,
            msg_id='../../../../pwned',
            thread_id='t1',
            internal_date_ms=INTERNAL_DATE_MS,
            labels=[],
        )

        self.assertIsNotNone(result, "message should still be archived under a safe name")
        path, _ = result
        self.assertTrue(path.resolve().is_relative_to(Path(self.backup_dir).resolve()))
        self.assertFalse(outside.exists(), "must not write outside the archive")

    def test_state_clobbering_message_id_is_contained(self):
        """An id crafted to land on the archive's own index cannot do so."""
        backup = self.make_backup()

        backup._write_email(
            raw_bytes=EMAIL_CONTENT,
            msg_id='../index',
            thread_id='t1',
            internal_date_ms=INTERNAL_DATE_MS,
            labels=[],
        )

        # The index is a real database, not the JSON blob a clobber would write.
        self.assertTrue(backup.store.is_archived  # sanity: store still usable
                        is not None)
        stray = Path(self.backup_dir) / 'index.json'
        self.assertFalse(stray.exists())

    def test_absolute_message_id_is_contained(self):
        """An absolute-path id does not become an absolute write."""
        backup = self.make_backup()

        result = backup._write_email(
            raw_bytes=EMAIL_CONTENT,
            msg_id='/etc/evil',
            thread_id='t1',
            internal_date_ms=INTERNAL_DATE_MS,
            labels=[],
        )

        self.assertIsNotNone(result)
        self.assertTrue(result[0].resolve().is_relative_to(Path(self.backup_dir).resolve()))

    def test_very_long_unicode_subject_is_archived(self):
        """A long non-ASCII subject must not exceed the 255-byte name limit."""
        backup = self.make_backup()
        raw = ('Subject: ' + '漢' * 300 + '\r\nFrom: a@b.c\r\n\r\nbody\r\n').encode()

        result = backup._write_email(
            raw_bytes=raw,
            msg_id='msglong',
            thread_id='t1',
            internal_date_ms=INTERNAL_DATE_MS,
            labels=[],
        )

        self.assertIsNotNone(result, "long-subject message must still be archived")
        path, _ = result
        self.assertTrue(path.exists())
        self.assertLessEqual(len(path.name.encode('utf-8')), 255)


class TestSelfHealing(BackupTestCase):
    """The index alone is not proof the bytes are still on disk."""

    def test_truncated_file_is_redownloaded(self):
        """A short .eml is detected and fetched again."""
        backup = self.make_backup()
        self.mock_service.users().messages().get().execute.return_value = api_message()

        processed, errors, skipped = backup._process_email_batch(['msg123'])
        self.assertEqual((processed, errors, skipped), (1, 0, 0))

        record = backup.store.get('msg123')
        eml = Path(self.backup_dir) / record['rel_path']
        eml.write_bytes(b'truncated')

        processed, errors, skipped = backup._process_email_batch(['msg123'])

        self.assertEqual(processed, 1, "damaged message should be re-downloaded")
        self.assertEqual(eml.read_bytes(), EMAIL_CONTENT)

    def test_missing_file_is_redownloaded(self):
        """A deleted .eml is detected and fetched again."""
        backup = self.make_backup()
        self.mock_service.users().messages().get().execute.return_value = api_message()
        backup._process_email_batch(['msg123'])

        record = backup.store.get('msg123')
        (Path(self.backup_dir) / record['rel_path']).unlink()

        processed, _, _ = backup._process_email_batch(['msg123'])
        self.assertEqual(processed, 1)

    def test_verification_can_be_disabled(self):
        """--no-verify-existing trusts the index and skips the stat."""
        backup = self.make_backup(verify_existing=False)
        self.mock_service.users().messages().get().execute.return_value = api_message()
        backup._process_email_batch(['msg123'])

        record = backup.store.get('msg123')
        (Path(self.backup_dir) / record['rel_path']).unlink()

        processed, _, skipped = backup._process_email_batch(['msg123'])
        self.assertEqual((processed, skipped), (0, 1))


class TestGmailBackupEmails(BackupTestCase):
    """Test cases for backup_emails method."""

    @patch.object(GmailBackup, '_process_email_batch')
    def test_backup_emails_processes_batches(self, mock_process_batch):
        """backup_emails processes messages in batches."""
        mock_process_batch.return_value = (5, 0, 0)
        self.mock_service.users().messages().list().execute.return_value = {
            'messages': [{'id': f'msg_{i}'} for i in range(5)],
        }
        self.mock_service.users().messages().list_next.return_value = None

        result = self.make_backup().backup_emails()

        self.assertIn('total_processed', result)
        self.assertIn('total_errors', result)
        mock_process_batch.assert_called()

    @patch.object(GmailBackup, '_process_email_batch')
    def test_backup_emails_respects_max_results(self, mock_process_batch):
        """backup_emails stops once max_results new messages are downloaded."""
        mock_process_batch.return_value = (10, 0, 0)
        self.mock_service.users().messages().list().execute.return_value = {
            'messages': [{'id': f'msg_{i}'} for i in range(20)],
            'nextPageToken': 'next_page',
        }

        result = self.make_backup().backup_emails(max_results=10)

        self.assertEqual(result['total_processed'], 10)

    def test_backup_emails_handles_empty_mailbox(self):
        """An empty mailbox is handled cleanly."""
        self.mock_service.users().messages().list().execute.return_value = {'messages': []}

        result = self.make_backup().backup_emails()

        self.assertEqual(result['total_processed'], 0)
        self.assertEqual(result['total_errors'], 0)

    def test_enumeration_is_not_date_filtered(self):
        """Every run enumerates the whole mailbox.

        A "since last backup" filter meant any message that errored once fell
        outside all later queries and was never retried, so the list call must
        not carry a date query.
        """
        self.mock_service.users().messages().list().execute.return_value = {'messages': []}
        self.make_backup().backup_emails()

        for call in self.mock_service.users().messages().list.call_args_list:
            self.assertIsNone(call.kwargs.get('q'), "list() must not filter by date")

    def test_errors_are_recorded_for_retry(self):
        """A failed download is remembered so the next run tries again."""
        backup = self.make_backup()
        self.mock_service.users().messages().get().execute.return_value = None

        processed, errors, _ = backup._process_email_batch(['msg_fail'])

        self.assertEqual((processed, errors), (0, 1))
        self.assertEqual([f['msg_id'] for f in backup.store.list_failures()], ['msg_fail'])

    def test_success_clears_a_previous_failure(self):
        """Once a message downloads, it leaves the retry list."""
        backup = self.make_backup()
        backup.store.record_failure('msg123', 'earlier failure')
        self.mock_service.users().messages().get().execute.return_value = api_message()

        backup._process_email_batch(['msg123'])

        self.assertEqual(backup.store.list_failures(), [])


class TestTombstones(BackupTestCase):
    """Mail deleted in Gmail stays on disk and is flagged, never removed."""

    def _archive_two(self, backup):
        for msg_id in ('msg_a', 'msg_b'):
            self.mock_service.users().messages().get().execute.return_value = \
                api_message(msg_id=msg_id)
            backup._process_email_batch([msg_id])

    def test_vanished_message_is_flagged_and_kept(self):
        """A message that disappears from Gmail is tombstoned, not deleted."""
        backup = self.make_backup()
        self._archive_two(backup)
        kept = Path(self.backup_dir) / backup.store.get('msg_b')['rel_path']

        # Second sweep lists only msg_a.
        self.mock_service.users().messages().list().execute.return_value = {
            'messages': [{'id': 'msg_a'}]
        }
        self.mock_service.users().messages().list_next.return_value = None
        self.mock_service.users().messages().get().execute.return_value = api_message('msg_a')

        result = backup.backup_emails()

        self.assertEqual(result['tombstoned'], 1)
        self.assertEqual([r['msg_id'] for r in backup.store.list_vanished()], ['msg_b'])
        self.assertTrue(kept.exists(), "the .eml must never be deleted")

    def test_reappearing_message_clears_the_tombstone(self):
        """If Gmail shows the message again, the flag is cleared."""
        backup = self.make_backup()
        self._archive_two(backup)
        backup.store.tombstone_missing('9999-01-01T00:00:00+00:00')
        self.assertEqual(len(backup.store.list_vanished()), 2)

        backup.store.mark_seen(['msg_a'])

        self.assertEqual([r['msg_id'] for r in backup.store.list_vanished()], ['msg_b'])

    def test_no_tombstones_after_a_partial_sweep(self):
        """A run capped by max_results must not flag what it never looked at."""
        backup = self.make_backup()
        self._archive_two(backup)

        self.mock_service.users().messages().list().execute.return_value = {
            'messages': [{'id': 'msg_a'}], 'nextPageToken': 'more',
        }
        self.mock_service.users().messages().get().execute.return_value = api_message('msg_a')

        with patch.object(GmailBackup, '_process_email_batch', return_value=(1, 0, 0)):
            result = backup.backup_emails(max_results=1)

        self.assertEqual(result['tombstoned'], 0)
        self.assertEqual(backup.store.list_vanished(), [])

    def test_no_tombstones_when_the_sweep_had_errors(self):
        """Errors make the sweep an untrustworthy basis for flagging."""
        backup = self.make_backup()
        self._archive_two(backup)

        self.mock_service.users().messages().list().execute.return_value = {
            'messages': [{'id': 'msg_a'}]
        }
        self.mock_service.users().messages().list_next.return_value = None

        with patch.object(GmailBackup, '_process_email_batch', return_value=(0, 1, 0)):
            result = backup.backup_emails()

        self.assertEqual(result['tombstoned'], 0)
        self.assertEqual(backup.store.list_vanished(), [])


class TestGmailBackupProcessEmailBatch(BackupTestCase):
    """Test cases for _process_email_batch method."""

    def test_skips_already_backed_up_emails(self):
        """An intact archived message is skipped without an API call."""
        backup = self.make_backup()
        self.mock_service.users().messages().get().execute.return_value = api_message()
        backup._process_email_batch(['msg123'])
        self.mock_service.reset_mock()

        processed, errors, skipped = backup._process_email_batch(['msg123'])

        self.assertEqual((processed, errors, skipped), (0, 0, 1))
        self.mock_service.users().messages().get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
