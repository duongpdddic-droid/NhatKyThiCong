"""Database layer — SQLite (dev) / PostgreSQL (prod via DATABASE_URL)."""
import os
from pathlib import Path

DATABASE_URL = os.getenv("DATABASE_URL", "")
DB_PATH = Path(os.getenv("DB_PATH", "nhatky.db"))
IS_PG = bool(DATABASE_URL)


def _adapt_sql(sql: str) -> str:
    if not IS_PG:
        return sql
    sql = sql.replace("?", "%s")
    sql = sql.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
    sql = sql.replace("datetime('now')", "NOW()")
    if "INSERT OR IGNORE" in sql:
        sql = sql.replace("INSERT OR IGNORE INTO", "INSERT INTO")
        if "ON CONFLICT" not in sql:
            sql = sql.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
    sql = sql.replace("PRAGMA foreign_keys = ON", "")
    return sql


def connect():
    if IS_PG:
        import psycopg2
        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = False
        return _PgConnection(conn)
    else:
        import sqlite3
        c = sqlite3.connect(str(DB_PATH))
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = ON")
        return c


class _PgConnection:
    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=()):
        sql = _adapt_sql(sql)
        cur = self._conn.cursor()
        try:
            cur.execute(sql, params)
        except Exception:
            self._conn.rollback()
            raise
        return _PgCursor(cur)

    def executescript(self, sql):
        sql = _adapt_sql(sql)
        cur = self._conn.cursor()
        cur.execute(sql)
        self._conn.commit()
        cur.close()

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()


class _PgCursor:
    def __init__(self, cur):
        self._cur = cur

    def fetchone(self):
        row = self._cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in self._cur.description]
        return dict(zip(cols, row))

    def fetchall(self):
        rows = self._cur.fetchall()
        cols = [d[0] for d in self._cur.description]
        return [dict(zip(cols, r)) for r in rows]

    def __iter__(self):
        cols = [d[0] for d in self._cur.description]
        for row in self._cur:
            yield dict(zip(cols, row))

    @property
    def rowcount(self):
        return self._cur.rowcount


