"""Temporary, disk-backed document metadata for large corpus preparations."""
import hashlib
import numbers
import sqlite3

from corpus_split import check_cancelled, positive_integer


class DocumentIndex:
    """Keep document records, duplicate lookups and split sorting out of Python RAM."""

    def __init__(self, path, seed=1337, stop_requested=None):
        self.connection = sqlite3.connect(path)
        self.seed, self.stop_requested = seed, stop_requested
        self.total = self.count = 0
        # This is disposable staging metadata; the published artifacts are synced.
        self.connection.executescript('''
            PRAGMA journal_mode=OFF;
            PRAGMA synchronous=OFF;
            PRAGMA temp_store=FILE;
            PRAGMA cache_size=-4096;
            CREATE TABLE groups (
                key TEXT PRIMARY KEY, tokens INTEGER NOT NULL, rank BLOB NOT NULL,
                split TEXT NOT NULL DEFAULT 'train'
            );
            CREATE TABLE records (
                id INTEGER PRIMARY KEY, offset INTEGER NOT NULL, count INTEGER NOT NULL,
                key TEXT NOT NULL, source TEXT, row INTEGER, truncated INTEGER NOT NULL,
                source_key TEXT
            );
            CREATE TABLE seen (key TEXT PRIMARY KEY);
            CREATE TABLE aliases (key TEXT PRIMARY KEY, group_key TEXT NOT NULL);
            CREATE INDEX alias_groups ON aliases(group_key);
        ''')
        self.connection.set_progress_handler(self._cancel_sql, 10000)

    def _cancel_sql(self):
        return int(bool(self.stop_requested and self.stop_requested()))

    def __enter__(self):
        return self

    def __exit__(self, kind, error, traceback):
        self.connection.close()
        if isinstance(error, sqlite3.OperationalError):
            check_cancelled(self.stop_requested)

    def contains(self, key):
        return self.connection.execute('SELECT 1 FROM aliases WHERE key=?', ('stored:' + key,)).fetchone() is not None

    def seen(self, key):
        return self.connection.execute('SELECT 1 FROM seen WHERE key=?', (key,)).fetchone() is not None

    def remember(self, key):
        self.connection.execute('INSERT OR IGNORE INTO seen VALUES(?)', (key,))

    def append(self, record, *, source=None, row=None, truncated=False, source_key=None):
        offset, count, key = record
        positive_integer(count, 'Document token count')
        if isinstance(offset, bool) or not isinstance(offset, numbers.Integral) or offset != self.total:
            raise ValueError('Document offsets must cover the token stream consecutively without gaps or overlaps')
        if not isinstance(key, str) or not key:
            raise ValueError('Document hashes must be nonempty strings')
        offset, count = int(offset), int(count)
        source_key = source_key or key
        identities = ('stored:' + key, 'source:' + source_key)
        existing = {row[0] for row in self.connection.execute(
            'SELECT group_key FROM aliases WHERE key IN (?,?)', identities)}
        group = min(existing) if existing else key
        if not existing:
            rank = hashlib.sha256(f'{self.seed}:{group}'.encode()).digest()
            self.connection.execute('INSERT INTO groups(key,tokens,rank) VALUES(?,0,?)', (group, rank))
        # A repeated source may have a shorter final prefix at the corpus cap.
        # Union both identities so full-source duplicates AND matching retained
        # prefixes cannot enter different splits. Records retain their own hashes.
        for other in existing - {group}:
            self.connection.execute('UPDATE groups SET tokens=tokens+(SELECT tokens FROM groups WHERE key=?) WHERE key=?', (other, group))
            self.connection.execute('UPDATE aliases SET group_key=? WHERE group_key=?', (group, other))
            self.connection.execute('DELETE FROM groups WHERE key=?', (other,))
        for identity in identities:
            self.connection.execute('INSERT OR IGNORE INTO aliases VALUES(?,?)', (identity, group))
        self.connection.execute('UPDATE groups SET tokens=tokens+? WHERE key=?', (count, group))
        self.connection.execute('INSERT INTO records(offset,count,key,source,row,truncated,source_key) VALUES(?,?,?,?,?,?,?)',
                                (offset, count, key, source, row, int(truncated), source_key))
        self.remember(source_key)
        self.total += count
        self.count += 1
        if self.count % 4096 == 0:
            self.connection.commit()

    def __len__(self):
        return self.count

    @property
    def group_count(self):
        return self.connection.execute('SELECT COUNT(*) FROM groups').fetchone()[0]

    def select_validation(self, fraction, minimum):
        if self.group_count < 2:
            raise ValueError('Document-level validation needs at least two distinct documents; '
                             'provide more JSONL/Parquet rows or separate text files.')
        self.connection.execute("UPDATE groups SET split='train'")
        target, n_val = self.total * fraction, 0
        # SQLite sorts to disk with a bounded cache, including deterministic ties.
        for key, count in self.connection.execute('SELECT key,tokens FROM groups ORDER BY ABS(tokens-?),rank', (target,)):
            check_cancelled(self.stop_requested)
            if self.total - n_val - count < minimum:
                continue
            if n_val < minimum or abs(n_val + count - target) < abs(n_val - target):
                self.connection.execute("UPDATE groups SET split='val' WHERE key=?", (key,))
                n_val += count
        if min(n_val, self.total - n_val) < minimum:
            raise ValueError('Whole-document splits are too short for this context; supply more documents '
                             'or increase the token cap. No document will be split across train and validation.')
        self.connection.commit()
        return n_val

    def split_records(self):
        return self.connection.execute('''SELECT r.offset,r.count,r.key,g.split,r.source,r.row,r.truncated,r.source_key,g.key
            FROM records r JOIN aliases a ON a.key='stored:' || r.key
            JOIN groups g ON g.key=a.group_key ORDER BY r.id''')
