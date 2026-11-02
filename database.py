"""The agent's memory: a small database recording every email it has handled.

SQLite keeps a whole database in a single file (inbox_agent.db) and comes with
Python, so there's nothing to install. The database lets the agent skip emails
it has already sorted, undo a run, and later feed the web page's daily summary.
"""

import json
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

    # What full_read.py found, for the emails that got a full read. Most never do (the
    # routine labels, and every email sorted before the full read existed), and a full
    # read is an event of its own, with its own time, model and cost. So by the rules of
    # database design ("normalization") it gets a table of its own, rather than mostly
    # empty columns in decisions. message_id links each full read to its email's row,
    # and ON DELETE CASCADE removes it when that row goes, for example with undo.
    # The steps are a list stored as JSON: a deliberate exception to "one value per
    # cell", because they're always saved, read and shown together, in order.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS full_reads (
            message_id TEXT PRIMARY KEY REFERENCES decisions(message_id) ON DELETE CASCADE,
            read_at TEXT,
            needs_me TEXT,
            kind TEXT,
            steps TEXT,
            place TEXT,
            deadline TEXT,
            time_sensitive INTEGER,
            reason TEXT,
            model TEXT,
            cost REAL,
            what_to_do TEXT
        )
    """)
    # The one-line what_to_do came after this table did, so a database file made earlier
    # doesn't have it. The same kind of migration as "archived" above.
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(full_reads)")}
    if "what_to_do" not in columns:
        conn.execute("ALTER TABLE full_reads ADD COLUMN what_to_do TEXT")
    # SQLite only enforces links between tables (like the one above) when this is
    # switched on, and only for this connection, so it's switched on every time.
    conn.execute("PRAGMA foreign_keys = ON")
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


def save_full_read(conn, message_id, result, model, cost):
    """Record what the full read found for one email (see full_read.py).

    The email must already have its row in decisions: the link between the tables
    refuses a full read for an email the log doesn't know. Reading an email again
    replaces its earlier full read.
    """
    conn.execute(
        """
        INSERT OR REPLACE INTO full_reads (
            message_id, read_at, needs_me, what_to_do, kind, steps, place, deadline, time_sensitive, reason, model, cost
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            message_id, datetime.now().isoformat(timespec="seconds"), result.needs_me, result.what_to_do, result.kind,
            json.dumps(result.steps, ensure_ascii=False), result.where,
            result.deadline.isoformat() if result.deadline else None, result.time_sensitive, result.reason, model, cost,
        ),
    )
    conn.commit()


def flagged_emails(conn):
    """The emails that need me and that the agent left in the inbox, newest first, with their full read if any.

    An email needs me if its full read says yes or, when it had no full read, if
    triage's first look flagged it: the full read has the last word. There's no time
    limit: an email that needs me is still a to-do however old it is. This only knows
    what the agent did, so an email I've archived myself since is still included.
    todos.py leaves those out.

    LEFT JOIN adds each email's full read from full_reads, and leaves those columns
    empty (NULL) for an email that has none.
    """
    return conn.execute("""
        SELECT decisions.*, f.needs_me AS full_read, f.what_to_do, f.kind, f.steps, f.place, f.deadline,
               f.time_sensitive, f.reason AS full_read_reason
        FROM decisions LEFT JOIN full_reads AS f ON f.message_id = decisions.message_id
        WHERE decisions.archived = 0
          AND (f.needs_me = 'yes' OR (f.needs_me IS NULL AND (decisions.needs_reply = 1 OR decisions.needs_action = 1)))
        ORDER BY decisions.received_at DESC
    """).fetchall()


def newest_emails(conn, count, except_labels=()):
    """The newest emails the agent has sorted, leaving out the given labels."""
    # One ? per label to leave out: the labels themselves still go in as values, never as SQL.
    skip = f"WHERE label NOT IN ({', '.join('?' * len(except_labels))})" if except_labels else ""
    return conn.execute(
        f"SELECT * FROM decisions {skip} ORDER BY received_at DESC LIMIT ?", (*except_labels, count)
    ).fetchall()


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


# For the web page's overview. SQL does the adding up: COUNT and SUM total the rows
# inside the database, so only the totals come back to Python, not thousands of rows.

def last_run_summary(conn):
    """The most recent run in numbers: when it started, and how many emails it sorted,
    archived and left in the inbox as needing me. None if there are no runs yet.

    The same rule as flagged_emails(): it has to be still in the inbox, and the full read
    has the last word. COALESCE takes its answer, and without a full read (NULL) it takes
    triage's first look instead.
    """
    return conn.execute("""
        SELECT MIN(decisions.processed_at) AS started, COUNT(*) AS emails, SUM(decisions.archived) AS archived,
               SUM(decisions.archived = 0 AND COALESCE(f.needs_me = 'yes', decisions.needs_reply = 1 OR decisions.needs_action = 1)) AS flagged
        FROM decisions LEFT JOIN full_reads AS f ON f.message_id = decisions.message_id
        GROUP BY decisions.run_id ORDER BY started DESC LIMIT 1
    """).fetchone()


def unsorted_count(conn):
    """How many emails fit no label. Triage labels those "Unsorted" (UNSORTED in triage.py)."""
    return conn.execute("SELECT COUNT(*) FROM decisions WHERE label = 'Unsorted'").fetchone()[0]


def sorting_costs(conn, since):
    """What sorting has cost in dollars, and for how many emails: since the given date, and in total.

    COALESCE turns "no rows, so no sum" into 0.
    """
    recent = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(cost), 0) FROM decisions WHERE processed_at >= ?", (since.isoformat(),)
    ).fetchone()
    total = conn.execute("SELECT COUNT(*), COALESCE(SUM(cost), 0) FROM decisions").fetchone()
    return {"emails_since": recent[0], "cost_since": recent[1], "emails": total[0], "cost": total[1]}


def full_read_costs(conn, since):
    """What full reads have cost in dollars, and how many there were: since the given date, and in total.

    Like sorting_costs(), it only knows what's in the log: reading an email again replaces
    its earlier cost, and undoing a run removes its costs.
    """
    recent = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(cost), 0) FROM full_reads WHERE read_at >= ?", (since.isoformat(),)
    ).fetchone()
    total = conn.execute("SELECT COUNT(*), COALESCE(SUM(cost), 0) FROM full_reads").fetchone()
    return {"reads_since": recent[0], "cost_since": recent[1], "reads": total[0], "cost": total[1]}


def delete_run(conn, run_id):
    """Forget every decision from one run, so those emails get sorted again next time."""
    conn.execute("DELETE FROM decisions WHERE run_id = ?", (run_id,))
    conn.commit()
