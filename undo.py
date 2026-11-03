"""Undo a sorting run, from the terminal or from the History page.

Removes the labels the agent added in Gmail during that run, moves the emails it
archived back into the inbox, and forgets those decisions so the emails get sorted
again next time. Labels an email already had before the run (for example from my
own Gmail filters) are left alone. Labels the agent created stay in Gmail, empty,
so you can delete them by hand if you want.

What I've handled myself stays as it is: an email whose label I've changed, that I've
rejected or marked as needing action, or that the agent left in the inbox and I've
archived since.

plan_undo() works out what would happen without changing anything, and apply_undo()
carries it out, as sort_email() and apply_sorted() do for sorting. The web page uses
both (see web/labelling.py), so what its fold promises is what happens.

Run it with:  python undo.py                 undo the most recent run
              python undo.py --list          show all runs
              python undo.py --run RUN_ID    undo one specific run
It says what it would do, and asks first.
"""

import argparse
from dataclasses import dataclass, field

import database
from gmail_client import change_labels_bulk, explain_failures, get_gmail_service, inbox_message_ids, list_labels

# How many emails Gmail takes in one many-at-once request.
BUNDLE_SIZE = 1000


@dataclass
class UndoPlan:
    """What undoing a run would do, worked out without changing anything."""

    run_id: str
    # Emails that need exactly the same change, so Gmail can do up to 1,000 per request:
    # {(the label to take off, or None; whether it goes back to the inbox): their message IDs}
    groups: dict = field(default_factory=dict)
    emails: int = 0  # how many get undone
    back_in_inbox: int = 0
    labels_removed: int = 0
    kept: int = 0  # how many stay as they are, because I've handled them myself
    to_dos: int = 0  # how many leave my to-do list until they're sorted again
    cost: float = 0.0  # what sorting them cost last time, in dollars


def plan_undo(conn, run_id, inbox_ids):
    """Work out what undoing a run would do, without changing anything.

    inbox_ids is what's in my inbox right now, from Gmail. What I've changed by hand stays,
    because my decisions outrank the agent's: a common reason to undo a run is that Claude
    sorted it badly, and then my fixes are the part worth keeping. An email the agent left
    in the inbox and I've archived since stays too. Otherwise it would lose its label and be
    forgotten, but never be sorted again, because only what's in the inbox gets sorted.
    """
    plan = UndoPlan(run_id)
    for row in database.run_emails(conn, run_id):
        archived_by_me = not row["archived"] and row["message_id"] not in inbox_ids
        if row["changed_by_hand"] or archived_by_me:
            plan.kept += 1
            continue
        label = row["label"] if row["label_added"] else None
        plan.groups.setdefault((label, bool(row["archived"])), []).append(row["message_id"])
        plan.emails += 1
        plan.back_in_inbox += bool(row["archived"])
        plan.labels_removed += bool(row["label_added"])
        plan.to_dos += not row["archived"] and bool(row["needs_me"])
        plan.cost += row["cost"]
    return plan


def _how_many(number, one, many):
    return f"{number:,} {one if number == 1 else many}"


def what_undo_would_do(plan):
    """A plan in sentences, the same for the terminal and for the fold on the History page."""
    sentences = []
    if plan.back_in_inbox:
        sentences.append(f"{_how_many(plan.back_in_inbox, 'email goes', 'emails go')} back to your inbox.")
    if plan.labels_removed:
        sentences.append(f"{_how_many(plan.labels_removed, 'email loses', 'emails lose')} the agent's label.")
    if plan.kept:
        sentences.append(
            "1 email stays as it is, because you changed or archived it yourself." if plan.kept == 1
            else f"{plan.kept:,} emails stay as they are, because you changed or archived them yourself."
        )
    if plan.to_dos:
        sentences.append(f"{_how_many(plan.to_dos, 'to-do leaves', 'to-dos leave')} your list until sorted again.")
    # backlog.py's runs cost half price, and that's the cheap way to sort them again.
    if plan.run_id.startswith("backlog-"):
        sentences.append(f"Sorting them again costs about ${plan.cost:.2f} with backlog.py, or about ${plan.cost * 2:.2f} with the Sort button.")
    else:
        sentences.append(f"Sorting them again costs about ${plan.cost:.2f}." if plan.cost >= 0.005 else "Sorting them again costs less than a cent.")
    sentences.append("Emails older than 30 days get archived when sorted again, even if they need you.")
    sentences.append("This can't itself be undone, because the log forgets Claude's answers for these emails.")
    return sentences


def apply_undo(conn, service, plan, on_progress=None):
    """Carry out a plan, one request of up to 1,000 emails at a time.

    Each bundle gets forgotten straight after its request, so the log always matches Gmail. If
    a request fails, what's done stays done, and undoing the run again finishes the job.

    Gmail skips an email that no longer exists, rather than refusing the whole request. Its
    docs don't say so, but it's what a real request with a made-up ID did, so one deleted
    email can't block an undo. on_progress is told how many emails are done after each bundle.
    """
    with explain_failures():
        label_ids = {label["name"]: label["id"] for label in list_labels(service)}
    done = 0
    for (label, back_to_inbox), message_ids in plan.groups.items():
        add = ["INBOX"] if back_to_inbox else []
        remove = [label_ids[label]] if label in label_ids else []  # None, or a label I've deleted in Gmail, isn't in there
        for start in range(0, len(message_ids), BUNDLE_SIZE):
            bundle = message_ids[start:start + BUNDLE_SIZE]
            if add or remove:
                with explain_failures():
                    change_labels_bulk(service, bundle, add=add, remove=remove)
            database.forget_emails(conn, bundle)
            done += len(bundle)
            if on_progress:
                on_progress(done)


def main():
    parser = argparse.ArgumentParser(description="Undo a run of the agent.")
    parser.add_argument("--list", action="store_true", help="show all runs, newest first")
    parser.add_argument("--run", help="the run to undo (default: the most recent one)")
    args = parser.parse_args()

    conn = database.connect()
    runs = database.list_runs(conn)
    if args.list:
        for run in runs:
            print(f"{run['run_id']:<32} {run['emails']:>5} emails")
        return
    if not runs:
        print("Nothing to undo.")
        return
    run_id = args.run or runs[0]["run_id"]

    service = get_gmail_service()
    try:
        plan = plan_undo(conn, run_id, inbox_message_ids(service))
        if not plan.emails:
            print(f"Nothing to undo in run {run_id}." + (f" You changed or archived its {_how_many(plan.kept, 'email', 'emails')} yourself." if plan.kept else ""))
            return
        print(f"Undoing run {run_id} ({_how_many(plan.emails, 'email', 'emails')}):")
        for sentence in what_undo_would_do(plan):
            print(f"  {sentence}")
        if _answer("Type yes to go ahead: ") != "yes":
            print("Nothing changed.")
            return
        apply_undo(conn, service, plan, on_progress=lambda done: print(f"  {done:,} of {plan.emails:,} done", flush=True))
    except RuntimeError as error:  # gmail_client.py's own errors, which say what went wrong
        print(f"Stopped: {error} What's undone stays undone, so run the same command again to finish.")
        return
    print(f"Removed {plan.labels_removed} labels and moved {plan.back_in_inbox} emails back into the inbox.")
    print("These emails will be sorted again on the next run.")


def _answer(question):
    """What I type, in lower case. With nobody there to answer, that's nothing."""
    try:
        return input(question).strip().lower()
    except EOFError:
        return ""


if __name__ == "__main__":
    main()
