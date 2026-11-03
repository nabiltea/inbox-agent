"""Step 7: the web page, my hub for the inbox agent.

A FastAPI app that runs on my own computer. Each address, like "/", is a Python
function, and each page is an HTML template in web/templates/ that the function
fills in. There are three pages, Action needed, Unsorted and History, which share
one layout with the menu and the "Sort new emails" button (web/templates/base.html).
Action needed lists my to-dos, and Unsorted the emails that fit no label, where I decide
what happens to each one's suggested label. History shows what each sorting run did,
finds any email the agent has sorted, and undoes my own changes.

Start it from the project folder with:
  python -m uvicorn web.app:app --reload
then open http://127.0.0.1:8000 in a browser. Stop it with Ctrl+C.

uvicorn is the server: it listens for requests and hands each one to "app" in
this file. --reload restarts it whenever a Python file is saved. It only listens
on 127.0.0.1, so only this computer can open the page, not other devices on the
same Wi-Fi.

The page's styles are in web/static/app.css, which Tailwind's program builds from
the classes used in web/ (see web/tailwind.css). Build it with:
  tools/tailwindcss.exe -i web/tailwind.css -o web/static/app.css
While editing templates, add --watch and leave it running in a second terminal: it
rebuilds the CSS every time a file is saved. (A public site would add --minify.)
"""

import hashlib
import logging
import mimetypes
from contextlib import closing
from datetime import date, datetime
from math import ceil
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Form, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

import database
from gmail_client import (
    EmailGoneError,
    GoogleTemporaryError,
    GoogleUnreachableError,
    LabelRefusedError,
    SignInFailedError,
    SignInNeededError,
    finish_web_sign_in,
    get_gmail_service,
    gmail_link,
    inbox_message_ids,
    saved_account,
    start_web_sign_in,
)
from todos import list_todos
from triage import load_labels
from web import labelling, sorting
from web.csrf import csrf_context, protect_against_other_websites

log = logging.getLogger("uvicorn.error")

# This folder (web/). Building paths from this file's own location means they work
# whatever folder the server is started from.
WEB_DIR = Path(__file__).parent

# FastAPI normally adds its own documentation pages (/docs, /redoc and /openapi.json).
# They load scripts from an outside server, and a script running at this app's address
# could read my pages and press my buttons. This app serves pages to me, not an API to
# other programs, so they're switched off.
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

# Other websites can't press this app's buttons (see web/csrf.py).
app.middleware("http")(protect_against_other_websites)

# Only answers requests addressed to this computer. A website could otherwise point an address
# of its own at 127.0.0.1 ("DNS rebinding"), which would let it read my pages as if they were its own.
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])

# The HTML templates live in web/templates/. Jinja2 escapes every value in templates
# whose names end in .html: a "<" in an email subject is shown as text, so email
# content can never run as code on the page. Every template gets the session's secret code,
# and how sorting is going, for the top of every page.
templates = Jinja2Templates(directory=WEB_DIR / "templates", context_processors=[csrf_context, sorting.sort_context])

# Files sent to the browser as they are, like the CSS: web/static/app.css is served at
# /static/app.css. Only files inside web/static/ can be reached this way, never the rest
# of the project. The name "static" is what url_for('static', ...) in the templates uses.
app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")

# The server labels each file with its type, looked up from the file's extension in
# Python's list of types. That list doesn't include .woff2 (the font) on this computer,
# so the font was sent as an unknown file, "application/octet-stream". This adds it.
mimetypes.add_type("font/woff2", ".woff2")


def file_version(path):
    """A short fingerprint of a file in web/static/, for its link, like app.css?v=9c5b2e95.

    Browsers keep a copy of files like the CSS, and since the server doesn't say for how
    long, they may keep reusing an outdated copy after the CSS is rebuilt. The fingerprint
    changes whenever the file does, so the link changes too and the browser fetches the
    new file. While the file stays the same, the browser can keep using its copy.
    """
    return hashlib.sha256((WEB_DIR / "static" / path).read_bytes()).hexdigest()[:8]


# Lets the templates call file_version(...), the way they call url_for(...)
templates.env.globals["file_version"] = file_version

# Kept in the server's memory from one page load to the next. The app has one user, so
# one of each is enough: the sign-in in progress, until Google sends the browser back,
# and a message to show once on the next page. pop() takes a value out in one step, so
# two page loads at the same moment can't both get it.
_waiting = {}

