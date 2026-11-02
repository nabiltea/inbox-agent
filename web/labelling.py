"""Changing an email's label by hand, behind the buttons on the Unsorted page.

A change goes in the same order as sorting (apply_sorted() in triage.py): first Gmail,
then the log. It only takes a few quick requests to Gmail, so unlike the Sort button,
the click simply waits for the answer.
"""

import threading
from contextlib import closing
from dataclasses import dataclass

import database
from gmail_client import (
    EmailGoneError,
    change_labels,
    current_labels,
    explain_failures,
    get_gmail_service,
    get_or_create_labels,
)
from triage import ARCHIVE_SORTED_EMAILS, UNSORTED, load_labels

_one_at_a_time = threading.Lock()  # so two clicks at the same moment can't both change the same email


class NoSuchLabelError(ValueError):
    """The label isn't one of mine, for example because the page it came from is out of date."""


@dataclass
class Changed:
    """What happened to an email after a click, for its row on the page."""

    subject: str
    label: str | None  # the label it moved to. None when I rejected the suggestion, so it stays under Unsorted
    left_inbox: bool
    still_needs_me: bool  # it needs me and it's in my inbox, which is why it stays there


def _leaves_inbox(email, labels_now):
    """Whether my choice takes an email out of the inbox: if it's there, and it doesn't need me.

    The same rule as sorting. Its age doesn't matter here: the 30-day rule is only for
    first-time sorting. An email that's already out of the inbox stays out.
    """
    return ARCHIVE_SORTED_EMAILS and "INBOX" in labels_now and not email["needs_me"]


def move_to_label(message_id, label):
    """Move an Unsorted email to a label I already have: change it in Gmail, then record it in the log.

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
        database.save_label_change(conn, message_id, "picked", label, label_added, left_inbox)
    return Changed(email["subject"], label, left_inbox, "INBOX" in labels_now and bool(email["needs_me"]))


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
