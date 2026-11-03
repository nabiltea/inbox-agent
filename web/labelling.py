"""Changing an email's label by hand, behind the buttons on the Unsorted page, and undoing that from the History page.

A change goes in the same order as sorting (apply_sorted() in triage.py): first Gmail,
then the log. It only takes a few quick requests to Gmail, so unlike the Sort button,
the click simply waits for the answer.
"""

import logging
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
    list_labels,
)
from triage import ARCHIVE_SORTED_EMAILS, UNSORTED, add_label, load_labels, remove_label

log = logging.getLogger("uvicorn.error")

# How long a new label's name and description can be. My longest description is 643
# characters, so this leaves room to write one by hand. Claude's suggestions are far shorter.
MAX_NAME = 40
MAX_DESCRIPTION = 700

_one_at_a_time = threading.Lock()  # so two clicks at the same moment can't both change the same email


class NoSuchLabelError(ValueError):
    """The label isn't one of mine, for example because the page it came from is out of date."""


class BadLabelError(ValueError):
    """A new label that can't be saved. The message says why, in words for the page."""


class SameLabelError(ValueError):
    """The email already has that label, for example because the page it came from is out of date."""


class NotNewestChangeError(RuntimeError):
    """A newer change to the same email has to be undone first, for example because the page is out of date."""


class AlreadyNeedsMeError(RuntimeError):
    """The email is already marked or on my to-do list, for example because the page is out of date. The message says which."""


@dataclass
class Changed:
    """What happened to an email after a click, for its row on the page."""

    subject: str
    label: str | None  # the label it moved to. None when I rejected the suggestion, so it stays under Unsorted
    left_inbox: bool
    still_needs_me: bool  # it needs me and it's in my inbox, which is why it stays there
    label_created: bool = False  # this click added the label to labels.json


@dataclass
class Undone:
    """What undoing a change did, for its row on the page."""

    subject: str
    old_label: str  # the label it has again
    waiting_for_label: bool  # it's back on the Unsorted page. Not if it's still rejected, from before the undone change
    back_in_inbox: bool
    email_gone: bool  # it isn't in Gmail any more, so only the log changed
    was_mark: bool = False  # what got undone was marking it as needing action, so its label never changed
    out_of_inbox: bool = False  # the mark had put it back in the inbox, and now it has left again
    created_label: str | None = None  # the label the undone click added to labels.json, if it did
    others_with_label: int = 0  # how many other emails have that label, which is why it stays
    label_stuck: bool = False  # labels.json couldn't be changed, so the label is still there


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


def _label_id(service, label_ids, name):
    """The ID of a label that should be in Gmail, which gets made again if I've deleted it there."""
    return label_ids.get(name) or get_or_create_labels(service, [name])[name]


def _move(conn, service, email, label, action, label_created=False):
    """Move an email from the label it has to another: change it in Gmail, then record it in the log."""
    message_id = email["message_id"]
    with explain_failures():
        label_ids = {known["name"]: known["id"] for known in list_labels(service)}
        new_id = _label_id(service, label_ids, label)
        old_id = label_ids.get(email["label"])  # None if I've deleted it in Gmail, and then there's nothing to take off
        labels_now = current_labels(service, message_id)
        # If the email already has this label (for example from my own Gmail filter),
        # note that, so undo won't remove a label this change didn't add.
        label_added = new_id not in labels_now
        # An email waiting for a label stays in the inbox only until I've decided. Any other email
        # in my inbox is there because it needs me or I put it there, so fixing its label leaves it.
        left_inbox = email["label"] == UNSORTED and _leaves_inbox(email, labels_now)
        add = [new_id] if label_added else []
        remove = [old_id] if old_id in labels_now else []
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


def change_label(message_id, label):
    """Give any email in the log another of my labels, for example because Claude chose the wrong one.

    It stays recorded as a change, with Claude's answer as the old label, which Step 8's accuracy
    tests can use. Returns what happened, or None if the log doesn't know the email any more,
    for example because its run has been undone.
    """
    if label not in load_labels():
        raise NoSuchLabelError(label)
    with _one_at_a_time, closing(database.connect()) as conn:
        email = database.listed_email(conn, message_id)
        if email is None:
            return None
        if email["label"] == label:
            raise SameLabelError(label)
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