# Where signing in ends, whether it worked or not: the address behind "Inbox Agent" in
# the top bar, the home page.
HOME = "/"

# The list of emails on the History page: how many on a page, and how much of a search counts.
EMAILS_PER_PAGE = 50
MAX_SEARCH = 100


def show_date(date):
    """A date like "Fri 24 Oct", with the year added if it isn't this year.

    How dates look is the page's business, so this stays here rather than in todos.py.
    """
    text = f"{date:%a} {date.day} {date:%b}"
    return text if date.year == datetime.now().year else f"{text} {date.year}"


def show_logged_date(text):
    """The same as show_date(), for a date straight from the log, where dates are saved as text."""
    return show_date(datetime.fromisoformat(text))


def show_logged_moment(text):
    """A date and time straight from the log, like "Fri 24 Oct, 18:02", which tells apart two runs on one day."""
    moment = datetime.fromisoformat(text)
    return f"{show_date(moment)}, {moment:%H:%M}"


def show_deadline(deadline):
    """A deadline in words: "Due today", "Due tomorrow", "Due Tue 10 Nov" or "Was due Fri 6 Nov"."""
    days_left = (deadline - date.today()).days
    if days_left < 0:
        return f"Was due {show_date(deadline)}"
    if days_left == 0:
        return "Due today"
    if days_left == 1:
        return "Due tomorrow"
    return f"Due {show_date(deadline)}"


def what_i_did(change):
    """One of my changes from the log, in a sentence for its row on the History page."""
    if change["action"] == "needs_action":
        return "Marked as needing action, and put back in your inbox." if change["entered_inbox"] else "Marked as needing action."
    if change["action"] == "rejected":
        text = "Rejected: no label needed"
    elif change["label_created"]:
        text = f"Added the new label {change['new_label']} and moved this email there"
    else:
        text = f"Moved from {change['old_label']} to {change['new_label']}"
    return f"{text}, and it left your inbox." if change["left_inbox"] else f"{text}."


# Lets the templates use these as "filters": {{ todo.received|show_date }}
templates.env.filters["show_date"] = show_date
templates.env.filters["show_logged_date"] = show_logged_date
templates.env.filters["show_logged_moment"] = show_logged_moment
templates.env.filters["show_deadline"] = show_deadline
templates.env.filters["what_i_did"] = what_i_did
templates.env.filters["clean"] = labelling.clean


# Lets the templates call gmail_link(...) from gmail_client.py directly, the way they
# call url_for(...). The MCP server uses the same function, so both give the same links.
templates.env.globals["gmail_link"] = gmail_link


def check_inbox():
    """Ask Gmail which emails are in my inbox, for this page load.

    Returns (the IDs, None), or (None, problem), where problem is "sign_in" if Google
    has ended the login, "offline" if Google couldn't be reached, or "google_problem"
    if Google has a temporary problem on its side.

    Every page load makes its own Gmail connection instead of sharing one: FastAPI may
    handle two page loads at the same time, and Google's library can't be used by two at
    once. A new connection also reads token.json again, so a new sign-in works straight away.
    """
    try:
        gmail = get_gmail_service(allow_browser_sign_in=False)
        return inbox_message_ids(gmail), None
    except SignInNeededError:
        return None, "sign_in"
    except GoogleUnreachableError:
        return None, "offline"
    except GoogleTemporaryError:
        return None, "google_problem"


def in_my_inbox(email, inbox_ids):
    """Whether an email from the log is in my inbox. Gmail knows right now, and when it can't be asked, the log says what the agent last did."""
    return not email["archived"] if inbox_ids is None else email["message_id"] in inbox_ids


