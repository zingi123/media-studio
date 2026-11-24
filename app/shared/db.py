import sqlite3
import threading
import time
from pathlib import Path
from .paths import data_dir


class DB:
    def __init__(self, path=None):
        self.path = path or str(data_dir() / "mediastudio.db")
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self.lock:
            self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS history(
                id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT, title TEXT, platform TEXT,
                status TEXT, path TEXT, size INTEGER, created REAL);
            CREATE TABLE IF NOT EXISTS library(
                id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT UNIQUE, title TEXT,
                kind TEXT, source TEXT, size INTEGER, created REAL);
            """)

    def _exec(self, sql, args=()):
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur

    def add_history(self, url, title, platform, status, path="", size=0):
        self._exec("INSERT INTO history(url,title,platform,status,path,size,created) VALUES(?,?,?,?,?,?,?)",
                   (url, title, platform, status, path, size, time.time()))

    def history(self, limit=500):
        with self.lock:
            return [dict(r) for r in self.conn.execute(
                "SELECT * FROM history ORDER BY id DESC LIMIT ?", (limit,))]

    def history_count(self):
        with self.lock:
            return int(self.conn.execute("SELECT COUNT(*) FROM history").fetchone()[0])

    def clear_history(self):
        """Delete the download-history list.  Returns how many rows were removed."""
        n = self.history_count()
        self._exec("DELETE FROM history")
        return n

    def completed_path(self, url):
        """Return an existing file path if this URL was already downloaded."""
        with self.lock:
            rows = self.conn.execute(
                "SELECT path FROM history WHERE url=? AND status='Done' ORDER BY id DESC", (url,)).fetchall()
        for r in rows:
            if r["path"] and Path(r["path"]).exists():
                return r["path"]
        return None

    def add_library(self, path, title, kind, source):
        p = Path(path)
        size = p.stat().st_size if p.exists() else 0
        self._exec("INSERT OR IGNORE INTO library(path,title,kind,source,size,created) VALUES(?,?,?,?,?,?)",
                   (str(p), title, kind, source, size, time.time()))

    def library(self):
        with self.lock:
            return [dict(r) for r in self.conn.execute("SELECT * FROM library ORDER BY id DESC")]

    def remove_library(self, path):
        self._exec("DELETE FROM library WHERE path=?", (path,))
