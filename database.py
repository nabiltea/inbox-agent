"""The agent's memory: a small database recording every email it has handled.

SQLite keeps a whole database in a single file (inbox_agent.db) and comes with
Python, so there's nothing to install. The database lets the agent skip emails
it has already sorted, undo a run, and later feed the web page's daily summary.
"""

import sqlite3
from datetime import datetime
from pathlib import Path

DB_FILE = Path(__file__).parent / "inbox_agent.db"


def connect():
    """Open the database, creating the file and its table the first time."""
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row  # lets us read a column by name, like row["subject"]
    # One row per email. "IF NOT EXISTS" makes this safe to run every time.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS decisions (
            message_id TEXT PRIMARY KEY,
            thread_id TEXT,
            run_id TEXT,
            processed_at TEXT,
            received_at TEXT,
            sender TEXT,
            subject TEXT,
            label TEXT,
            label_added INTEGER,
            archived INTEGER,
            needs_reply INTEGER,
            needs_action INTEGER,
            action TEXT,
            suggested_label TEXT,
            suggested_description TEXT,
            reason TEXT,
            model TEXT,
            cost REAL
        )
    """)
    # A database file made by an older version of this code has no "archived"
    # column, and CREATE TABLE IF NOT EXISTS won't change a table that exists.
    # So add the column if it's missing. Updating an existing database's structure
    # like this is called a "migration".
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(decisions)")}
    if "archived" not in columns:
        conn.execute("ALTER TABLE decisions ADD COLUMN archived INTEGER DEFAULT 0")
    return conn


def is_processed(conn, message_id):
    """True if the agent has already sorted this email."""
    row = conn.execute("SELECT 1 FROM decisions WHERE message_id = ?", (message_id,)).fetchone()
    return row is not None


def save_decision(conn, run_id, email, result, label_added, archived, model, cost):
    """Record what the agent decided about one email.

    label_added is False when the email already had that label before this run,
    for example from one of my own Gmail filters. Undo leaves those alone.
    archived is True when the agent moved the email out of the inbox.
    """
    # The ? marks are filled in by SQLite with the values below. Never paste values
    # into the SQL text yourself: a subject containing a quote mark would break the
    # query, and a crafted one could change what it does ("SQL injection").
    conn.execute(
        """
        INSERT INTO decisions (
            message_id, thread_id, run_id, processed_at, received_at, sender, subject,
            label, label_added, archived, needs_reply, needs_action, action,
            suggested_label, suggested_description, reason, model, cost
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            email.id, email.thread_id, run_id, datetime.now().isoformat(timespec="seconds"),
            email.date.isoformat(), email.sender, email.subject,
            result.label, label_added, archived, result.needs_reply, result.needs_action, result.action,
            result.suggested_label, result.suggested_description, result.reason, model, cost,
        ),
    )
    conn.commit()  # save to the file now, so a crash halfway through a run loses nothing


def recent_todos(conn, since):
    """Emails received since the given date that were flagged as needing a reply or an action, newest first."""
    return conn.execute("""
        SELECT * FROM decisions
        WHERE (needs_reply = 1 OR needs_action = 1) AND received_at >= ?
        ORDER BY received_at DESC
    """, (since.isoformat(),)).fetchall()


def open_todos(conn):
    """Emails flagged as needing a reply or an action that the agent left in the inbox, newest first.

    Unlike recent_todos, there's no time limit: a flagged email that's still in the inbox
    is still a to-do, however old it is. This only knows what the agent did, so an email
    I've archived myself since is still included.
    """
    return conn.execute("""
        SELECT * FROM decisions
        WHERE (needs_reply = 1 OR needs_action = 1) AND archived = 0
        ORDER BY received_at DESC
    """).fetchall()


def list_runs(conn):
    """Every run, newest first, with how many emails it sorted."""
    return conn.execute("""
        SELECT run_id, COUNT(*) AS emails, MIN(processed_at) AS started
        FROM decisions GROUP BY run_id ORDER BY started DESC
    """).fetchall()


def get_run(conn, run_id):
    """Return all decisions from one run."""
    return conn.execute("SELECT * FROM decisions WHERE run_id = ?", (run_id,)).fetchall()


def last_run(conn):
    """Return all decisions from the most recent run, or an empty list if there are none."""
    runs = list_runs(conn)
    return get_run(conn, runs[0]["run_id"]) if runs else []


def delete_run(conn, run_id):
    """Forget every decision from one run, so those emails get sorted again next time."""
    conn.execute("DELETE FROM decisions WHERE run_id = ?", (run_id,))
    conn.commit()
