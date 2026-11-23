"""自產資料與離線示範的整合驗證；預期數值固定於評測集。"""
import csv
import json
import os
import secrets
from pathlib import Path
import subprocess
import sys

import pytest

from partpilot.auth import init_users
from partpilot.auth import UserStore
from partpilot.clean import load_clean, run_clean
from partpilot.config import Config, ROOT
from partpilot.service import Service
from scripts.eval import score_case
from scripts.make_sample_data import HEADERS, make_sample_data

CASES = json.loads((ROOT / "eval/eval_set.json").read_text())["cases"]
TEST_PASSWORD = secrets.token_urlsafe(18)


@pytest.fixture(scope="module")
def demo_service(tmp_path_factory):
    path = tmp_path_factory.mktemp("demo")
    cfg = Config(mode="demo", api_key="", model="demo", raw_dir=str(ROOT / "data/sample"),
                 clean_dir=str(path / "clean"), state_dir=str(path / "state"), init_password=TEST_PASSWORD)
    init_users(str(path / "state/users.json"), cfg.init_password)
    svc = Service(cfg)
    svc.run_clean(svc.login("sys_admin", cfg.init_password))
    return svc


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_demo_eval_and_computed_results(demo_service, case, monkeypatch):
    def fail_network(*args, **kwargs):
        raise AssertionError("demo 不可建立外部 LLM client")
    monkeypatch.setattr("partpilot.service.AnthropicClient", fail_network)
    svc = demo_service
    user = svc.login(case["user"], TEST_PASSWORD)
    assert user is not None
    if user["role"] == "admin":
        with pytest.raises(PermissionError, match="權限規則"):
            svc.agent(user).ask(case["question"])
        return
    result = svc.agent(user).ask(case["question"])
    assert score_case(case, result["answer"])["correct"]
    assert result["tool_calls"] >= 1
    assert result["usage"]["cost_usd"] == 0
    saved = json.loads((Path(svc.cfg.state_dir) / "conversations" / (result["conv_id"] + ".json")).read_text())
    tool_results = [json.loads(block["content"]) for message in saved["messages"]
                    if isinstance(message["content"], list) for block in message["content"]
                    if block.get("type") == "tool_result"]
    actual = tool_results[-1]
    expected = case["expected"]
    if "rm_qty" in expected:
        assert {part: row["qty"] for part, row in actual["rm"].items()} == expected["rm_qty"]
    if "direct_parents" in expected:
        assert actual["finished_goods"] == expected["finished_goods"]
        assert [row["Parent"] for row in actual["direct_parents"]] == expected["direct_parents"]
    if "qty" in expected:
        assert actual["qty"] == expected["qty"]
    if "pos" in expected:
        assert [row["PONo"] for row in actual["pos"]] == expected["pos"]
    if "usable" in expected:
        assert [row["Alt"] for row in actual["usable"]] == expected["usable"]
        assert [row["Alt"] for row in actual["rejected"]] == expected["rejected"]
    if "shortages" in expected:
        assert {row["PartNo"]: row["short"] for row in actual["shortages"]} == expected["shortages"]
        assert actual["max_buildable"] == expected["max_buildable"]
    if "alt_used" in expected:
        assert {alt["Alt"]: alt["qty_used"] for row in actual["covered_by_inbound_or_alt"]
                for alt in row["alt_cover"]} == expected["alt_used"]
    if "error" in expected:
        assert actual["error"] == expected["error"]


def test_sample_data_reproducible_and_dirty(tmp_path):
    first, second = tmp_path / "one", tmp_path / "two"
    counts = make_sample_data(first)
    assert counts == make_sample_data(second)
    for name, count in counts.items():
        assert 50 <= count <= 300
        assert (first / name).read_bytes() == (second / name).read_bytes()
        assert (first / name).read_bytes() == (ROOT / "data/sample" / name).read_bytes()
        with (first / name).open(newline="") as stream:
            assert next(csv.reader(stream)) == HEADERS[name].split(",")
    run_clean(str(first), str(tmp_path / "clean"))
    data = load_clean(str(tmp_path / "clean"))
    issues = {row["Issue"]: row["Count"] for row in data["issues"]}
    for kind in ("duplicate_row", "invalid_attr_value", "invalid_UnitCostUSD", "invalid_eta",
                 "invalid_dates", "invalid_alternate_parts", "bom_cycle", "alt_pending"):
        assert issues[kind] >= 2
    assert "CAP-000101" in data["parts"] and "CAP-000102" in data["parts"]
    assert all(row["EffFrom"] == "2026-01-01" for row in data["bom"])
    assert not any(row["Parent"] in {"SA100111", "SA100112"} for row in data["bom"])


