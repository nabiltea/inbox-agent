"""A check that refuses any commit containing my private details.

The repo is public as soon as I push, so real details from my emails must never be
committed: no names (not even first names), companies, booking or contract names, codes,
amounts or dates of real events. Reading every line myself works until one tired
evening, so Git runs this script by itself before every commit, from VS Code too, and
again on the commit message. A commit stays on my computer until I push it, so a refused
commit has made nothing public.

Set it up once in each copy of the repo, because Git doesn't copy hooks when cloning:
  python check_private.py install

What it refuses, in the lines a commit adds, the names of its files and its message:
1. A word or phrase from my private list, .claude/private-words.txt, which Git ignores.
2. A word from my own data that isn't already public in this repo: the senders in the
   agent's log, any code in an email subject (a word with both letters and digits, like a
   booking code), and the names in my labels file and about-me file (words with a capital).
   Words the repo already contains, like LinkedIn or plain English, pass. A new word that
   turns out to be a false alarm goes in .claude/public-words.txt, which Git ignores too.
3. Anything that looks like an API key or a Google login, and any email address outside
   the example domains.
4. A file that .gitignore keeps out, added anyway with `git add --force`.

It can't read pictures, so screenshots must only ever show made-up emails. And it only
knows what's in the lists and the log, so I still read what I commit.

Two more ways to run it:
  python check_private.py changes   checks everything not committed yet, before committing
  python check_private.py all       checks every file and commit message in the last commit
                                    against rules 1 and 3
"""

import io
import re
import sqlite3
import subprocess
import sys
import tarfile
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

from database import DB_FILE, plain_text

PROJECT = Path(__file__).parent
PRIVATE_WORDS = PROJECT / ".claude" / "private-words.txt"
PUBLIC_WORDS = PROJECT / ".claude" / "public-words.txt"
LABELS_FILE = PROJECT / "labels.json"
ABOUT_ME = PROJECT / "profile" / "about_me.md"

# Words shorter than this are too common to judge, like "re", "nl" or initials.
MIN_LENGTH = 3

# Letters and digits, so "Reply-To" gives "reply" and "to". [^\W_] is \w without the underscore.
WORD = re.compile(r"[^\W_]+")

# Shapes of secrets. The finding names the kind, never the secret itself.
SECRETS = {
    "a Claude API key": re.compile(r"sk-ant-[A-Za-z0-9_-]{10,}"),
    "a Google client secret": re.compile(r"GOCSPX-[A-Za-z0-9_-]{10,}"),
    "a Google access key": re.compile(r"ya29\.[A-Za-z0-9_-]{20,}"),
    "a Google login (refresh token)": re.compile(r"1//0[A-Za-z0-9_-]{20,}"),
}

EMAIL_ADDRESS = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")

# Addresses that are made up by design (example.com is reserved for examples), or
# GitHub's no-reply address, which is what my commits use instead of my real one.
SAFE_EMAIL_DOMAINS = ("example.com", "example.org", "example.net", "users.noreply.github.com")

# The hooks Git runs. pre-commit checks what's about to be committed, and commit-msg
# checks the message, which doesn't exist yet when pre-commit runs.
HOOKS = {"pre-commit": "staged", "commit-msg": 'message "$1"'}
HOOK_MARK = "# Installed by check_private.py"
HOOK_SCRIPT = """#!/bin/sh
{mark}: refuses a commit that would make private details public.
cd "$(git rev-parse --show-toplevel)" || exit 1
if [ -x .venv/Scripts/python.exe ]; then
    exec .venv/Scripts/python.exe check_private.py {arguments}
fi
exec .venv/bin/python check_private.py {arguments}
"""


@dataclass
class Finding:
    where: str  # like "docs/build-log.md, line 12", or "the commit message"
    found: str  # the word, phrase or address, or the kind of secret
    why: str


@dataclass
class Rules:
    """Everything one check compares against, read once."""

    private: list  # (phrase, pattern) from my private list
    risky: dict  # word -> where it came from, for words from my data that aren't public yet
    safe_addresses: set  # addresses from the false-alarm list
    not_found: list = field(default_factory=list)  # data the check couldn't read, to say so


