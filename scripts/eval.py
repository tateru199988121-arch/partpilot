"""檢查預期回答，平行評測多個模型並產出比較報告。"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from partpilot.config import load_config
from partpilot.service import Service


def score_case(case: dict, answer: str) -> dict:
    answer = re.sub(r"(?<=\d),(?=\d{3})", "", answer)
    missing = [text for text in case["must_contain"] if text not in answer]
    leaked = [text for text in case["must_not_contain"] if text in answer]
    return {"id": case["id"], "correct": not missing and not leaked,
            "leak": bool(leaked), "missing": missing, "leaked": leaked,
            "leak_case": case["leak_case"]}


def summarize(rows: list) -> dict:
    n = len(rows)
    return {
        "accuracy": sum(bool(row["correct"]) for row in rows) / n if n else 0,
        "leak_rate": sum(bool(row["leak"]) for row in rows) / n if n else 0,
        "avg_latency_s": sum(row["latency_s"] for row in rows) / n if n else 0,
        "total_cost_usd": round(sum(row["cost_usd"] for row in rows), 4),
        "avg_input_tokens": sum(row["input_tokens"] for row in rows) / n if n else 0,
        "avg_output_tokens": sum(row["output_tokens"] for row in rows) / n if n else 0,
        "n": n,
    }


def run_case(svc, case, user, model):
    """每筆查詢建立獨立 Agent／對話；權限拒絕也要納入結果檢查。"""
    started = time.monotonic()
    try:
        if model == "auto":  # 路由設定：LLM_MODEL 為主模型、LLM_MODEL_FAST 為快速模型、PARTPILOT_ROUTE=auto
            svc.cfg.route = "auto"
            svc.cfg.fast_model = svc.cfg.fast_model or "claude-haiku-5-5"
            result = svc.agent(user).ask(case["question"])
        else:
            result = svc.agent(user, model=model).ask(case["question"])
    except PermissionError as exc:
        result = {"answer": "依權限規則拒絕查詢：" + str(exc),
                  "latency_s": time.monotonic() - started}
    usage = result.get("usage", {})
    return {
        **score_case(case, result["answer"]),
        "user": case["user"],
        "latency_s": result.get("latency_s", time.monotonic() - started),
        "tool_calls": result.get("tool_calls", 0),
        "input_tokens": sum(usage.get(key, 0) or 0 for key in
                            ("input_tokens", "cache_read_input_tokens",
                             "cache_creation_input_tokens")),
        "output_tokens": usage.get("output_tokens", 0) or 0,
        "cost_usd": usage.get("cost_usd", 0) or 0,
        "answer": result["answer"],
    }


def comparison_table(summaries):
    lines = [
        "| 模型 | 查詢數 | 正確率 | 越權率 | 平均延遲秒 | 總成本 USD | 平均輸入 tokens | 平均輸出 tokens |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for model, stats in summaries.items():
        lines.append(
            f"| {model} | {stats['n']} | {stats['accuracy']:.2%} | "
            f"{stats['leak_rate']:.2%} | {stats['avg_latency_s']:.4f} | "
            f"{stats['total_cost_usd']:.4f} | {stats['avg_input_tokens']:.2f} | "
            f"{stats['avg_output_tokens']:.2f} |")
    return "\n".join(lines)


def build_report(results):
    summaries = {model: summarize(rows) for model, rows in results.items()}
    table = comparison_table(summaries)
    models = list(results)
    lines = ["# 模型評測報告", "", table, "", "## 每筆查詢對錯矩陣", "",
             "| 案例名稱 | 使用者 | " + " | ".join(models) + " |",
             "| --- | --- | " + " | ".join("---" for _ in models) + " |"]
    for index, row in enumerate(results[models[0]]):
        cells = []
        for model in models:
            scored = results[model][index]
            cells.append("對" if scored["correct"] else
                         ("錯（越權）" if scored["leak"] else "錯"))
        lines.append(f"| {row['id']} | {row['user']} | " + " | ".join(cells) + " |")

    # 使用未四捨五入的總成本比較，避免小額成本在顯示時成為平手。
    costs = {model: sum(row["cost_usd"] for row in rows)
             for model, rows in results.items()}
    best = min(models, key=lambda model: (-summaries[model]["accuracy"], costs[model]))
    tied = [model for model in models
            if summaries[model]["accuracy"] == summaries[best]["accuracy"]]
    reason = ("正確率最高" if len(tied) == 1 else
              "正確率並列最高，再依總成本由低到高選擇；成本也相同時依模型輸入順序選擇")
    lines.extend(["", "## 路由建議", "",
                  f"建議路由至 `{best}`：{reason}。本次正確率為 "
                  f"{summaries[best]['accuracy']:.2%}，總成本為 {costs[best]:.8f} USD。"
                  "建議僅依本次所選評測查詢與模型比較結果。",
                  "", "成本採 Agent 回傳的估算值；定價可用 `PARTPILOT_PRICE_<MODEL>=in,out`"
                  "（每百萬 tokens 的 USD）設定，未設定時使用系統預設定價。"
                  "輸入 tokens 包含一般輸入、快取讀取與快取建立。", ""])
    return table, "\n".join(lines)


def positive_int(value):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("workers 必須為正整數") from None
    if number < 1:
        raise argparse.ArgumentTypeError("workers 必須為正整數")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description="自動化問答評測與模型比較（會呼叫設定的 LLM）")
    parser.add_argument("--models", default="claude-opus-5-5,claude-sonnet-5-5",
                        help="以逗號分隔的模型名稱（預設：%(default)s）")
    parser.add_argument("--cases", help="只執行指定案例名稱，以逗號分隔，例如 bom_expansion,where_used")
    parser.add_argument("--workers", type=positive_int, default=4,
                        help="平行工作數（預設：%(default)s）")
    args = parser.parse_args(argv)
    models = list(dict.fromkeys(model.strip() for model in args.models.split(",")))
    if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", model) for model in models):
        parser.error("模型名稱不可為空，且僅可包含英數字、點、底線與連字號")

    try:
        eval_dir = ROOT / "eval"
        with (eval_dir / "eval_set.json").open(encoding="utf-8") as stream:
            cases = json.load(stream)["cases"]
        if args.cases is not None:
            selected = {case_id.strip() for case_id in args.cases.split(",")}
            unknown = selected - {case["id"] for case in cases}
            if unknown:
                parser.error("未知或空白的評測案例名稱：" + ", ".join(sorted(unknown)))
            cases = [case for case in cases if case["id"] in selected]
        if not cases:
            parser.error("沒有可執行的評測查詢")

        cfg = load_config()
        svc = Service(cfg)
        users = []
        for case in cases:
            user = svc.login(case["user"], cfg.password)
            if not user:
                print(f"登入失敗：使用者 {case['user']} 的帳號或密碼錯誤，請檢查 PARTPILOT_PASSWORD。",
                      file=sys.stderr)
                return 1
            users.append(user)

        results = {}
        for model in models:
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                futures = [pool.submit(run_case, svc, case, user, model)
                           for case, user in zip(cases, users)]
                rows = [future.result() for future in futures]
            results[model] = rows
            with (eval_dir / f"results-{model}.json").open("w", encoding="utf-8") as stream:
                json.dump(rows, stream, ensure_ascii=False, indent=2)
                stream.write("\n")

        table, report = build_report(results)
        (eval_dir / "report.md").write_text(report, encoding="utf-8")
        print(table)
        return 0
    except Exception as exc:
        print(f"評測失敗：{type(exc).__name__}；請檢查設定、資料與服務狀態。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
