"""扫描全平台套利机会。只读，不碰私钥。

用法:
  .venv/bin/python scripts/scan_arb.py
  .venv/bin/python scripts/scan_arb.py --max-events 500 --min-edge 0.003
"""
import sys

sys.path.insert(0, ".")
from polymarket_agent.arbitrage import main

if __name__ == "__main__":
    raise SystemExit(main())
