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


def _table_columns(c, table):
    """Return the set of column names for a table (SQLite pragma only)."""
    if IS_PG:
        return set()
    try:
        return {r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
    except Exception:
        return set()


def _migrate_reminder_columns(c):
    """Additive migration for the reminder feature on pre-existing DBs.

    New columns and tables are added only when missing; nothing is dropped or
    renamed, so old databases keep working after an upgrade.
    """
    if not IS_PG:
        cols = _table_columns(c, "projects")
        if "reminder_enabled" not in cols:
            c.execute("ALTER TABLE projects ADD COLUMN reminder_enabled INTEGER DEFAULT 1")
        if "reminder_time" not in cols:
            c.execute("ALTER TABLE projects ADD COLUMN reminder_time TEXT DEFAULT '08:00'")
        c.executescript("""
            CREATE TABLE IF NOT EXISTS project_reminders (
                project_id INTEGER PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
                enabled INTEGER DEFAULT 1,
                time TEXT DEFAULT '08:00'
            );
            CREATE TABLE IF NOT EXISTS reminder_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                log_date TEXT NOT NULL,
                sent_at TEXT DEFAULT (datetime('now')),
                UNIQUE(project_id, log_date)
            );
        """)
    else:
        # PostgreSQL: CREATE TABLE IF NOT EXISTS (run earlier in init_db) is
        # idempotent for the new tables, but it does NOT add columns to an
        # existing table.  Use ADD COLUMN IF NOT EXISTS (PG >= 9.6) so a
        # duplicate column does not abort the surrounding transaction.
        c.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS reminder_enabled INTEGER DEFAULT 1")
        c.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS reminder_time TEXT DEFAULT '08:00'")


def _migrate_weather(c):
    """Add project coordinates and one cached weather row per session/day."""
    if not IS_PG:
        cols = _table_columns(c, "projects")
        if "lat" not in cols:
            c.execute("ALTER TABLE projects ADD COLUMN lat REAL DEFAULT 21.0")
        if "lon" not in cols:
            c.execute("ALTER TABLE projects ADD COLUMN lon REAL DEFAULT 105.8")
        c.execute("UPDATE projects SET lat=21.0 WHERE lat IS NULL")
        c.execute("UPDATE projects SET lon=105.8 WHERE lon IS NULL")
        c.executescript("""
            CREATE TABLE IF NOT EXISTS project_weather_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                log_date TEXT NOT NULL,
                session TEXT NOT NULL,
                temp REAL,
                description TEXT,
                icon TEXT,
                fetched_at TEXT DEFAULT (datetime('now')),
                UNIQUE(project_id, log_date, session)
            );
        """)
    else:
        c.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS lat REAL DEFAULT 21.0")
        c.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS lon REAL DEFAULT 105.8")
        c.execute("UPDATE projects SET lat=21.0 WHERE lat IS NULL")
        c.execute("UPDATE projects SET lon=105.8 WHERE lon IS NULL")
        c.executescript("""
            CREATE TABLE IF NOT EXISTS project_weather_log (
                id SERIAL PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                log_date TEXT NOT NULL, session TEXT NOT NULL, temp REAL,
                description TEXT, icon TEXT, fetched_at TIMESTAMP DEFAULT NOW(),
                UNIQUE(project_id, log_date, session)
            );
        """)


def _migrate_library_columns(c):
    """Additive migration: soft-disable flag for labor/machinery library."""
    if not IS_PG:
        for table in ("labor_types", "machinery_types"):
            cols = _table_columns(c, table)
            if "is_active" not in cols:
                c.execute(f"ALTER TABLE {table} ADD COLUMN is_active INTEGER DEFAULT 1")
        c.execute("UPDATE labor_types SET is_active=1 WHERE is_active IS NULL")
        c.execute("UPDATE machinery_types SET is_active=1 WHERE is_active IS NULL")
    else:
        c.execute("ALTER TABLE labor_types ADD COLUMN IF NOT EXISTS is_active INTEGER DEFAULT 1")
        c.execute("ALTER TABLE machinery_types ADD COLUMN IF NOT EXISTS is_active INTEGER DEFAULT 1")
        c.execute("UPDATE labor_types SET is_active=1 WHERE is_active IS NULL")
        c.execute("UPDATE machinery_types SET is_active=1 WHERE is_active IS NULL")


def _migrate_recipient_columns(c):
    """Additive migration: telegram_type for recipients (account/group)."""
    if not IS_PG:
        cols = _table_columns(c, "recipients")
        if "telegram_type" not in cols:
            c.execute("ALTER TABLE recipients ADD COLUMN telegram_type TEXT DEFAULT 'account'")
        c.execute("UPDATE recipients SET telegram_type='account' WHERE telegram_type IS NULL OR telegram_type=''")
    else:
        c.execute("ALTER TABLE recipients ADD COLUMN IF NOT EXISTS telegram_type TEXT DEFAULT 'account'")
        c.execute("UPDATE recipients SET telegram_type='account' WHERE telegram_type IS NULL OR telegram_type=''")


def _migrate_photo_size_column(c):
    """Additive migration: size_bytes on diary_photos.

    New databases get the column from the CREATE TABLE above; pre-existing
    databases need an explicit ALTER so the quota ledger can read real stored
    sizes instead of guessing.
    """
    if not IS_PG:
        cols = _table_columns(c, "diary_photos")
        if "size_bytes" not in cols:
            c.execute("ALTER TABLE diary_photos ADD COLUMN size_bytes INTEGER")
    else:
        c.execute("ALTER TABLE diary_photos ADD COLUMN IF NOT EXISTS size_bytes INTEGER")


def _migrate_issue_columns(c):
    """Additive migration: issue-tracking fields on diaries.

    ``issue_status`` is the legacy recommendation/solution text and is
    intentionally preserved.  New columns hold the richer cause / related
    party / workflow / resolved-at / notification-marker state.
    """
    if IS_PG:
        for col, sql in [
            ("cause", "ALTER TABLE diaries ADD COLUMN IF NOT EXISTS cause TEXT DEFAULT ''"),
            ("related_party", "ALTER TABLE diaries ADD COLUMN IF NOT EXISTS related_party TEXT DEFAULT ''"),
            ("workflow_status", "ALTER TABLE diaries ADD COLUMN IF NOT EXISTS workflow_status TEXT DEFAULT 'none'"),
            ("resolved_at", "ALTER TABLE diaries ADD COLUMN IF NOT EXISTS resolved_at TEXT"),
            ("issue_notified_at", "ALTER TABLE diaries ADD COLUMN IF NOT EXISTS issue_notified_at TEXT"),
        ]:
            c.execute(sql)
    else:
        cols = _table_columns(c, "diaries")
        adds = []
        if "cause" not in cols:
            adds.append("ALTER TABLE diaries ADD COLUMN cause TEXT DEFAULT ''")
        if "related_party" not in cols:
            adds.append("ALTER TABLE diaries ADD COLUMN related_party TEXT DEFAULT ''")
        if "workflow_status" not in cols:
            adds.append("ALTER TABLE diaries ADD COLUMN workflow_status TEXT DEFAULT 'none'")
        if "resolved_at" not in cols:
            adds.append("ALTER TABLE diaries ADD COLUMN resolved_at TEXT")
        if "issue_notified_at" not in cols:
            adds.append("ALTER TABLE diaries ADD COLUMN issue_notified_at TEXT")
        for sql in adds:
            c.execute(sql)
    if not IS_PG:
        c.execute("UPDATE diaries SET workflow_status='none' "
                  "WHERE workflow_status IS NULL OR workflow_status=''")
    else:
        c.execute("UPDATE diaries SET workflow_status='none' "
                  "WHERE workflow_status IS NULL OR workflow_status=''")


def _migrate_photo_kind_column(c):
    """Additive migration: photo_kind on diary_photos (work/site/issue)."""
    if not IS_PG:
        cols = _table_columns(c, "diary_photos")
        if "photo_kind" not in cols:
            c.execute("ALTER TABLE diary_photos ADD COLUMN photo_kind TEXT DEFAULT 'work'")
        c.execute("UPDATE diary_photos SET photo_kind='work' "
                  "WHERE photo_kind IS NULL OR photo_kind=''")
    else:
        c.execute("ALTER TABLE diary_photos ADD COLUMN IF NOT EXISTS photo_kind TEXT DEFAULT 'work'")
        c.execute("UPDATE diary_photos SET photo_kind='work' "
                  "WHERE photo_kind IS NULL OR photo_kind=''")


def _migrate_diary_issues_table(c):
    """Additive migration: child table diary_issues + photo link column.

    New writes go to the child table (one-to-many).  Legacy single-issue
    columns on ``diaries`` are kept for backward compatibility / read-only.
    Existing single-issue rows are migrated into the child table exactly once
    (guarded by a settings flag) so old diaries display correctly through the
    same read path.
    """
    if not IS_PG:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS diary_issues (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                diary_id INTEGER NOT NULL REFERENCES diaries(id) ON DELETE CASCADE,
                description TEXT,
                cause TEXT,
                related_party TEXT,
                suggestion TEXT,
                level TEXT DEFAULT 'none',
                workflow_status TEXT DEFAULT 'pending',
                resolved_at TEXT,
                notified_at TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );
        """)
        cols = _table_columns(c, "diary_photos")
        if "issue_id" not in cols:
            c.execute("ALTER TABLE diary_photos ADD COLUMN issue_id INTEGER "
                      "REFERENCES diary_issues(id) ON DELETE CASCADE")
    else:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS diary_issues (
                id SERIAL PRIMARY KEY,
                diary_id INTEGER NOT NULL REFERENCES diaries(id) ON DELETE CASCADE,
                description TEXT,
                cause TEXT,
                related_party TEXT,
                suggestion TEXT,
                level TEXT DEFAULT 'none',
                workflow_status TEXT DEFAULT 'pending',
                resolved_at TEXT,
                notified_at TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            );
        """)
        c.execute("ALTER TABLE diary_photos ADD COLUMN IF NOT EXISTS issue_id INTEGER "
                  "REFERENCES diary_issues(id) ON DELETE CASCADE")

    # Migrate legacy single-issue rows into the child table exactly once.
    row = c.execute("SELECT value FROM settings WHERE key='diary_issues_migrated'").fetchone()
    if not row or row["value"] != "1":
        migrated = c.execute(
            """SELECT id, issues, issue_level, issue_status, cause, related_party,
                     workflow_status, resolved_at, issue_notified_at
               FROM diaries WHERE issues IS NOT NULL AND issues != ''""").fetchall()
        for d in migrated:
            existing = c.execute(
                "SELECT COUNT(*) AS n FROM diary_issues WHERE diary_id=?",
                (d["id"],)).fetchone()["n"]
            if existing:
                continue
            c.execute(
                """INSERT INTO diary_issues (diary_id, description, cause, related_party,
                       suggestion, level, workflow_status, resolved_at, notified_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (d["id"], d["issues"], d["cause"] or "", d["related_party"] or "",
                 d["issue_status"] or "", d["issue_level"] or "none",
                 d["workflow_status"] or "pending", d["resolved_at"],
                 d["issue_notified_at"]))
        c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?,?)",
                  ("diary_issues_migrated", "1"))


_connecting = False


def connect():
    if IS_PG:
        import psycopg2
        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = False
        return _PgConnection(conn)
    else:
        import sqlite3
        global _connecting
        if not DB_PATH.exists() and not _connecting:
            # Auto-init a missing database so admin-scoped tests that seed a
            # user against whatever DB_PATH happens to be set do not 404.
            _connecting = True
            try:
                init_db()
            finally:
                _connecting = False
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
            reminder_enabled INTEGER DEFAULT 1,
            reminder_time TEXT DEFAULT '08:00',
            lat REAL DEFAULT 21.0,
            lon REAL DEFAULT 105.8,
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
            sort_order INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS machinery_types (
            id SERIAL PRIMARY KEY,
            name TEXT UNIQUE NOT NULL,
            unit TEXT DEFAULT 'máy',
            sort_order INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1
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
            cause TEXT DEFAULT '',
            related_party TEXT DEFAULT '',
            workflow_status TEXT DEFAULT 'none',
            resolved_at TEXT,
            issue_notified_at TEXT,
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
            uploaded_at TIMESTAMP DEFAULT NOW(),
            size_bytes INTEGER,
            photo_kind TEXT DEFAULT 'work',
            issue_id INTEGER REFERENCES diary_issues(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS diary_issues (
            id SERIAL PRIMARY KEY,
            diary_id INTEGER NOT NULL REFERENCES diaries(id) ON DELETE CASCADE,
            description TEXT,
            cause TEXT,
            related_party TEXT,
            suggestion TEXT,
            level TEXT DEFAULT 'none',
            workflow_status TEXT DEFAULT 'pending',
            resolved_at TEXT,
            notified_at TEXT,
            created_at TIMESTAMP DEFAULT NOW()
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
            telegram_type TEXT DEFAULT 'account',
            role TEXT,
            frequency TEXT DEFAULT 'daily',
            created_at TIMESTAMP DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        -- Reminder: per-project nag to create today's diary.
        CREATE TABLE IF NOT EXISTS project_reminders (
            project_id INTEGER PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
            enabled INTEGER DEFAULT 1,
            time TEXT DEFAULT '08:00'
        );
        CREATE TABLE IF NOT EXISTS reminder_log (
            id SERIAL PRIMARY KEY,
            project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            log_date TEXT NOT NULL,
            sent_at TIMESTAMP DEFAULT NOW(),
            UNIQUE(project_id, log_date)
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
        _migrate_library_columns(c)
        _migrate_recipient_columns(c)
        # Additive migrations for existing databases (backward compatible).
        _migrate_photo_size_column(c)
        _migrate_reminder_columns(c)
        _migrate_weather(c)
        _migrate_issue_columns(c)
        _migrate_photo_kind_column(c)
        _migrate_diary_issues_table(c)
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