def init_db():
    c = connect()
    try:
        schema = """
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            full_name TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'viewer',
            phone TEXT,
            email TEXT,
            telegram_chat_id TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS projects (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            address TEXT,
            investor TEXT,
            contractor TEXT,
            supervisor TEXT,
            start_date TEXT,
            end_date TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS user_projects (
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
            PRIMARY KEY (user_id, project_id)
        );
        CREATE TABLE IF NOT EXISTS labor_types (
            id SERIAL PRIMARY KEY,
            name TEXT UNIQUE NOT NULL,
            unit TEXT DEFAULT 'người',
            sort_order INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS machinery_types (
            id SERIAL PRIMARY KEY,
            name TEXT UNIQUE NOT NULL,
            unit TEXT DEFAULT 'máy',
            sort_order INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS diaries (
            id SERIAL PRIMARY KEY,
            project_id INTEGER NOT NULL REFERENCES projects(id),
            date TEXT NOT NULL,
            weather_temp REAL,
            weather_desc TEXT,
            weather_icon TEXT,
            foreman TEXT,
            total_workers INTEGER DEFAULT 0,
            notes TEXT,
            issues TEXT,
            issue_level TEXT DEFAULT 'none',
            issue_status TEXT DEFAULT 'none',
            created_by INTEGER REFERENCES users(id),
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW(),
            UNIQUE(project_id, date)
        );
        CREATE TABLE IF NOT EXISTS diary_labor (
            id SERIAL PRIMARY KEY,
            diary_id INTEGER NOT NULL REFERENCES diaries(id) ON DELETE CASCADE,
            labor_type_id INTEGER NOT NULL REFERENCES labor_types(id),
            count INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS diary_machinery (
            id SERIAL PRIMARY KEY,
            diary_id INTEGER NOT NULL REFERENCES diaries(id) ON DELETE CASCADE,
            machinery_type_id INTEGER NOT NULL REFERENCES machinery_types(id),
            hours REAL NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS diary_materials (
            id SERIAL PRIMARY KEY,
            diary_id INTEGER NOT NULL REFERENCES diaries(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            unit TEXT,
            quantity REAL NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS diary_photos (
            id SERIAL PRIMARY KEY,
            diary_id INTEGER NOT NULL REFERENCES diaries(id) ON DELETE CASCADE,
            filename TEXT NOT NULL,
            caption TEXT,
            is_previous INTEGER DEFAULT 0,
            uploaded_at TIMESTAMP DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS diary_tasks (
            id SERIAL PRIMARY KEY,
            diary_id INTEGER NOT NULL REFERENCES diaries(id) ON DELETE CASCADE,
            task_name TEXT NOT NULL,
            quantity REAL,
            unit TEXT,
            cumulative_pct REAL
        );
        CREATE TABLE IF NOT EXISTS recipients (
            id SERIAL PRIMARY KEY,
            project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            email TEXT,
            phone TEXT,
            telegram_chat_id TEXT,
            role TEXT,
            frequency TEXT DEFAULT 'daily',
            created_at TIMESTAMP DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        """
        if IS_PG:
            c.executescript(schema)
        else:
            c.executescript(schema.replace("SERIAL PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT")
                            .replace("TIMESTAMP DEFAULT NOW()", "TEXT DEFAULT (datetime('now'))"))

        labor_seed = [
            ("Kỹ sư", "người", 1), ("Công nhân xây dựng", "người", 2),
            ("Thợ xây", "người", 3), ("Thợ điện", "người", 4),
            ("Thợ nước", "người", 5), ("Thợ sắt", "người", 6),
            ("Thợ mộc", "người", 7), ("Thợ sơn", "người", 8),
            ("Thợ lát gạch", "người", 9), ("Phụ việc", "người", 10),
        ]
        for name, unit, order in labor_seed:
            if IS_PG:
                c.execute("INSERT INTO labor_types (name, unit, sort_order) VALUES (%s,%s,%s) ON CONFLICT (name) DO NOTHING",
                          (name, unit, order))
            else:
                c.execute("INSERT OR IGNORE INTO labor_types (name, unit, sort_order) VALUES (?,?,?)",
                          (name, unit, order))
        machinery_seed = [
            ("Máy xúc", "máy", 1), ("Máy trộn bê tông", "máy", 2),
            ("Cần cẩu", "máy", 3), ("Máy đầm", "máy", 4),
            ("Máy bơm nước", "máy", 5), ("Máy cắt", "máy", 6),
            ("Xe tải", "máy", 7), ("Máy hàn", "máy", 8),
        ]
        for name, unit, order in machinery_seed:
            if IS_PG:
                c.execute("INSERT INTO machinery_types (name, unit, sort_order) VALUES (%s,%s,%s) ON CONFLICT (name) DO NOTHING",
                          (name, unit, order))
            else:
                c.execute("INSERT OR IGNORE INTO machinery_types (name, unit, sort_order) VALUES (?,?,?)",
                          (name, unit, order))
        # Migrate legacy 'giờ' unit to 'máy' for existing library entries.
        c.execute("UPDATE machinery_types SET unit='máy' WHERE unit='giờ'")
        import hashlib
        # Only create admin if not exists (never overwrite password)
        existing = c.execute("SELECT id FROM users WHERE username='admin'").fetchone()
        if not existing:
            pw = hashlib.sha256("admin123".encode()).hexdigest()
            if IS_PG:
                c.execute("INSERT INTO users (username, password_hash, full_name, role) VALUES (%s,%s,%s,%s)",
                          ("admin", pw, "Quản trị viên", "admin"))
            else:
                c.execute("INSERT INTO users (username, password_hash, full_name, role) VALUES (?,?,?,?)",
                          ("admin", pw, "Quản trị viên", "admin"))
        c.commit()
    finally:
        c.close()


if __name__ == "__main__":
    init_db()
    print("Database initialized. PG mode:", IS_PG)
