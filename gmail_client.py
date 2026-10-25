"""Everything that talks to Gmail lives in this file.

The rest of the project calls these functions and never deals with Google's
login or API details directly. In a later step we'll wrap these same
functions in an MCP server.
"""

import html
import time
from dataclasses import dataclass
from datetime import datetime
from email.utils import parseaddr
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

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


def get_gmail_service():
    """Log in to Gmail and return an object for making Gmail API calls."""
    creds = _load_saved_login()
    if creds is None:
        creds = _sign_in()
    return build("gmail", "v1", credentials=creds)


def list_recent_emails(service, max_results=20, label_id="INBOX"):
    """Return the newest emails with the given label (the inbox by default), newest first."""
    ids = list_message_ids(service, label_id=label_id, max_results=max_results)
    return get_emails(service, ids)


def list_message_ids(service, label_id="INBOX", query=None, max_results=None):
    """Return the IDs of emails with the given label, newest first.

    query uses Gmail's search syntax, for example "after:2021/10/22". Gmail returns
    at most 500 IDs per request, so this keeps asking for the next "page" of results
    until it has them all, or max_results of them.
    """
    ids = []
    page_token = None
    while True:
        page_size = 500 if max_results is None else min(500, max_results - len(ids))
        response = service.users().messages().list(
            userId="me", labelIds=[label_id], q=query, maxResults=page_size, pageToken=page_token
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
        print(f"Gmail's per-minute limit reached after {len(found)} emails. Waiting a minute...", flush=True)
        time.sleep(60)

    if to_fetch or skipped:
        print(f"Couldn't fetch {len(to_fetch) + len(skipped)} emails from Gmail. They're skipped this time.")
    return [found[message_id] for message_id in message_ids if message_id in found]


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
            print(f"Created label in Gmail: {name}")
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


def _to_email(message):
    """Turn Gmail's raw message data into our simpler Email object."""
    headers = {h["name"].lower(): h["value"] for h in message["payload"]["headers"]}
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
