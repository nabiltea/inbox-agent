"""Sorting new emails from the web page, behind the "Sort new emails" button.

Sorting takes about 2 seconds per email, too long for a page to wait for. So the button
starts it on a second thread and gets an answer straight away, and the page asks how far
it is once a second, like a buzzer in a café instead of standing at the counter. The steps
for each email are the same as in triage.py, which still works from the terminal.

There's one run at a time, kept in the server's memory. Each email is finished in Gmail
and in the log before the next one starts, so if the server stops partway, nothing is
lost: the emails it hadn't got to are still new the next time.
"""

import logging
import threading
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import datetime

import anthropic

import database
from gmail_client import (
    GoogleTemporaryError,
    GoogleUnreachableError,
    SignInNeededError,
    get_emails,
    get_gmail_service,
    get_or_create_labels,
    inbox_message_ids,
)
from triage import UNSORTED, apply_sorted, read_sorting_rules, sort_email

log = logging.getLogger("uvicorn.error")

# One click stops here, in dollars. My budget is a couple of dollars a month, and a normal
# email costs well under a tenth of a cent, so this only matters on an unusual run, like
# after a long break. The rest stay new for the next click.
COST_LIMIT = 0.50


@dataclass
class SortRun:
    """One press of the button: how far it is, and what has happened so far.

    The second thread is the only one that writes here. The page only reads.
    """

    running: bool = True
    total: int | None = None  # how many new emails there are. Unknown until Gmail has answered
    done: int = 0  # how many of them have been dealt with
    archived: int = 0
    need_me: int = 0
    to_dos: list[str] = field(default_factory=list)  # what the emails that need me say to do, without repeats
    unsorted: int = 0
    skipped: int = 0  # emails left for next time, because they couldn't be read
    cost: float = 0.0  # in dollars
    hit_limit: bool = False  # whether it stopped at COST_LIMIT
    problem: str | None = None  # why the run stopped early, if it did, in words for the page
    shown: bool = False  # whether a page has shown the finished run

    @property
    def sorted_count(self):
        """How many emails it really sorted: the ones it dealt with, apart from the ones it had to skip."""
        return self.done - self.skipped

    @property
    def still_new(self):
        """How many emails it didn't get to, plus the ones it skipped. They're all still new for next time."""
        return (self.total or 0) - self.sorted_count


_run = None  # the latest run, or None if the button hasn't been pressed since the server started
_starting = threading.Lock()  # so two clicks at the same moment can't both start a run


def start():
    """Start sorting on a second thread, unless a run is already going. Returns the run either way."""
    global _run
    with _starting:
        if _run is None or not _run.running:
            _run = SortRun()
            # daemon: this thread doesn't keep the server alive when it's told to stop
            threading.Thread(target=_sort_new_emails, args=(_run,), name="sort-new-emails", daemon=True).start()
    return _run


def current():
    """The latest run, or None."""
    return _run


class SortingNowError(RuntimeError):
    """A run is going, so a change that mustn't overlap with one has to wait."""


@contextmanager
def no_sorting_meanwhile():
    """For a change that mustn't overlap with a run, like taking a label away from Claude's choices.

    Raises SortingNowError if a run is going. It holds the lock that starting a run needs, so
    a press of the button during the change waits for the change to finish.
    """
    with _starting:
        if _run is not None and _run.running:
            raise SortingNowError("sorting is going")
        yield


def sort_context(request):
    """For the templates: the run to show. A finished run is shown once, on the next page drawn."""
    if _run is None or (not _run.running and _run.shown):
        return {"sort_run": None}
    if not _run.running:
        _run.shown = True
    return {"sort_run": _run}


def _sort_new_emails(run):
    """The second thread's work: sort every email in the inbox that isn't in the log yet, newest first.

    It catches everything: with nothing to catch an error here, the thread would just
    end, and the page would say "sorting" forever.
    """
    try:
        # This thread makes its own connections: neither Gmail's nor SQLite's can be shared with another thread.
        service = get_gmail_service(allow_browser_sign_in=False)
        with closing(database.connect()) as conn:
            new_ids = [message_id for message_id in inbox_message_ids(service) if not database.is_processed(conn, message_id)]
            emails = sorted(get_emails(service, new_ids), key=lambda email: email.date, reverse=True)
            run.total = len(emails)
            if not emails:
                return
            client = anthropic.Anthropic()
            # Read here, not when the server starts, so a label I've just added works in this run.
            rules = read_sorting_rules()
            label_ids = get_or_create_labels(service, [*rules.labels, UNSORTED])
            run_id = datetime.now().isoformat(timespec="seconds")
            for email in emails:
                if run.cost >= COST_LIMIT:
                    run.hit_limit = True
                    break
                _sort_one(client, service, conn, run_id, rules, label_ids, email, run)
                run.done += 1
    except Exception as error:
        log.exception("Sorting stopped after %s emails", run.done)
        run.problem = explain(error)
    finally:
        run.running = False


def _sort_one(client, service, conn, run_id, rules, label_ids, email, run):
    """Decide about one email, carry it out, and add it to the run's numbers."""
    sorted_email = sort_email(client, service, email, rules)
    run.cost += sorted_email.cost + sorted_email.read_cost
    if sorted_email.result is None or sorted_email.read_problem:
        run.skipped += 1
        return
    apply_sorted(service, conn, run_id, label_ids, sorted_email)
    run.archived += sorted_email.archive
    run.unsorted += sorted_email.result.label == UNSORTED
    if sorted_email.needs_me and not sorted_email.archive:
        run.need_me += 1
        if sorted_email.what_to_do not in run.to_dos:  # several reminders can say the same thing
            run.to_dos.append(sorted_email.what_to_do)


def explain(error):
    """What went wrong behind a button, in words for the page that say what to do about it.

    Anything else gets no details on the page, because they could be long or come from an
    email. They're in the server's log.
    """
    if isinstance(error, SignInNeededError):
        return "Gmail needs you to sign in again. Then press the button again."
    if isinstance(error, GoogleUnreachableError):
        return "Google couldn't be reached. Check your internet connection, then press the button again."
    if isinstance(error, GoogleTemporaryError):
        return "Google has a temporary problem on its side. Try again in a few minutes."
    if isinstance(error, anthropic.APIConnectionError):
        return "Claude couldn't be reached. Check your internet connection, then press the button again."
    if isinstance(error, anthropic.AuthenticationError):
        return "Claude didn't accept the API key. Check the key in the .env file."
    if isinstance(error, anthropic.APIStatusError):
        if error.status_code == 402 or "credit balance" in str(error).lower():
            return "Claude's credit has run out. Add credit on Anthropic's site, then press the button again."
        if error.status_code == 429:
            return "Claude is limiting how many requests it takes right now. Try again in a few minutes."
        if error.status_code >= 500:
            return "Claude has a temporary problem on its side. Try again in a few minutes."
    return f"something unexpected went wrong ({type(error).__name__}). The details are in the server's log."