# @app.get("/") connects the function below it to the address "/", the home page. GET
# means the browser only asks to see something. Anything that changes Gmail will use POST.
# Each page passes "active", the name of its own function, so the menu can show which
# page is open. url_for() in the templates builds the menu's links from the same names.
@app.get("/")
def home(request: Request):
    """The home page: an overview of what needs me and what the agent did lately."""
    inbox_ids, gmail_problem = check_inbox()
    month_start = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    with closing(database.connect()) as conn:
        todos = list_todos(database.flagged_emails(conn), inbox_ids)
        # Emails in my inbox that aren't in the log: the agent hasn't sorted them yet.
        # Unknown (None) when Gmail couldn't be asked.
        new = None if inbox_ids is None else sum(not database.is_processed(conn, i) for i in inbox_ids)
        last_run = database.last_run_summary(conn)
        costs = database.sorting_costs(conn, since=month_start)
        read_costs = database.full_read_costs(conn, since=month_start)
        unsorted = database.unsorted_count(conn)
    return templates.TemplateResponse(
        request=request,
        name="home.html",
        context={
            "active": "home",  # no menu item has this name, so none is highlighted
            "need_you": len(todos),
            "urgent": sum(todo.urgency == "urgent" for todo in todos),
            "overdue": sum(todo.urgency == "overdue" for todo in todos),
            "actions": sum(todo.needs_action for todo in todos),
            "replies": sum(todo.needs_reply for todo in todos),
            "new": new,
            "unsorted": unsorted,
            "last_run": last_run,
            "last_run_date": datetime.fromisoformat(last_run["started"]) if last_run else None,
            "costs": costs,
            "read_costs": read_costs,
            "cost_this_month": costs["cost_since"] + read_costs["cost_since"],
            "cost_total": costs["cost"] + read_costs["cost"],
            "gmail_problem": gmail_problem,  # None, or why Gmail couldn't be checked
            "message": _waiting.pop("message", None),  # shown once, after signing in
        },
    )


@app.get("/action-needed")
def action_needed(request: Request):
    """Action needed: everything that needs a reply or an action from me, one row per conversation."""
    # The two lists the to-do recipe (todos.py) works from: what's in my inbox, from
    # Gmail, and what the agent flagged, from its log.
    inbox_ids, gmail_problem = check_inbox()
    # A new database connection for every page load: FastAPI may handle each request on a
    # different thread, and a SQLite connection only works on the thread that opened it.
    # closing() closes it again afterwards, even if something goes wrong.
    with closing(database.connect()) as conn:
        flagged = database.flagged_emails(conn)
    todos = list_todos(flagged, inbox_ids)
    return templates.TemplateResponse(
        request=request,  # the browser's request, which templates need to build links
        name="action.html",
        context={  # the values the template can use
            "active": "action_needed",
            "todos": todos,
            "urgent": sum(todo.urgency == "urgent" for todo in todos),
            "overdue": sum(todo.urgency == "overdue" for todo in todos),
            # For the "Open in Gmail" links: read from the saved login, so no request to Gmail.
            # The links work even when the app can't reach Gmail: Chrome has its own sign-in.
            "gmail_address": saved_account(),
            "gmail_problem": gmail_problem,  # None, or why Gmail couldn't be checked
            "message": _waiting.pop("message", None),  # shown once, after signing in
        },
    )


@app.get("/unsorted")
def unsorted(request: Request):
    """Unsorted: the emails that fit no label, with the label each one suggests, those in my inbox first."""
    inbox_ids, gmail_problem = check_inbox()
    with closing(database.connect()) as conn:
        emails = database.unsorted_emails(conn)
    labels = sorted(load_labels(), key=str.lower)  # read fresh for every page load
    return templates.TemplateResponse(
        request=request,
        name="unsorted.html",
        context={
            "active": "unsorted",
            "in_inbox": [email for email in emails if in_my_inbox(email, inbox_ids)],
            "older": [email for email in emails if not in_my_inbox(email, inbox_ids)],
            "labels": labels,  # for each row's dropdown
            "my_labels": {label.lower(): label for label in labels},  # to spot a suggestion I already have as a label
            "max_name": labelling.MAX_NAME,
            "max_description": labelling.MAX_DESCRIPTION,
            # For the "Open in Gmail" links, the same as on Action needed
            "gmail_address": saved_account(),
            "gmail_problem": gmail_problem,  # None, or why Gmail couldn't be checked
            "message": _waiting.pop("message", None),  # shown once, after signing in
        },
    )


# The buttons on a row of the Unsorted page. The answer normally replaces the row
# (see _unsorted_row.html).
@app.post("/unsorted/{message_id}/move")
def move_email(request: Request, message_id: str, label: Annotated[str, Form()] = ""):
    """Move an Unsorted email to a label I already have, and show how it went in its row."""
    return change_row(request, message_id, lambda: labelling.move_to_label(message_id, label))


