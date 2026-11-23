"""CLI 與 HTTP 共用的登入、授權與業務流程。"""
import datetime as dt
import json
from pathlib import Path
import threading

from .router import choose_model
from .agent import Agent
from .audit import Audit, UsageLog
from .auth import UserStore, _write_private_json
from .authz import Policy
from .clean import run_clean, load_clean
from .engine import Engine
from .llm import AnthropicClient, FakeLLM
from .demo import DemoLLM
from .tools import tool_specs, ToolExecutor


class Service:
    def __init__(self, cfg):
        self.cfg = cfg
        self.state_dir = Path(cfg.state_dir)
        self.users = UserStore(str(self.state_dir / "users.json"))
        self.audit = Audit(str(self.state_dir / "audit.jsonl"))
        self.usage = UsageLog(str(self.state_dir / "usage.jsonl"))
        self._engine = None
        self._lock = threading.RLock()

    def _require(self, user, roles, action):
        if user.get("role") not in roles:
            self.audit.log(user, "denied", {"action": action})
            raise PermissionError("依權限規則，此操作僅限 " + "、".join(sorted(roles)))

    def login(self, username, password):
        user = self.users.authenticate(username, password)
        self.audit.log(user or {"username": username}, "login" if user else "login_failed", {})
        return user

    def _approvals(self):
        path = self.state_dir / "approvals.json"
        if not path.exists():
            return {}
        with path.open(encoding="utf-8") as stream:
            return json.load(stream)

    @staticmethod
    def _counts(summary):
        return {"counts": {key: value for key, value in summary.get("counts", {}).items()
                           if isinstance(value, (int, float))}, "issues": summary.get("issues", 0)
                if isinstance(summary.get("issues", 0), (int, float)) else 0}

    def run_clean(self, user):
        self._require(user, {"admin"}, "clean")
        with self._lock:
            self._engine = None
            summary = self._counts(run_clean(self.cfg.raw_dir, self.cfg.clean_dir, self._approvals()))
            self.audit.log(user, "clean", summary)
            return summary

    def engine(self):
        with self._lock:
            if self._engine is None:
                if not (Path(self.cfg.clean_dir) / "clean.json").exists():
                    raise ValueError("尚無清理後資料，請先由 admin 系統管理員執行資料清理")
                self._engine = Engine(load_clean(self.cfg.clean_dir))
            return self._engine

    def agent(self, user, model=None):
        self._require(user, {"supply_chain", "product_line", "plant_control", "component"}, "ask")
        engine = self.engine()
        if self.cfg.mode == "demo":
            llm = DemoLLM(user)
        elif self.cfg.mode == "fake":
            llm = FakeLLM([{"content": [{"type": "text", "text": "（離線模式）"}]}])
        else:
            if not self.cfg.api_key:
                raise ValueError("請設定 LLM_API_KEY，或設定 LLM_MODE=demo 使用離線模式")
            llm = AnthropicClient(self.cfg.api_key, self.cfg.model, self.cfg.base_url or None)
        return Agent(llm, user, tool_specs(user), ToolExecutor(user, engine, self.audit),
                     str(self.state_dir), self.audit, self.usage,
                     model="demo" if self.cfg.mode == "demo" else model if model is not None else self.cfg.model,
                     router=None if model is not None or self.cfg.mode == "demo" else
                     (lambda q: choose_model(q, self.cfg.model, self.cfg.fast_model, self.cfg.route)))

    def review_items(self, user):
        self._require(user, {"component"}, "review")
        engine = self.engine()
        policy = Policy(user, engine)
        items = []
        for item in engine.data.get("review", []):
            commodity = item.get("Commodity")
            if commodity == "SA" and item.get("Kind", "").startswith("bom_"):
                items.append({**item, "Options": [], "can_approve": False,
                              "approval_note": "SA 類別的 BOM 無任何 component 群組可核准，保留待確認"})
            elif policy.can_approve(commodity):
                items.append({**item, "can_approve": True})
        return items

    def own_approvals(self, user):
        self._require(user, {"component"}, "review")
        return [{"ReviewID": key, **record} for key, record in self._approvals().items()
                if record.get("by") == user["username"]]

    def decide(self, user, review_id, decision, choice=None):
        self._require(user, {"component"}, "approve")
        if decision not in {"approve", "reject"}:
            raise ValueError("decision 必須為 approve 或 reject")
        with self._lock:
            engine = self.engine()
            item = next((item for item in engine.data.get("review", []) if item["ReviewID"] == review_id), None)
            if item is None or item.get("Commodity") == "SA" or not Policy(user, engine).can_approve(item.get("Commodity")):
                self.audit.log(user, "denied", {"action": "approve"})
                raise PermissionError("依權限規則，只可核准負責物料群組的待確認項目")
            if decision == "approve" and item.get("Kind", "").endswith("conflict"):
                if type(choice) is not int or not 0 <= choice < len(item.get("Options", [])):
                    raise ValueError("衝突資料核准須選擇有效的候選索引")
            elif choice is not None:
                if type(choice) is not int or not 0 <= choice < len(item.get("Options", [])):
                    raise ValueError("候選索引無效")
            record = dict(by=user["username"], at=dt.datetime.now(dt.timezone.utc).isoformat(),
                          decision=decision, choice=choice)
            approvals = self._approvals()
            approvals[review_id] = record
            _write_private_json(self.state_dir / "approvals.json", approvals)
            self.audit.log(user, "approve", {"review_id": review_id, **record})
            self._engine = None
            summary = self._counts(run_clean(self.cfg.raw_dir, self.cfg.clean_dir, approvals))
            self.engine()
            return {"ReviewID": review_id, **record, **summary}

    def audit_view(self, user, limit=200):
        from .audit import sanitize
        return [sanitize(r) for r in self.audit.read(user, limit)]

    def usage_summary(self, user):
        self._require(user, {"admin"}, "usage_summary")
        return self.usage.summary()
