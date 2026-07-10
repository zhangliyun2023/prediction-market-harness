"""从 .env 读密钥，派生 CLOB V2 交易客户端。私钥全程只在本机内存里。"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from dotenv import load_dotenv
from py_clob_client_v2 import ApiCreds, ClobClient

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137  # Polygon mainnet
_CREDS_PATH = Path(__file__).resolve().parent.parent / ".clob_api_creds.json"


def _load_cached_creds() -> ApiCreds | None:
    if not _CREDS_PATH.exists():
        return None
    try:
        data = json.loads(_CREDS_PATH.read_text())
        return ApiCreds(
            api_key=data["api_key"],
            api_secret=data["api_secret"],
            api_passphrase=data["api_passphrase"],
        )
    except Exception:
        return None


def _save_cached_creds(creds: ApiCreds) -> None:
    _CREDS_PATH.write_text(
        json.dumps(
            {
                "api_key": creds.api_key,
                "api_secret": creds.api_secret,
                "api_passphrase": creds.api_passphrase,
            },
            indent=2,
        )
    )
    try:
        os.chmod(_CREDS_PATH, 0o600)
    except OSError:
        pass


def _derive_creds(private_key: str) -> ApiCreds:
    last_err: Exception | None = None
    for i in range(4):
        try:
            temp_client = ClobClient(host=CLOB_HOST, chain_id=CHAIN_ID, key=private_key)
            try:
                return temp_client.derive_api_key()
            except Exception:
                return temp_client.create_or_derive_api_key()
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            time.sleep(0.8 * (i + 1))
    assert last_err is not None
    raise last_err


def build_client(use_cache: bool = True) -> ClobClient:
    private_key = os.getenv("PRIVATE_KEY")
    funder = os.getenv("FUNDER_ADDRESS")
    if not private_key or not funder:
        raise RuntimeError("先把 .env.example 复制成 .env 并填好 PRIVATE_KEY / FUNDER_ADDRESS")

    # 0=EOA, 1=Magic proxy, 2=Gnosis Safe, 3=POLY_1271 deposit/proxy (当前账户实测可用)
    signature_type = int(os.getenv("SIGNATURE_TYPE", "3"))

    api_creds = _load_cached_creds() if use_cache else None
    if api_creds is None:
        api_creds = _derive_creds(private_key)
        if use_cache:
            _save_cached_creds(api_creds)

    return ClobClient(
        host=CLOB_HOST,
        chain_id=CHAIN_ID,
        key=private_key,
        creds=api_creds,
        signature_type=signature_type,
        funder=funder,
    )
