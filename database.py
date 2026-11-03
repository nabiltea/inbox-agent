"""The agent's memory: a small database recording every email it has handled.

SQLite keeps a whole database in a single file (inbox_agent.db) and comes with
Python, so there's nothing to install. The database lets the agent skip emails
it has already sorted, undo a run, and later feed the web page's daily summary.
"""

import json
import sqlite3
import unicodedata
from datetime import datetime
from pathlib import Path

DB_FILE = Path(__file__).parent / "inbox_agent.db"

# Whether an email needs me, written once for every query that asks. The full read has the last
# word: COALESCE takes its answer, and without a full read (NULL) it takes triage's first look
# instead. f is the email's row in full_reads, which those queries add with a LEFT JOIN.
_NEEDS_ME = "COALESCE(f.needs_me = 'yes', decisions.needs_reply = 1 OR decisions.needs_action = 1)"


def plain_text(text):
    """Text as the email search compares it: capitals and accents don't count, so "cafe" finds "Café".

    SQLite's own LIKE only ignores capitals on plain English letters, and about 1 in 5 of my
    subjects has other characters. NFKD splits "é" into "e" and a separate accent, which gets dropped.
    """
    split = unicodedata.normalize("NFKD", text or "")
    return "".join(character for character in split if not unicodedata.combining(character)).casefold()


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

    # Every change I make to an email's label by hand. It works like a bank account:
    # decisions always holds the label now, the balance, so everything that reads it
    # stays right, and this table is the statement, with one row per change. That also
    # keeps Claude's first answer, which the accuracy tests in Step 8 can compare with mine.
    # action says what I did: "picked" one of my labels, "approved" the suggestion, "edited"
    # it before approving, or "rejected" it, which keeps the email under Unsorted. The next
    # four columns are what undoing one change needs: whether the agent had added the old
    # label, whether this change added the new one (not if the email already had it), whether
    # it took the email out of the inbox, and whether it added the label itself to labels.json.
    # undone_at is when I undid the change. Like a line on a statement, it gets crossed out,
    # not erased, so Step 8 can still see it. ON DELETE CASCADE works as it does for full_reads above.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS label_changes (
            id INTEGER PRIMARY KEY,
            message_id TEXT REFERENCES decisions(message_id) ON DELETE CASCADE,
            changed_at TEXT,
            action TEXT,
            old_label TEXT,
            new_label TEXT,
            old_label_added INTEGER,
            new_label_added INTEGER,
            left_inbox INTEGER,
            label_created INTEGER DEFAULT 0,
            undone_at TEXT
        )
    """)
    # label_created and undone_at came after this table did. The same kind of migration as above.
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(label_changes)")}
    if "label_created" not in columns:
        conn.execute("ALTER TABLE label_changes ADD COLUMN label_created INTEGER DEFAULT 0")
    if "undone_at" not in columns:
        conn.execute("ALTER TABLE label_changes ADD COLUMN undone_at TEXT")
    # SQLite only enforces links between tables (like the one above) when this is
    # switched on, and only for this connection, so it's switched on every time.
    conn.execute("PRAGMA foreign_keys = ON")
    # Lets this connection's SQL call plain_text(), for the email search.
    conn.create_function("plain", 1, plain_text, deterministic=True)
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
    return conn.execute(f"""
        SELECT decisions.*, f.needs_me AS full_read, f.what_to_do, f.kind, f.steps, f.place, f.deadline,
               f.time_sensitive, f.reason AS full_read_reason
        FROM decisions LEFT JOIN full_reads AS f ON f.message_id = decisions.message_id
        WHERE decisions.archived = 0 AND {_NEEDS_ME}
        ORDER BY decisions.received_at DESC
    """).fetchall()


def _matching(search, run_id, label):
    """The conditions behind the email list, and the values for their ? marks. An empty one limits nothing.

    Each word of the search has to be somewhere in the subject or sender, in any order. LIKE treats
    % and _ as wildcards, so a backslash goes in front of any in the word, which ESCAPE makes mean "literally".
    """
    conditions, values = ["1"], []
    for word in search.split():
        conditions.append(r"plain(decisions.subject || ' ' || decisions.sender) LIKE ? ESCAPE '\'")
        values.append("%" + plain_text(word).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
    if run_id:
        conditions.append("decisions.run_id = ?")
        values.append(run_id)
    if label:
        conditions.append("decisions.label = ?")
        values.append(label)
    return " AND ".join(conditions), values


def find_emails(conn, search="", run_id="", label="", limit=50, offset=0):
    """One page of the emails a search and filters find, newest first, with whether each one is flagged and why.

    flagged follows the same rule as flagged_emails(). The message ID settles the order of emails
    with the same date, of which the log has dozens: without it, one could jump between pages.
    """
    where, values = _matching(search, run_id, label)
    return conn.execute(
        f"""
        SELECT decisions.*, decisions.archived = 0 AND {_NEEDS_ME} AS flagged, f.reason AS full_read_reason
        FROM decisions LEFT JOIN full_reads AS f ON f.message_id = decisions.message_id
        WHERE {where}
        ORDER BY decisions.received_at DESC, decisions.message_id
        LIMIT ? OFFSET ?
        """,
        (*values, limit, offset),
    ).fetchall()


def count_emails(conn, search="", run_id="", label=""):
    """How many emails a search and filters find altogether."""
    where, values = _matching(search, run_id, label)
    return conn.execute(f"SELECT COUNT(*) FROM decisions WHERE {where}", values).fetchone()[0]


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


# For the web page's overview and History page. SQL does the adding up: COUNT and SUM total
# the rows inside the database, so only the totals come back to Python, not thousands of rows.

# Each run in numbers, newest first: when it started, how many emails it sorted, archived and
# left in the inbox as needing me, how many have the label Unsorted now, how many I've changed
# by hand, and what it cost, with its full reads. backlog.py starts its runs' names with "backlog-".
#
# Needing me follows the same rule as flagged_emails(): it has to be still in the inbox.
# EXISTS counts an email once, however many changes it has.
_RUN_NUMBERS = f"""
    SELECT decisions.run_id, decisions.run_id LIKE 'backlog-%' AS from_backlog,
           MIN(decisions.processed_at) AS started, COUNT(*) AS emails, SUM(decisions.archived) AS archived,
           SUM(decisions.archived = 0 AND {_NEEDS_ME}) AS flagged,
           SUM(decisions.label = 'Unsorted') AS unsorted,
           SUM(EXISTS (SELECT 1 FROM label_changes AS c WHERE c.message_id = decisions.message_id AND c.undone_at IS NULL)) AS changed_by_hand,
           COALESCE(SUM(decisions.cost), 0) + COALESCE(SUM(f.cost), 0) AS cost
    FROM decisions LEFT JOIN full_reads AS f ON f.message_id = decisions.message_id
    GROUP BY decisions.run_id ORDER BY started DESC
