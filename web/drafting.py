"""Drafting a reply from the web page, behind the "Draft reply" link on Action needed.

The draft page shows the conversation, read from Gmail, and a box for my hints. "Write draft"
runs drafter.py's write_draft() on a second thread, like the Sort button (see sorting.py):
a draft takes a minute or so, too long for a page to wait. The page asks how it's going once
a second and shows each step as a sentence.

There's one draft at a time for the whole app, kept in the server's memory. Each press of
the button gets a row in the log's drafts table, and its cost is saved after every call to
Claude. So if the server stops partway, the row says what was spent, and the page marks the
draft "interrupted" the next time it finds it unfinished.
"""

import asyncio
import logging
import re
import threading
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parseaddr

import anthropic
from mcp import Client

import database
import drafter
from gmail_client import (
    EmailGoneError,
    GoogleTemporaryError,
    GoogleUnreachableError,
    SignInNeededError,
    explain_failures,
    get_gmail_service,
    get_thread,
)
from web.sorting import explain

log = logging.getLogger("uvicorn.error")

# The longest hints the box takes, in characters. They go into the Drafter's prompt,
# and a note about what to say is normally a sentence or two.
MAX_HINTS = 2000

# How much of a folded message's text its one line shows. The page cuts it shorter to fit.
PREVIEW_LENGTH = 150


@dataclass
class ShownMessage:
    """One message of the conversation, ready for the draft page."""

    name: str  # "You", or the sender's name, or their address if the email has no name
    sender: str  # the whole From line, like "Sam Rivera <sam@example.com>"
    to: str
    cc: str
    date: str  # as the log writes dates, like "2026-11-18T09:12"
    body: str  # exactly the text the Drafter gets, quoted copies and cut-off included
    preview: str  # the start of the text on one line, for when it's folded
    open: bool  # shown in full, rather than folded to one line
    answered: bool  # the message the reply answers


def read_conversation(thread_id):
    """Read a conversation from Gmail for the draft page, which costs nothing.

    Returns (the conversation, None), or (None, problem), where problem is "sign_in",
    "offline" or "google_problem", like on every page, or "gone" if Gmail doesn't have it.
    The conversation is what gmail_client.get_thread() returns, the same as the Drafter gets.
    It asks Gmail once: when Gmail refuses for a moment, the page says so straight away,
    rather than loading for minutes while get_thread() waits to try again.
    """
    try:
        service = get_gmail_service(allow_browser_sign_in=False)
        with explain_failures():
            return get_thread(service, thread_id, attempts=1), None
    except SignInNeededError:
        return None, "sign_in"
    except GoogleUnreachableError:
        return None, "offline"
    except GoogleTemporaryError:
        return None, "google_problem"
    except EmailGoneError:
        return None, "gone"


def shown_messages(thread):
    """The conversation's messages for the page, oldest first, like Gmail.

    The reply answers the newest message from someone else, as in drafter.py and
    build_reply() in gmail_client.py. If every message is mine, it follows up on the newest.
    That message and the newest one are open, and the rest are folded to one line.
    """
    messages = thread["messages"]
    from_others = [index for index, message in enumerate(messages) if not message["from_me"]]
    answered = from_others[-1] if from_others else len(messages) - 1
    shown = []
    for index, message in enumerate(messages):
        name, address = parseaddr(message["from"])
        shown.append(ShownMessage(
            name="You" if message["from_me"] else (name or address or message["from"]),
            sender=message["from"],
            to=message["to"],
            cc=message["cc"],
            date=message["date"],
            body=message["body"],
            preview=re.sub(r"\s+", " ", message["body"]).strip()[:PREVIEW_LENGTH],
            open=index in (answered, len(messages) - 1),
            answered=index == answered,
        ))
    return shown


# --- Writing the draft, on a second thread ----------------------------------------------------

# The Critic's kinds of problem (Problem in drafter.py), in words for the page.
PROBLEM_KINDS = {
    "invented_fact": "Invented fact",
    "unapproved_promise": "A promise you haven't made",
    "missed_question": "Missed question",
    "tone_or_language": "Tone or language",
    "recipients": "Recipients",
    "other": "Other",
}


@dataclass
class ShownStep:
    """One step of the Drafter's work, as the page shows it: a sentence, with any details listed under it."""

    text: str
    details: list[str] = field(default_factory=list)


def _count(number, word, plural=None):
    """Like "1 message" or "3 messages"."""
    return f"{number} {word if number == 1 else plural or word + 's'}"


def describe(step):
    """A step that drafter.write_draft() reports, as a sentence for the page.

    The terminal shows the same steps its own way (terminal_lines() in drafter.py). match picks
    the case by what kind of record the step is, and LookedUp(tool=...) also checks a field.
    """
    match step:
        case drafter.ReadConversation():
            return ShownStep(f"Read the conversation: {_count(step.messages, 'message')}, answering {step.person}.")
        case drafter.GotContext():
            earlier = (f"{_count(step.recent_emails, 'earlier email')} of yours to them" if step.recent_emails
                       else "no earlier emails of yours to them")
            files = f", and {_count(step.profile_files, 'profile file')}" if step.profile_files else ""
            people = _count(step.writers, "person", "people")
            return ShownStep(f"Found {earlier}{files}. Its lookups only cover emails with the {people} who wrote here.")
        case drafter.LookedUp(tool="search_emails"):
            query = str(step.asked.get("query", "")).strip()
            if step.failed:
                return ShownStep("Tried to search your emails with them, but the search didn't work.")
            return ShownStep(f'Searched your emails with them for "{query}".' if query else "Looked through your emails with them.")
        case drafter.LookedUp():
            if step.failed:
                return ShownStep("Tried to read another conversation, but it wasn't allowed or couldn't be read.")
            return ShownStep("Read another conversation with them.")
        case drafter.Submitted(round=1):
            return ShownStep("Wrote a draft. The Critic is checking it…")
        case drafter.Submitted():
            return ShownStep(f"Revised the draft. The Critic is checking it again (round {step.round})…")
        case drafter.Reviewed():
            review = step.review
            if review.approved:
                return ShownStep(f"The Critic approved it: {review.summary}")
            return ShownStep(
                f"The Critic found {_count(len(review.problems), 'problem')}: {review.summary}",
                [f"{PROBLEM_KINDS.get(p.kind, 'Other')}: {p.detail}" for p in review.problems],
            )
        case drafter.HitLimit():
            return ShownStep(f"Stopped at the ${drafter.COST_LIMIT:.2f} limit for one draft, after ${step.cost:.2f}.")
    return ShownStep(str(step))


