"""Everything that talks to Gmail lives in this file.

The rest of the project calls these functions and never deals with Google's
login or API details directly. In a later step we'll wrap these same
functions in an MCP server.
"""

import html
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
    response = service.users().messages().list(
        userId="me", labelIds=[label_id], maxResults=max_results
    ).execute()

    emails = []
    for item in response.get("messages", []):
        # The list call only returns IDs, so fetch each email's sender and subject.
        message = service.users().messages().get(
            userId="me",
            id=item["id"],
            format="metadata",
            metadataHeaders=["From", "Subject"],
        ).execute()
        emails.append(_to_email(message))
    return emails


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
