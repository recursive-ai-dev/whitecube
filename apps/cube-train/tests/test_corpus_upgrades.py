"""Regression coverage for scalable ingestion, document controls and corpus reuse."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'pipeline'))
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from corpus_artifacts import verify_corpus
from corpus_index import DocumentIndex
from corpus_split import split_documents
from prepare_folder import prepare_folder


class CorpusUpgradeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.docs = [f'Document {i}: ' + chr(65 + i) * 700 for i in range(20)]
        self.source = self.root / 'docs.jsonl'
        self.write_rows(self.source, self.docs)
        output = contextlib.redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)

    def write_rows(self, path, docs):
        path.write_text('\n'.join(json.dumps({'text': text}) for text in docs), encoding='utf-8')

    def stored(self, directory):
        values = {}
        for split in ('train', 'val'):
            tokens = np.fromfile(directory / f'{split}.bin', dtype=np.uint16)
            starts = [0] + list(np.where(tokens == 257)[0] + 1)
            self.assertEqual(starts[-1], len(tokens))
            values[split] = [bytes(tokens[start:end - 1].astype(np.uint8)).decode('utf-8')
                             for start, end in zip(starts, starts[1:])]
        return values

    def test_duplicates_do_not_consume_the_cap_or_exclude_later_documents(self):
        self.write_rows(self.source, [self.docs[0]] * 20 + self.docs[1:8])
        report = prepare_folder(self.source, self.root / 'out', max_tokens=6000, deduplicate=True)
        self.assertEqual(report['duplicate_documents'], 19)
        self.assertEqual(report['document_records'], 8)
        self.assertEqual(report['repeated_document_records'], 0)
        self.assertCountEqual(sum(self.stored(self.root / 'out').values(), []), self.docs[:8])

    def test_deduplication_across_formats_and_normalized_html(self):
        source = self.root / 'mixed'
        source.mkdir()
        self.write_rows(source / 'a.jsonl', self.docs[:4])
        pq.write_table(pa.table({'text': self.docs[:2] + self.docs[4:6]}), source / 'b.parquet')
        (source / 'c.txt').write_bytes(('\ufeff' + self.docs[0]).encode())
        (source / 'd.jsonl').write_text(json.dumps({'document': {'html': '<p>' + self.docs[1] + '</p>'}}))
        report = prepare_folder(source, self.root / 'out', deduplicate=True)
        self.assertEqual(report['duplicate_documents'], 4)
        self.assertCountEqual(sum(self.stored(self.root / 'out').values(), []), self.docs[:6])

    def test_document_limits_preserve_utf8_and_keep_reading_after_long_rows(self):
        docs = ['tiny'] + [f'{i:02d} ' + '😀漢é' * 1000 for i in range(12)]
        self.write_rows(self.source, docs)
        report = prepare_folder(self.source, self.root / 'out', min_document_bytes=100, max_document_bytes=701)
        stored = sum(self.stored(self.root / 'out').values(), [])
        self.assertEqual(len(stored), 12)
        self.assertEqual(report['short_documents'], 1)
        self.assertEqual(report['document_limit_truncations'], 12)
        self.assertEqual(report['corpus_limit_truncations'], 0)
        self.assertFalse(report['cap_reached'])
        for text in stored:
            self.assertLessEqual(len(text.encode()), 701)
            self.assertTrue(any(original.startswith(text) for original in docs[1:]))

    def test_identical_truncated_prefixes_stay_together(self):
        self.write_rows(self.source, ['a' * 700 + 'one', 'a' * 700 + 'two', 'b' * 900])
        report = prepare_folder(self.source, self.root / 'out', max_document_bytes=700)
        values = self.stored(self.root / 'out')
        self.assertFalse(set(values['train']) & set(values['val']))
        self.assertEqual(report['unique_document_groups'], 2)
        self.assertEqual(report['repeated_document_records'], 1)
        dedup = prepare_folder(self.source, self.root / 'dedup', max_document_bytes=700, deduplicate=True)
        self.assertEqual(dedup['duplicate_documents'], 1)
        self.assertEqual(dedup['document_records'], 2)

    def test_plaintext_streaming_preserves_whitespace_and_has_one_eos(self):
        source = self.root / 'plain'
        source.mkdir()
        docs = ['a' * 65536 + ' ' * 65536 + '😀' * 20000, 'b' * 130000]
        for i, text in enumerate(docs):
            (source / f'{i}.txt').write_text(text, encoding='utf-8')
        report = prepare_folder(source, self.root / 'out')
        self.assertEqual(report['document_records'], 2)
        self.assertEqual(report['rows_scanned'], 2)
        self.assertEqual(report['total_tokens'], sum(len(text.encode()) + 1 for text in docs))
        self.assertCountEqual(sum(self.stored(self.root / 'out').values(), []), docs)

    def test_source_duplicates_with_different_retained_lengths_stay_together(self):
        long = 'Repeated source ' + 'x' * 4000
        self.write_rows(self.source, [long, 'Independent ' + 'y' * 1000, long])
        report = prepare_folder(self.source, self.root / 'out', max_tokens=6000)
        self.assertEqual(report['unique_document_groups'], 2)
        records = [json.loads(line) for line in (self.root / 'out/documents.jsonl').read_text().splitlines()]
        self.assertEqual(records[0]['source_sha256'], records[2]['source_sha256'])
        self.assertNotEqual(records[0]['sha256'], records[2]['sha256'])
        self.assertEqual(records[0]['split'], records[2]['split'])

    def test_bridge_document_unions_source_and_retained_identity_groups(self):
        with DocumentIndex(self.root / 'index.sqlite') as index:
            index.append((0, 700, 'prefix-a'), source_key='source-a')
            index.append((700, 700, 'prefix-b'), source_key='source-b')
            index.append((1400, 700, 'prefix-a'), source_key='source-b')
            index.append((2100, 700, 'prefix-c'), source_key='source-c')
            self.assertEqual(index.group_count, 2)
            index.select_validation(.2, 258)
            records = list(index.split_records())
            self.assertEqual(len({row[3] for row in records[:3]}), 1)

    def test_index_addresses_exact_stored_documents_and_physical_jsonl_lines(self):
        self.source.write_text('\n' + json.dumps(self.docs[0]) + '\n\n' + json.dumps(self.docs[1]))
        report = prepare_folder(self.source, self.root / 'out')
        records = [json.loads(line) for line in (self.root / 'out/documents.jsonl').read_text().splitlines()]
        self.assertEqual([record['row'] for record in records], [2, 4])
        positions = {'train': 0, 'val': 0}
        for record in records:
            self.assertEqual(record['source'], str(self.source))
            self.assertEqual(record['offset'], positions[record['split']])
            tokens = np.fromfile(self.root / 'out' / (record['split'] + '.bin'), dtype=np.uint16,
                                 offset=record['offset'] * 2, count=record['tokens'])
            self.assertEqual(tokens[-1], 257)
            digest = hashlib.sha256(bytes(tokens[:-1].astype(np.uint8))).hexdigest()
            self.assertEqual(digest, record['sha256'])
            self.assertEqual(digest, record['source_sha256'])
            positions[record['split']] += record['tokens']
        self.assertEqual(positions, {'train': report['train_tokens'], 'val': report['val_tokens']})
        verify_corpus(self.root / 'out')

    def test_reuse_is_independent_preserves_splits_and_requires_no_source_files(self):
        original = prepare_folder(self.source, self.root / 'a', deduplicate=True)
        self.source.unlink()
        reused = prepare_folder(self.root / 'a', self.root / 'b', max_tokens=6000, seed=999, val_fraction=.4)
        self.assertTrue(reused['reuse_verified'])
        self.assertEqual(reused['reused_from'], str(self.root / 'a'))
        self.assertEqual(reused['artifacts'], original['artifacts'])
        self.assertEqual(reused['split_seed'], original['split_seed'])
        for name in original['artifacts']:
            self.assertEqual((self.root / 'a' / name).read_bytes(), (self.root / 'b' / name).read_bytes())
            self.assertNotEqual((self.root / 'a' / name).stat().st_ino, (self.root / 'b' / name).stat().st_ino)
        (self.root / 'a/train.bin').write_bytes(b'corrupted')
        verify_corpus(self.root / 'b')

    def test_corrupt_token_or_index_prevents_reuse_and_cleans_staging(self):
        for name in ('train.bin', 'documents.jsonl'):
            with self.subTest(name=name):
                prepare_folder(self.source, self.root / name)
                artifact = self.root / name / name
                data = bytearray(artifact.read_bytes())
                data[0] ^= 1
                artifact.write_bytes(data)
                with self.assertRaisesRegex(ValueError, 'integrity verification'):
                    prepare_folder(self.root / name, self.root / 'out')
                self.assertFalse((self.root / 'out').exists())
                self.assertFalse(list(self.root.glob('.reuse-*')))

    def test_legacy_prepared_corpora_can_be_reused_and_gain_integrity_records(self):
        prepare_folder(self.source, self.root / 'a')
        (self.root / 'a/corpus.json').write_text('{}')
        (self.root / 'a/documents.jsonl').unlink()
        report = prepare_folder(self.root / 'a', self.root / 'out')
        self.assertEqual(set(report['artifacts']), {'train.bin', 'val.bin'})
        self.assertGreater(report['total_tokens'], 0)
        verify_corpus(self.root / 'out')

    def test_reuse_cancellation_before_publication_removes_all_staging(self):
        prepare_folder(self.source, self.root / 'a')
        for phase in ('reusing', 'verifying', 'publishing'):
            stopped = False
            def progress(event):
                nonlocal stopped
                stopped |= event['phase'] == phase
            with self.subTest(phase=phase), self.assertRaises(InterruptedError):
                prepare_folder(self.root / 'a', self.root / 'out', progress_callback=progress,
                               stop_requested=lambda: stopped)
            self.assertFalse((self.root / 'out').exists())
            self.assertFalse(list(self.root.glob('.reuse-*')))

    def test_prepared_source_mutation_during_copy_is_detected(self):
        prepare_folder(self.source, self.root / 'a')
        changed = False
        def progress(event):
            nonlocal changed
            if event['phase'] == 'verifying' and not changed:
                changed = True
                with (self.root / 'a/train.bin').open('ab') as handle:
                    handle.write(b'\0\0')
        with self.assertRaisesRegex(ValueError, 'changed during reuse'):
            prepare_folder(self.root / 'a', self.root / 'out', progress_callback=progress)
        self.assertFalse((self.root / 'out').exists())

    def test_invalid_document_controls_fail_before_output(self):
        for options in ({'deduplicate': 1}, {'min_document_bytes': -1}, {'max_document_bytes': True},
                        {'min_document_bytes': 100, 'max_document_bytes': 10}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                prepare_folder(self.source, self.root / 'out', **options)
            self.assertFalse((self.root / 'out').exists())

    def test_disk_backed_index_accepts_a_single_pass_record_stream(self):
        combined = self.root / 'tokens.bin'
        combined.write_bytes(b'\0\0' * 2000)
        records = ((i * 100, 100, str(i)) for i in range(20))
        train, val = split_documents(combined, self.root / 'out', records, block_size=16)
        self.assertEqual(train + val, 2000)
        self.assertEqual(len((self.root / 'out/documents.jsonl').read_text().splitlines()), 20)
        with DocumentIndex(self.root / 'index.sqlite') as index:
            index.append((0, 1000, 'a'))
            index.append((1000, 1000, 'b'))
            self.assertEqual(index.group_count, 2)
            self.assertEqual(index.select_validation(.1, 258), 1000)


if __name__ == '__main__':
    unittest.main()