@dataclass
class DraftRun:
    """One press of Write draft while it's going: what the page shows. The second thread is the only one that writes here."""

    thread_id: str
    subject: str
    draft_id: int  # its row in the drafts table
    started: datetime = field(default_factory=datetime.now)
    running: bool = True
    steps: list[ShownStep] = field(default_factory=list)
    cost: float = 0.0  # in dollars, so far

    @property
    def elapsed(self):
        """How long it has been going, like "0:42"."""
        seconds = int((datetime.now() - self.started).total_seconds())
        return f"{seconds // 60}:{seconds % 60:02d}"


class BusyError(RuntimeError):
    """A draft for another conversation is being written. The run is in .run."""

    def __init__(self, run):
        super().__init__(f"already drafting a reply to {run.subject}")
        self.run = run


class SavedCosts(drafter.CostTracker):
    """drafter.py's CostTracker, which also saves the total to the draft's row after every call to Claude."""

    def __init__(self, run):
        super().__init__()
        self.run = run

    def add(self, model, usage):
        super().add(model, usage)
        self.run.cost = self.total
        # This thread has its own database connection: a connection only works on the thread that opened it.
        with closing(database.connect()) as conn:
            database.save_draft_cost(conn, self.run.draft_id, self.total)


_run = None  # the latest run, or None if Write draft hasn't been pressed since the server started
_lock = threading.Lock()  # so two clicks at the same moment can't both start a run


def start(thread_id, subject, hints):
    """Start writing a draft on a second thread, and return its run.

    Pressing again while this conversation's draft is being written returns the same run, so
    eight quick clicks give one draft. Raises BusyError while another conversation's is.
    """
    global _run
    with _lock:
        if _run is not None and _run.running:
            if _run.thread_id == thread_id:
                return _run
            raise BusyError(_run)
        with closing(database.connect()) as conn:
            draft_id = database.start_draft(conn, thread_id, subject, hints)
        _run = DraftRun(thread_id, subject, draft_id)
        # daemon: this thread doesn't keep the server alive when it's told to stop
        threading.Thread(target=_write, args=(_run, hints), name="write-draft", daemon=True).start()
        return _run


def current():
    """The latest run, or None."""
    return _run


def latest(thread_id):
    """The newest draft for a conversation from the log, and its run if it's still being written.

    Returns (row or None, run or None). A row still marked "writing" with no run behind it was
    cut off when the server stopped, so it's marked "interrupted" here. The lock makes sure a
    draft that's just starting isn't mistaken for one that was cut off.
    """
    with _lock, closing(database.connect()) as conn:
        row = database.latest_draft(conn, thread_id)
        live = _run if _run is not None and _run.running and row is not None and _run.draft_id == row["id"] else None
        if row is not None and row["status"] == "writing" and live is None:
            database.end_draft(conn, row["id"], "interrupted", "the server stopped while the draft was being written.")
            row = database.latest_draft(conn, thread_id)
    return row, live


def _write(run, hints):
    """The second thread's work: write the draft, then record how it ended.

    It catches everything: with nothing to catch an error here, the thread would just end,
    and the page would say "drafting" forever.
    """
    try:
        drafted, stopped = asyncio.run(_write_with_claude(run, hints))
        with closing(database.connect()) as conn:
            if drafted is not None:
                database.finish_draft(conn, run.draft_id, drafted, run.cost)
            else:
                database.end_draft(conn, run.draft_id, "stopped", stopped, run.cost)
    except Exception as error:
        log.exception("Drafting stopped")
        # Errors from inside the MCP connection arrive wrapped in a group: explain the first one.
        while isinstance(error, BaseExceptionGroup):
            error = error.exceptions[0]
        with closing(database.connect()) as conn:
            database.end_draft(conn, run.draft_id, "failed", explain(error), run.cost)
    finally:
        run.running = False


async def _write_with_claude(run, hints):
    """Run drafter.write_draft() with its own connections, as the terminal does. Returns (Drafted, None) or (None, why it stopped).

    asyncio.run() above gives this thread an event loop of its own, which starts my MCP server
    in the background. DrafterStopped is caught inside "async with", before the MCP connection
    wraps it in a group of errors (see part 1 of piece 6 in the build log).
    """
    costs = SavedCosts(run)
    async with anthropic.AsyncAnthropic() as claude, Client(drafter.SERVER) as gmail:
        try:
            drafted = await drafter.write_draft(
                gmail, claude, costs, run.thread_id, hints or None, report=lambda step: run.steps.append(describe(step))
            )
        except drafter.DrafterStopped as stopped:
            return None, str(stopped)
    return drafted, None
