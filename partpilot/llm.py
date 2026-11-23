"""LLM 介面。真實版 AnthropicClient、離線版 FakeLLM，由建構子注入。"""
from __future__ import annotations

from typing import List, Optional
from copy import deepcopy
import anthropic


class LLMError(Exception):
    """外部錯誤（逾時、429、5xx、格式錯）統一轉成這個。"""


class LLMResponse:
    def __init__(self, content: list, usage: dict, model: str, stop_reason: str):
        self.content = content          # [{"type":"text","text":...} | {"type":"tool_use","id","name","input"}]
        self.usage = usage              # {"input_tokens","output_tokens","cache_read_input_tokens","cache_creation_input_tokens"}
        self.model = model
        self.stop_reason = stop_reason  # "end_turn" | "tool_use" | ...


class FakeLLM:
    """依序回傳預先寫好的回應（list of dict：{"content": [...], "usage": {...}, "stop_reason": ...}）。記錄每次呼叫參數到 self.calls。"""

    def __init__(self, script: List[dict], model: str = "fake"):
        self.script = deepcopy(script)
        self.model = model
        self.calls = []

    def create(self, system: str, messages: list, tools: list, max_tokens: int = 2000, model: Optional[str] = None) -> LLMResponse:
        self.calls.append(deepcopy(dict(system=system, messages=messages, tools=tools,
                                        max_tokens=max_tokens, model=model or self.model)))
        if not self.script:
            raise LLMError("離線回應腳本已用完")
        response = self.script.pop(0)
        return LLMResponse(response.get("content", []), response.get("usage", {}),
                           response.get("model", model or self.model), response.get("stop_reason", "end_turn"))


class AnthropicClient:
    """用 anthropic SDK；api_key/base_url/model 由建構子傳入（來源是環境變數 LLM_API_KEY/LLM_BASE_URL/LLM_MODEL）。
    不可傳 temperature（此模型不接受）。system 與最後一個 tool 加 cache_control ephemeral 以利 prompt caching。"""

    def __init__(self, api_key: str, model: str, base_url: Optional[str] = None, timeout: float = 120):
        self.model = model
        try:
            self.client = anthropic.Anthropic(api_key=api_key, base_url=base_url,
                                              timeout=timeout, max_retries=2)
        except Exception as exc:
            raise LLMError("LLM 初始化錯誤：" + type(exc).__name__) from exc

    def create(self, system: str, messages: list, tools: list, max_tokens: int = 8000, model: Optional[str] = None) -> LLMResponse:
        cached_tools = deepcopy(tools)
        if cached_tools:
            cached_tools[-1]["cache_control"] = {"type": "ephemeral"}
        try:
            response = self.client.messages.create(
                model=model or self.model, max_tokens=max_tokens,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=messages, tools=cached_tools)
            content = []
            for block in response.content:
                if block.type == "text":
                    content.append({"type": "text", "text": block.text})
                elif block.type == "tool_use":
                    content.append({"type": "tool_use", "id": block.id, "name": block.name, "input": block.input})
                else:
                    # thinking / redacted_thinking 等區塊原樣保留，工具回合要原樣送回
                    content.append(block.model_dump(exclude_none=True))
            usage = {key: getattr(response.usage, key, 0) or 0 for key in
                     ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")}
            return LLMResponse(content, usage, response.model, response.stop_reason)
        except Exception as exc:
            # 不回傳 SDK 的完整 request/response，避免洩漏憑證與未經授權的內容。
            raise LLMError("LLM 呼叫錯誤：" + type(exc).__name__) from exc
