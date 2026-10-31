"""Step 7: the web page, my hub for the inbox agent.

A FastAPI app that runs on my own computer. Each address, like "/", is a Python
function, and each page is an HTML template in web/templates/ that the function
fills in. For now there's a single page, which shows that the server, FastAPI
and the templates work together.

Start it from the project folder with:
  python -m uvicorn web.app:app --reload
then open http://127.0.0.1:8000 in a browser. Stop it with Ctrl+C.

uvicorn is the server: it listens for requests and hands each one to "app" in
this file. --reload restarts it whenever a Python file is saved. It only listens
on 127.0.0.1, so only this computer can open the page, not other devices on the
same Wi-Fi.
"""

from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates

# FastAPI normally adds its own documentation pages (/docs, /redoc and /openapi.json).
# They load scripts from an outside server, and a script running at this app's address
# could read my pages and press my buttons. This app serves pages to me, not an API to
# other programs, so they're switched off.
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

# The templates live in web/templates/, next to this file. Building the path from
# this file's own location means it works whatever folder the server starts from.
# Jinja2 escapes every value in templates whose names end in .html: a "<" in an
# email subject is shown as text, so email content can never run as code on the page.
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


# @app.get("/") connects this function to the address "/", the home page. GET means
# the browser only asks to see something. Anything that changes Gmail will use POST.
@app.get("/")
def home(request: Request):
    """The home page. For now it shows the time it was made, which changes on every refresh."""
    return templates.TemplateResponse(
        request=request,  # the browser's request, which templates need to build links later
        name="hello.html",
        context={"made_at": f"{datetime.now():%H:%M:%S}"},  # the values the template can use
    )