def mark_needs_action(message_id):
    """Put an email the agent missed on my to-do list: back in the inbox if it isn't there, and marked in the log.

    An email only shows on the list if it's in my inbox. Gmail gets asked, because I may have
    moved it myself since. Claude's answer stays as it is in the log, beside my mark.

    Returns True, or None if the log doesn't know the email any more, for example because its run has been undone.
    """
    with _one_at_a_time, closing(database.connect()) as conn:
        email = database.listed_email(conn, message_id)
        if email is None:
            return None
        if email["marked"]:
            raise AlreadyNeedsMeError("it's already marked as needing action.")
        service = get_gmail_service(allow_browser_sign_in=False)
        with explain_failures():
            labels_now = current_labels(service, message_id)
            if email["flagged"] and "INBOX" in labels_now:
                raise AlreadyNeedsMeError("it's already on your to-do list.")
            entered_inbox = "INBOX" not in labels_now
            if entered_inbox:
                change_labels(service, message_id, add=["INBOX"])
        database.save_needs_action(conn, message_id, entered_inbox)
    return True


def _put_back_in_gmail(service, change):
    """Reverse a change in Gmail. Repeating this is harmless, so if it fails partway, the same click finishes it."""
    add, remove = [], []
    if change["new_label"] != change["old_label"]:  # rejecting or marking keeps the label, so then there's none to swap
        label_ids = {label["name"]: label["id"] for label in list_labels(service)}
        add.append(_label_id(service, label_ids, change["old_label"]))
        # The new label only comes off if this change added it, and if it's still in Gmail.
        if change["new_label_added"] and change["new_label"] in label_ids:
            remove.append(label_ids[change["new_label"]])
    if change["left_inbox"]:
        add.append("INBOX")
    if change["entered_inbox"]:
        remove.append("INBOX")
    if add or remove:
        change_labels(service, change["message_id"], add=add, remove=remove)


def _forget_new_label(conn, label, undone):
    """After undoing the click that made a label: take it out of labels.json, unless another email in the log has it.

    Otherwise a label approved by mistake would stay in Claude's instructions for every future
    email. It stays in Gmail, empty, because deleting a label there can't be undone, and it may
    be an old one of mine, on emails the log knows nothing about. By now the email is undone in
    Gmail and the log, so a problem with the file only gets mentioned.
    """
    undone.created_label = label
    undone.others_with_label = database.emails_with_label(conn, label)
    if undone.others_with_label:
        return
    try:
        remove_label(label)
    except (OSError, ValueError, KeyError):
        log.exception("Couldn't take a label out of labels.json")
        undone.label_stuck = True


def undo_change(change_id):
    """Undo one of my changes: the email goes back to how it was, first in Gmail, then in the log.

    Only an email's newest change can be undone (see _MY_CHANGES in database.py). An email that
    isn't in Gmail any more has nothing to change there, so only the log changes.

    Returns what happened, or None if there's nothing to undo any more, for example after a second click.
    """
    with _one_at_a_time, closing(database.connect()) as conn:
        change = database.my_change(conn, change_id)
        if change is None:
            return None
        if not change["newest"]:
            raise NotNewestChangeError(change_id)
        service = get_gmail_service(allow_browser_sign_in=False)
        email_gone = False
        try:
            with explain_failures():
                _put_back_in_gmail(service, change)
        except EmailGoneError:
            email_gone = True
        database.save_undone_change(conn, change)
        undone = Undone(
            subject=change["subject"],
            old_label=change["old_label"],
            waiting_for_label=database.unsorted_email(conn, change["message_id"]) is not None,
            back_in_inbox=bool(change["left_inbox"]) and not email_gone,
            email_gone=email_gone,
            was_mark=change["action"] == "needs_action",
            out_of_inbox=bool(change["entered_inbox"]) and not email_gone,
        )
        if change["label_created"]:
            _forget_new_label(conn, change["new_label"], undone)
    return undone
