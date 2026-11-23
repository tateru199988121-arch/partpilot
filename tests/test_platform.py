"""登入、稽核、token 紀錄、LLM 介面、Agent loop、CLI。"""
import json, os, secrets, subprocess, sys
import pytest
from partpilot.auth import UserStore, init_users
from partpilot.audit import Audit, UsageLog
from partpilot.llm import FakeLLM, LLMError
from partpilot.agent import Agent, system_prompt

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
product_line = {"username": "line_dc", "role": "product_line", "product_line": "DC", "plant": None, "commodity_group": None}
admin = {"username": "sys_admin", "role": "admin", "product_line": None, "plant": None, "commodity_group": None}

def test_users_hash_only(tmp_path):
    p = str(tmp_path / "users.json")
    password = secrets.token_urlsafe(18)
    assert init_users(p, password) == 13
    raw = open(p, encoding="utf-8").read()
    assert password not in raw
    s = UserStore(p)
    u = s.authenticate("line_dc", password)
    assert u["role"] == "product_line" and u["product_line"] == "DC" and "hash" not in u
    assert s.authenticate("line_dc", "wrong") is None
    assert s.authenticate("nobody", password) is None
    with pytest.raises(ValueError):
        init_users(str(tmp_path / "u2.json"), "")

def test_audit_admin_only(tmp_path):
    a = Audit(str(tmp_path / "audit.jsonl"))
    a.log(product_line, "ask", {"question": "q"})
    with pytest.raises(PermissionError):
        a.read(product_line)
    rows = a.read(admin)
    assert rows[0]["event"] == "ask" and rows[0]["username"] == "line_dc"
    assert any(r["event"] == "audit_view" for r in a.read(admin))

def test_usage_cost(tmp_path):
    u = UsageLog(str(tmp_path / "usage.jsonl"))
    r = u.log("line_dc", "c1", "m", {"input_tokens": 1000000, "output_tokens": 0}, 1.0, "answer")
    assert r["cost_usd"] == pytest.approx(5.0)
    assert u.summary()["calls"] == 1 and u.summary()["input_tokens"] == 1000000

def tool_resp(name, inp):
    return {"content": [{"type": "tool_use", "id": "t1", "name": name, "input": inp}], "usage": {"input_tokens": 100, "output_tokens": 10}, "stop_reason": "tool_use"}

def text_resp(t):
    return {"content": [{"type": "text", "text": t}], "usage": {"input_tokens": 50, "output_tokens": 20}, "stop_reason": "end_turn"}

def test_agent_tool_loop_and_conversation(tmp_path):
    calls = []
    def executor(name, args):
        calls.append((name, args)); return {"qty": 42}
    llm = FakeLLM([tool_resp("get_inventory", {"part_no": "CAP-000001"}), text_resp("可用 42"), text_resp("承上 42")])
    usage = UsageLog(str(tmp_path / "usage.jsonl")); audit = Audit(str(tmp_path / "audit.jsonl"))
    ag = Agent(llm, product_line, [{"name": "get_inventory", "description": "d", "input_schema": {"type": "object"}}], executor, str(tmp_path), audit, usage)
    r = ag.ask("庫存多少？")
    assert r["answer"] == "可用 42" and r["tool_calls"] == 1 and calls == [("get_inventory", {"part_no": "CAP-000001"})]
    assert r["usage"]["input_tokens"] == 150 and r["usage"]["output_tokens"] == 30
    # 工具結果回給 LLM
    second_msgs = llm.calls[1]["messages"]
    assert any(isinstance(m["content"], list) and any(b.get("type") == "tool_result" and "42" in json.dumps(b, ensure_ascii=False) for b in m["content"]) for m in second_msgs)
    r2 = ag.ask("延續對話？", conv_id=r["conv_id"])
    assert r2["conv_id"] == r["conv_id"] and r2["answer"] == "承上 42"
    assert "庫存多少？" in json.dumps(llm.calls[2]["messages"], ensure_ascii=False)
    events = [x["event"] for x in audit.read(admin)]
    assert "ask" in events and "answer" in events

def test_agent_max_steps_and_error(tmp_path):
    llm = FakeLLM([tool_resp("x", {})] * 5)
    ag = Agent(llm, product_line, [], lambda n, a: {}, str(tmp_path), max_steps=3)
    r = ag.ask("q")
    assert r["steps"] == 3 and r["answer"]
    class Boom:
        def create(self, *a, **k): raise LLMError("429")
    r = Agent(Boom(), product_line, [], lambda n, a: {}, str(tmp_path)).ask("q")
    assert "429" in r["answer"] or "錯誤" in r["answer"]

def test_system_prompt_mentions_rules():
    s = system_prompt(product_line)
    assert "2026-11-06" in s and "product_line" in s and "DC" in s
    assert "外部" in s   # 不提供外寄

def test_cli_help():
    out = subprocess.run([sys.executable, "-m", "partpilot", "--help"], cwd=ROOT, capture_output=True, text=True)
    assert out.returncode == 0
    for cmd in ("init-users", "clean", "ask", "serve", "audit"):
        assert cmd in out.stdout


def test_audit_view_sanitized_for_admin(tmp_path):
    from partpilot.audit import sanitize
    row = {"ts": "t", "username": "line_dc", "role": "product_line", "event": "tool",
           "detail": {"name": "get_inventory", "args": {"part_no": "CAP-000001"}, "result": "ok", "question": "CAP-000001 庫存？"}}
    s = sanitize(row)
    assert "CAP-000001" not in json.dumps(s, ensure_ascii=False)
    assert s["detail"]["name"] == "get_inventory" and s["detail"]["result"] == "ok"