"""


def run_summaries(conn):
    """Every run in numbers, newest first (see _RUN_NUMBERS)."""
    return conn.execute(_RUN_NUMBERS).fetchall()


def last_run_summary(conn):
    """The most recent run in numbers (see _RUN_NUMBERS). None if there are no runs yet."""
    return conn.execute(f"{_RUN_NUMBERS} LIMIT 1").fetchone()


def run_label_counts(conn):
    """How many of each run's emails have each label now, biggest first: {run ID: rows of label and emails}.

    decisions holds the label each email has now, so my hand changes count. That's what Gmail
    shows, and what undoing the run would reverse. Claude's first answer stays in label_changes.
    """
    rows = conn.execute("SELECT run_id, label, COUNT(*) AS emails FROM decisions GROUP BY run_id, label ORDER BY emails DESC, label")
    counts = {}
    for row in rows:
        counts.setdefault(row["run_id"], []).append(row)
    return counts


def run_started(conn, run_id):
    """When a run started, or None if the log has no such run, for example because it has been undone."""
    return conn.execute("SELECT MIN(processed_at) FROM decisions WHERE run_id = ?", (run_id,)).fetchone()[0]


# Which emails wait for me to decide about their label: those that fit no label, which triage
# labels "Unsorted" (UNSORTED in triage.py), unless I've said none is needed and haven't undone
# that. Whether I have is only kept in label_changes, not in a second place in decisions, so the
# two can't disagree.
_WAITING_FOR_LABEL = """
    decisions.label = 'Unsorted' AND NOT EXISTS (
        SELECT 1 FROM label_changes AS c
        WHERE c.message_id = decisions.message_id AND c.action = 'rejected' AND c.undone_at IS NULL
    )
