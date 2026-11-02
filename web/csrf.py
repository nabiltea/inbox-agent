"""Protection against other websites pressing this app's buttons ("cross-site request forgery", or CSRF).

Any website I visit can make my browser send a request to this app. It can't read the
answer, but it doesn't need to if the request itself changes something. So every request
that changes something (every POST) has to carry a secret code that only this app's own
pages contain. Another website can't read my pages, so it can't know the code.

This is the "synchronizer token pattern" from OWASP's guide to web security. FastAPI has
no built-in protection, and the add-on packages use a weaker method the guide discourages.

Each browser gets a session: a random ID in a cookie, and on the server, that session's
secret code. The code never goes in a cookie or a web address, because browsers send
cookies to this app by themselves, whoever asked them to, and addresses end up in logs.
Pages carry it in the HTML instead: HTMX sends it as a header, and plain forms in a
hidden field. Sessions live in the server's memory, so a restart ends them, and a page
from before the restart gets refused until it's reloaded.

As OWASP also advises, a request saying it came from another website is refused even
with the right code, and the session cookie has SameSite=Lax, so browsers leave it out
of forms sent from other websites.
"""

import logging
import secrets
from urllib.parse import urlsplit

from fastapi.responses import HTMLResponse

SESSION_COOKIE = "inbox_agent_session"
TOKEN_HEADER = "X-CSRF-Token"
TOKEN_FIELD = "csrf_token"

# Requests that only ask to see something. Nothing behind them may change anything.
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")

# The app has one user, so this is plenty. The limit stops the memory growing without end
# if something keeps opening pages without keeping the cookie. The oldest session goes first.
MAX_SESSIONS = 100

log = logging.getLogger("uvicorn.error")

_sessions = {}  # {session ID: that session's secret code}. A dictionary keeps the order things were added


def csrf_context(request):
    """The session's secret code, for the templates. A new browser gets a new session here.

    Only pages that are drawn from a template need one, so files like the CSS don't start
    a session. The cookie itself is set on the way out, in protect_against_other_websites().
    """
    session_id = request.cookies.get(SESSION_COOKIE)
    if session_id not in _sessions:
        if len(_sessions) >= MAX_SESSIONS:
            _sessions.pop(next(iter(_sessions)))
        session_id = secrets.token_urlsafe(32)
        _sessions[session_id] = secrets.token_urlsafe(32)
        request.state.new_session_id = session_id
    return {"csrf_token": _sessions[session_id]}


async def protect_against_other_websites(request, call_next):
    """Refuse any request that could change something, unless it came from one of this app's own pages.

    It runs for every request, so a new POST address is protected without having to remember it.
    """
    if request.method not in SAFE_METHODS and not (_came_from_this_app(request) and await _has_right_code(request)):
        log.warning("Refused a %s to %s: it didn't come from this app's own page", request.method, request.url.path)
        return _refusal(request)
    response = await call_next(request)
    new_session_id = getattr(request.state, "new_session_id", None)
    if new_session_id:
        # HttpOnly keeps scripts from reading the cookie. There's no Secure setting, because this
        # app runs on plain http://127.0.0.1. A public site would need it, and HTTPS.
        response.set_cookie(SESSION_COOKIE, new_session_id, httponly=True, samesite="lax")
    return response


def _came_from_this_app(request):
    """Browsers say which website a request came from. A request with no Origin still needs the right code."""
    origin = request.headers.get("origin")
    return origin is None or urlsplit(origin).netloc == request.headers.get("host")


async def _has_right_code(request):
    """Whether the request carries its session's secret code, in the header or in a form's hidden field."""
    expected = _sessions.get(request.cookies.get(SESSION_COOKIE))
    if expected is None:
        return False
    sent = request.headers.get(TOKEN_HEADER)
    if sent is None:
        # body() first keeps a copy of what was sent. Reading the form on its own would use it
        # up, and the page's own function would then find the form empty.
        await request.body()
        sent = (await request.form()).get(TOKEN_FIELD)
    # compare_digest takes the same time wherever the first difference is, so timing can't give the code away.
    return isinstance(sent, str) and secrets.compare_digest(expected.encode(), sent.encode())


def _refusal(request):
    """The answer to a refused request. Nothing has happened, and a page that's only out of date gets reloaded."""
    headers = {"HX-Refresh": "true"} if request.headers.get("HX-Request") else None
    message = (
        "<p>Nothing happened: this page was out of date, or the request didn't come from Inbox Agent itself.</p>"
        '<p><a href="/">Back to Inbox Agent</a></p>'
    )
    return HTMLResponse(message, status_code=403, headers=headers)
