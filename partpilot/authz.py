"""角色權限。所有料件資料出口都經過這裡。"""
from __future__ import annotations

GROUPS = {
    "功率半導體": ["MOS", "IGB", "DIO"],
    "被動元件": ["CAP", "RES", "IND"],
    "IC": ["ICS", "MCU"],
    "機電與連接": ["RLY", "CON", "CBL", "FUS", "SPD", "RCD", "MTR"],
    "機構與其他": ["PCB", "MEC", "FAN", "LCD", "SEN"],
}
SENSITIVE = {"SupplierName", "MfrPartNo"}
HIGHLY = {"UnitCostUSD", "UnitPriceUSD"}

_PART_FIELDS = {
    "PartNo", "Part", "part", "Child", "Parent", "Alt", "Primary",
    "AltPartNo", "PrimaryPartNo", "primary", "parent", "child", "fg",
}
_PART_RE = __import__("re").compile(r"\b(?:FG[DA]\d{3}-\d{2}|SA\d{6}|[A-Z]{3}-\d{6})\b")
_FREE_TEXT = {"notes", "note", "message", "reasons"}
_PART_LISTS = {"finished_goods", "part_nos", "only_parts"}
_DASHES = str.maketrans({c: "-" for c in "‐‑‒–—−﹣－"})
_REMOVED = object()


def _part_no(value: str) -> str:
    return "".join(value.split()).translate(_DASHES).upper()


class PermissionDenied(Exception):
    """訊息為中文、可直接給使用者看；不得含被拒資料的內容。"""


class Policy:
    """user: {"username","role":"supply_chain".."admin","product_line","plant","commodity_group"}；engine: partpilot.engine.Engine。"""

    def __init__(self, user: dict, engine):
        self.user = user
        self.engine = engine
        self._scope_ready = False
        self._scope = None

    def scope_parts(self):
        """product_line/component 可看的料號集合（動態由 BOM／替代料／主檔計算）；supply_chain/plant_control 回 None 表示全集團；admin 回空集合。"""
        if self._scope_ready:
            return self._scope
        role = self.user.get("role")
        parts = self.engine.data.get("parts", {})
        bom = self.engine.data.get("bom", [])
        if role in {"supply_chain", "plant_control"}:
            scope = None
        elif role in {"product_line", "component"}:
            edges = {}
            if role == "product_line":
                prefix = {"DC": "FGD", "AC": "FGA"}.get(
                    self.user.get("product_line"))
                scope = {p for p, row in parts.items()
                         if prefix and p.startswith(prefix) and row.get("PartType") == "FG"}
                for row in bom:
                    edges.setdefault(row["Parent"], set()).add(row["Child"])
            else:
                commodities = GROUPS.get(self.user.get("commodity_group"), [])
                scope = {p for p, row in parts.items()
                         if row.get("PartType") == "RM" and row.get("Commodity") in commodities}
                for row in bom:
                    edges.setdefault(row["Child"], set()).add(row["Parent"])
            pending = list(scope)
            while pending:
                for related in edges.get(pending.pop(), ()):
                    if related not in scope:
                        scope.add(related)
                        pending.append(related)
            if role == "product_line":
                # 替代關係以 BOM 範圍為起點，不因資料列順序串接替代料。
                bom_scope = scope.copy()
                for row in self.engine.data.get("alternates", []):
                    if row["Primary"] in bom_scope:
                        scope.add(row["Alt"])
                    if row.get("Bidirectional") in (True, "Y") and row["Alt"] in bom_scope:
                        scope.add(row["Primary"])
            scope = frozenset(scope)
        else:
            scope = frozenset()
        self._scope = scope
        self._scope_ready = True
        return scope

    def check_part(self, part_no: str) -> None:
        """料號不在範圍 → PermissionDenied。"""
        scope = self.scope_parts()
        if scope is not None and _part_no(part_no) not in scope:
            raise PermissionDenied("依權限規則不可查看此料件資料。")

    def check_plant(self, plant: str) -> None:
        """plant_control 只能看自己工廠的庫存與在途 → 其他工廠 PermissionDenied。"""
        if self.user.get("role") == "plant_control":
            own = self.user.get("plant")
            if not own or not plant or plant.strip().upper() != own.strip().upper():
                raise PermissionDenied("依權限規則不可查看此工廠資料。")

    def redact(self, obj):
        """遞迴移除該角色不可看的欄位（敏感、高度敏感），並移除範圍外料號的資料列。回傳新物件。"""
        role = self.user.get("role")
        if role not in {"supply_chain", "product_line", "plant_control", "component"}:
            return [] if isinstance(obj, list) else {} if isinstance(obj, dict) else None
        scope = self.scope_parts()
        hidden = (HIGHLY if role != "component" else set()) | (
            SENSITIVE if role not in {"plant_control", "component"} else set())

        def outside(value):
            return scope is not None and isinstance(value, str) and _part_no(value) not in scope

        def visit(value, field=None):
            if isinstance(value, dict):
                if any(key in _PART_FIELDS and outside(item) for key, item in value.items()):
                    return _REMOVED
                if (role == "product_line" and value.get("Scope") == "PRODUCTLINE"
                        and value.get("ScopeValue") not in (None, "", self.user.get("product_line"))):
                    return _REMOVED
                sv = value.get("ScopeValue")
                if isinstance(sv, str) and sv in self.engine.data.get("parts", {}) and outside(sv):
                    return _REMOVED
                result = {}
                for key, item in value.items():
                    if key in hidden:
                        continue
                    # rm 等資料以料號作為 key，而非列內的 PartNo。
                    if scope is not None and (field == "rm" or key in self.engine.data.get("parts", {})) and outside(key):
                        continue
                    cleaned = visit(item, key)
                    if cleaned is not _REMOVED:
                        result[key] = cleaned
                return result
            if isinstance(value, str) and scope is not None and field in _FREE_TEXT:
                if any(outside(m) for m in _PART_RE.findall(value)):
                    return _REMOVED
                return value
            if isinstance(value, list):
                if field == "path" and any(outside(p) for p in value):
                    return _REMOVED
                result = []
                for item in value:
                    if field == "paths" and isinstance(item, list) and any(outside(p) for p in item):
                        continue
                    is_part = isinstance(item, str) and item in self.engine.data.get("parts", {})
                    if (field in _PART_LISTS or field == "path" or is_part) and outside(item):
                        continue
                    cleaned = visit(item, field if isinstance(item, str) else None)
                    if cleaned is not _REMOVED:
                        result.append(cleaned)
                return result
            return value

        result = visit(obj)
        return {} if result is _REMOVED else result

    def can_approve(self, commodity: str) -> bool:
        """只有 component 且 commodity 在自己的群組內。"""
        return (self.user.get("role") == "component"
                and commodity in GROUPS.get(self.user.get("commodity_group"), []))