@app.post("/unsorted/{message_id}/approve")
def approve_suggestion(request: Request, message_id: str):
    """Give an Unsorted email the label it suggests, as it stands."""
    return change_row(request, message_id, lambda: labelling.approve_suggestion(message_id))


@app.post("/unsorted/{message_id}/edit")
def edit_suggestion(
    request: Request, message_id: str, name: Annotated[str, Form()] = "", description: Annotated[str, Form()] = ""
):
    """Give an Unsorted email a new label, with the name and description from the row's Edit fold."""
    return change_row(request, message_id, lambda: labelling.approve_suggestion(message_id, edited=(name, description)))


@app.post("/unsorted/{message_id}/reject")
def reject_suggestion(request: Request, message_id: str):
    """Say that an Unsorted email needs no label, and show how it went in its row."""
    return change_row(request, message_id, lambda: labelling.reject_suggestion(message_id))


def what_happened(changed):
    """What a button did to an email, in a sentence for the page."""
    if changed.label is None:
        text = "Rejected: no label needed, so it won't be listed here again"
    elif changed.label_created:
        text = f"Added the new label {changed.label} and moved this email there"
    else:
        text = f"Moved to {changed.label}"
    if changed.left_inbox:
        return f"{text}, and it left your inbox."
    if changed.still_needs_me:
        return f"{text}. It stays in your inbox because it needs you."
    return f"{text}."


def attempt(request, row_id, change, not_done="Not moved"):
    """Carry out a button's change to one email, on any page.

    Returns what happened and no answer, or, if it went wrong, nothing and the answer that says so.
    not_done is how a problem with the email itself starts, in words that fit the button.
    """
    try:
        return change(), None
    except SignInNeededError:
        # A row has no room for the sign-in button, so the page reloads and shows its notice.
        return None, Response(headers={"HX-Refresh": "true"})
    except labelling.NoSuchLabelError:
        problem = "Not moved: choose a label from the list."
    except labelling.SameLabelError:
        problem = "Not moved: it already has that label."
    except (EmailGoneError, labelling.AlreadyNeedsMeError) as error:
        problem = f"{not_done}: {error}"
    except (labelling.BadLabelError, LabelRefusedError) as error:
        problem = f"Not saved: {error}"
    except sorting.SortingNowError:
        problem = "Not undone: sorting is going. Try again when it's finished."
    except labelling.NotNewestChangeError:
        problem = "Not undone: a newer change to this email has to be undone first."
    except Exception as error:
        log.exception("Couldn't finish changing an email")
        problem = f"Couldn't finish: {sorting.explain(error)}"
    return None, row_problem(request, row_id, problem)


def change_row(request, message_id, change):
    """Carry out a button's change to one email on the Unsorted page, and answer with what its row should say."""
    changed, problem = attempt(request, message_id, change)
    if problem:
        return problem
    if changed and changed.label_created:
        # Every row's dropdown needs the new label, so the page reloads, and says once what happened.
        _waiting["message"] = {"ok": True, "text": f"{changed.subject}: {what_happened(changed)} You can undo this on the History page."}
        return Response(headers={"HX-Refresh": "true"})
    with closing(database.connect()) as conn:
        total = database.unsorted_count(conn)
    return templates.TemplateResponse(
        request=request,
        name="_unsorted_changed.html",
        context={"changed": changed, "what_happened": what_happened(changed) if changed else None, "total": total},
    )


def row_problem(request, row_id, problem):
    """Show what went wrong inside a row, leaving the rest of the row as it is.

    HX-Reswap: none tells HTMX not to replace the row with this answer. It only puts the
    problem in its place in the row ("out of band"), so the label I chose stays selected.
    """
    return templates.TemplateResponse(
        request=request,
        name="_row_problem.html",
        context={"row_id": row_id, "problem": problem},
        headers={"HX-Reswap": "none"},
    )