"""


def unsorted_count(conn):
    """How many emails are waiting for me to decide about their label."""
    return conn.execute(f"SELECT COUNT(*) FROM decisions WHERE {_WAITING_FOR_LABEL}").fetchone()[0]


# The emails waiting for a label, each with whether it needs me (needs_me).
_UNSORTED_EMAILS = f"""
    SELECT decisions.*, {_NEEDS_ME} AS needs_me
    FROM decisions LEFT JOIN full_reads AS f ON f.message_id = decisions.message_id
    WHERE {_WAITING_FOR_LABEL}
"""


def unsorted_emails(conn):
    """The emails waiting for a label, newest first, each with whether it needs me."""
    return conn.execute(f"{_UNSORTED_EMAILS} ORDER BY decisions.received_at DESC").fetchall()


def unsorted_email(conn, message_id):
    """One email waiting for a label, with whether it needs me. None if it isn't waiting, or the log doesn't know it."""
    return conn.execute(f"{_UNSORTED_EMAILS} AND decisions.message_id = ?", (message_id,)).fetchone()


def save_label_change(conn, message_id, action, label, label_added, left_inbox, label_created=False):
    """Record that I changed an email's label: a new row in label_changes, and the new label in decisions.

    label_added is False when the email already had that label. left_inbox is True when this
    change took the email out of the inbox, which also counts as archived, so undoing its run
    brings it back. label_created is True when this change added the label to labels.json.
    One commit saves both (a "transaction"), so a crash can't save only one. Rejecting a
    suggestion goes the same way, with the label staying the same.
    """
    old = conn.execute("SELECT label, label_added FROM decisions WHERE message_id = ?", (message_id,)).fetchone()
    conn.execute(
        """
        INSERT INTO label_changes (
            message_id, changed_at, action, old_label, new_label, old_label_added, new_label_added, left_inbox, label_created
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            message_id, datetime.now().isoformat(timespec="seconds"), action,
            old["label"], label, old["label_added"], label_added, left_inbox, label_created,
        ),
    )
    conn.execute(
        "UPDATE decisions SET label = ?, label_added = ?, archived = archived OR ? WHERE message_id = ?",
        (label, label_added, left_inbox, message_id),
    )
    conn.commit()


# The changes I've made by hand and haven't undone, with which email each one is about.
_MY_CHANGES = """
    SELECT c.*, decisions.subject, decisions.sender, decisions.thread_id
    FROM label_changes AS c JOIN decisions ON decisions.message_id = c.message_id
    WHERE c.undone_at IS NULL
"""


def my_changes(conn):
    """The changes I've made by hand and haven't undone, newest first. The ID settles changes in the same second."""
    return conn.execute(f"{_MY_CHANGES} ORDER BY c.changed_at DESC, c.id DESC").fetchall()


def my_change(conn, change_id):
    """One change I've made by hand. None if I've undone it, or the log doesn't know it."""
    return conn.execute(f"{_MY_CHANGES} AND c.id = ?", (change_id,)).fetchone()


def save_undone_change(conn, change):
    """Record that I undid a change: the email gets its old label back, and the change's row gets crossed out.

    A change that took the email out of the inbox found it there, so the email is back to not
    being archived. One commit saves both, so a crash can't save only one.
    """
    conn.execute(
        "UPDATE label_changes SET undone_at = ? WHERE id = ?",
        (datetime.now().isoformat(timespec="seconds"), change["id"]),
    )
    conn.execute(
        "UPDATE decisions SET label = ?, label_added = ?, archived = archived AND NOT ? WHERE message_id = ?",
        (change["old_label"], change["old_label_added"], change["left_inbox"], change["message_id"]),
    )
    conn.commit()


def emails_with_label(conn, label):
    """How many emails in the log have this label now."""
    return conn.execute("SELECT COUNT(*) FROM decisions WHERE label = ?", (label,)).fetchone()[0]


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
