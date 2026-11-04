"""Database layer — SQLite (dev) / PostgreSQL (prod)."""
import os
import sqlite3
from pathlib import Path

DB_PATH = Path(os.getenv("DB_PATH", "nhatky.db"))


def connect():
    c = sqlite3.connect(str(DB_PATH))
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def init_db():
    c = connect()
    try:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            full_name TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'viewer',
            phone TEXT,
            email TEXT,
            telegram_chat_id TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            address TEXT,
            investor TEXT,
            contractor TEXT,
            supervisor TEXT,
            start_date TEXT,
            end_date TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS user_projects (
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
            PRIMARY KEY (user_id, project_id)
        );

        CREATE TABLE IF NOT EXISTS labor_types (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            unit TEXT DEFAULT 'người',
            sort_order INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS machinery_types (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            unit TEXT DEFAULT 'giờ',
            sort_order INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS diaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            UNIQUE(project_id, date)
        );

        CREATE TABLE IF NOT EXISTS diary_labor (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            diary_id INTEGER NOT NULL REFERENCES diaries(id) ON DELETE CASCADE,
            labor_type_id INTEGER NOT NULL REFERENCES labor_types(id),
            count INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS diary_machinery (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            diary_id INTEGER NOT NULL REFERENCES diaries(id) ON DELETE CASCADE,
            machinery_type_id INTEGER NOT NULL REFERENCES machinery_types(id),
            hours REAL NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS diary_materials (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            diary_id INTEGER NOT NULL REFERENCES diaries(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            unit TEXT,
            quantity REAL NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS diary_photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            diary_id INTEGER NOT NULL REFERENCES diaries(id) ON DELETE CASCADE,
            filename TEXT NOT NULL,
            caption TEXT,
            is_previous INTEGER DEFAULT 0,
            uploaded_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS diary_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            diary_id INTEGER NOT NULL REFERENCES diaries(id) ON DELETE CASCADE,
            task_name TEXT NOT NULL,
            quantity REAL,
            unit TEXT,
            cumulative_pct REAL
        );

        CREATE TABLE IF NOT EXISTS recipients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            email TEXT,
            phone TEXT,
            telegram_chat_id TEXT,
            role TEXT,
            frequency TEXT DEFAULT 'daily',
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        """)
        # Seed labor types
        labor_seed = [
            ("Kỹ sư", "người", 1), ("Công nhân xây dựng", "người", 2),
            ("Thợ xây", "người", 3), ("Thợ điện", "người", 4),
            ("Thợ nước", "người", 5), ("Thợ sắt", "người", 6),
            ("Thợ mộc", "người", 7), ("Thợ sơn", "người", 8),
            ("Thợ lát gạch", "người", 9), ("Phụ việc", "người", 10),
        ]
        for name, unit, order in labor_seed:
            c.execute("INSERT OR IGNORE INTO labor_types (name, unit, sort_order) VALUES (?,?,?)",
                      (name, unit, order))
        machinery_seed = [
            ("Máy xúc", "giờ", 1), ("Máy trộn bê tông", "giờ", 2),
            ("Cần cẩu", "giờ", 3), ("Máy đầm", "giờ", 4),
            ("Máy bơm nước", "giờ", 5), ("Máy cắt", "giờ", 6),
            ("Xe tải", "giờ", 7), ("Máy hàn", "giờ", 8),
        ]
        for name, unit, order in machinery_seed:
            c.execute("INSERT OR IGNORE INTO machinery_types (name, unit, sort_order) VALUES (?,?,?)",
                      (name, unit, order))
        # Default admin user (password: admin123)
        import hashlib
        pw = hashlib.sha256("admin123".encode()).hexdigest()
        c.execute("INSERT OR IGNORE INTO users (username, password_hash, full_name, role) VALUES (?,?,?,?)",
                  ("admin", pw, "Quản trị viên", "admin"))
        c.commit()
    finally:
        c.close()


if __name__ == "__main__":
    init_db()
    print("Database initialized:", DB_PATH)
