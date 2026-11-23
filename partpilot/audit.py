"""稽核紀錄與 token 用量。JSON Lines。"""
from __future__ import annotations
import datetime as dt
import json
import os
from pathlib import Path
import threading

_LOCK = threading.RLock()
TOKEN_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def _append(path, record):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")


def _read(path):
    with _LOCK:
        if not Path(path).exists():
            return []
        with open(path, encoding="utf-8") as stream:
            return [json.loads(line) for line in stream if line.strip()]


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


class Audit:
    def __init__(self, path: str):
        self.path = path

    def log(self, user: dict, event: str, detail: dict) -> None:
        """寫一行 {"ts","username","role","event","detail"}。event：login / login_failed / ask / answer / tool / clean / approve / denied / audit_view。"""
        _append(self.path, dict(ts=_now(), username=user.get("username", ""),
                               role=user.get("role"), event=event, detail=detail))

    def read(self, requester: dict, limit: int = 200) -> list:
        """只有 admin 可讀（其他角色 → PermissionError）；讀取本身也寫一筆 audit_view。回最新 limit 筆。"""
        if requester.get("role") != "admin":
            self.log(requester, "denied", {"action": "audit_view"})
            raise PermissionError("依權限規則，只有 admin 系統管理員可查看稽核紀錄")
        self.log(requester, "audit_view", {"limit": limit})
        return _read(self.path)[-limit:] if limit > 0 else []


PRICES_PER_MTOK = {  # USD / 百萬 tokens（估計值，可用環境變數 PARTPILOT_PRICE_<MODEL>=in,out 覆寫）
    "default": (5.0, 25.0),
    "claude-opus-5-5": (5.0, 25.0),
    "claude-sonnet-5-5": (3.0, 15.0),
    "claude-haiku-5-5": (1.0, 5.0),
}


class UsageLog:
    def __init__(self, path: str):
        self.path = path

    def log(self, username: str, conv_id: str, model: str, usage: dict, latency_s: float, step: str) -> dict:
        """寫一行並回傳 {"input_tokens","output_tokens","cache_read_input_tokens","cache_creation_input_tokens","cost_usd",...}。
        cost = (input + cache_creation×1.25 + cache_read×0.1)×in價 + output×out價，除以 1e6。"""
        prices = PRICES_PER_MTOK.get(model, PRICES_PER_MTOK["default"])
        override = os.environ.get("PARTPILOT_PRICE_" + model)
        if override:
            try:
                prices = tuple(float(value) for value in override.split(","))
                if len(prices) != 2 or any(value < 0 for value in prices):
                    raise ValueError
            except ValueError:
                raise ValueError("token 價格設定須為非負數的 in,out") from None
        record = {key: int(usage.get(key, 0) or 0) for key in TOKEN_KEYS}
        record["cost_usd"] = ((record["input_tokens"] + record["cache_creation_input_tokens"] * 1.25
                               + record["cache_read_input_tokens"] * 0.1) * prices[0]
                              + record["output_tokens"] * prices[1]) / 1e6
        record.update(ts=_now(), username=username, conv_id=conv_id, model=model,
                      latency_s=latency_s, step=step)
        _append(self.path, record)
        return record

    def summary(self) -> dict:
        """{"calls","input_tokens","output_tokens","cost_usd","by_model": {...}}"""
        def empty():
            return dict(calls=0, cost_usd=0.0, **{key: 0 for key in TOKEN_KEYS})
        result = empty()
        result["by_model"] = {}
        for row in _read(self.path):
            subtotal = result["by_model"].setdefault(row["model"], empty())
            for target in (result, subtotal):
                target["calls"] += 1
                for key in (*TOKEN_KEYS, "cost_usd"):
                    target[key] += row.get(key, 0)
        return result


_SAFE_DETAIL = ("name", "result", "action", "tool_calls", "tokens", "conv_id", "counts", "issues", "decision", "kind")


def sanitize(row: dict) -> dict:
    """給 admin 看的稽核紀錄：只留事件類型、時間、帳號、結果等白名單欄位，不含提問原文與工具參數（可能含料號）。"""
    detail = row.get("detail") or {}
    return {"ts": row.get("ts"), "username": row.get("username"), "role": row.get("role"), "event": row.get("event"),
            "detail": {k: detail[k] for k in _SAFE_DETAIL if k in detail}}