def test_demo_cli_question_option(demo_service):
    cfg = demo_service.cfg
    env = dict(os.environ, LLM_MODE="demo", LLM_API_KEY="", PARTPILOT_PASSWORD="",
               PARTPILOT_INIT_PASSWORD=TEST_PASSWORD, PARTPILOT_RAW_DIR=cfg.raw_dir,
               PARTPILOT_CLEAN_DIR=cfg.clean_dir, PARTPILOT_STATE_DIR=cfg.state_dir)
    result = subprocess.run([sys.executable, "-m", "partpilot", "ask", "--user", "scm_lead",
                             "--question", "SA100101 的 BOM"], cwd=ROOT, env=env,
                            text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert "CAP-000101" in result.stdout and "BOM" in result.stdout


def test_demo_simple_alternates_and_conversation(demo_service):
    user = demo_service.login("scm_lead", TEST_PASSWORD)
    agent = demo_service.agent(user)
    first = agent.ask("CAP-000101 的替代料")
    assert "CAP-000111" in first["answer"] and "alt_approval" in first["answer"]
    second = agent.ask("CAP-000101 庫存", first["conv_id"])
    assert second["conv_id"] == first["conv_id"] and "140" in second["answer"]


def test_demo_plant_default_and_late_po(demo_service):
    user = demo_service.login("plant_a", TEST_PASSWORD)
    result = demo_service.agent(user).ask("CAP-000101 庫存")
    assert "140" in result["answer"] and "PLANT-A" in result["answer"]
    result = demo_service.agent(user).ask("CAP-000102 在途")
    assert "生產日前可用在途：0" in result["answer"] and "PO00000002" in result["answer"]


@pytest.mark.parametrize("letter", ["A", "B", "C", "D"])
def test_neutral_plant_names_and_plant_control_accounts(demo_service, letter):
    from partpilot.clean import _plant as clean_plant
    from partpilot.engine import Engine
    from partpilot.tools import _plant as tool_plant

    plant = "PLANT-" + letter
    user = demo_service.login("plant_" + letter.lower(), TEST_PASSWORD)
    assert user is not None and user["role"] == "plant_control" and user["plant"] == plant
    for alias in (plant, "plant" + letter.lower(), "廠區 " + letter, "廠區" + letter):
        assert clean_plant(alias) == plant
        assert Engine._plant(alias) == plant
        assert tool_plant(alias) == plant
    result = demo_service.agent(user).ask("廠區 " + letter + " CAP-000101 庫存")
    assert "查詢拒絕" not in result["answer"] and plant in result["answer"]
    other = "B" if letter == "A" else "A"
    result = demo_service.agent(user).ask("廠區 " + other + " CAP-000101 庫存")
    assert "查詢拒絕" in result["answer"] and "權限" in result["answer"]


@pytest.mark.parametrize("configured", [False, True], ids=["demo-default", "real-container"])
def test_startup_initializes_and_preserves_accounts(tmp_path, configured):
    # 真正執行初始化與清理；只攔截最後的 serve，以便在禁止綁 port 的沙箱驗證腳本。
    wrapper = tmp_path / "python-wrapper"
    python = str(Path(sys.executable).absolute())
    wrapper.write_text(
        '#!/usr/bin/env bash\n'
        'if [[ "${1:-}" == "-u" && "${4:-}" == "serve" ]]; then\n'
        '  echo "serve_mode=$LLM_MODE args=$*"\n'
        '  exit 0\n'
        'fi\n'
        f'exec "{python}" "$@"\n')
    wrapper.chmod(0o700)
    env = dict(os.environ, LLM_MODE="real", PARTPILOT_PYTHON=str(wrapper),
               PARTPILOT_STATE_DIR=str(tmp_path / "state"), PARTPILOT_CLEAN_DIR=str(tmp_path / "clean"),
               PARTPILOT_PASSWORD="", PARTPILOT_HOST="127.0.0.1")
    env.pop("PARTPILOT_INIT_PASSWORD", None)
    password = secrets.token_urlsafe(18) if configured else None
    if configured:
        env["PARTPILOT_INIT_PASSWORD"] = password
    args = ["bash", "scripts/demo.sh"] + (["--configured-mode"] if configured else [])
    first = subprocess.run(args, cwd=ROOT, env=env, text=True, capture_output=True, timeout=20)
    assert first.returncode == 0, first.stderr
    assert "已建立 13 個示範帳號" in first.stdout
    assert "serve_mode=" + ("real" if configured else "demo") in first.stdout
    assert "--host 127.0.0.1 --port 18765" in first.stdout
    if not configured:
        password = first.stdout.split("本次示範初始密碼：", 1)[1].splitlines()[0]
        assert len(password) >= 24
    users = tmp_path / "state/users.json"
    original = users.read_bytes()
    assert UserStore(str(users)).authenticate("scm_lead", password) is not None
    assert (tmp_path / "clean/clean.json").exists()
    env["PARTPILOT_PASSWORD"] = password
    second = subprocess.run(args, cwd=ROOT, env=env, text=True, capture_output=True, timeout=20)
    assert second.returncode == 0, second.stderr
    assert "保留既有帳號與密碼" in second.stdout
    assert users.read_bytes() == original
