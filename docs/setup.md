# Setup and project files

How to run Inbox Agent yourself, and what each file does. [Back to the README](../README.md).

## Setup

The commands work on Windows (in PowerShell) and on a Mac (in Terminal), unless a step shows one for each. The commands that start with `python` expect the virtual environment from step 2 to be active in that terminal. The Mac commands are for an Apple chip: on an Intel Mac, step 2's `cryptography` package has no ready-made version, and step 15's program is `tailwindcss-macos-x64`. I've only run these steps on Windows so far, so the Mac commands are untested.

1. Install Python 3.13 from python.org. On a Mac, that's `python-3.13.17-macos11.pkg`, the last 3.13 with a Mac installer. Finish by double-clicking **Install Certificates.command** in `/Applications/Python 3.13/`.
2. Create a virtual environment and install the dependencies. `constraints.txt` holds the exact version of every package, so each computer gets the same ones.

   On Windows:
   ```
   py -3.13 -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt -c constraints.txt
   ```
   On a Mac:
   ```
   python3.13 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt -c constraints.txt
   ```
3. Copy `.env.example` to `.env` and paste in your API key from the Claude Console.
4. In Google Cloud Console, turn on the Gmail API, set up the OAuth consent screen with yourself as a test user, and create a **Desktop app** OAuth client. Save its JSON file as `credentials.json` in the project folder.
5. Run `python hello_claude.py` to check the Claude connection.
6. Run `python show_inbox.py` to sign in to Gmail and list your 20 newest emails.
7. Run `python triage.py` to see which label Claude would give each of your 20 newest emails, which ones need a reply or an action, and which fit no label. Add a number to sort more, for example `python triage.py 50`. Nothing in Gmail changes.
8. Run `python label_designer.py` to have Claude Opus suggest a label set from 200 random inbox emails from the last 5 years, plus your current labels (about $0.40). If you already have a `labels.json`, it improves those labels instead of starting over. It saves the suggestion to `labels_proposed.json`. Nothing in Gmail changes. Review it, then save the labels you want to keep as `labels.json`, in the same format as `labels.example.json`. Until you do, triage uses the example labels.
9. Run `python triage.py 10 --apply` to sort emails in Gmail for real, starting small. It adds the labels, and archives each sorted email unless it needs a reply or an action or is Unsorted. Gmail asks you to sign in again the first time, because changing labels needs more permission than reading. Emails that are already sorted get skipped. To reverse the last run, use `python undo.py`, which says what it would do and asks first, or the History page.
10. To sort a large backlog at half price, use `python backlog.py sample 40` to check the labels on a random sample first. Then run `python backlog.py submit`, `python backlog.py wait` (usually under an hour) and `python backlog.py apply`. Each `apply` is one run that `python undo.py` can reverse.
11. To look inside the log (`inbox_agent.db`) in VS Code, install the **SQLite Viewer** extension. The file is a database, not text, so VS Code can't open it on its own.
12. Run `python check_mcp_server.py` to check the Gmail MCP server. To use it in Claude Desktop, add an `mcpServers` section to Claude Desktop's `claude_desktop_config.json`, keeping everything already in the file. The server's `command` is the full path to the project's Python (`.venv\Scripts\python.exe` on Windows, `.venv/bin/python` on a Mac), and its `args` is the full path to `gmail_mcp_server.py`. Then quit and reopen Claude Desktop. **Settings → Developer → Edit Config** shows where the file is. On a Mac, that's `~/Library/Application Support/Claude/`. On Windows, the Microsoft Store version of Claude keeps it in `%LOCALAPPDATA%\Claude-Data\<app folder>\`, not `%APPDATA%\Claude\` as many guides say.
13. Build your profile: run `python style_builder.py collect`, check `style_source_emails.json`, then run `python style_builder.py write` (about $0.15). Read and edit `profile/writing_style.md`, and fill in `profile/about_me.md` (see `profile.example/`).
14. Run `python drafter.py --search "from:someone@example.com"` to draft a reply to the newest matching email. Add `--note "what to say"` to tell it what you want, and `--save` to save the approved reply as a Gmail draft. Without `--save`, nothing changes in Gmail. A draft costs roughly $0.05 to $0.20.
15. Get Tailwind's program, once per computer, from github.com/tailwindlabs/tailwindcss/releases/tag/v4.3.3. Each system has its own. Check that it's the genuine file by comparing its fingerprint with the one in the release's `sha256sums.txt`, and only use the program if they're the same.

    On Windows, open that page in a browser and, under **Assets**, download `tailwindcss-windows-x64.exe` and `sha256sums.txt`. Create a `tools` folder in the project, move the program into it and rename it `tailwindcss.exe`. Then run `Get-FileHash tools\tailwindcss.exe` and compare the Hash with the line for `tailwindcss-windows-x64.exe` in `sha256sums.txt`.

    On a Mac, download both with `curl`, because macOS can block a program downloaded in a browser. The last two commands each print a fingerprint to compare:
    ```
    mkdir -p tools
    curl -fLo tools/tailwindcss https://github.com/tailwindlabs/tailwindcss/releases/download/v4.3.3/tailwindcss-macos-arm64
    curl -fLo tools/sha256sums.txt https://github.com/tailwindlabs/tailwindcss/releases/download/v4.3.3/sha256sums.txt
    shasum -a 256 tools/tailwindcss
    grep tailwindcss-macos-arm64 tools/sha256sums.txt
    ```
    If they're the same, make the program runnable with `chmod +x tools/tailwindcss`.
16. Build the page's CSS with `tools/tailwindcss -i web/tailwind.css -o web/static/app.css`. While editing templates, add `--watch` and leave it running in a second terminal, so the CSS is rebuilt every time a file is saved.
17. Start the web page from the project folder with `python -m uvicorn web.app:app --reload`, then open http://127.0.0.1:8000 in a browser. Only this computer can open it. Stop it with Ctrl+C. When the page says Gmail needs you to sign in again, click **Sign in to Gmail** on it. The app only accepts the Google account its login was first made with, so to switch accounts on purpose, delete `token.json` first.

## Project files

| File | What it does |
|---|---|
| `hello_claude.py` | Step 1: first call to Claude |
| `gmail_client.py` | All Gmail code: login (also from the web page), fetch emails |
| `show_inbox.py` | Step 2: prints the 20 newest inbox emails |
| `triage.py` | Step 3: sorts emails with Claude Haiku, flags replies and actions, and suggests labels. Then runs the full read and decides what to archive. A dry run unless told `--apply` |
| `full_read.py` | Step 7: reads a whole email and says what exactly to do, where and by when. Claude Haiku first, Claude Sonnet when Haiku is unsure |
| `label_designer.py` | Step 4: Claude Opus suggests a label set from my inbox and existing labels |
| `labels.example.json` | A shareable example label set. My real labels are in `labels.json`, which isn't in Git |
| `database.py` | The agent's memory: a SQLite log of every email it handled and what it decided |
| `todos.py` | Step 7: what counts as a to-do, in one place. `list_todos()` combines the agent's flagged emails with Gmail's list of what's in my inbox, works out how pressing each one is, and puts the most pressing first. Used by the web page and the MCP server |
| `undo.py` | Reverses a sorting run, from the terminal or the History page: removes the labels it added and moves archived emails back into the inbox, leaving alone what I've changed or archived myself. It says what it would do first |
| `backlog.py` | Sorts the whole inbox backlog with Claude's Message Batches API: `sample`, `submit`, `wait`, `apply` |
| `gmail_mcp_server.py` | My Gmail MCP server: search, read threads, list to-dos, save reply drafts. No send, delete or archive |
| `check_mcp_server.py` | Starts the MCP server the way an app would, lists its tools and tries the read-only ones |
| `drafter.py` | The Drafter agent and the Critic: writes a reply to one conversation, reviews it, and saves it as a Gmail draft with `--save` |
| `style_builder.py` | Builds my writing-style file: `collect` gathers my sent emails, `write` has Claude Opus describe my style |
| `web/` | Step 7: the web page. `app.py` is the FastAPI app, with one function per page. `templates/` holds the HTML templates: `base.html` is the shared layout with the menu, and each page fills it in. `csrf.py` stops other websites pressing the app's buttons, `sorting.py` runs the "Sort new emails" button's work on a second thread, and `labelling.py` changes an email's label behind the Unsorted page's buttons, and undoes that from the History page. `tailwind.css` tells Tailwind's program what to build, and `static/` holds files sent to the browser as they are: the built CSS, the Inter font and HTMX |
| `profile.example/` | Example versions of my profile files. The real ones are in `profile/`, which isn't in Git |
| `requirements.txt` | Python packages, with exact versions |
| `constraints.txt` | The exact version of every package, including what the ones in `requirements.txt` bring with them, so each computer gets the same ones |
| `.env.example` | Template for the API key file |
| `.gitattributes` | Keeps every text file's line endings the same on every computer |
| `docs/` | The longer documents: how it works and why (`design-decisions.md`), how it was built (`build-log.md`), and these setup steps (`setup.md`) |

Not in Git: `.env` (API key), `credentials.json` (Google app file), `token.json` (saved Gmail login), `labels_proposed.json` (label suggestions that quote my real emails), `labels.json` and its temporary `labels.json.tmp` (my labels, whose descriptions name companies I applied to), `inbox_agent.db` (the log of my emails and decisions), `backups/` (copies of the log and my labels), `backlog_batch.json` (which batch is waiting, deleted after use), `style_source_emails.json` (my sent emails, collected for the writing-style file), `profile/` (my writing-style and about-me files), `.claude/` (settings for testing the web page in the Claude app), `tools/` (Tailwind's program, 80 to 112 MB depending on the computer), `web/static/app.css` (the CSS that Tailwind's program builds), `.venv/` (Python environment), `.DS_Store` and `._*` (files Macs create by themselves).
