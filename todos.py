"""What counts as a to-do, in one place.

The web page uses it, and so does the MCP server (Claude Desktop), so both always show
the same list. list_todos() is given two lists and works out the to-dos from them: the
flagged emails from the agent's log, and the emails in my inbox from Gmail. It never
opens the database or talks to Gmail itself. Whoever uses it fetches the two lists first
and deals with any problem in its own way: the page shows a notice, the MCP server tells
Claude. That also makes it easy to test with made-up lists.

It also decides how pressing each to-do is. That's code, not the model: the full read
only finds the deadline. Urgency isn't saved in the log, because it changes by itself as
the days pass, so it's worked out from the deadline and today's date every time.

And it shows each task once. Gmail keeps four reminders for one contract as four separate
conversations, but they're one thing for me to do (see _same_task).
"""

import json
from dataclasses import dataclass
from datetime import date, datetime

# Due today, tomorrow or the day after counts as urgent.
URGENT_WITHIN_DAYS = 2

# The order of the list. Overdue sits below what I can still do with a clock on it, but
# above the rest, so a missed deadline stays in view.
ORDER = ("urgent", "time-sensitive", "overdue", "normal")

# The kinds of full read (KINDS in full_read.py) where someone is waiting for an answer.
# Every other kind counts as an action.
REPLY_KINDS = ("reply by email", "reply on another site")

# Kinds that never share a row (see _same_task). "other" could be anything, and each
# conversation of a reply by email needs a reply of its own.
NEVER_MERGED = ("other", "reply by email")


@dataclass
class OtherConversation:
    """Another conversation about the same task, so I can reach it from the task's row."""

    thread_id: str
    what_to_do: str
    received: datetime


@dataclass
class Todo:
    """One task that needs me, described by its newest flagged email still in the inbox."""

    thread_id: str
    message_id: str
    needs_reply: bool
    needs_action: bool
    what_to_do: str  # the full read's line, what triage said to do, or "Reply to [sender]"
    sender: str
    subject: str
    received: datetime
    label: str
    more: int  # how many other flagged emails about this task are still in the inbox
    # What the full read found: empty for an email that hasn't had one.
    kind: str | None
    steps: list[str]
    place: str | None  # plain text, like "Dropbox Sign (dropbox.com)". Never a link: it comes from an email
    why: str | None
    deadline: date | None  # from the task's newest email that gives one (see list_todos)
    urgency: str  # one of ORDER
    others: list[OtherConversation]  # the task's other conversations, newest first. Usually none


def list_todos(flagged, inbox_ids, today=None):
    """My to-do list: one Todo per task, most pressing first (see _same_task and _position).

    flagged: the emails that need me and that the agent left in the inbox, newest first,
    from database.flagged_emails().
    inbox_ids: the IDs of the emails in my inbox right now, from
    gmail_client.inbox_message_ids(). None means Gmail couldn't be asked: then nothing is
    left out, so the list may include emails I've archived since.
    today: the date to work out urgency from. Normally left out, which means today. A test
    can pass another date, to see what the list will look like on that day.

    A flagged email I've archived is done, even if a new reply has brought its
    conversation back to the inbox: the reply only becomes a to-do if triage flags it.
    """
    today = today or date.today()
    todos = {}
    for email in flagged:
        if inbox_ids is not None and email["message_id"] not in inbox_ids:
            continue  # archived since, so it's done
        deadline = date.fromisoformat(email["deadline"]) if email["deadline"] else None
        if email["thread_id"] in todos:
            # The emails come newest first, so the conversation already has its row from
            # a newer email. This one adds to "more", and its date counts if no newer email
            # gave one: a reminder without a date doesn't erase the deadline.
            todo = todos[email["thread_id"]]
            todo.more += 1
            if todo.deadline is None and deadline:
                todo.deadline = deadline
                todo.urgency = _urgency(deadline, email["time_sensitive"], today)
            continue
        # The full read has the last word: its kind makes an email one or the other. Without
        # one, triage's flags stand, and those can be both.
        read = email["full_read"] is not None
        needs_reply = email["kind"] in REPLY_KINDS if read else bool(email["needs_reply"])
        todos[email["thread_id"]] = Todo(
            thread_id=email["thread_id"],
            message_id=email["message_id"],
            needs_reply=needs_reply,
            needs_action=not needs_reply if read else bool(email["needs_action"]),
            what_to_do=_what_to_do(email),
            sender=email["sender"],
            subject=email["subject"],
            received=datetime.fromisoformat(email["received_at"]),
            label=email["label"],
            more=0,
            kind=email["kind"],
            steps=json.loads(email["steps"]) if email["steps"] else [],
            place=email["place"],
            why=email["full_read_reason"],
            deadline=deadline,
            urgency=_urgency(deadline, email["time_sensitive"], today),
            others=[],
        )
    return sorted(_one_row_per_task(todos.values(), today), key=_position)


def _one_row_per_task(rows, today):
    """Puts conversations about the same task on one row, which describes the newest of them.

    rows come newest first, and a row's date follows the same rule as inside a conversation:
    an older one's date only counts if no newer one gave a date.
    """
    tasks = []
    for row in rows:
        task = next((task for task in tasks if _same_task(task, row)), None)
        if task is None:
            tasks.append(row)
            continue
        task.more += row.more + 1
        task.others.append(OtherConversation(row.thread_id, row.what_to_do, row.received))
        if task.deadline is None and row.deadline:
            task.deadline = row.deadline
            task.urgency = _urgency(row.deadline, False, today)
    return tasks


def _same_task(task, row):
    """Whether a row is about the same task: the same sender and the same kind, like five
    "[name] messaged you" emails on LinkedIn or four reminders for one contract.

    Two rows with different dates stay apart, like two events from one organizer. Without
    a full read there's no kind, so nothing is merged. Two different tasks can still end up
    together, like two documents from one person, so the page lists every conversation.
    """
    if task.kind is None or task.kind in NEVER_MERGED or (task.sender, task.kind) != (row.sender, row.kind):
        return False
    return task.deadline is None or row.deadline is None or task.deadline == row.deadline


def _what_to_do(email):
    """The to-do itself: the full read's line, what triage said to do, or "Reply to [sender]"."""
    if email["what_to_do"]:
        return email["what_to_do"]  # like "Reply to Sam on LinkedIn"
    if email["needs_action"] and email["action"]:
        return email["action"]  # like "Sign the contract on Dropbox Sign"
    if email["needs_reply"]:
        return f"Reply to {email['sender']}"
    return email["subject"]


def _urgency(deadline, time_sensitive, today):
    """How pressing a to-do is, as one of ORDER. "Time-sensitive" only counts without a deadline."""
    if deadline is None:
        return "time-sensitive" if time_sensitive else "normal"
    days_left = (deadline - today).days
    if days_left < 0:
        return "overdue"
    return "urgent" if days_left <= URGENT_WITHIN_DAYS else "normal"


def _position(todo):
    """Where a to-do goes in the list: first by urgency, then by date.

    The soonest deadline comes first, and no deadline comes after all of those (date.max
    is the last date there is). Overdue goes the other way, least late first. sorted()
    keeps rows that tie in the order they came, so rows without a date stay newest first.
    """
    day = (todo.deadline or date.max).toordinal()
    return ORDER.index(todo.urgency), -day if todo.urgency == "overdue" else day
