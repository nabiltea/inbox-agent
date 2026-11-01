"""What counts as a to-do, in one place.

The web page uses it, and so will the MCP server (Claude Desktop), so both always show
the same list. list_todos() is given two lists and works out the to-dos from them: the
flagged emails from the agent's log, and the emails in my inbox from Gmail. It never
opens the database or talks to Gmail itself. Whoever uses it fetches the two lists first
and deals with any problem in its own way: the page shows a notice, the MCP server tells
Claude. That also makes it easy to test with made-up lists.
"""

from dataclasses import dataclass
from datetime import datetime


@dataclass
class Todo:
    """One conversation that needs me, described by its newest flagged email still in the inbox."""

    thread_id: str
    message_id: str
    needs_reply: bool
    needs_action: bool
    what_to_do: str  # what triage said to do, or "Reply to [sender]"
    sender: str
    subject: str
    received: datetime
    label: str
    more: int  # how many other flagged emails from this conversation are still in the inbox


def list_todos(flagged, inbox_ids):
    """My to-do list: one Todo per conversation, newest first.

    flagged: the emails the agent flagged and left in the inbox, newest first, from
    database.flagged_emails().
    inbox_ids: the IDs of the emails in my inbox right now, from
    gmail_client.inbox_message_ids(). None means Gmail couldn't be asked: then nothing is
    left out, so the list may include emails I've archived since.

    A flagged email I've archived is done, even if a new reply has brought its
    conversation back to the inbox: the reply only becomes a to-do if triage flags it.
    """
    todos = {}
    for email in flagged:
        if inbox_ids is not None and email["message_id"] not in inbox_ids:
            continue  # archived since, so it's done
        if email["thread_id"] in todos:
            # The emails come newest first, so the conversation already has its row
            # from a newer email. This one only adds to "more".
            todos[email["thread_id"]].more += 1
            continue
        todos[email["thread_id"]] = Todo(
            thread_id=email["thread_id"],
            message_id=email["message_id"],
            needs_reply=bool(email["needs_reply"]),
            needs_action=bool(email["needs_action"]),
            what_to_do=_what_to_do(email),
            sender=email["sender"],
            subject=email["subject"],
            received=datetime.fromisoformat(email["received_at"]),
            label=email["label"],
            more=0,
        )
    return list(todos.values())  # a dictionary keeps the order things were added: newest first


def _what_to_do(email):
    """The to-do itself: what triage said to do, or "Reply to [sender]"."""
    if email["needs_action"] and email["action"]:
        return email["action"]  # like "Sign the contract on Dropbox Sign"
    if email["needs_reply"]:
        return f"Reply to {email['sender']}"
    return email["subject"]
