"""Step 1: our first call to Claude.

We give Claude a made-up email and ask the two questions our agent will
eventually ask about every email in the inbox: what category does it
belong to, and does it need a reply?
"""

import os
import sys

import anthropic
from dotenv import load_dotenv

# Copy the API key from the .env file into this program's environment.
load_dotenv()

MODEL = "claude-opus-5-5"

# Claude Opus 5.5 prices in dollars per million tokens, used to show the cost of each call.
INPUT_PRICE = 4.00
OUTPUT_PRICE = 20.00

SYSTEM_PROMPT = """\
You help sort a personal Gmail inbox. For the email you are given, answer in exactly this format:
Category: a short category name (for example Friends, Newsletters, Receipts, Jobs)
Needs reply: yes or no
Reason: one sentence explaining your choice
"""

SAMPLE_EMAIL = """\
From: Sarah Chen <sarah.chen@example.com>
Subject: Coffee next week?

Hi! I'm back in town next week and would love to catch up.
Are you free Tuesday or Wednesday afternoon?

Sarah
"""

if not os.environ.get("ANTHROPIC_API_KEY"):
    sys.exit("No API key found. Copy .env.example to .env and paste your key into it.")

client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the environment

try:
    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=2000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": SAMPLE_EMAIL}],
        # How much the model thinks before answering. Sorting one email is easy, so keep it low.
        output_config={"effort": "low"},
        # Safety net: if this model declines a request, the API retries it on another Claude model.
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
except anthropic.AuthenticationError:
    sys.exit("Anthropic rejected the API key. Check the key in your .env file.")

if response.stop_reason == "refusal":
    print("Claude declined to answer this one.")
else:
    # The reply is a list of blocks. Claude may put a thinking block before its
    # answer, so print only the text blocks.
    for block in response.content:
        if block.type == "text":
            print(block.text)

usage = response.usage
cost = (usage.input_tokens * INPUT_PRICE + usage.output_tokens * OUTPUT_PRICE) / 1_000_000
print(f"\nTokens: {usage.input_tokens} in, {usage.output_tokens} out (about ${cost:.4f})")
