"""Step 2: sign in to Gmail and print the 20 newest emails in your inbox."""

from gmail_client import get_gmail_service, list_recent_emails

service = get_gmail_service()
emails = list_recent_emails(service, max_results=20)

for email in emails:
    print(f"{email.date:%d %b %H:%M}  {email.sender[:25]:<25}  {email.subject[:70]}")

print(f"\n{len(emails)} emails")
