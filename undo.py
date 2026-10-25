"""Undo the most recent run: "python triage.py --apply" or "python backlog.py apply".

Removes the labels the agent added in Gmail during that run, moves the emails it
archived back into the inbox, and forgets those decisions so the emails get sorted
again next time. Labels an email already had before the run (for example from my
own Gmail filters) are left alone. Labels the agent created stay in Gmail, empty,
so you can delete them by hand if you want.
"""

import database
from gmail_client import change_labels_bulk, get_gmail_service, list_labels


def main():
    conn = database.connect()
    decisions = database.last_run(conn)
    if not decisions:
        print("Nothing to undo.")
        return

    run_id = decisions[0]["run_id"]
    print(f"Undoing run {run_id} ({len(decisions)} emails)...")

    service = get_gmail_service()
    label_ids = {label["name"]: label["id"] for label in list_labels(service)}

    # Group emails that need exactly the same change, so Gmail can do up to 1,000 per request.
    groups = {}
    for row in decisions:
        remove_label = bool(row["label_added"]) and row["label"] in label_ids
        key = (label_ids[row["label"]] if remove_label else None, bool(row["archived"]))
        groups.setdefault(key, []).append(row["message_id"])

    labels_removed = back_in_inbox = 0
    for (label_id, archived), message_ids in groups.items():
        add = ["INBOX"] if archived else []
        remove = [label_id] if label_id else []
        if add or remove:
            change_labels_bulk(service, message_ids, add=add, remove=remove)
        labels_removed += len(message_ids) if label_id else 0
        back_in_inbox += len(message_ids) if archived else 0

    database.delete_run(conn, run_id)
    print(f"Removed {labels_removed} labels and moved {back_in_inbox} emails back into the inbox.")
    print("These emails will be sorted again on the next run.")


if __name__ == "__main__":
    main()
