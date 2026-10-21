"""Undo the most recent "python triage.py --apply" run.

Removes the labels the agent added in Gmail during that run, moves the emails it
archived back into the inbox, and forgets those decisions so the emails get sorted
again next time. Labels an email already had before the run (for example from my
own Gmail filters) are left alone. Labels the agent created stay in Gmail, empty,
so you can delete them by hand if you want.
"""

import database
from gmail_client import change_labels, get_gmail_service, list_labels


def main():
    conn = database.connect()
    decisions = database.last_run(conn)
    if not decisions:
        print("Nothing to undo.")
        return

    run_id = decisions[0]["run_id"]
    print(f"Undoing run {run_id} ({len(decisions)} emails)\n")

    service = get_gmail_service()
    label_ids = {label["name"]: label["id"] for label in list_labels(service)}

    for row in decisions:
        remove_label = row["label_added"] and row["label"] in label_ids
        add = ["INBOX"] if row["archived"] else []
        remove = [label_ids[row["label"]]] if remove_label else []
        if add or remove:
            change_labels(service, row["message_id"], add=add, remove=remove)

        label_note = "removed label" if remove_label else "kept label   "
        inbox_note = "  (back in inbox)" if row["archived"] else ""
        print(f"{label_note} {row['label']:<20} {row['subject'][:50]}{inbox_note}")

    database.delete_run(conn, run_id)
    print("\nDone. These emails will be sorted again on the next run.")


if __name__ == "__main__":
    main()
