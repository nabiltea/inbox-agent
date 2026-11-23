"""Drafting a reply from the web page, behind the "Draft reply" link on Action needed.

The draft page shows the conversation, read from Gmail, and a box for my hints. The
Drafter itself (drafter.py) starts working from this page in part 3 of piece 6.
"""

import re
from dataclasses import dataclass
from email.utils import parseaddr

from gmail_client import (
    EmailGoneError,
    GoogleTemporaryError,
    GoogleUnreachableError,
    SignInNeededError,
    explain_failures,
    get_gmail_service,
    get_thread,
)

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
