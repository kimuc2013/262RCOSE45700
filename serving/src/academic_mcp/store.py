import hashlib
import json
import sqlite3
import copy
import threading
from contextlib import contextmanager
from pathlib import Path
from .runtime import checkpoint, remaining_timeout


class Store:
    """Short-lived connections permit MCP tools to run in different threads."""

    def __init__(self, path: str):
        self.path = path
        self._snapshot_lock = threading.Lock()
        self._tables = {}
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            # Online revision reads must not fail while another request commits
            # its cache entry. WAL lets readers keep a consistent snapshot.
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript("""
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS chunks (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS cache (
                    key TEXT PRIMARY KEY, revision TEXT NOT NULL,
                    expires REAL NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS vectors (
                    revision TEXT NOT NULL, model TEXT NOT NULL,
                    payload TEXT NOT NULL, PRIMARY KEY (revision, model)
                );
                CREATE INDEX IF NOT EXISTS cache_expiry ON cache(expires);
            """)

    @contextmanager
    def connect(self, timeout=10):
        db = sqlite3.connect(self.path, timeout=remaining_timeout(timeout))
        try:
            with db:
                yield db
                checkpoint()
        finally:
            db.close()

    def snapshot(self):
        with self.connect() as db:
            db.execute("BEGIN")
            row = db.execute("SELECT value FROM meta WHERE key='revision'").fetchone()
            chunks = [json.loads(r[0]) for r in db.execute("SELECT payload FROM chunks ORDER BY id")]
            events = [json.loads(r[0]) for r in db.execute("SELECT payload FROM events ORDER BY id")]
        return row[0] if row else "empty", chunks, events

    def revision(self):
        with self.connect(timeout=1) as db:
            row = db.execute("SELECT value FROM meta WHERE key='revision'").fetchone()
        return row[0] if row else 'empty'

    def table_snapshot(self, table):
        """Keep only the latest decoded revision per table, including external updates.

        Copies protect shared state from callers. The revision and rows are read
        in one SQLite transaction; no process-local revision assumption is made.
        """
        if table not in ('chunks', 'events'):
            raise ValueError('Unsupported snapshot table')
        if not self._snapshot_lock.acquire(timeout=remaining_timeout(1)):
            from .runtime import Overloaded
            raise Overloaded('Snapshot preparation busy; retry later')
        try:
            checkpoint()
            with self.connect(timeout=1) as db:
                db.execute('BEGIN')
                row = db.execute("SELECT value FROM meta WHERE key='revision'").fetchone()
                revision = row[0] if row else 'empty'
                cached = self._tables.get(table)
                if cached is None or cached[0] != revision:
                    rows = [json.loads(r[0]) for r in db.execute(
                        f'SELECT payload FROM {table} ORDER BY id')]
                    self._tables[table] = (revision, rows)
            result = copy.deepcopy(self._tables[table])
            checkpoint()
            return result
        finally:
            self._snapshot_lock.release()

    def replace(self, chunks: list[dict], events: list[dict]):
        # Hash the complete validated snapshot. Even date/metadata-only updates invalidate caches.
        encoded = json.dumps([sorted(chunks, key=lambda c: c['id']),
                              sorted(events, key=lambda e: json.dumps(e, sort_keys=True))],
                             ensure_ascii=False, sort_keys=True)
        revision = hashlib.sha256(encoded.encode()).hexdigest()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT value FROM meta WHERE key='revision'").fetchone()
            if old and old[0] == revision:
                return {"changed": False, "revision": revision, "chunks": len(chunks), "events": len(events)}
            db.execute("DELETE FROM chunks")
            db.execute("DELETE FROM events")
            db.executemany("INSERT INTO chunks VALUES (?, ?)",
                           [(c['id'], json.dumps(c, ensure_ascii=False)) for c in chunks])
            db.executemany("INSERT INTO events VALUES (?, ?)",
                           [(str(i), json.dumps(e, ensure_ascii=False)) for i, e in enumerate(events)])
            db.execute("INSERT OR REPLACE INTO meta VALUES ('revision', ?)", (revision,))
            db.execute("DELETE FROM cache")
            db.execute("DELETE FROM vectors")
        return {"changed": True, "revision": revision, "chunks": len(chunks), "events": len(events)}

    def save_vectors(self, revision, model, vectors):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT value FROM meta WHERE key='revision'").fetchone()
            if not row or row[0] != revision:
                raise RuntimeError("Data changed while embedding; rebuild vectors")
            db.execute("INSERT OR REPLACE INTO vectors VALUES (?, ?, ?)",
                       (revision, model, json.dumps(vectors)))
            db.execute("DELETE FROM cache")

    def load_vectors(self, revision, model):
        with self.connect() as db:
            row = db.execute("SELECT payload FROM vectors WHERE revision=? AND model=?", (revision, model)).fetchone()
        if not row:
            raise RuntimeError("No matching vector index. Run python -m academic_mcp.index first")
        return json.loads(row[0])
