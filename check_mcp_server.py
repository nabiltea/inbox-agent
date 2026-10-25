"""Check my Gmail MCP server the way an AI app would use it.

Starts gmail_mcp_server.py in the background, lists its tools, and tries the
read-only ones. It never calls create_reply_draft, so nothing in Gmail changes.

Run it with:  python check_mcp_server.py
"""

import asyncio
import json
import sys
from pathlib import Path

from mcp import Client, StdioServerParameters

# How to start the server: this same Python, running gmail_mcp_server.py. Apps like
# Claude Desktop get exactly this information from their settings.
SERVER = StdioServerParameters(
    command=sys.executable,
    args=[str(Path(__file__).parent / "gmail_mcp_server.py")],
)


def data(result):
    """The tool's answer as Python data.

    Tools send structured data when their return type is precise enough, and a list
    arrives wrapped as {"result": [...]}. Otherwise the answer is JSON text.
    """
    if result.is_error:
        sys.exit(f"Tool error: {result.content}")
    content = result.structured_content
    if content is None:
        return json.loads(result.content[0].text)
    return content.get("result", content) if isinstance(content, dict) else content


async def main():
    async with Client(SERVER) as client:  # starts the server and does the MCP handshake
        tools = (await client.list_tools()).tools
        print(f"The server offers {len(tools)} tools:")
        for tool in tools:
            kind = "read-only" if tool.annotations and tool.annotations.read_only_hint else "changes Gmail"
            print(f"  {tool.name:<20} ({kind}) {tool.description.splitlines()[0]}")

        emails = data(await client.call_tool("search_emails", {"query": "in:inbox", "max_results": 3}))
        print("\nsearch_emails('in:inbox', 3):")
        for email in emails:
            print(f"  {email['date']}  {email['subject'][:55]}  {email['labels']}")

        todos = data(await client.call_tool("list_todos", {"days": 14}))
        print(f"\nlist_todos(14 days): {len(todos)} to-dos")
        for todo in todos[:5]:
            print(f"  {todo['label']:<16} {todo['action'] or 'reply'}")

        thread_id = (todos or emails)[0]["thread_id"]
        thread = data(await client.call_tool("read_thread", {"thread_id": thread_id}))
        print(f"\nread_thread: {thread['subject'][:55]!r}, {len(thread['messages'])} message(s), "
              f"first one {len(thread['messages'][0]['body'])} characters")


if __name__ == "__main__":
    asyncio.run(main())
