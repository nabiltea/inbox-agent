"""Changing an email's label by hand, behind the buttons on the Unsorted page.

A change goes in the same order as sorting (apply_sorted() in triage.py): first Gmail,
then the log. It only takes a few quick requests to Gmail, so unlike the Sort button,
the click simply waits for the answer.
"""

import threading
from contextlib import closing
from dataclasses import dataclass

import database
from full_read import remove_invisible
from gmail_client import (
    EmailGoneError,
    change_labels,
    create_label_if_missing,
    current_labels,
    explain_failures,
    get_gmail_service,
    get_or_create_labels,
)
from triage import ARCHIVE_SORTED_EMAILS, UNSORTED, add_label, load_labels

# How long a new label's name and description can be. My longest description is 643
# characters, so this leaves room to write one by hand. Claude's suggestions are far shorter.
MAX_NAME = 40
MAX_DESCRIPTION = 700

_one_at_a_time = threading.Lock()  # so two clicks at the same moment can't both change the same email


class NoSuchLabelError(ValueError):
    """The label isn't one of mine, for example because the page it came from is out of date."""


class BadLabelError(ValueError):
    """A new label that can't be saved. The message says why, in words for the page."""


@dataclass
class Changed:
    """What happened to an email after a click, for its row on the page."""

    subject: str
    label: str | None  # the label it moved to. None when I rejected the suggestion, so it stays under Unsorted
    left_inbox: bool
    still_needs_me: bool  # it needs me and it's in my inbox, which is why it stays there
    label_created: bool = False  # this click added the label to labels.json


def clean(text):
    """A label's name or description as it would be saved: one line, with nothing invisible in it."""
    return remove_invisible(text or "").strip()


def _check_new_label(name, description):
    """Raise BadLabelError if a new label's name and description, already cleaned, can't be saved.

    A description joins the instructions Claude gets for every future email. Claude writes a
    suggestion after reading an outside email, so a crafted email could try to plant a sentence
    there ("stored prompt injection"). I'm the guard, so what I see must be exactly what gets
    saved. That's why clean() takes out invisible characters and line breaks, so it can't pose
    as another line of the instructions, and why the length has a limit.
    """
    if not name:
        raise BadLabelError("a label needs a name.")
    if not description:
        raise BadLabelError("a label needs a description, because that's how Claude knows what it's for.")
    if len(name) > MAX_NAME:
        raise BadLabelError(f"a name can be up to {MAX_NAME} characters, and this one has {len(name)}. Use Edit to shorten it.")
    if len(description) > MAX_DESCRIPTION:
        raise BadLabelError(f"a description can be up to {MAX_DESCRIPTION} characters, and this one has {len(description)}. Use Edit to shorten it.")
    if "/" in name:
        raise BadLabelError("a name can't contain /, because that's how Gmail puts one label inside another.")
    if name.lower() == UNSORTED.lower():
        raise BadLabelError(f"{UNSORTED} is what the agent calls emails with no label. Choose another name.")


def _leaves_inbox(email, labels_now):
    """Whether my choice takes an email out of the inbox: if it's there, and it doesn't need me.

    The same rule as sorting. Its age doesn't matter here: the 30-day rule is only for
    first-time sorting. An email that's already out of the inbox stays out.
    """
    return ARCHIVE_SORTED_EMAILS and "INBOX" in labels_now and not email["needs_me"]


def _move(conn, service, email, label, action, label_created=False):
    """Move an email from Unsorted to a label: change it in Gmail, then record it in the log."""
    message_id = email["message_id"]
    with explain_failures():
        label_ids = get_or_create_labels(service, [label, UNSORTED])
        labels_now = current_labels(service, message_id)
        # If the email already has this label (for example from my own Gmail filter),
        # note that, so undo won't remove a label this change didn't add.
        label_added = label_ids[label] not in labels_now
        left_inbox = _leaves_inbox(email, labels_now)
        add = [label_ids[label]] if label_added else []
        remove = [label_ids[UNSORTED]] if label_ids[UNSORTED] in labels_now else []
        if left_inbox:
            remove.append("INBOX")  # archiving means removing Gmail's INBOX label
        if add or remove:
            change_labels(service, message_id, add=add, remove=remove)
    database.save_label_change(conn, message_id, action, label, label_added, left_inbox, label_created)
    return Changed(email["subject"], label, left_inbox, "INBOX" in labels_now and bool(email["needs_me"]), label_created)


def move_to_label(message_id, label):
    """Move an Unsorted email to a label I already have.

    Returns what happened, or None if the email isn't waiting for a label any more, for example
    because it has already been moved. Gmail's problems come as gmail_client.py's own errors.
    """
    if label not in load_labels():
        raise NoSuchLabelError(label)
    with _one_at_a_time, closing(database.connect()) as conn:
        email = database.unsorted_email(conn, message_id)
        if email is None:
            return None
        service = get_gmail_service(allow_browser_sign_in=False)
        return _move(conn, service, email, label, "picked")


def approve_suggestion(message_id, edited=None):
    """Give an Unsorted email the label it suggests, or my edited version: a (name, description) pair.

    A name I already have, capitals aside, just moves the email there. Otherwise the new label
    goes to Gmail first and then labels.json, so a problem in Gmail leaves the file alone. If
    moving the email fails after that, the label stays, and the same click finishes the move.

    Returns what happened, or None if the email isn't waiting for a label any more.
    """
    with _one_at_a_time, closing(database.connect()) as conn:
        email = database.unsorted_email(conn, message_id)
        if email is None:
            return None
        suggestion = clean(email["suggested_label"]), clean(email["suggested_description"])
        name, description = (clean(edited[0]), clean(edited[1])) if edited else suggestion
        # What I did is saved for the accuracy tests in Step 8: an edit that changes nothing is still an approval.
        action = "approved" if (name, description) == suggestion else "edited"
        label = {mine.lower(): mine for mine in load_labels()}.get(name.lower())
        label_created = label is None
        if label_created:
            _check_new_label(name, description)
        service = get_gmail_service(allow_browser_sign_in=False)
        if label_created:
            with explain_failures():
                label = create_label_if_missing(service, name)
            add_label(label, description)
        return _move(conn, service, email, label, action, label_created)


def reject_suggestion(message_id):
    """Say that an Unsorted email needs no label: it stays under Unsorted in Gmail and leaves the page's list.

    Gmail still gets asked, because the email leaves the inbox on the same rule as a move. An
    email that isn't in Gmail any more has nothing to change there, so the answer just gets
    recorded, which is also the way to clear such an email off the list.

    Returns what happened, or None if the email isn't waiting for a label any more.
    """
    with _one_at_a_time, closing(database.connect()) as conn:
        email = database.unsorted_email(conn, message_id)
        if email is None:
            return None
        service = get_gmail_service(allow_browser_sign_in=False)
        try:
            with explain_failures():
                labels_now = current_labels(service, message_id)
                left_inbox = _leaves_inbox(email, labels_now)
                if left_inbox:
                    change_labels(service, message_id, remove=["INBOX"])
        except EmailGoneError:
            labels_now, left_inbox = [], False
        database.save_label_change(conn, message_id, "rejected", UNSORTED, email["label_added"], left_inbox)
    return Changed(email["subject"], None, left_inbox, "INBOX" in labels_now and bool(email["needs_me"]))
