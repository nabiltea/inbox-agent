"""Everything that talks to Gmail lives in this file.

The rest of the project calls these functions and never deals with Google's
login or API details directly. gmail_mcp_server.py offers some of them to AI
apps as MCP tools.

Messages for the person running a script go to stderr (print(..., file=sys.stderr)),
never stdout: an MCP server uses stdout to talk to the app, and any stray text
there would break the conversation.
"""

import base64
import html
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr, getaddresses, parseaddr
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

PROJECT_DIR = Path(__file__).parent
CREDENTIALS_FILE = PROJECT_DIR / "credentials.json"  # identifies this app to Google
TOKEN_FILE = PROJECT_DIR / "token.json"  # your saved login, created after the first sign-in

# What we ask Google permission to do. "gmail.modify" lets the app read emails,
# create labels and change which labels an email has. It's the narrowest Google
# permission that allows changing labels on emails, but it also covers drafts and
# sending. So the real safety boundary is this file: it has no function that sends
# or deletes anything. (Changing this list makes you sign in again.)
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


@dataclass
class Email:
    id: str
    thread_id: str
    sender: str  # the sender's name, or their address if the email has no name
    sender_address: str
    subject: str
    date: datetime
    snippet: str  # the first line or so of the email body, as Gmail shows in the inbox
    label_ids: list[str]  # the labels the email already has, including Gmail's own like INBOX


def get_gmail_service(allow_browser_sign_in=True):
    """Log in to Gmail and return an object for making Gmail API calls.

    If the saved login doesn't work, this opens a browser to sign in again, unless
    allow_browser_sign_in is False. The MCP server runs in the background, where
    nobody would see the browser, so it asks you to sign in from a script instead.
    """
    creds = _load_saved_login()
    if creds is None:
        if not allow_browser_sign_in:
            raise RuntimeError("The Gmail login has expired. Run 'python show_inbox.py' once to sign in again.")
        creds = _sign_in()
    # cache_discovery=False switches off a cache this setup doesn't support (and its warning).
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def list_recent_emails(service, max_results=20, label_id="INBOX"):
    """Return the newest emails with the given label (the inbox by default), newest first."""
    ids = list_message_ids(service, label_id=label_id, max_results=max_results)
    return get_emails(service, ids)


def list_message_ids(service, label_id="INBOX", query=None, max_results=None):
    """Return the IDs of emails with the given label, newest first. label_id=None searches all mail.

    query uses Gmail's search syntax, for example "after:2021/10/22". Gmail returns
    at most 500 IDs per request, so this keeps asking for the next "page" of results
    until it has them all, or max_results of them.
    """
    ids = []
    page_token = None
    while True:
        page_size = 500 if max_results is None else min(500, max_results - len(ids))
        response = service.users().messages().list(
            userId="me",
            labelIds=[label_id] if label_id else None,
            q=query,
            maxResults=page_size,
            pageToken=page_token,
        ).execute()
        ids += [item["id"] for item in response.get("messages", [])]
        page_token = response.get("nextPageToken")  # None when there are no more pages
        if page_token is None or (max_results is not None and len(ids) >= max_results):
            return ids


def get_emails(service, message_ids):
    """Fetch the sender, subject and preview of many emails, in the same order as the IDs.

    Fetching emails one at a time takes one request each. Instead, this sends them
    to Gmail in bundles of 50 ("batch requests"). Gmail limits how much one account
    can fetch per minute. When it refuses because of that limit, this waits a minute
    for the limit to reset and carries on where it stopped.
    """
    found = {}
    retry = []  # refused for a temporary reason, like the per-minute limit
    skipped = []  # refused for good, for example because the email was deleted

    def save(request_id, response, exception):
        # Gmail calls this once for each email in a bundle, with either the email or an error.
        if exception is None:
            found[request_id] = _to_email(response)
        elif _is_temporary(exception):
            retry.append(request_id)
        else:
            skipped.append(request_id)

    to_fetch = list(message_ids)
    for _ in range(30):  # at most 30 one-minute waits
        retry.clear()
        for start in range(0, len(to_fetch), 50):
            batch = service.new_batch_http_request(callback=save)
            for message_id in to_fetch[start:start + 50]:
                batch.add(
                    service.users().messages().get(
                        userId="me", id=message_id, format="metadata", metadataHeaders=["From", "Subject"]
                    ),
                    request_id=message_id,
                )
            batch.execute()
            if retry:
                # Limit reached: keep the refused emails and everything not tried yet for later.
                to_fetch = retry + to_fetch[start + 50:]
                break
        else:
            to_fetch = []  # the loop got through every bundle without a refusal
        if not to_fetch:
            break
        print(f"Gmail's per-minute limit reached after {len(found)} emails. Waiting a minute...", file=sys.stderr, flush=True)
        time.sleep(60)

    if to_fetch or skipped:
        print(f"Couldn't fetch {len(to_fetch) + len(skipped)} emails from Gmail. They're skipped this time.", file=sys.stderr)
    return [found[message_id] for message_id in message_ids if message_id in found]


