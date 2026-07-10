"""intel 工作目录解析 。

数据(JSONL)和任何会话状态放工作目录,不放代码仓:代码可以同步/分享/回滚,
采集数据和登录态永远不该跟着走。一个环境变量整体搬迁。

解析顺序:
  1. $INTEL_WORKDIR(显式覆盖)
  2. ~/market-intel(默认,首次自动创建)

"""
import os

def _resolve_workdir() -> str:
    env = os.environ.get("INTEL_WORKDIR")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return os.path.expanduser("~/market-intel")

BASE = _resolve_workdir()
OUT = os.path.join(BASE, "out")          # 采集到的 JSONL
SIGNALS = os.path.join(BASE, "signals")  # evidence.py 的信号库
for _d in (BASE, OUT, SIGNALS):
    os.makedirs(_d, exist_ok=True)

if __name__ == "__main__":
    print("INTEL_WORKDIR ->", BASE)
    print("out           ->", OUT)
    print("signals       ->", SIGNALS)
