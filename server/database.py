import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data.db"

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS zones (
            id   TEXT PRIMARY KEY,
            name TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS maintenance (
            id           TEXT PRIMARY KEY,
            zone_id      TEXT NOT NULL,
            person_name  TEXT NOT NULL,
            content      TEXT NOT NULL,
            before_photo TEXT,
            after_photo  TEXT,
            created_at   TEXT NOT NULL,
            FOREIGN KEY (zone_id) REFERENCES zones(id)
        );

        CREATE TABLE IF NOT EXISTS thanks (
            id             TEXT PRIMARY KEY,
            maintenance_id TEXT NOT NULL,
            created_at     TEXT NOT NULL,
            source         TEXT NOT NULL DEFAULT 'user',
            FOREIGN KEY (maintenance_id) REFERENCES maintenance(id)
        );

        CREATE TABLE IF NOT EXISTS push_subscriptions (
            id           TEXT PRIMARY KEY,
            person_name  TEXT NOT NULL,
            subscription TEXT NOT NULL
        );
    """)
    conn.commit()
    conn.close()
