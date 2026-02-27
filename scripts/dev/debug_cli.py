#!/usr/bin/env python3
"""
Run luma CLI flows under debugger.

Usage from IntelliJ IDEA / PyCharm:
- Open this file and run with the debugger.
- Set breakpoints in:
  - agent/agent.py
  - command_query.py
  - event_store.py
"""

import sys

from luma.cli import main


if __name__ == "__main__":
    # Change argv to simulate different CLI invocations.

    # Free-text agent query:
    sys.argv = ["luma", "--debug", "this weekend events, all"]

    # Flag-based query:
    # sys.argv = ["luma", "--days", "7", "--min-guest", "100"]

    # Agent query with flags:
    # sys.argv = ["luma", "--days", "7", "show me AI events"]

    # JSON output:
    # sys.argv = ["luma", "--json", "what's popular this week?"]

    # Refresh:
    # sys.argv = ["luma", "refresh"]

    raise SystemExit(main())
