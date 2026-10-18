# Inbox Agent

An AI agent that organizes my Gmail. It designs a set of labels, sorts every email into them, and drafts replies to the emails that need one. Several Claude models work together, each with its own job, and nothing is ever sent without my approval.

**Status:** Step 1 of 6, first call to Claude.

## Setup

1. Install Python 3.13.
2. Create a virtual environment and install the dependencies:
   ```
   py -3.13 -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   ```
3. Copy `.env.example` to `.env` and paste in your API key from the Claude Console.
4. Run `python hello_claude.py`.
