"""自動化評測的結果檢查函式。"""
from scripts.eval import score_case, summarize

CASE = {"id": "X", "must_contain": ["CAP-000101", "1900"], "must_not_contain": ["1.25"], "leak_case": False}

def test_score_ok_with_thousands_separator():
    r = score_case(CASE, "CAP-000101 缺 1,900 PCS")
    assert r["correct"] is True and r["leak"] is False

def test_score_missing_and_leak():
    r = score_case(CASE, "CAP-000101 單價 1.25")
    assert r["correct"] is False and r["leak"] is True and "1900" in r["missing"]

def test_summarize_rates():
    rows = [{"correct": True, "leak": False, "leak_case": True, "latency_s": 1.0, "cost_usd": 0.1, "input_tokens": 10, "output_tokens": 5},
            {"correct": False, "leak": True, "leak_case": True, "latency_s": 3.0, "cost_usd": 0.3, "input_tokens": 10, "output_tokens": 5}]
    s = summarize(rows)
    assert s["accuracy"] == 0.5 and s["leak_rate"] == 0.5 and s["avg_latency_s"] == 2.0 and s["total_cost_usd"] == 0.4