def _execute(request, attempts=5):
    """Run one Gmail request. If Gmail refuses because of its per-minute limit, wait a minute and try again."""
    for attempt in range(attempts):
        try:
            return request.execute()
        except HttpError as error:
            if not _is_temporary(error) or attempt == attempts - 1:
                raise
            print("Gmail's per-minute limit reached. Waiting a minute...", file=sys.stderr, flush=True)
            time.sleep(60)


def _is_temporary(error):
    """True for Gmail errors worth retrying later: rate limits and brief outages."""
    status = getattr(getattr(error, "resp", None), "status", None)
    reason = str(getattr(error, "reason", "")).lower()
    return status in (429, 500, 503) or (status == 403 and ("quota" in reason or "rate" in reason))


def list_labels(service):
    """Return the labels I made myself in Gmail, as a list of {"id": ..., "name": ...}."""
    response = service.users().labels().list(userId="me").execute()
    # Gmail's built-in labels like INBOX and SPAM have type "system". The ones I made have type "user".
    return [
        {"id": label["id"], "name": label["name"]}
        for label in response.get("labels", [])
        if label["type"] == "user"
    ]


def get_or_create_labels(service, names):
    """Return {label name: label ID} for the given names, creating any that don't exist in Gmail yet."""
    response = service.users().labels().list(userId="me").execute()
    ids = {label["name"]: label["id"] for label in response.get("labels", [])}
    for name in names:
        if name not in ids:
            created = service.users().labels().create(
                userId="me",
                body={"name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show"},
            ).execute()
            ids[name] = created["id"]
            print(f"Created label in Gmail: {name}", file=sys.stderr)
    return {name: ids[name] for name in names}


def change_labels(service, message_id, add=(), remove=()):
    """Add and/or remove labels on one email, in a single request.

    Archiving in Gmail just means removing the INBOX label: the email stays in
    All Mail and under its other labels, so nothing is lost and storage doesn't change.
    """
    service.users().messages().modify(
        userId="me", id=message_id, body={"addLabelIds": list(add), "removeLabelIds": list(remove)}
    ).execute()


def change_labels_bulk(service, message_ids, add=(), remove=()):
    """Make the same label change to many emails at once, up to 1,000 per request."""
    for start in range(0, len(message_ids), 1000):
        service.users().messages().batchModify(
            userId="me",
            body={"ids": message_ids[start:start + 1000], "addLabelIds": list(add), "removeLabelIds": list(remove)},
        ).execute()


def get_my_address(service):
    """My own email address."""
    return service.users().getProfile(userId="me").execute()["emailAddress"]


