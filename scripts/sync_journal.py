#!/usr/bin/env python3
"""交易日志 CLI: python scripts/sync_journal.py sync|show"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from polymarket_agent.journal import show, sync


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "show"
    if cmd == "sync":
        sync()
    elif cmd == "show":
        show()
    else:
        print("用法: python scripts/sync_journal.py [sync|show]")
        sys.exit(1)


if __name__ == "__main__":
    main()
