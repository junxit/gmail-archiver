"""Tests for the filesystem-safety and durability primitives.

These cover the guarantees the archive depends on: an untrusted message id can
never escape the backup directory, a long or non-ASCII subject can never make a
message unwritable, and a partial write can never leave a truncated file behind.
"""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from gmail_archiver.restore import GmailRestore
from gmail_archiver.utils import (
    MAX_FILENAME_BYTES,
    build_email_filename,
    ensure_within,
    safe_key,
    truncate_to_bytes,
    write_atomic,
)


class TestSafeKey(unittest.TestCase):
    """Message ids are sender-controlled on the IMAP fallback path."""

    def test_plain_id_passes_through(self):
        self.assertEqual(safe_key('1234567890', 'fb'), '1234567890')

    def test_angle_brackets_stripped(self):
        self.assertEqual(safe_key('<abc@example.com>', 'fb'), 'abc@example.com')

    def test_path_separators_removed(self):
        key = safe_key('../../../../etc/passwd', 'fb')

        self.assertNotIn('/', key)
        self.assertNotIn('\\', key)

    def test_dot_segments_cannot_survive(self):
        """'..' must never come back as a usable component."""
        for hostile in ('..', '.', '../', './.', '...'):
            key = safe_key(hostile, 'fallback')
            self.assertNotIn(key, ('.', '..'), f"{hostile!r} produced {key!r}")

    def test_empty_falls_back(self):
        self.assertEqual(safe_key('', 'fallback'), 'fallback')
        self.assertEqual(safe_key(None, 'fallback'), 'fallback')

    def test_absolute_path_neutralized(self):
        key = safe_key('/etc/shadow', 'fb')

        self.assertFalse(key.startswith('/'))
        self.assertNotIn('/', key)

    def test_null_byte_removed(self):
        self.assertNotIn('\x00', safe_key('abc\x00def', 'fb'))

    def test_no_leading_dot(self):
        """A leading dot would create a hidden file."""
        self.assertFalse(safe_key('.hidden', 'fb').startswith('.'))

    def test_result_is_length_bounded(self):
        key = safe_key('x' * 5000, 'fb')
        self.assertLessEqual(len(key.encode('utf-8')), 128)


class TestTruncateToBytes(unittest.TestCase):
    """Filesystem limits are on bytes, not characters."""

    def test_short_string_unchanged(self):
        self.assertEqual(truncate_to_bytes('hello', 100), 'hello')

    def test_truncates_by_bytes(self):
        result = truncate_to_bytes('漢' * 100, 10)
        self.assertLessEqual(len(result.encode('utf-8')), 10)

    def test_never_produces_invalid_utf8(self):
        """Cutting mid-character drops the partial sequence."""
        for limit in range(0, 12):
            result = truncate_to_bytes('漢字漢字', limit)
            result.encode('utf-8').decode('utf-8')  # must not raise

    def test_zero_budget(self):
        self.assertEqual(truncate_to_bytes('abc', 0), '')


class TestBuildEmailFilename(unittest.TestCase):
    """Assembled names must fit the 255-byte component limit."""

    def test_normal_case_includes_all_parts(self):
        name = build_email_filename('msg1', 'a' * 64, 'Hello World')

        self.assertTrue(name.startswith('msg1_aaaaaaaa_'))
        self.assertTrue(name.endswith('.eml'))

    def test_long_unicode_subject_is_clamped(self):
        name = build_email_filename('msg1', 'a' * 64, '漢' * 300)

        self.assertLessEqual(len(name.encode('utf-8')), MAX_FILENAME_BYTES)
        self.assertTrue(name.endswith('.eml'))

    def test_long_ascii_subject_is_clamped(self):
        name = build_email_filename('msg1', 'a' * 64, 'x' * 1000)

        self.assertLessEqual(len(name.encode('utf-8')), MAX_FILENAME_BYTES)

    def test_max_length_key_still_fits(self):
        name = build_email_filename('k' * 128, 'a' * 64, '漢' * 300)

        self.assertLessEqual(len(name.encode('utf-8')), MAX_FILENAME_BYTES)

    def test_empty_subject_handled(self):
        name = build_email_filename('msg1', 'a' * 64, '')

        self.assertTrue(name.endswith('.eml'))
        self.assertIn('msg1', name)

    def test_subject_separators_removed(self):
        name = build_email_filename('msg1', 'a' * 64, 'a/b/c')

        self.assertNotIn('/', name)


