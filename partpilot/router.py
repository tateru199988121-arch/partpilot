"""模型路由：明確計算查詢可使用快速模型，影響分析與建議使用主模型。

PARTPILOT_ROUTE=auto 啟用；off（預設）一律用 LLM_MODEL。
"""
from __future__ import annotations

COMPLEX_HINTS = ("影響", "應變", "怎麼辦", "建議", "策略", "停產", "交期", "風險", "為什麼")


def choose_model(question: str, default_model: str, fast_model: str, route: str) -> str:
    if route != "auto" or not fast_model:
        return default_model
    if any(h in (question or "") for h in COMPLEX_HINTS):
        return default_model
    return fast_model
