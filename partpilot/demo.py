"""離線關鍵字介面：只選擇工具，數量與權限一律由既有工具處理。"""
from __future__ import annotations

import json
import re
import unicodedata

from .llm import LLMResponse
from .answers import render, summarize

PART = re.compile(r"(?:FG[DA]\d{3}-\d{2}|SA\d{6}|[A-Z]{3}-\d{6})", re.I)


class DemoLLM:
    model = "demo"

    def __init__(self, user):
        self.user = user

    @staticmethod
    def _text(text):
        return LLMResponse([{"type": "text", "text": text}], {}, "demo", "end_turn")

    @staticmethod
    def _tool(name, args):
        return LLMResponse([{"type": "tool_use", "id": "demo_" + name, "name": name, "input": args}],
                           {}, "demo", "tool_use")

    def create(self, system, messages, tools, max_tokens=2000, model=None):
        question = next(m["content"] for m in reversed(messages)
                        if m["role"] == "user" and isinstance(m["content"], str))
        question = unicodedata.normalize("NFKC", question).upper()
        parts = PART.findall(question)
        last = messages[-1]["content"]
        if isinstance(last, list) and last and last[0].get("type") == "tool_result":
            try:
                result = json.loads(last[0]["content"])
            except ValueError:
                return self._text("工具結果過長或格式無效，請縮小查詢範圍。")
            name = messages[-2]["content"][0]["name"]
            if name == "where_used" and "替代料" in question and not result.get("error"):
                parents = [row["Parent"] for row in result.get("direct_parents", [])]
                parent = next((p for p in parts[1:] if p in parents), next(iter(parents), None))
                if parent is None:
                    return self._tool("alternate_records", {"part_no": parts[0]})
                args = {"primary": parts[0], "parent": parent, **self._date(question)}
                fgs = result.get("finished_goods", [])
                # 未指定成品時不猜上下階關係；GLOBAL/PARENT 仍可判斷，產品線範圍須補成品。
                fg = next((p for p in parts[1:] if p in fgs), None)
                if fg:
                    args["fg"] = fg
                return self._tool("get_alternates", args)
            return self._text(self._render(name, result))
        if not parts:
            return self._text("離線 demo 請提供料號，可查 BOM、用在哪裡、庫存、在途、替代料，或「做 N 台 料號 缺什麼料」。")
        part = parts[0]
        args = {"part_no": part, **self._date(question)}
        plant = re.search(r"PLANT-[ABCD]|廠區\s*[ABCD]", question)
        if plant:
            args["plant"] = plant[0]
        quantity = re.search(r"做\s*(\d+)\s*台", question)
        if quantity and ("缺" in question or "試算" in question):
            args.update(qty=int(quantity[1]), plant=args.get("plant", self.user.get("plant") or "PLANT-A"),
                        include_po="在途" in question, include_alt="替代料" in question)
            return self._tool("shortage", args)
        if "替代料" in question:
            return self._tool("where_used", {"part_no": part, **self._date(question)})
        if "用在哪" in question or "反查" in question:
            return self._tool("where_used", {"part_no": part, **self._date(question)})
        if "BOM" in question:
            return self._tool("explode_bom", {"part_no": part, "qty": 1, **self._date(question)})
        if "在途" in question:
            return self._tool("get_inbound", args)
        if "庫存" in question:
            return self._tool("get_inventory", args)
        return self._text("離線 demo 支援 BOM、用在哪裡、庫存、在途、替代料與缺料試算；請選擇上述查詢。")

    @staticmethod
    def _date(question):
        match = re.search(r"\d{4}-\d{2}-\d{2}", question)
        return {"prod_date": match[0]} if match else {}

    @staticmethod
    def _render(name, result):
        return render(summarize(name, result), result)
