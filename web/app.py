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

import mimetypes
from contextlib import closing
from datetime import datetime
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
    start_web_sign_in,
)

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

# Kept in the server's memory from one page load to the next. The app has one user, so
# one of each is enough: the sign-in in progress, until Google sends the browser back,
# and a message to show once on the next page. pop() takes a value out in one step, so
# two page loads at the same moment can't both get it.
_waiting = {}

# Where signing in ends, whether it worked or not: the address behind "Inbox Agent" in
# the top bar. That's Action needed for now, and the home page once piece 1f is built.
HOME = "/"


def show_date(iso_text):
    """A date like "Fri 24 Oct", with the year added if it isn't this year."""
    date = datetime.fromisoformat(iso_text)
    text = f"{date:%a} {date.day} {date:%b}"
    return text if date.year == datetime.now().year else f"{text} {date.year}"


def todos_for_page(rows):
    """Turn the flagged emails from the log into the rows of the to-do list: one per conversation.

    Some conversations have many flagged emails, like a long back-and-forth with a buyer.
    rows come newest first, so the first one seen for each conversation is its newest email,
    and that one decides what the row says. "more" counts the conversation's other flagged
    emails. Everything the template shows is worked out here, so the template only draws.
    """
    todos = {}
    for row in rows:
        if row["thread_id"] in todos:
            todos[row["thread_id"]]["more"] += 1
            continue
        if row["needs_action"] and row["action"]:
            text = row["action"]  # what triage said to do, like "Sign the contract on Dropbox Sign"
        elif row["needs_reply"]:
            text = f"Reply to {row['sender']}"
        else:
            text = row["subject"]
        todos[row["thread_id"]] = {
            "needs_action": bool(row["needs_action"]),
            "needs_reply": bool(row["needs_reply"]),
            "text": text,
            "sender": row["sender"],
            "subject": row["subject"],
            "date": show_date(row["received_at"]),
            "label": row["label"],
            "more": 0,
        }
    return list(todos.values())  # a dictionary keeps the order things were added: newest first


def connect_to_gmail():
    """Make a new Gmail connection for this page load, or say why it can't be made.

    Every page load makes its own connection instead of sharing one: FastAPI may handle
    two page loads at the same time, and Google's library can't be used by two at once.
    A new connection also reads token.json again, so a new sign-in works straight away.

    Returns (connection, None), or (None, problem), where problem is "sign_in" if Google
    has ended the login, "offline" if Google couldn't be reached, or "google_problem" if
    Google has a temporary problem on its side.
    """
    try:
        return get_gmail_service(allow_browser_sign_in=False), None
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
def action_needed(request: Request):
    """Action needed: everything that needs a reply or an action from me, one row per conversation."""
    # The Gmail connection isn't used yet: the inbox check comes in part 3 of piece 1e.
    # For now, connecting tells the page whether Gmail can be reached, for the notice.
    gmail, gmail_problem = connect_to_gmail()
    # A new database connection for every page load: FastAPI may handle each request on a
    # different thread, and a SQLite connection only works on the thread that opened it.
    # closing() closes it again afterwards, even if something goes wrong.
    with closing(database.connect()) as conn:
        rows = database.open_todos(conn)
    return templates.TemplateResponse(
        request=request,  # the browser's request, which templates need to build links
        name="action.html",
        context={  # the values the template can use
            "active": "action_needed",
            "todos": todos_for_page(rows),
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
