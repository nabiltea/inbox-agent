"""Step 7: the web page, my hub for the inbox agent.

A FastAPI app that runs on my own computer. Each address, like "/", is a Python
function, and each page is an HTML template in web/templates/ that the function
fills in. There are three pages, Action needed, Unsorted and History, which share
one layout with the menu (web/templates/base.html). For now they only say what's
coming.

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
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

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


# @app.get("/") connects the function below it to the address "/", the home page. GET
# means the browser only asks to see something. Anything that changes Gmail will use POST.
# Each page passes "active", the name of its own function, so the menu can show which
# page is open. url_for() in the templates builds the menu's links from the same names.
@app.get("/")
def action_needed(request: Request):
    """Action needed: everything that needs a reply or an action from me. The to-dos arrive in piece 1d."""
    return templates.TemplateResponse(
        request=request,  # the browser's request, which templates need to build links
        name="action.html",
        context={"active": "action_needed"},  # the values the template can use
    )


@app.get("/unsorted")
def unsorted(request: Request):
    """Unsorted: emails that fit no label, with their suggested labels. A placeholder until piece 4."""
    return templates.TemplateResponse(request=request, name="unsorted.html", context={"active": "unsorted"})


@app.get("/history")
def history(request: Request):
    """History: past runs, what was sorted where, and undo. A placeholder until piece 5."""
    return templates.TemplateResponse(request=request, name="history.html", context={"active": "history"})