def get_thread(service, thread_id, max_chars_per_message=4000):
    """Return a whole conversation: its subject, and each message's sender, recipients, date and text.

    Each message says whether I sent it ("from_me"). Long messages are cut to
    max_chars_per_message characters, mostly to drop the quoted copies of earlier
    messages that replies tend to repeat at the bottom.
    """
    thread = _execute(service.users().threads().get(userId="me", id=thread_id, format="full"))
    me = get_my_address(service).lower()
    messages = []
    for message in thread["messages"]:
        headers = _headers(message)
        body = _extract_body(message["payload"])
        if len(body) > max_chars_per_message:
            body = body[:max_chars_per_message] + "\n[... cut off ...]"
        messages.append({
            "id": message["id"],
            "from": headers.get("from", ""),
            "from_me": parseaddr(headers.get("from", ""))[1].lower() == me,
            "to": headers.get("to", ""),
            "cc": headers.get("cc", ""),
            "date": datetime.fromtimestamp(int(message["internalDate"]) / 1000).isoformat(timespec="minutes"),
            "body": body,
        })
    subject = _headers(thread["messages"][0]).get("subject", "(no subject)")
    return {"thread_id": thread_id, "subject": subject, "messages": messages}


def get_message(service, message_id):
    """One email with its sender, recipients, subject, date and full text."""
    message = _execute(service.users().messages().get(userId="me", id=message_id, format="full"))
    headers = _headers(message)
    return {
        "id": message["id"],
        "thread_id": message["threadId"],
        "from": headers.get("from", ""),
        "to": headers.get("to", ""),
        "subject": headers.get("subject") or "(no subject)",
        "date": datetime.fromtimestamp(int(message["internalDate"]) / 1000).isoformat(timespec="minutes"),
        "body": _extract_body(message["payload"]),
    }


def create_reply_draft(service, thread_id, body, reply_all=False):
    """Save a reply to a conversation as a draft. It's never sent: I review and send drafts myself."""
    thread = service.users().threads().get(
        userId="me", id=thread_id, format="metadata",
        metadataHeaders=["From", "To", "Cc", "Reply-To", "Subject", "Message-ID", "References"],
    ).execute()
    reply = build_reply(thread["messages"], get_my_address(service), body, reply_all)
    raw = base64.urlsafe_b64encode(reply.as_bytes()).decode()
    draft = service.users().drafts().create(
        userId="me", body={"message": {"raw": raw, "threadId": thread_id}}
    ).execute()
    return {"draft_id": draft["id"], "to": reply["To"], "cc": reply["Cc"] or "", "subject": reply["Subject"]}


def build_reply(messages, my_address, body, reply_all=False):
    """Build a reply to the newest message in a conversation that someone else sent.

    By default it goes only to that message's sender, like Gmail's Reply. With
    reply_all, everyone else on that message (its To and Cc, except me) goes in Cc,
    like Gmail's Reply all.

    The In-Reply-To and References headers tell email apps which message this
    answers, so the reply shows up inside the same conversation.
    """
    others = [m for m in messages if my_address.lower() not in _headers(m).get("from", "").lower()]
    if others:
        headers = _headers(others[-1])
        to = headers.get("reply-to") or headers.get("from", "")
    else:  # every message is mine, so write to the person I wrote to
        headers = _headers(messages[-1])
        to = headers.get("to", "")

    subject = headers.get("subject", "")
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"

    reply = EmailMessage()
    reply["To"] = to
    reply["Subject"] = subject
    if reply_all:
        # getaddresses splits header text like '"Ann" <ann@x.com>, bob@y.com' into (name, address) pairs.
        others = getaddresses([headers.get("to", ""), headers.get("cc", "")])
        skip = {my_address.lower()} | {address.lower() for _, address in getaddresses([to])}
        cc = [formataddr((name, address)) for name, address in others if address and address.lower() not in skip]
        if cc:
            reply["Cc"] = ", ".join(dict.fromkeys(cc))  # dict.fromkeys drops duplicates, keeping the order
    if headers.get("message-id"):
        reply["In-Reply-To"] = headers["message-id"]
        reply["References"] = f"{headers.get('references', '')} {headers['message-id']}".strip()
    reply.set_content(body)
    return reply


def _load_saved_login():
    """Return the saved login if it still works, otherwise None."""
    if not TOKEN_FILE.exists():
        return None
    creds = Credentials.from_authorized_user_file(TOKEN_FILE)
    if not creds.has_scopes(SCOPES):
        return None  # we now need permissions the saved login doesn't have
    if creds.valid:
        return creds
    try:
        creds.refresh(Request())
    except RefreshError:
        return None  # Google ended the login (every 7 days while the app is in testing)
    TOKEN_FILE.write_text(creds.to_json())
    return creds


