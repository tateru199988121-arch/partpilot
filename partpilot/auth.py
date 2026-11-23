"""帳號與登入。密碼只存 PBKDF2 雜湊，程式碼中沒有任何明碼密碼。"""
from __future__ import annotations

from typing import Optional
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import tempfile
import threading

_LOCK = threading.RLock()
ITERATIONS = 200000


def _write_private_json(path, data):
    """以私有暫存檔原子替換，避免半寫入及短暫的公開權限。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

DEFAULT_ACCOUNTS = [
    # username, role, product_line, plant, commodity_group
    ("scm_lead", "supply_chain", None, None, None),
    ("line_dc", "product_line", "DC", None, None),
    ("line_ac", "product_line", "AC", None, None),
    ("plant_c", "plant_control", None, "PLANT-C", None),
    ("plant_d", "plant_control", None, "PLANT-D", None),
    ("plant_a", "plant_control", None, "PLANT-A", None),
    ("plant_b", "plant_control", None, "PLANT-B", None),
    ("engineer_power", "component", None, None, "功率半導體"),
    ("engineer_passive", "component", None, None, "被動元件"),
    ("engineer_ic", "component", None, None, "IC"),
    ("engineer_emech", "component", None, None, "機電與連接"),
    ("engineer_mech", "component", None, None, "機構與其他"),
    ("sys_admin", "admin", None, None, None),
]


class UserStore:
    """users.json：{username: {"role","product_line","plant","commodity_group","salt","hash","iterations"}}"""

    def __init__(self, path: str):
        self.path = Path(path)

    def _read(self):
        if not self.path.exists():
            return {}
        with self.path.open(encoding="utf-8") as stream:
            return json.load(stream)

    def create_user(self, username: str, password: str, role: str, product_line=None, plant=None, commodity_group=None) -> None:
        if not username or not password:
            raise ValueError("帳號與密碼不可空白")
        if role not in {"supply_chain", "product_line", "plant_control", "component", "admin"}:
            raise ValueError("角色代碼無效")
        salt = secrets.token_bytes(16)
        record = dict(role=role, product_line=product_line, plant=plant,
                      commodity_group=commodity_group, salt=salt.hex(),
                      hash=hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS).hex(),
                      iterations=ITERATIONS)
        with _LOCK:
            users = self._read()
            if username in users:
                raise ValueError("帳號已存在")
            users[username] = record
            _write_private_json(self.path, users)

    def authenticate(self, username: str, password: str) -> Optional[dict]:
        """成功回 {"username","role","product_line","plant","commodity_group"}（不含雜湊），失敗回 None。用 hmac.compare_digest。"""
        record = self._read().get(username)
        if record is None:
            # 不存在的帳號也進行同等工作，降低帳號列舉的時間差。
            hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes(16), ITERATIONS)
            return None
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                     bytes.fromhex(record["salt"]), record["iterations"]).hex()
        if not hmac.compare_digest(actual, record["hash"]):
            return None
        return {"username": username, **{key: record.get(key) for key in
                ("role", "product_line", "plant", "commodity_group")}}


def init_users(path: str, password: str) -> int:
    """以同一個初始密碼（來自環境變數 PARTPILOT_INIT_PASSWORD）建立 DEFAULT_ACCOUNTS，回傳建立數。password 空 → ValueError。"""
    if not password:
        raise ValueError("請設定 PARTPILOT_INIT_PASSWORD，初始密碼不可空白")
    store = UserStore(path)
    with _LOCK:
        if store._read():
            raise ValueError("帳號檔已有帳號，不可重新初始化")
        for username, role, line, plant, group in DEFAULT_ACCOUNTS:
            store.create_user(username, password, role, line, plant, group)
    return len(DEFAULT_ACCOUNTS)
