"""Corpus ingestion, publication and split integrity regression tests."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import stat
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'pipeline'))
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from corpus_split import document_key, split_documents, utf8_prefix
from files import atomic_json, file_sha256, publish_directory
from prepare_folder import prepare_folder, texts
from prepare_parquet import pick_column, plain_html, prepare


class CorpusImprovementsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.docs = [f'Document {i}: ' + chr(65 + i % 26) * 650 for i in range(24)]
        self.source = self.root / 'docs.jsonl'
        self.source.write_text('\n'.join(json.dumps({'text': text}) for text in self.docs), encoding='utf-8')
        self.stdout = contextlib.redirect_stdout(io.StringIO())
        self.stdout.__enter__()
        self.addCleanup(self.stdout.__exit__, None, None, None)

    def documents(self, output):
        result = []
        for split in ('train.bin', 'val.bin'):
            tokens = np.fromfile(output / split, dtype=np.uint16)
            start = 0
            for end in np.where(tokens == 257)[0]:
                result.append(bytes(tokens[start:end].astype(np.uint8)).decode('utf-8'))
                start = end + 1
            self.assertEqual(start, len(tokens), 'Every stored document ends with EOS')
        return result

    def parquet(self, table, name='data.parquet'):
        path = self.root / name
        pq.write_table(table, path)
        return path

    def test_utf8_prefix_preserves_characters_at_every_boundary(self):
        original = 'Aé漢😀z'
        for limit in range(len(original.encode()) + 2):
            with self.subTest(limit=limit):
                raw, truncated = utf8_prefix(original, limit)
                self.assertTrue(original.startswith(raw.decode('utf-8')))
                self.assertLessEqual(len(raw), limit)
                self.assertEqual(truncated, raw.decode('utf-8') != original)

    def test_unicode_token_cap_keeps_valid_documents(self):
        docs = ['a' * 700, 'b' * 700, '😀漢é' * 3000]
        self.source.write_text('\n'.join(json.dumps(text) for text in docs), encoding='utf-8')
        report = prepare_folder(self.source, self.root / 'out', max_tokens=6000)
        result = self.documents(self.root / 'out')
        self.assertEqual(len(result), 3)
        self.assertTrue(any(docs[-1].startswith(value) and value.startswith('😀') for value in result))
        self.assertLessEqual(report['total_tokens'], 6000)
        self.assertTrue(report['cap_reached'])
        self.assertEqual(report['truncated_documents'], 1)

    def test_jsonl_utf8_bom_strings_nested_and_literal_dotted_keys(self):
        path = self.root / 'bom.jsonl'
        path.write_text('\ufeff"hello"\n\n{"document":{"html":"<h4>nested</h4>"}}\n'
                        '{"document.html":"<p>literal</p>"}\n', encoding='utf-8')
        self.assertEqual(list(texts(path)), ['hello', 'nested', 'literal'])

    def test_jsonl_null_and_blank_rows_are_counted_and_skipped(self):
        self.source.write_text('\n'.join(json.dumps(item) for item in
            [{'text': None}, {'text': ' \t '}, {'text': ''}] + [{'text': text} for text in self.docs]), encoding='utf-8')
        report = prepare_folder(self.source, self.root / 'out')
        self.assertEqual(report['rows_scanned'], len(self.docs) + 3)
        self.assertEqual(report['skipped_documents'], 3)
        self.assertCountEqual(self.documents(self.root / 'out'), self.docs)

    def test_jsonl_parse_error_includes_file_line_and_column(self):
        path = self.root / 'bad.jsonl'
        path.write_text('"valid"\n{"text":!}\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, r'bad.jsonl:2:9: invalid JSON'):
            list(texts(path))

    def test_jsonl_utf8_error_is_actionable(self):
        path = self.root / 'bad.jsonl'
        path.write_bytes(b'"valid"\n"\xff"\n')
        with self.assertRaisesRegex(ValueError, r'bad.jsonl:2: invalid UTF-8'):
            list(texts(path))

    def test_jsonl_limit_counts_bytes_not_unicode_characters(self):
        path = self.root / 'large.jsonl'
        path.write_text('"😀😀😀"\n', encoding='utf-8')
        with patch('prepare_folder.JSONL_LINE_BYTES', 12):
            with self.assertRaisesRegex(ValueError, 'line exceeds'):
                list(texts(path))

    def test_malformed_nested_json_field_does_not_use_parent_string(self):
        path = self.root / 'bad.jsonl'
        path.write_text('{"document":"not the requested field"}', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'no string text field'):
            list(texts(path, 'document.html'))

    def test_plaintext_bom_and_newlines_do_not_create_duplicate_groups(self):
        directory = self.root / 'plain'
        directory.mkdir()
        text = 'Same document\n' + 'x' * 650
        (directory / 'a.txt').write_text(text, encoding='utf-8')
        (directory / 'b.txt').write_bytes(('\ufeff' + text.replace('\n', '\r\n')).encode('utf-8'))
        (directory / 'c.md').write_text('Different document ' + 'y' * 650, encoding='utf-8')
        report = prepare_folder(directory, self.root / 'out')
        self.assertEqual(report['unique_document_groups'], 2)
        memberships = []
        for name in ('train.bin', 'val.bin'):
            token_bytes = np.fromfile(self.root / 'out' / name, dtype=np.uint16)
            memberships.append(sum(text.encode() in part for part in bytes(token_bytes.astype(np.uint8)).split(b'\x01')))
        self.assertIn(0, memberships)
        self.assertCountEqual(self.documents(self.root / 'out'), [text, text, 'Different document ' + 'y' * 650])

    def test_parquet_auto_column_skips_nontext_candidate(self):
        source = self.parquet(pa.table({'text': range(len(self.docs)), 'content': self.docs}))
        report = prepare(source, self.root / 'out')
        self.assertEqual(report['column'], 'content')
        self.assertCountEqual(self.documents(self.root / 'out'), self.docs)

    def test_parquet_null_parent_cannot_leak_hidden_child_values(self):
        values = pa.StructArray.from_arrays([pa.array(self.docs)], names=['html'],
                                           mask=pa.array([True] + [False] * (len(self.docs) - 1)))
        source = self.parquet(pa.table({'document': values}))
        self.assertEqual(list(texts(source)), self.docs[1:])
        report = prepare(source, self.root / 'out')
        self.assertEqual(report['skipped_documents'], 1)
        self.assertNotIn(self.docs[0], self.documents(self.root / 'out'))

    def test_parquet_dictionary_text_and_literal_dotted_column(self):
        for field in ('text', 'document.html'):
            with self.subTest(field=field):
                source = self.parquet(pa.table({field: pa.array(self.docs).dictionary_encode()}), field + '.parquet')
                self.assertEqual(list(texts(source, field)), self.docs)

    def test_empty_parquet_after_token_cap_is_rejected(self):
        source = self.root / 'shards'
        source.mkdir()
        (source / 'a.jsonl').write_bytes(self.source.read_bytes())
        pq.write_table(pa.table({'text': pa.array([], type=pa.string())}), source / 'z.parquet')
        with self.assertRaisesRegex(ValueError, 'Empty dataset'):
            prepare_folder(source, self.root / 'out', max_tokens=6000)
        self.assertFalse((self.root / 'out').exists())

    def test_html_discards_noncontent_and_preserves_structural_boundaries(self):
        html = '<h4>Title</h4><section>A<br/>B</section><table><tr><th>C</th><td>D</td></tr></table>'
        html += '<template>secret<style>x</style></template><noscript>hidden</noscript><p>E &amp; F</p>'
        self.assertEqual(plain_html(html), 'Title\nA\nB\nC D\nE & F')
        self.assertEqual(plain_html('<template/><p>kept</p>'), 'kept')

    def test_numeric_and_column_options_fail_before_output(self):
        options = [{'max_tokens': True}, {'max_tokens': 6500.5}, {'val_fraction': float('nan')},
                   {'val_fraction': True}, {'val_fraction': '0.1'}, {'seed': 1.5},
                   {'block_size': 0}, {'batch_rows': 0}, {'column': ''}, {'shuffle_files': 'yes'}]
        for values in options:
            with self.subTest(values=values), self.assertRaises(ValueError):
                prepare_folder(self.source, self.root / 'out', **values)
            self.assertFalse((self.root / 'out').exists())

    def test_missing_source_and_existing_symlink_are_not_silently_accepted(self):
        with self.assertRaisesRegex(FileNotFoundError, 'does not exist'):
            prepare_folder(self.root / 'missing', self.root / 'out')
        output = self.root / 'out'
        output.symlink_to(self.root / 'missing-output', target_is_directory=True)
        with self.assertRaises(FileExistsError):
            prepare_folder(self.source, output)
        self.assertTrue(output.is_symlink())

    def test_manifest_checksums_counts_and_source_provenance(self):
        with self.source.open('a', encoding='utf-8') as source:
            source.write('\n' + json.dumps({'text': self.docs[0]}))
        report = prepare_folder(self.source, self.root / 'out')
        self.assertEqual(report['schema_version'], 3)
        self.assertEqual(report['unique_document_groups'], len(self.docs))
        self.assertEqual(report['repeated_document_records'], 1)
        self.assertAlmostEqual(report['actual_val_fraction'], report['val_tokens'] / report['total_tokens'])
        self.assertEqual(report['consumed_files'][0]['source_bytes'], self.source.stat().st_size)
        for name, artifact in report['artifacts'].items():
            data = (self.root / 'out' / name).read_bytes()
            self.assertEqual(artifact['bytes'], len(data))
            self.assertEqual(artifact['sha256'], hashlib.sha256(data).hexdigest())
        self.assertEqual(json.loads((self.root / 'out' / 'corpus.json').read_text()), report)

    def test_source_changes_abort_preparation(self):
        changed = False
        def progress(event):
            nonlocal changed
            if event['phase'] == 'reading' and not changed:
                changed = True
                with self.source.open('a', encoding='utf-8') as handle:
                    handle.write('\n' + json.dumps({'text': 'Another document ' * 100}))
        with self.assertRaisesRegex(ValueError, 'changed during preparation'):
            prepare_folder(self.source, self.root / 'out', progress_callback=progress)
        self.assertFalse((self.root / 'out').exists())

    def test_progress_is_monotonic_and_cancellation_before_publish_cleans_up(self):
        events = []
        stopped = False
        def progress(event):
            nonlocal stopped
            events.append(event)
            stopped = event['phase'] == 'publishing'
        with self.assertRaises(InterruptedError):
            prepare_folder(self.source, self.root / 'out', progress_callback=progress,
                           stop_requested=lambda: stopped)
        counts = [event['tokens'] for event in events if 'tokens' in event]
        self.assertEqual(counts, sorted(counts))
        self.assertEqual({event['phase'] for event in events}, {'checking', 'reading', 'splitting', 'publishing'})
        self.assertFalse((self.root / 'out').exists())
        self.assertFalse(list(self.root.glob('.prepare-*')))

    def test_configured_context_size_is_respected(self):
        report = prepare_folder(self.source, self.root / 'out', block_size=1400)
        self.assertGreaterEqual(min(report['train_tokens'], report['val_tokens']), 1402)
        self.assertEqual(report['block_size'], 1400)

    def test_shuffle_manifest_and_seed_reproducibility(self):
        source = self.root / 'shards'
        source.mkdir()
        for i, text in enumerate(self.docs):
            (source / f'{i:02d}.txt').write_text(text, encoding='utf-8')
        first = prepare_folder(source, self.root / 'a', seed=42, shuffle_files=True)
        second = prepare_folder(source, self.root / 'b', seed=42, shuffle_files=True)
        self.assertIn('seeded shuffled files', first['selection'])
        self.assertEqual(first['available_files'], second['available_files'])
        self.assertNotEqual(first['available_files'], sorted(first['available_files']))
        self.assertEqual(first['artifacts'], second['artifacts'])

    def test_parquet_seed_cancellation_and_utf8_cap(self):
        source = self.parquet(pa.table({'text': self.docs[:2] + ['😀' * 6000]}))
        report = prepare(source, self.root / 'a', max_tokens=6000, seed=42)
        self.assertEqual(report['split_seed'], 42)
        self.assertEqual(len(self.documents(self.root / 'a')), 3)
        with self.assertRaises(InterruptedError):
            prepare(source, self.root / 'b', stop_requested=lambda: True)
        self.assertFalse((self.root / 'b').exists())

    def test_split_rejects_gaps_overlaps_counts_and_stream_size(self):
        source = self.root / 'tokens.bin'
        source.write_bytes(np.arange(40, dtype=np.uint16).tobytes())
        records = [(0, 20, 'a'), (20, 20, 'b')]
        for invalid in ([(0, 20, 'a'), (21, 19, 'b')], [(0, 20, 'a'), (19, 21, 'b')],
                        [(0, -1, 'a')], [(0, 20, '')], [(0, 20.5, 'a')], [(0, 19, 'a'), (19, 20, 'b')]):
            with self.subTest(records=invalid), self.assertRaises(ValueError):
                split_documents(source, self.root / 'out', invalid, block_size=2)
        train, val = split_documents(source, self.root / 'out', iter(records), block_size=2)
        self.assertEqual((train, val), (20, 20))

    def test_split_cancellation_does_not_publish_partial_files(self):
        source = self.root / 'tokens.bin'
        source.write_bytes(b'\0\0' * 1000)
        calls = 0
        def stop():
            nonlocal calls
            calls += 1
            return calls >= 7
        with self.assertRaises(InterruptedError):
            split_documents(source, self.root / 'out', [(0, 500, 'a'), (500, 500, 'b')], stop_requested=stop)
        self.assertFalse((self.root / 'out' / 'train.bin').exists())
        self.assertFalse((self.root / 'out' / 'val.bin').exists())

    def test_split_refuses_to_overwrite_existing_artifact(self):
        source = self.root / 'tokens.bin'
        source.write_bytes(b'\0\0' * 1000)
        (self.root / 'train.bin').write_bytes(b'existing')
        with self.assertRaises(FileExistsError):
            split_documents(source, self.root, [(0, 500, 'a'), (500, 500, 'b')])
        self.assertEqual((self.root / 'train.bin').read_bytes(), b'existing')

    def test_atomic_unicode_metadata_and_failure_preserve_prior_version(self):
        path = self.root / 'metadata.json'
        atomic_json(str(path), {'text': '日本語 😀'})
        prior = path.read_bytes()
        self.assertIn('日本語', prior.decode('utf-8'))
        with self.assertRaises(ValueError):
            atomic_json(path, {'invalid': float('nan')})
        self.assertEqual(path.read_bytes(), prior)
        with patch('files.os.fsync', side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):
                atomic_json(path, {'new': True})
        self.assertEqual(path.read_bytes(), prior)
        self.assertFalse(list(self.root.glob('.metadata.json-*')))

    def test_metadata_and_corpus_sync_files_before_publishing_directory_entries(self):
        events = []
        original_sync, original_replace = os.fsync, os.replace
        def sync(descriptor):
            events.append('directory' if stat.S_ISDIR(os.fstat(descriptor).st_mode) else 'file')
            original_sync(descriptor)
        def replace(source, destination):
            events.append('replace')
            original_replace(source, destination)
        with patch('files.os.fsync', side_effect=sync), patch('files.os.replace', side_effect=replace):
            atomic_json(self.root / 'metadata.json', {'complete': True})
            self.assertEqual(events, ['file', 'replace', 'directory'])
            events.clear()
            stage = self.root / 'stage'
            stage.mkdir()
            (stage / 'train.bin').write_bytes(b'\0\0')
            (stage / 'val.bin').write_bytes(b'\0\0')
            publish_directory(stage, self.root / 'out')
            self.assertEqual(events, ['file', 'file', 'directory', 'replace', 'directory'])

    def test_invalid_plaintext_utf8_names_the_source_and_leaves_no_output(self):
        path = self.root / 'bad.txt'
        path.write_bytes(b'text\xff')
        with self.assertRaisesRegex(ValueError, r'bad.txt: invalid UTF-8'):
            prepare_folder(path, self.root / 'out')
        self.assertFalse((self.root / 'out').exists())

    def test_failed_progress_observer_rolls_back_unpublished_corpus(self):
        def progress(event):
            if event['phase'] == 'publishing':
                raise RuntimeError('Observer disconnected')
        with self.assertRaisesRegex(RuntimeError, 'Observer disconnected'):
            prepare_folder(self.source, self.root / 'out', progress_callback=progress)
        self.assertFalse((self.root / 'out').exists())
        self.assertFalse(list(self.root.glob('.prepare-*')))

    def test_publication_failure_removes_only_reserved_empty_destination(self):
        stage, output = self.root / 'stage', self.root / 'out'
        stage.mkdir()
        (stage / 'complete').write_text('ready', encoding='utf-8')
        with patch('files.os.replace', side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):
                publish_directory(stage, output)
        self.assertFalse(output.exists())
        self.assertTrue((stage / 'complete').exists())
        publish_directory(stage, output)
        self.assertEqual((output / 'complete').read_text(), 'ready')
        self.assertFalse(stage.exists())

    def test_streaming_hash_matches_utf8_digest_and_is_cancellable(self):
        text = '😀é漢' * 30000
        self.assertEqual(document_key(text), hashlib.sha256(text.encode('utf-8')).hexdigest())
        self.assertEqual(file_sha256(self.source), hashlib.sha256(self.source.read_bytes()).hexdigest())
        with self.assertRaises(InterruptedError):
            file_sha256(self.source, stop_requested=lambda: True)


if __name__ == '__main__':
    unittest.main()