class TestEnsureWithin(unittest.TestCase):
    """Containment must be checked on resolved paths, not lexically."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.root = Path(self.temp_dir) / 'archive'
        self.root.mkdir()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_inside_path_allowed(self):
        result = ensure_within(self.root / 'emails' / 'a.eml', self.root)
        self.assertTrue(result.is_relative_to(self.root.resolve()))

    def test_root_itself_allowed(self):
        self.assertEqual(ensure_within(self.root, self.root), self.root.resolve())

    def test_dotdot_escape_rejected(self):
        with self.assertRaises(ValueError):
            ensure_within(self.root / 'emails' / '..' / '..' / 'evil.json', self.root)

    def test_absolute_outside_rejected(self):
        with self.assertRaises(ValueError):
            ensure_within(Path('/etc/passwd'), self.root)

    def test_sibling_directory_rejected(self):
        """A prefix match on the string must not be mistaken for containment."""
        sibling = Path(self.temp_dir) / 'archive-other' / 'x.eml'
        with self.assertRaises(ValueError):
            ensure_within(sibling, self.root)


class TestWriteAtomic(unittest.TestCase):
    """A crash must never leave a half-written file."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_writes_content(self):
        path = Path(self.temp_dir) / 'f.bin'
        write_atomic(path, b'hello')

        self.assertEqual(path.read_bytes(), b'hello')

    def test_overwrites_existing(self):
        path = Path(self.temp_dir) / 'f.bin'
        path.write_bytes(b'old content that is long')
        write_atomic(path, b'new')

        self.assertEqual(path.read_bytes(), b'new')

    def test_leaves_no_temp_file(self):
        path = Path(self.temp_dir) / 'f.bin'
        write_atomic(path, b'hello')

        self.assertEqual([p.name for p in Path(self.temp_dir).glob('*.tmp')], [])

    def test_honors_mode(self):
        path = Path(self.temp_dir) / 'secret'
        write_atomic(path, b'token', mode=0o600)

        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_original_survives_a_failed_write(self):
        """If the new content cannot be written, the old file is untouched."""
        path = Path(self.temp_dir) / 'f.bin'
        path.write_bytes(b'original')

        class Boom(bytes):
            pass

        with self.assertRaises(TypeError):
            write_atomic(path, "not bytes")  # str triggers a write failure

        self.assertEqual(path.read_bytes(), b'original')
        self.assertEqual([p.name for p in Path(self.temp_dir).glob('*.tmp')], [])


class TestRestorePathContainment(unittest.TestCase):
    """A backup_path in metadata is data, and must not escape the archive."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.backup_dir = Path(self.temp_dir) / 'backup'
        (self.backup_dir / 'emails').mkdir(parents=True)
        (self.backup_dir / 'metadata').mkdir(parents=True)
        self.mock_service = MagicMock()
        self.restore = GmailRestore(
            gmail_service=self.mock_service, backup_dir=str(self.backup_dir)
        )

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _metadata(self, name, backup_path):
        path = self.backup_dir / 'metadata' / f'{name}.json'
        path.write_text(json.dumps({'labels': ['INBOX'], 'backup_path': backup_path}))
        return path

    def test_absolute_path_is_refused(self):
        """An absolute backup_path must not upload an arbitrary local file."""
        secret = Path(self.temp_dir) / 'secret.txt'
        secret.write_bytes(b'private local file')
        meta = self._metadata('evil', str(secret))

        processed, errors = self.restore._process_restore_batch([meta])

        self.assertEqual((processed, errors), (0, 1))
        self.mock_service.users().messages().import_.assert_not_called()

    def test_traversal_path_is_refused(self):
        secret = Path(self.temp_dir) / 'secret.txt'
        secret.write_bytes(b'private local file')
        meta = self._metadata('evil', '../secret.txt')

        processed, errors = self.restore._process_restore_batch([meta])

        self.assertEqual((processed, errors), (0, 1))
        self.mock_service.users().messages().import_.assert_not_called()

    def test_normal_path_is_allowed(self):
        eml = self.backup_dir / 'emails' / 'ok.eml'
        eml.write_bytes(b'From: a@b.c\n\nbody')
        meta = self._metadata('ok', 'emails/ok.eml')
        self.mock_service.users().messages().import_().execute.return_value = {'id': 'new'}

        processed, errors = self.restore._process_restore_batch([meta])

        self.assertEqual((processed, errors), (1, 0))


if __name__ == '__main__':
    unittest.main()
