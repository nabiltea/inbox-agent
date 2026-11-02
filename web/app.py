"""Step 7: the web page, my hub for the inbox agent.

A FastAPI app that runs on my own computer. Each address, like "/", is a Python
function, and each page is an HTML template in web/templates/ that the function
fills in. There are three pages, Action needed, Unsorted and History, which share
one layout with the menu (web/templates/base.html). Action needed lists my to-dos.
The other two say which piece they arrive in.

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
import mimetypes
from contextlib import closing
from datetime import date, datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import database
from gmail_client import (
    GoogleTemporaryError,
    GoogleUnreachableError,
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

# This folder (web/). Building paths from this file's own location means they work
# whatever folder the server is started from.
WEB_DIR = Path(__file__).parent

# FastAPI normally adds its own documentation pages (/docs, /redoc and /openapi.json).
# They load scripts from an outside server, and a script running at this app's address
# could read my pages and press my buttons. This app serves pages to me, not an API to
# other programs, so they're switched off.
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

# The HTML templates live in web/templates/. Jinja2 escapes every value in templates
# whose names end in .html: a "<" in an email subject is shown as text, so email
# content can never run as code on the page.
templates = Jinja2Templates(directory=WEB_DIR / "templates")

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


def show_date(date):
    """A date like "Fri 24 Oct", with the year added if it isn't this year.

    How dates look is the page's business, so this stays here rather than in todos.py.
    """
    text = f"{date:%a} {date.day} {date:%b}"
    return text if date.year == datetime.now().year else f"{text} {date.year}"


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


# Lets the templates use these as "filters": {{ todo.received|show_date }}
templates.env.filters["show_date"] = show_date
templates.env.filters["show_deadline"] = show_deadline


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
    """Unsorted: emails that fit no label, with their suggested labels. A placeholder until piece 4."""
    return templates.TemplateResponse(request=request, name="unsorted.html", context={"active": "unsorted"})


@app.get("/history")
def history(request: Request):
    """History: past runs, what was sorted where, and undo. A placeholder until piece 5."""
    return templates.TemplateResponse(request=request, name="history.html", context={"active": "history"})


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