def words(text):
    """The words in a text, as compared: capitals and accents don't count."""
    return WORD.findall(plain_text(text))


def read_list(path):
    """A word list: one word or phrase per line. Blank lines and lines starting with # don't count."""
    if not path.exists():
        return []
    lines = (line.strip() for line in path.read_text(encoding="utf-8").splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def phrase_pattern(phrase):
    """Finds a phrase as whole words, whatever the capitals, accents and spaces.

    So "cafe" finds "Café's" and "CAFE", but not "cafeteria".
    """
    parts = [re.escape(part) for part in plain_text(phrase).split()]
    return re.compile(r"(?<![^\W_])" + r"\s+".join(parts) + r"(?![^\W_])")


def git(*arguments, binary=False):
    """Runs a Git command in this repo and returns what it printed. Raises CalledProcessError if it fails."""
    done = subprocess.run(
        ["git", "-c", "core.quotepath=off", *arguments], cwd=PROJECT, capture_output=True, check=True
    )
    return done.stdout if binary else done.stdout.decode("utf-8", errors="replace")


def files_at(commit="HEAD"):
    """Every text file in a commit, as (path, text). Pictures and fonts are skipped. Nothing if there's no commit yet."""
    try:
        archive = git("archive", "--format=tar", commit, binary=True)
    except subprocess.CalledProcessError:
        return
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        for member in tar:
            if member.isfile():
                try:
                    yield member.name, tar.extractfile(member).read().decode("utf-8")
                except UnicodeDecodeError:
                    continue


def public_words(commit="HEAD"):
    """Every word in the repo at a commit, and in every commit message up to it: what's already public once it's pushed."""
    vocabulary = set()
    for path, text in files_at(commit):
        vocabulary.update(words(path))
        vocabulary.update(words(text))
    try:
        vocabulary.update(words(git("log", "--format=%B", commit)))
    except subprocess.CalledProcessError:
        pass  # no commits yet
    return vocabulary


def my_data_words(not_found):
    """Words from my own data, each with where it came from. Data that isn't there is noted in not_found."""
    found = {}

    def add(word, source):
        if len(word) >= MIN_LENGTH:
            found.setdefault(word, source)

    if DB_FILE.exists():
        # mode=ro: the check only ever reads the log.
        with closing(sqlite3.connect(DB_FILE.as_uri() + "?mode=ro", uri=True)) as conn:
            for sender, subject in conn.execute("SELECT sender, subject FROM decisions"):
                for word in words(sender or ""):
                    add(word, "a sender in the agent's log")
                for word in words(subject or ""):
                    if re.search(r"\d", word) and re.search(r"[^\W\d_]", word):
                        add(word, "a code in an email subject in the log")
    else:
        not_found.append("the agent's log")
    for path, source in ((LABELS_FILE, "your labels file"), (ABOUT_ME, "your about-me file")):
        if path.exists():
            # Only words with a capital or a digit. These files are mostly ordinary sentences,
            # and what's private in them, like a company or a person, is written with a capital.
            for word in WORD.findall(path.read_text(encoding="utf-8")):
                if word[0].isupper() or re.search(r"\d", word):
                    add(plain_text(word), source)
        else:
            not_found.append(source)
    return found


def load_rules(commit="HEAD"):
    """Reads the lists and my data, and leaves out every word that's already public at that commit."""
    not_found = []
    allowed = read_list(PUBLIC_WORDS)
    allowed_words = {word for entry in allowed for word in words(entry)}
    public = public_words(commit) | allowed_words
    risky = {word: source for word, source in my_data_words(not_found).items() if word not in public}
    if not PRIVATE_WORDS.exists():
        not_found.append("your private list")
    return Rules(
        private=[(phrase, phrase_pattern(phrase)) for phrase in read_list(PRIVATE_WORDS)],
        risky=risky,
        safe_addresses={plain_text(entry) for entry in allowed if "@" in entry},
        not_found=not_found,
    )


def check_text(text, where, rules, my_data=True):
    """Everything private in one piece of text. my_data=False leaves out rule 2, for checking what's already public."""
    findings = []
    plain = plain_text(text)
    for phrase, pattern in rules.private:
        if pattern.search(plain):
            findings.append(Finding(where, phrase, "on your private list"))
    if my_data:
        on_list = {word for finding in findings for word in words(finding.found)}
        for word in dict.fromkeys(words(text)):  # each word once, in order
            if word in rules.risky and word not in on_list:
                findings.append(Finding(where, word, f"from {rules.risky[word]}"))
    for kind, pattern in SECRETS.items():
        if pattern.search(text):
            findings.append(Finding(where, kind, "looks like a secret"))
    for address in EMAIL_ADDRESS.findall(text):
        domain = address.rsplit("@", 1)[1].lower()
        made_up = any(domain == safe or domain.endswith("." + safe) for safe in SAFE_EMAIL_DOMAINS)
        if not made_up and plain_text(address) not in rules.safe_addresses:
            findings.append(Finding(where, address, "an email address"))
    return findings


HUNK = re.compile(r"^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def added_lines(diff):
    """The lines a diff adds, as (path, line number, text). Removed lines don't count: taking a detail out is fine.

    Each hunk header ("@@ -12,3 +12,4 @@") says how many lines it removes and adds, so a
    line is only read as a header once its hunk is used up, even if its text starts with "+++".
    """
    path, number, old_left, new_left = None, 0, 0, 0
    for line in diff.splitlines():
        if old_left or new_left:
            if line.startswith("+"):
                yield path, number, line[1:]
                number, new_left = number + 1, new_left - 1
            elif line.startswith("-"):
                old_left -= 1
            elif line.startswith(" "):
                number, old_left, new_left = number + 1, old_left - 1, new_left - 1
            continue  # "\ No newline at end of file" counts as neither
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else None  # "+++ /dev/null": the file is deleted
        elif match := HUNK.match(line):
            old_left = int(match.group(1) or 1)
            number = int(match.group(2))
            new_left = int(match.group(3) or 1)


DIFF = ["--no-color", "--no-ext-diff", "-U0", "--src-prefix=a/", "--dst-prefix=b/"]


def check_diff(diff, names, rules):
    """The findings in the lines a diff adds and in the names of new files."""
    findings = []
    for path, number, text in added_lines(diff):
        findings += check_text(text, f"{path}, line {number}", rules)
    for name in names:
        findings += check_text(name, f"the file name {name}", rules)
    return findings


def unreadable(numstat):
    """Files Git can't show as text, from `git diff --numstat`, which gives "-  -  path" for them."""
    return [line.split("\t", 2)[2] for line in numstat.splitlines() if line.startswith("-\t-\t")]


def check_staged(rules):
    """What's about to be committed: the lines it adds, the names of new files, and ignored files forced in."""
    diff = git("diff", "--cached", *DIFF)
    new_files = git("diff", "--cached", "--name-only", "--diff-filter=AR").splitlines()
    findings = check_diff(diff, new_files, rules)
    if new_files:
        # --no-index: whether .gitignore matches the file, even though it's being added.
        ignored = subprocess.run(
            ["git", "-c", "core.quotepath=off", "check-ignore", "--no-index", "--stdin"],
            cwd=PROJECT, input="\n".join(new_files).encode("utf-8"), capture_output=True,
        ).stdout.decode("utf-8", errors="replace").splitlines()
        findings += [Finding(name, name, "a file .gitignore keeps out, added anyway") for name in ignored]
    return findings, unreadable(git("diff", "--cached", "--numstat"))


def check_message(path, rules):
    """The commit message, every line of it.

    Git only drops lines starting with # when it opens an editor for the message. With
    -m or -F, which is how VS Code passes it, those lines are kept, so they count too.
    """
    return check_text(Path(path).read_text(encoding="utf-8", errors="replace"), "the commit message", rules)


def check_changes(rules):
    """Everything not committed yet: changes to tracked files, staged or not, and new files Git doesn't ignore."""
    try:
        diff = git("diff", "HEAD", *DIFF)
        numstat = git("diff", "HEAD", "--numstat")
        new_files = git("diff", "HEAD", "--name-only", "--diff-filter=AR").splitlines()
    except subprocess.CalledProcessError:  # no commit yet: everything counts as new
        diff = numstat = ""
        new_files = []
    findings = check_diff(diff, new_files, rules)
    cannot_read = unreadable(numstat)
    for name in git("ls-files", "--others", "--exclude-standard").splitlines():
        findings += check_text(name, f"the file name {name}", rules)
        try:
            text = (PROJECT / name).read_text(encoding="utf-8")
        except UnicodeDecodeError:
            cannot_read.append(name)
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            findings += check_text(line, f"{name}, line {number}", rules)
    return findings, cannot_read


def check_all(rules):
    """Every text file and commit message in the last commit, against the private list and the secrets rule.

    Leaves out rule 2: everything in the last commit counts as public, so there's nothing to compare with.
    """
    findings = []
    for path, text in files_at("HEAD"):
        for number, line in enumerate(text.splitlines(), start=1):
            findings += check_text(line, f"{path}, line {number}", rules, my_data=False)
    log = git("log", "--format=%h%x00%B%x01")
    for entry in log.split("\x01"):
        if "\x00" in entry:
            commit, message = entry.strip().split("\x00", 1)
            findings += check_text(message, f"the message of commit {commit}", rules, my_data=False)
    return findings


def install():
    """Adds the two hooks to this copy of the repo. Leaves alone hooks that something else put there."""
    hooks = Path(git("rev-parse", "--git-path", "hooks").strip())
    hooks = hooks if hooks.is_absolute() else PROJECT / hooks
    hooks.mkdir(parents=True, exist_ok=True)
    for name, arguments in HOOKS.items():
        path = hooks / name
        if path.exists() and HOOK_MARK not in path.read_text(encoding="utf-8", errors="replace"):
            print(f"Not installed: {path} already exists, and isn't this check's. "
                  f"Add a line to it that runs: python check_private.py {arguments}")
            return 1
        # newline="\n": Git runs hooks with sh, which can't read Windows line endings.
        path.write_text(HOOK_SCRIPT.format(mark=HOOK_MARK, arguments=arguments), encoding="utf-8", newline="\n")
        path.chmod(0o755)  # runnable, which a Mac needs and Windows ignores
    print(f"Installed: Git now runs check_private.py before every commit, and on every commit message ({hooks}).")
    return 0


def report(findings, cannot_read, rules, command):
    """Prints what was found, and what couldn't be checked."""
    if findings:
        print({"staged": "Commit stopped: it would make private details public.",
               "message": "Commit stopped: the commit message has private details.",
               }.get(command, "Found private details:"))
        seen = set()
        for finding in findings:
            if (finding.where, finding.found) not in seen:
                seen.add((finding.where, finding.found))
                print(f'  {finding.where}: "{finding.found}" ({finding.why})')
        print("Take them out, then try again. If one is a false alarm, add it to .claude/public-words.txt.")
    elif command in ("changes", "all"):
        print("Nothing private found.")
    if cannot_read:
        print(f"Couldn't read {len(cannot_read)} file(s), like pictures, so check them yourself: {', '.join(cannot_read)}")
    if rules.not_found and command != "message":
        print(f"Not checked against {', '.join(rules.not_found)}: not found.")


def main(arguments):
    # Git shows what a hook prints, and some characters would otherwise crash Windows' console.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    command = arguments[0] if arguments else "changes"
    if command == "install":
        return install()
    if command not in ("staged", "message", "changes", "all"):
        print(__doc__)
        return 2
    rules = load_rules()
    cannot_read = []
    if command == "staged":
        findings, cannot_read = check_staged(rules)
    elif command == "message":
        findings = check_message(arguments[1], rules)
    elif command == "changes":
        findings, cannot_read = check_changes(rules)
    else:
        findings = check_all(rules)
    report(findings, cannot_read, rules, command)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
