"""Sort the whole inbox backlog with Claude's Message Batches API.

Sending thousands of emails to Claude one at a time is slow. The Message Batches
API takes all the requests at once, processes them in the background (usually
within an hour, at most 24 hours) and costs half as much. Speed doesn't matter
for a one-time backlog, so it's the right tool here.

Steps:
  python backlog.py sample 40   dry run: sort 40 random unsorted inbox emails right away, change nothing
  python backlog.py submit      send every unsorted inbox email to Claude as one batch
  python backlog.py wait        check once a minute until the batch is finished
  python backlog.py apply       add the labels and archive in Gmail, and log every decision

Each apply is one run. "python undo.py" reverses the most recent run, so run it
once per batch to undo a backlog that was sorted in several batches.
"""

import argparse
import json
import random
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import anthropic
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request
from dotenv import load_dotenv

import database
from gmail_client import change_labels_bulk, get_emails, get_gmail_service, get_or_create_labels, list_message_ids
from triage import (
    ARCHIVE_SORTED_EMAILS, INPUT_PRICE, LABELS, MODEL, OUTPUT_PRICE, SYSTEM_PROMPT, UNSORTED,
    TriageResult, email_text, print_result, should_archive, triage,
)

load_dotenv()

# Remembers which batch we submitted, so "wait" and "apply" can find it later,
# even after closing the terminal. It's deleted once the results are applied.
BATCH_FILE = Path(__file__).parent / "backlog_batch.json"

# Batches cost half the normal price per token.
BATCH_DISCOUNT = 0.5


def unsorted_inbox_ids(service, conn):
    """The IDs of every inbox email the agent hasn't sorted yet."""
    return [message_id for message_id in list_message_ids(service) if not database.is_processed(conn, message_id)]


def print_summary(results, extra_lines=()):
    """Emails per label, then any extra lines."""
    print("\nSummary")
    for label, number in Counter(result.label for result in results).most_common():
        print(f"  {label:<20} {number}")
    for line in extra_lines:
        print(f"  {line}")


def sample(count):
    """Dry run: sort a random sample right away (at normal price) to check the labels fit."""
    service = get_gmail_service()
    conn = database.connect()
    ids = random.sample(unsorted_inbox_ids(service, conn), count)
    emails = sorted(get_emails(service, ids), key=lambda email: email.date)

    client = anthropic.Anthropic()
    print("Dry run: nothing in Gmail changes\n")
    results = []
    cost = 0.0
    for email in emails:
        response = triage(client, email)
        cost += (response.usage.input_tokens * INPUT_PRICE + response.usage.output_tokens * OUTPUT_PRICE) / 1_000_000
        if response.parsed_output is None:
            continue
        results.append(response.parsed_output)
        print(f"[{email.date:%Y-%m-%d}]")
        print_result(email, response.parsed_output)

    unsorted = sum(result.label == UNSORTED for result in results)
    print_summary(results, [f"Unsorted: {unsorted} of {len(results)}", f"Cost: about ${cost:.4f}"])


def submit():
    """Send every unsorted inbox email to Claude as one batch."""
    if BATCH_FILE.exists():
        sys.exit(f"A batch is already waiting. Run 'python backlog.py apply' first (or delete {BATCH_FILE.name}).")

    service = get_gmail_service()
    conn = database.connect()
    ids = unsorted_inbox_ids(service, conn)
    print(f"Fetching {len(ids)} unsorted inbox emails from Gmail (a few minutes)...")
    emails = get_emails(service, ids)

    # A batch request takes the same settings as a normal call. messages.parse() normally
    # turns TriageResult into a JSON schema for us; here we do that step ourselves.
    schema = anthropic.transform_schema(TriageResult.model_json_schema())
    requests = [
        Request(
            custom_id=email.id,  # links each result back to its email
            params=MessageCreateParamsNonStreaming(
                model=MODEL,
                max_tokens=2000,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": email_text(email)}],
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
            ),
        )
        for email in emails
    ]

    print(f"Submitting {len(requests)} requests to the Message Batches API...")
    batch = anthropic.Anthropic().messages.batches.create(requests=requests)
    BATCH_FILE.write_text(json.dumps(
        {"batch_id": batch.id, "submitted_at": datetime.now().isoformat(timespec="seconds"), "emails": len(requests)},
        indent=2,
    ))
    print(f"Submitted batch {batch.id}. Next: python backlog.py wait")