@app.get("/history")
def history(request: Request):
    """History: what each sorting run did, newest first, and my own changes, each with a way to undo it.

    The numbers all come from the log. Gmail still gets asked, as on the other pages, because
    Undo needs it, so the page has to say when it needs me to sign in.
    """
    _, gmail_problem = check_inbox()
    with closing(database.connect()) as conn:
        runs = database.run_summaries(conn)
        label_counts = database.run_label_counts(conn)
        changes = database.my_changes(conn)
    return templates.TemplateResponse(
        request=request,
        name="history.html",
        context={
            "active": "history",
            "runs": runs,
            "label_counts": label_counts,
            "changes": changes,
            "total": len(changes),  # for the count beside the heading, as in the answer to a click
            "max_search": MAX_SEARCH,
            # For the "Open in Gmail" links, the same as on Action needed
            "gmail_address": saved_account(),
            "gmail_problem": gmail_problem,  # None, or why Gmail couldn't be checked
        },
    )


def what_undo_did(undone):
    """What undoing a change did to an email, in a sentence or two for its row."""
    where = "on the Unsorted page" if undone.waiting_for_label else f"under {undone.old_label}"
    if undone.email_gone:
        text = "Undone, but this email isn't in Gmail any more, so only the log changed."
    elif undone.was_mark:
        text = "Undone: it's off your to-do list, and out of your inbox again." if undone.out_of_inbox else "Undone: it's off your to-do list."
    elif undone.back_in_inbox:
        text = f"Undone: it's back {where}, and back in your inbox."
    else:
        text = f"Undone: it's back {where}."
    label = undone.created_label
    if label is None:
        return text
    if undone.others_with_label:
        others = "1 other email has" if undone.others_with_label == 1 else f"{undone.others_with_label} other emails have"
        return f"{text} {label} stays one of your labels, because {others} it."
    if undone.label_stuck:
        return f"{text} {label} is still one of your labels, because labels.json couldn't be changed."
    return f"{text} {label} is no longer one of your labels. It stays in Gmail, empty, and you can delete it there."


def undo_unless_sorting(change_id):
    """Undo one of my changes, unless a run is going (see no_sorting_meanwhile() in sorting.py)."""
    with sorting.no_sorting_meanwhile():
        return labelling.undo_change(change_id)


@app.post("/history/changes/{change_id}/undo")
def undo_my_change(request: Request, change_id: int):
    """Undo one of my changes from the list of them, and show how it went in its row."""
    undone, problem = attempt(request, f"change-{change_id}", lambda: undo_unless_sorting(change_id))
    if problem:
        return problem
    with closing(database.connect()) as conn:
        total = len(database.my_changes(conn))
    return templates.TemplateResponse(
        request=request,
        name="_change_undone.html",
        context={"undone": undone, "what_undo_did": what_undo_did(undone) if undone else None, "total": total},
    )


def my_labels():
    """My labels for a dropdown, read fresh for every page load, with no capitals in the way of the order.

    Empty if labels.json can't be read, because the list of emails is still useful without a dropdown.
    """
    try:
        return sorted(load_labels(), key=str.lower)
    except (OSError, ValueError, KeyError):
        log.exception("Couldn't read labels.json")
        return []


def email_row(request, message_id, message=None):
    """Draw one row of the list of emails again after a click, with what happened if there's something to say."""
    inbox_ids, _ = check_inbox()
    with closing(database.connect()) as conn:
        email = database.listed_email(conn, message_id)
        changes = database.newest_changes(conn, [message_id]).get(message_id, [])
    return templates.TemplateResponse(
        request=request,
        name="_email_row.html",
        context={
            "email": email,
            "in_inbox": email is not None and in_my_inbox(email, inbox_ids),
            "changes": changes,
            "message": message,
            "labels": my_labels(),
            "gmail_address": saved_account(),
        },
    )


# The buttons on a row of the list of emails. The answer normally replaces the row (see _email_row.html).
@app.post("/history/emails/{message_id}/label")
def change_email_label(request: Request, message_id: str, label: Annotated[str, Form()] = ""):
    """Give an email another of my labels, and draw its row again."""
    _, problem = attempt(request, message_id, lambda: labelling.change_label(message_id, label))
    return problem or email_row(request, message_id)


@app.post("/history/emails/{message_id}/needs-action")
def mark_needs_action(request: Request, message_id: str):
    """Put an email the agent missed on my to-do list, and draw its row again."""
    _, problem = attempt(request, message_id, lambda: labelling.mark_needs_action(message_id), not_done="Not added")
    return problem or email_row(request, message_id)