def _sign_in():
    """Open a browser window so you can sign in, then save the login for next time."""
    if not CREDENTIALS_FILE.exists():
        raise FileNotFoundError(
            f"{CREDENTIALS_FILE.name} not found. Download it from Google Cloud Console "
            "and put it in the project folder."
        )
    flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
    creds = flow.run_local_server(port=0)
    TOKEN_FILE.write_text(creds.to_json())
    return creds


# Where the quoted older message starts in a reply. Everything from there on was
# written earlier (by someone else, or by me), so strip_quoted cuts it off.
QUOTE_STARTS = [
    # Gmail, Apple Mail: "On Tue, 3 Oct, X wrote:". Long ones wrap, so "wrote:" can be alone on the next line.
    re.compile(r"^On [^\n]+\n?[^\n]*wrote:\s*$", re.M),
    re.compile(r"^Op [^\n]+\n?[^\n]*schreef[^\n]*:\s*$", re.M),  # the same in Dutch
    re.compile(r"^-{2,} ?(Original Message|Forwarded message|Forwarded Conversation|Oorspronkelijk bericht|Doorgestuurd bericht)", re.M | re.I),
    re.compile(r"^Begin forwarded message:", re.M),  # Apple Mail's forwards
    re.compile(r"^Forwarded Conversation\s*$", re.M),  # Gmail's forwards of a whole conversation
    re.compile(r"^(From|Van): [^\n]+\n(Sent|Date|Verzonden|Datum): ", re.M),  # Outlook's quoted header
    re.compile(r"^_{20,}\s*$", re.M),  # Outlook's separator line
]

# Lines added by phones and mail apps, not by the person writing.
APP_LINES = re.compile(r"^\s*(sent from my iphone|verzonden vanaf mijn iphone|sent from outlook[^\n]*|get outlook for [^\n]*)\s*$", re.I | re.M)


def strip_quoted(body):
    """Keep only the new text of an email: cut the quoted older messages and app signatures."""
    text = body.replace("\r\n", "\n")
    cut = min([match.start() for pattern in QUOTE_STARTS if (match := pattern.search(text))], default=len(text))
    text = text[:cut]
    text = "\n".join(line for line in text.split("\n") if not line.startswith(">"))  # ">" marks quoted lines
    text = APP_LINES.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _headers(message):
    """A message's headers as a dictionary with lowercase names, like {"from": ..., "subject": ...}."""
    return {h["name"].lower(): h["value"] for h in message["payload"].get("headers", [])}


def _extract_body(payload):
    """Find the readable text of an email: plain text if there is any, otherwise HTML with the tags removed.

    An email is a tree of parts (plain text, HTML, attachments, parts inside parts),
    so this walks the whole tree.
    """
    plain, html_parts = [], []

    def walk(part):
        data = part.get("body", {}).get("data")
        if data and part.get("mimeType") == "text/plain":
            plain.append(_decode(data))
        elif data and part.get("mimeType") == "text/html":
            html_parts.append(_decode(data))
        for child in part.get("parts", []):
            walk(child)

    walk(payload)
    if plain:
        return "\n".join(plain).strip()
    text = "\n".join(html_parts)
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.S | re.I)  # drop code and styling
    text = re.sub(r"<br\s*/?>|</p>|</div>|</tr>", "\n", text, flags=re.I)  # keep line breaks
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))  # drop the remaining tags
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\s*\n\s*", "\n", text).strip()


def _decode(data):
    """Gmail sends email text in "base64url" encoding. Turn it back into normal text."""
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")


def _to_email(message):
    """Turn Gmail's raw message data into our simpler Email object."""
    headers = _headers(message)
    name, address = parseaddr(headers.get("from", ""))
    return Email(
        id=message["id"],
        thread_id=message["threadId"],
        sender=name or address,
        sender_address=address,
        subject=headers.get("subject") or "(no subject)",
        date=datetime.fromtimestamp(int(message["internalDate"]) / 1000),
        snippet=html.unescape(message.get("snippet", "")),
        label_ids=message.get("labelIds", []),
    )
