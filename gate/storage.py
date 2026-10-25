import csv
import sqlite3
from dataclasses import asdict

class Recorder:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS sessions (
          id INTEGER PRIMARY KEY, start_utc_ns INTEGER, start_monotonic_ns INTEGER,
          interval_s REAL, timing TEXT);
        CREATE TABLE IF NOT EXISTS samples (
          session_id INTEGER, tick INTEGER, tick_ns INTEGER, instrument TEXT,
          channel TEXT, value REAL, unit TEXT, quality TEXT,
          read_start_ns INTEGER, read_end_ns INTEGER);
        CREATE TABLE IF NOT EXISTS events (
          session_id INTEGER, elapsed_ns INTEGER, instrument TEXT, detail TEXT);
        """)
        self.session = None
    def start(self, utc_ns, mono_ns, interval):
        self.session = self.db.execute("INSERT INTO sessions VALUES(NULL,?,?,?,?)",
            (utc_ns, mono_ns, interval, "Host monotonic scheduling; sequential reads, not hardware synchronized")).lastrowid
        self.db.commit()
    def sample(self, tick, tick_ns, instrument, reading, start, end):
        self.db.execute("INSERT INTO samples VALUES(?,?,?,?,?,?,?,?,?,?)",
            (self.session, tick, tick_ns, instrument, *asdict(reading).values(), start, end))
    def event(self, elapsed, instrument, detail):
        self.db.execute("INSERT INTO events VALUES(?,?,?,?)", (self.session, elapsed, instrument, detail))
    def flush(self):
        self.db.commit()
    def close(self):
        self.db.commit()
        self.db.close()

def export_csv(source, destination):
    with sqlite3.connect(source) as db, open(destination, "w", newline="") as stream:
        rows = db.execute("""SELECT s.*, sessions.start_utc_ns + s.tick_ns AS tick_utc_ns
            FROM samples s JOIN sessions ON s.session_id=sessions.id
            ORDER BY session_id,tick,instrument,channel""")
        writer = csv.writer(stream)
        writer.writerow([c[0] for c in rows.description])
        writer.writerows(rows)
