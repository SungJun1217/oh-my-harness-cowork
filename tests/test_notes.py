from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest

from omhc import brief, cli, due, notes
from ._repo import TempRepo

NOW = 1800000000


class TestNotes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = str(Path(self.tmp.name) / 'state')

    def test_listing_missing_notes_creates_nothing(self):
        self.assertEqual(notes.list_notes(self.state), [])
        self.assertFalse(Path(self.state).exists())

    def test_custom_ttl_expires_the_entire_normalized_note(self):
        record = notes.add(self.state, 'First\nsecond\tthird', ttl=3600, now=NOW)
        self.assertEqual(record['expires_at'], NOW + 3600)
        self.assertEqual(brief._notes(self.state, now=NOW), ['First second third'])
        self.assertEqual(brief._notes(self.state, now=NOW + 3601), [])
        listed = notes.list_notes(self.state, now=NOW + 3601)
        self.assertEqual(listed[0]['id'], record['id'])
        self.assertTrue(listed[0]['expired'])

    def test_legacy_and_default_notes_keep_existing_expiry_rules(self):
        Path(self.state).mkdir()
        (Path(self.state) / 'notes.txt').write_text('Legacy note\n')
        notes.add(self.state, 'Current note', now=NOW)
        self.assertEqual(notes.recent(self.state, now=NOW + due.MAX_AGE_SECONDS + 1), ['Legacy note'])

    def test_explicit_expiry_cannot_extend_the_reader_beyond_seven_days(self):
        Path(self.state).mkdir()
        line = '{}\t!expires={}\tOld note\n'.format(NOW, NOW + 10 * due.MAX_AGE_SECONDS)
        (Path(self.state) / 'notes.txt').write_text(line)
        self.assertEqual(notes.recent(self.state, now=NOW + due.MAX_AGE_SECONDS + 1), [])

    def test_ttl_parser_accepts_units_and_rejects_invalid_or_overlong_values(self):
        for text, seconds in [('30s', 30), ('30m', 1800), ('12h', 43200), ('7d', due.MAX_AGE_SECONDS)]:
            self.assertEqual(notes.parse_ttl(text), seconds)
        for text in ('0s', '-1d', '8d', '1.5h', 'forever', '1', '999999d'):
            with self.subTest(text=text), self.assertRaises(notes.NoteError):
                notes.parse_ttl(text)

    def test_ids_stay_stable_after_deleting_another_note(self):
        first = notes.add(self.state, 'First note', now=NOW)
        second = notes.add(self.state, 'Second note', now=NOW + 1)
        self.assertEqual(notes.delete(self.state, first['id'][:8]), 1)
        self.assertEqual(notes.list_notes(self.state)[0]['id'], second['id'])
        self.assertEqual(notes.delete(self.state, second['id']), 1)
        self.assertEqual(notes.list_notes(self.state), [])

    def test_identical_records_are_an_explicit_group_with_one_id(self):
        first = notes.add(self.state, 'Repeated note', now=NOW)
        notes.add(self.state, 'Repeated note', now=NOW)
        self.assertEqual(notes.list_notes(self.state)[0]['copies'], 2)
        self.assertEqual(notes.delete(self.state, first['id']), 2)

    def test_ambiguous_prefix_and_unknown_id_do_not_rewrite_notes(self):
        prefixes = {}
        for i in range(10000):
            line = 'Legacy note {}'.format(i)
            prefix = hashlib.sha256(line.encode()).hexdigest()[:4]
            if prefix in prefixes:
                pair = [prefixes[prefix], line]
                break
            prefixes[prefix] = line
        else:
            self.fail('No short-prefix collision found')
        Path(self.state).mkdir()
        path = Path(self.state) / 'notes.txt'
        path.write_text('\n'.join(pair) + '\n')
        before = path.read_bytes()
        for note_id in (prefix, 'ffffffffffabcdef'):
            with self.assertRaises(notes.NoteError):
                notes.delete(self.state, note_id)
            self.assertEqual(path.read_bytes(), before)

    def test_mutations_refuse_unrelated_invalid_utf8_before_writing(self):
        record = notes.add(self.state, 'Known note', now=NOW)
        path = Path(self.state) / 'notes.txt'
        with path.open('ab') as fh:
            fh.write(b'\xff\n')
        before = path.read_bytes()
        with self.assertRaises(UnicodeError):
            notes.delete(self.state, record['id'])
        self.assertEqual(path.read_bytes(), before)
        with self.assertRaises(UnicodeError):
            notes.add(self.state, 'Do not partially append this', now=NOW)
        self.assertEqual(path.read_bytes(), before)

    def test_concurrent_append_and_delete_do_not_lose_other_notes(self):
        original = notes.add(self.state, 'Remove this note', now=NOW)
        with ThreadPoolExecutor(max_workers=5) as pool:
            jobs = [pool.submit(notes.add, self.state, 'Keep {}'.format(i), now=NOW) for i in range(20)]
            jobs.append(pool.submit(notes.delete, self.state, original['id']))
            for job in jobs:
                job.result(timeout=10)
        self.assertEqual({r['text'] for r in notes.list_notes(self.state)},
                         {'Keep {}'.format(i) for i in range(20)})


class TestNoteCli(unittest.TestCase):
    def setUp(self):
        self.t = TempRepo()
        self.addCleanup(self.t.close)
        previous = os.getcwd()
        os.chdir(self.t.repo)
        self.addCleanup(os.chdir, previous)

    def call(self, *args):
        out = io.StringIO()
        code = cli.main(['note', *args], home=self.t.home, out=out)
        return code, out.getvalue()

    def test_old_add_syntax_and_new_management_commands_work_together(self):
        code, text = self.call('Remember the export', '--ttl', '1d')
        self.assertEqual(code, 0)
        self.assertIn('id=', text)
        code, text = self.call('list', '--json')
        record = json.loads(text)[0]
        self.assertEqual(record['text'], 'Remember the export')
        self.assertEqual(record['expires_at'] - record['created_at'], 86400)
        self.assertEqual(self.call('delete', record['id'])[0], 0)
        self.assertEqual(json.loads(self.call('list', '--json')[1]), [])

    def test_reserved_words_can_still_be_literal_note_text(self):
        self.assertEqual(self.call('add', 'list')[0], 0)
        self.assertEqual(json.loads(self.call('list', '--json')[1])[0]['text'], 'list')