def load_batch_id():
    if not BATCH_FILE.exists():
        sys.exit("No batch is waiting. Start with: python backlog.py submit")
    return json.loads(BATCH_FILE.read_text())["batch_id"]


def wait():
    """Check the batch once a minute until it's finished."""
    batch_id = load_batch_id()
    client = anthropic.Anthropic()
    while True:
        batch = client.messages.batches.retrieve(batch_id)
        counts = batch.request_counts
        print(
            f"{datetime.now():%H:%M} {batch.processing_status}: {counts.succeeded} done, "
            f"{counts.processing} processing, {counts.errored} errored",
            flush=True,  # print right away, even when the output goes to a file
        )
        if batch.processing_status == "ended":
            print("Finished. Next: python backlog.py apply")
            return
        time.sleep(60)


def apply():
    """Apply the batch's results in Gmail, and log every decision."""
    batch_id = load_batch_id()
    client = anthropic.Anthropic()
    if client.messages.batches.retrieve(batch_id).processing_status != "ended":
        sys.exit("The batch isn't finished yet. Run: python backlog.py wait")

    # 1. Read Claude's answers. Each result carries the custom_id we gave it: the email's ID.
    results, costs, failed = {}, {}, 0
    for item in client.messages.batches.results(batch_id):
        if item.result.type != "succeeded":  # errored, expired or canceled
            failed += 1
            continue
        message = item.result.message
        usage = message.usage
        costs[item.custom_id] = (
            (usage.input_tokens * INPUT_PRICE + usage.output_tokens * OUTPUT_PRICE) / 1_000_000 * BATCH_DISCOUNT
        )
        if message.stop_reason != "end_turn":  # declined, or the answer was cut off
            failed += 1
            continue
        text = next(block.text for block in message.content if block.type == "text")
        results[item.custom_id] = TriageResult.model_validate_json(text)  # JSON text -> TriageResult

    # 2. Get each email's current labels from Gmail, in case anything changed since submitting.
    service = get_gmail_service()
    conn = database.connect()
    todo = [message_id for message_id in results if not database.is_processed(conn, message_id)]
    print(f"{len(results)} emails sorted, {failed} failed. Fetching {len(todo)} from Gmail to apply...")
    emails = get_emails(service, todo)
    label_ids = get_or_create_labels(service, [*LABELS, UNSORTED])

    # 3. Group emails that need exactly the same change, so Gmail can do up to 1,000 per request.
    groups = {}
    for email in emails:
        result = results[email.id]
        label_id = label_ids[result.label]
        label_added = label_id not in email.label_ids
        archived = ARCHIVE_SORTED_EMAILS and should_archive(email, result) and "INBOX" in email.label_ids
        key = (label_id if label_added else None, archived)
        groups.setdefault(key, []).append((email, result, label_added, archived))

    # 4. Apply each group in Gmail, then log its emails. The whole backlog is one run,
    #    so "python undo.py" can reverse it in one go.
    run_id = f"backlog-{datetime.now().isoformat(timespec='seconds')}"
    for (label_id, archive), items in groups.items():
        add = [label_id] if label_id else []
        remove = ["INBOX"] if archive else []
        if add or remove:
            change_labels_bulk(service, [email.id for email, *_ in items], add=add, remove=remove)
        for email, result, label_added, archived in items:
            database.save_decision(conn, run_id, email, result, label_added, archived, MODEL, costs[email.id])

    archived_count = sum(archived for items in groups.values() for *_, archived in items)
    sorted_results = [result for items in groups.values() for _, result, *_ in items]
    print_summary(sorted_results, [
        f"Archived:            {archived_count}",
        f"Left in inbox:       {len(sorted_results) - archived_count}",
        f"Failed (next run):   {failed}",
        f"Cost: about ${sum(costs.values()):.2f} (half price)",
    ])
    BATCH_FILE.unlink()  # this batch is done
    # Each apply is its own run, so undo.py reverses this batch only. Running it again
    # reverses the run before.
    print("\nDone. To undo this batch: python undo.py")


def main():
    parser = argparse.ArgumentParser(description="Sort the inbox backlog with the Message Batches API.")
    parser.add_argument("step", choices=["sample", "submit", "wait", "apply"])
    parser.add_argument("count", nargs="?", type=int, default=40, help="how many emails for 'sample' (default 40)")
    args = parser.parse_args()

    if args.step == "sample":
        sample(args.count)
    elif args.step == "submit":
        submit()
    elif args.step == "wait":
        wait()
    else:
        apply()


if __name__ == "__main__":
    main()
