import sqlite3
from contextlib import contextmanager


class Database:
    def __init__(self, root):
        root.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(root / 'reminders.db')
        self.connection.row_factory = sqlite3.Row

    @contextmanager
    def transaction(self):
        with self.connection:
            yield self.connection

    def rows(self, sql, args=()):
        return [dict(r) for r in self.connection.execute(sql, args)]

    def close(self):
        self.connection.close()
