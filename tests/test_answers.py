"""摘要、完整 JSON 與 real 模式呈現的離線驗證。"""
import json
import secrets

import pytest

from partpilot.answers import render, summarize
from partpilot.auth import init_users
from partpilot.config import Config, ROOT
from partpilot.llm import FakeLLM
from partpilot.service import Service


@pytest.mark.parametrize("uom,amount", [("PCS", 17), ("M", 2.35)])
def test_shortage_summary_preserves_units_and_all_raw_rows(uom, amount):
    rows = [{"PartNo": f"MAT-{i:06}", "short": amount + i, "uom": uom} for i in range(8)]
    result = {"part": "FGD111-01", "plant": "PLANT-C", "prod_date": "2026-12-01",
              "qty": 137, "max_buildable": 91, "shortages": rows}
    summary = summarize("shortage", result)
    assert 2 <= len(summary.splitlines()) <= 5
    assert "最多可做 91 台" in summary and "缺 8 種料" in summary
    assert f"MAT-000000 缺 {amount} {uom}" in summary
    assert "另有 2 種料" in summary
    raw = render(summary, result).split("原始 JSON：\n", 1)[1]
    assert json.loads(raw) == result


def test_real_cli_preserves_multi_tool_summary_and_authorized_json(tmp_path, monkeypatch, capsys):
    from partpilot import __main__ as cli

    cfg = Config(mode="real", api_key="offline-test-placeholder", password=secrets.token_urlsafe(18),
                 raw_dir=str(ROOT / "data/sample"), clean_dir=str(tmp_path / "clean"),
                 state_dir=str(tmp_path / "state"))
    init_users(str(tmp_path / "state/users.json"), cfg.password)
    svc = Service(cfg)
    svc.run_clean(svc.login("sys_admin", cfg.password))
    summary = "CAP-000101 在 PLANT-A 可用庫存 140 PCS。\n2026-11-20 前另有 90 PCS 在途可用。\n可依生產日安排備料。"
    llm = FakeLLM([
        {"content": [{"type": "tool_use", "id": "stock", "name": "get_inventory",
                      "input": {"part_no": "CAP-000101", "plant": "PLANT-A"}},
                     {"type": "tool_use", "id": "incoming", "name": "get_inbound",
                      "input": {"part_no": "CAP-000101", "plant": "PLANT-A", "prod_date": "2026-11-20"}}]},
        {"content": [{"type": "text", "text": summary}]},
    ])
    # real 的 Service、工具與 CLI 全程照常執行；只替換外部模型傳輸。
    monkeypatch.setattr("partpilot.service.AnthropicClient", lambda *args: llm)
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    assert cli.main(["ask", "--user", "scm_lead", "--question", "CAP-000101 庫存與在途"]) == 0
    output = capsys.readouterr().out
    first, raw = output.split("\n\n原始 JSON：\n", 1)
    assert first == summary
    results = json.loads(raw)
    assert [(row["tool"], row["result"]["qty"]) for row in results] == [("get_inventory", 140), ("get_inbound", 90)]
    assert "SupplierName" not in raw and "UnitCostUSD" not in raw
    assert "2～5 行中文摘要" in llm.calls[0]["system"]