@app.post("/history/emails/{message_id}/changes/{change_id}/undo")
def undo_change_in_list(request: Request, message_id: str, change_id: int):
    """Undo one of my changes from the email's row in the list, and draw the row again."""
    undone, problem = attempt(request, message_id, lambda: undo_unless_sorting(change_id))
    return problem or email_row(request, message_id, what_undo_did(undone) if undone else "This change has already been undone.")


@app.get("/history/emails")
def find_email(request: Request, q: str = "", run: str = "", label: str = "", page: int = 1):
    """Every email the agent has sorted, newest first, with a search, filters and pages.

    It only looks, so it's a GET, and the search goes in the address, which makes Back and reloading work.
    """
    inbox_ids, gmail_problem = check_inbox()
    search = q.strip()[:MAX_SEARCH]
    with closing(database.connect()) as conn:
        total = database.count_emails(conn, search, run, label)
        last_page = max(ceil(total / EMAILS_PER_PAGE), 1)
        page = min(max(page, 1), last_page)  # an old link can ask for a page that no longer exists
        first = (page - 1) * EMAILS_PER_PAGE
        emails = database.find_emails(conn, search, run, label, limit=EMAILS_PER_PAGE, offset=first)
        changes = database.newest_changes(conn, [email["message_id"] for email in emails])
        started = database.run_started(conn, run) if run else None
    return templates.TemplateResponse(
        request=request,
        name="history_emails.html",
        context={
            "active": "history",
            "emails": [(email, in_my_inbox(email, inbox_ids), changes.get(email["message_id"], [])) for email in emails],
            "labels": my_labels(),  # for each row's dropdown
            "total": total,
            "first": first,
            "page": page,
            "last_page": last_page,
            "search": search,
            "run": run,
            "label": label,
            "run_started": started,
            # What the links to other pages of this list have to keep
            "filters": {name: value for name, value in (("q", search), ("run", run), ("label", label)) if value},
            "max_search": MAX_SEARCH,
            # For the "Open in Gmail" links, the same as on Action needed
            "gmail_address": saved_account(),
            "gmail_problem": gmail_problem,  # None, or why Gmail couldn't be checked
        },
    )


# The "Sort new emails" button. The sorting itself runs on a second thread (see sorting.py).
@app.post("/sort")
def start_sorting(request: Request):
    """Start sorting, unless it's already going, and answer straight away with how it's going."""
    sorting.start()
    return templates.TemplateResponse(request=request, name="_sort_started.html", context={})


@app.get("/sort/status")
def sort_status(request: Request):
    """How sorting is going, which the page asks once a second. When it's over, this tells the page to reload."""
    run = sorting.current()
    if run is None or not run.running:
        return Response(headers={"HX-Refresh": "true"})
    return templates.TemplateResponse(
        request=request, name="_sort_status.html", context={}, headers={"Cache-Control": "no-store"}
    )


# Signing in to Gmail from the page. The button sends the browser to Google's sign-in
# page, and Google sends it back to /sign-in/callback with a one-time ticket that
# gmail_client.py trades for the login. Both end with a redirect: an answer that tells
# the browser to go to another address, which it does by itself. 303 is the standard
# redirect after a POST: it tells the browser to load the new address normally.
@app.post("/sign-in")
def sign_in(request: Request):
    """The "Sign in to Gmail" button: send the browser to Google's sign-in page.

    A POST, because it does something: it starts a sign-in and remembers its protections.
    """
    try:
        google_address, in_progress = start_web_sign_in(str(request.url_for("sign_in_callback")))
    except SignInFailedError as error:
        _waiting["message"] = {"ok": False, "text": str(error)}
        return RedirectResponse(HOME, status_code=303)
    _waiting["sign_in"] = in_progress  # replaces any earlier attempt, which then stops working
    return RedirectResponse(google_address, status_code=303)


@app.get("/sign-in/callback")
def sign_in_callback(request: Request):
    """Where Google sends the browser back after its sign-in page, with a one-time ticket or an error."""
    in_progress = _waiting.pop("sign_in", None)  # so each attempt can be used only once
    try:
        address = finish_web_sign_in(in_progress, dict(request.query_params))
        _waiting["message"] = {"ok": True, "text": f"Signed in to Gmail as {address}."}
    except SignInFailedError as error:
        _waiting["message"] = {"ok": False, "text": str(error)}
    return RedirectResponse(HOME, status_code=303)
