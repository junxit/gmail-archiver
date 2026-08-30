"""Tests for the SQLite archive index."""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from gmail_archiver.store import (
    ArchiveStore,
    migrate_from_json,
    rebuild_from_metadata,
)


class StoreTestCase(unittest.TestCase):
    """Shared temp-dir fixture with an open store."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, 'index.db')
        self.store = ArchiveStore(self.db_path)

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def add(self, msg_id, size=100, rel_path=None, labels=('INBOX',)):
        self.store.record(
            msg_id=msg_id,
            thread_id='t1',
            internal_date=1704110400000,
            size=size,
            sha256='a' * 64,
            rel_path=rel_path or f'emails/2024/01/{msg_id}.eml',
            labels=list(labels),
        )


class TestArchiveStoreBasics(StoreTestCase):
    """Recording, lookup and dedup."""

    def test_unknown_message_is_not_archived(self):
        self.assertFalse(self.store.is_archived('nope'))

    def test_record_then_archived(self):
        self.add('m1')
        self.assertTrue(self.store.is_archived('m1'))

    def test_get_returns_fields_with_decoded_labels(self):
        self.add('m1', labels=['INBOX', 'Work'])
        record = self.store.get('m1')

        self.assertEqual(record['msg_id'], 'm1')
        self.assertEqual(record['size'], 100)
        self.assertEqual(record['labels'], ['INBOX', 'Work'])

    def test_re_recording_updates_in_place(self):
        """Re-archiving a repaired message must not duplicate or double-count."""
        self.add('m1', size=100)
        self.add('m1', size=250)

        self.assertEqual(self.store.stats()['total_emails'], 1)
        self.assertEqual(self.store.stats()['total_size'], 250)

    def test_stats_totals(self):
        self.add('m1', size=100)
        self.add('m2', size=50)

        stats = self.store.stats()
        self.assertEqual(stats['total_emails'], 2)
        self.assertEqual(stats['total_size'], 150)

    def test_state_survives_reopen(self):
        """A committed index is durable across process restarts."""
        self.add('m1')
        self.store.close()

        reopened = ArchiveStore(self.db_path)
        try:
            self.assertTrue(reopened.is_archived('m1'))
        finally:
            reopened.close()

    def test_meta_round_trip(self):
        self.store.set_meta('uidvalidity', '4242')
        self.assertEqual(self.store.get_meta('uidvalidity'), '4242')

    def test_schema_version_recorded(self):
        self.assertIsNotNone(self.store.get_meta('schema_version'))


class TestTombstones(StoreTestCase):
    """Vanished-message bookkeeping."""

    def test_unseen_message_is_tombstoned(self):
        self.add('m1')
        self.add('m2')
        self.store.mark_seen(['m1'])

        # A sweep that started after the records were written but is only
        # refreshing m1 leaves m2 behind.
        later = '9999-01-01T00:00:00+00:00'
        self.store.mark_seen(['m1'])
        count = self.store.tombstone_missing(later)

        vanished = {r['msg_id'] for r in self.store.list_vanished()}
        self.assertEqual(count, 2)
        self.assertEqual(vanished, {'m1', 'm2'})

    def test_mark_seen_clears_a_tombstone(self):
        """A message that reappears in Gmail is un-flagged."""
        self.add('m1')
        self.store.tombstone_missing('9999-01-01T00:00:00+00:00')
        self.assertEqual(len(self.store.list_vanished()), 1)

        self.store.mark_seen(['m1'])

        self.assertEqual(self.store.list_vanished(), [])

    def test_recording_again_clears_a_tombstone(self):
        self.add('m1')
        self.store.tombstone_missing('9999-01-01T00:00:00+00:00')

        self.add('m1')

        self.assertEqual(self.store.list_vanished(), [])

    def test_recently_seen_messages_are_not_tombstoned(self):
        """A sweep timestamp older than last_seen flags nothing."""
        self.add('m1')
        count = self.store.tombstone_missing('1970-01-01T00:00:00+00:00')

        self.assertEqual(count, 0)
        self.assertEqual(self.store.list_vanished(), [])


class TestFailures(StoreTestCase):
    """The retry list."""

    def test_record_and_list(self):
        self.store.record_failure('m1', 'rate limited')
        failures = self.store.list_failures()

        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]['msg_id'], 'm1')
        self.assertEqual(failures[0]['attempts'], 1)

    def test_repeat_failure_increments_attempts(self):
        self.store.record_failure('m1', 'rate limited')
        self.store.record_failure('m1', 'rate limited again')

        self.assertEqual(self.store.list_failures()[0]['attempts'], 2)

    def test_clear_failure(self):
        self.store.record_failure('m1', 'boom')
        self.store.clear_failure('m1')

        self.assertEqual(self.store.list_failures(), [])

    def test_failures_counted_in_stats(self):
        self.store.record_failure('m1', 'boom')
        self.assertEqual(self.store.stats()['failures'], 1)


class TestMigrateFromJson(StoreTestCase):
    """Importing a pre-SQLite state file."""

    def _write_legacy(self, payload):
        path = Path(self.temp_dir) / 'backup_state.json'
        path.write_text(json.dumps(payload))
        return path

    def test_imports_emails_and_flat_ids(self):
        path = self._write_legacy({
            'emails': {
                'm1': {
                    'message_id': 'm1', 'thread_id': 't1', 'labels': ['INBOX'],
                    'internal_date': '2024-01-01T12:00:00+00:00',
                    'backup_path': 'emails/2024/01/m1.eml', 'size': 42,
                },
            },
            'backed_up_message_ids': ['m1', 'm2'],
        })

        imported = migrate_from_json(path, self.store)

        self.assertEqual(imported, 2)
        self.assertTrue(self.store.is_archived('m1'))
        self.assertTrue(self.store.is_archived('m2'))
        self.assertEqual(self.store.get('m1')['size'], 42)

    def test_unreadable_legacy_file_imports_nothing(self):
        path = Path(self.temp_dir) / 'broken.json'
        path.write_text('not json {{{')

        self.assertEqual(migrate_from_json(path, self.store), 0)

    def test_missing_legacy_file_imports_nothing(self):
        self.assertEqual(
            migrate_from_json(Path(self.temp_dir) / 'absent.json', self.store), 0
        )


class TestRebuildFromMetadata(unittest.TestCase):
    """The index can be reconstructed from the archive itself."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.backup_dir = Path(self.temp_dir) / 'backup'
        (self.backup_dir / 'emails' / '2024' / '01').mkdir(parents=True)
        (self.backup_dir / 'metadata').mkdir(parents=True)
        self.store = ArchiveStore(Path(self.temp_dir) / 'index.db')

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _archive_one(self, msg_id='m1', body=b'raw message bytes'):
        rel = f'emails/2024/01/{msg_id}.eml'
        (self.backup_dir / rel).write_bytes(body)
        (self.backup_dir / 'metadata' / f'{msg_id}.json').write_text(json.dumps({
            'message_id': msg_id, 'thread_id': 't1', 'labels': ['INBOX'],
            'internal_date': '1704110400000', 'backup_path': rel,
            'size': len(body),
        }))
        return rel

    def test_rebuilds_index_from_disk(self):
        """Losing the database is recoverable from the archive alone."""
        self._archive_one('m1')
        self._archive_one('m2')

        indexed = rebuild_from_metadata(self.backup_dir, self.store)

        self.assertEqual(indexed, 2)
        self.assertTrue(self.store.is_archived('m1'))
        self.assertTrue(self.store.is_archived('m2'))

    def test_rebuild_recomputes_hash_and_size(self):
        body = b'some specific bytes'
        self._archive_one('m1', body=body)

        rebuild_from_metadata(self.backup_dir, self.store)
        record = self.store.get('m1')

        self.assertEqual(record['size'], len(body))
        self.assertEqual(len(record['sha256']), 64)

    def test_metadata_without_its_eml_is_skipped(self):
        """A sidecar whose .eml is gone must not be indexed as archived."""
        (self.backup_dir / 'metadata' / 'ghost.json').write_text(json.dumps({
            'message_id': 'ghost', 'backup_path': 'emails/2024/01/ghost.eml',
        }))

        indexed = rebuild_from_metadata(self.backup_dir, self.store)

        self.assertEqual(indexed, 0)
        self.assertFalse(self.store.is_archived('ghost'))

    def test_unreadable_metadata_is_skipped(self):
        self._archive_one('m1')
        (self.backup_dir / 'metadata' / 'bad.json').write_text('not json {{{')

        indexed = rebuild_from_metadata(self.backup_dir, self.store)

        self.assertEqual(indexed, 1)

    def test_missing_metadata_dir_raises(self):
        empty = Path(self.temp_dir) / 'empty'
        empty.mkdir()

        with self.assertRaises(ValueError):
            rebuild_from_metadata(empty, self.store)


if __name__ == '__main__':
    unittest.main()
