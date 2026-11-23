"""依資料字典清理六份 CSV；尚未確認的紀錄不參與問答計算。"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import math
import re
import unicodedata
from collections import Counter, defaultdict
from decimal import Decimal, DecimalException
from pathlib import Path
from typing import Optional


BASE_DATE = "2026-11-06"
FILES = {
    "parts_master.csv": "parts", "part_attributes.csv": "attrs",
    "bom.csv": "bom", "alternates.csv": "alternates",
    "inventory.csv": "inventory", "open_po.csv": "po",
}
COMMODITIES = set("MOS IGB DIO CAP RES IND ICS MCU RLY CON CBL FUS SPD RCD MTR PCB MEC FAN LCD SEN".split())
PART_PATTERNS = {
    "FG": re.compile(r"^FG[DA]\d{3}-\d{2}$"),
    "SA": re.compile(r"^SA\d{6}$"),
    "RM": re.compile(r"^(MOS|IGB|DIO|CAP|RES|IND|ICS|MCU|RLY|CON|CBL|FUS|SPD|RCD|MTR|PCB|MEC|FAN|LCD|SEN)-\d{6}$"),
}
NUMBER = r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?"
NUMERIC = re.compile(NUMBER)
ATTRS = "Capacitance Resistance Inductance RatedVoltage RatedCurrent PowerRating Tolerance TempMin TempMax MSL ShelfLifeMonths Package RoHS SafetyCert".split()
ATTR_CODES = {a.lower(): a for a in ATTRS}
STANDARD_UNITS = {
    "Capacitance": "µF", "Resistance": "Ω", "Inductance": "µH",
    "RatedVoltage": "V", "RatedCurrent": "A", "PowerRating": "W",
    "Tolerance": "%", "TempMin": "°C", "TempMax": "°C", "ShelfLifeMonths": "月",
}
ISSUE_LABELS = {
    "duplicate_row": "所有欄位完全相同的重複列",
    "invalid_csv_row": "CSV 欄位數不符",
    "invalid_part_no": "料號格式無效",
    "invalid_part_type": "料號類型與格式或檔案要求不符",
    "part_conflict": "同料號主檔內容衝突",
    "part_conflict_reference": "關聯料號主檔尚未確認或已拒絕",
    "orphan_part": "料號不存在於有效主檔（孤兒資料）",
    "invalid_uom": "單位無效、無法換算或與主檔不符",
    "invalid_commodity": "物料類別不在允許清單",
    "product_line_mismatch": "成品產品線與料號不一致",
    "invalid_product_line": "半成品產品線無效",
    "invalid_lifecycle": "生命週期無效",
    "invalid_make_buy": "自製／外購代碼無效",
    "invalid_LeadTimeDays": "前置天數不是正整數",
    "invalid_MOQ": "最小訂購量不是正整數",
    "invalid_UnitCostUSD": "標準單價不是大於零的數字",
    "invalid_attr_code": "屬性代碼不在允許清單",
    "invalid_attr_value": "屬性缺值、無法換算或單位不符",
    "duplicate_attribute": "屬性換算後相同",
    "attr_conflict": "同料號屬性換算後不同",
    "invalid_bom_part_type": "BOM 上下階類型無效",
    "invalid_line_or_qty": "BOM 行號或用量無效",
    "invalid_dates": "日期格式、日期值或起訖順序無效",
    "invalid_scrap_rate": "損耗率無法判讀或超出 0～0.18",
    "bom_conflict": "同上階、版次與行號的 BOM 內容衝突",
    "bom_cycle": "BOM 行位於循環參照",
    "bom_cycle_rejected": "component 已拒絕環上的 BOM 行",
    "invalid_alternate_parts": "主料與替代料相同或不是原材料",
    "invalid_alt_record_id": "替代料紀錄編號格式無效",
    "invalid_scope": "替代料範圍或範圍值無效",
    "invalid_priority_or_ratio": "優先順序不是整數或用量比無效",
    "invalid_flags_or_bundle": "雙向、混用或成組代碼無效",
    "invalid_approval_status": "核准狀態不在允許清單",
    "alt_conflict": "同主料、替代料與範圍的紀錄衝突",
    "alt_pending": "替代料來源紀錄為 Pending",
    "invalid_snapshot_date": "庫存快照日無效或不是基準日",
    "invalid_plant": "工廠或別名無效",
    "invalid_quantity": "數量非數字、負值或保留／已收超過總量",
    "invalid_datecode": "製造週別格式無效或 ISO 年中不存在該週",
    "invalid_qc_status": "品檢狀態不在允許清單",
    "invalid_po_number_or_line": "採購單號格式無效或項次不是整數",
    "invalid_status": "採購單狀態不在允許清單",
    "invalid_order_date": "下單日期無效",
    "eta_before_order_date": "預計到廠日早於下單日",
    "invalid_eta": "預計到廠日無效或缺值",
    "invalid_unit_price": "採購單價不是大於零的數字",
}
PREFIXES = {"": Decimal(1), "p": Decimal("1e-12"), "n": Decimal("1e-9"),
            "u": Decimal("1e-6"), "µ": Decimal("1e-6"), "μ": Decimal("1e-6"),
            "m": Decimal("1e-3"), "k": Decimal(1000), "K": Decimal(1000), "M": Decimal("1e6")}
PLANTS = {}
for _code, _alias, _name in (("PLANT-C", "PLANTC", "廠區C"), ("PLANT-D", "PLANTD", "廠區D"),
                            ("PLANT-A", "PLANTA", "廠區A"), ("PLANT-B", "PLANTB", "廠區B")):
    for _label in (_code, _alias, _name):
        PLANTS[_label] = _code


def normalize_part(value: str) -> str:
    """所有輸入檔案共用的料號正規化；不猜測 O/0 等錯字。"""
    return unicodedata.normalize("NFKC", value).strip().upper().translate(str.maketrans("–—－‐", "----"))


def _part_type(part: str) -> Optional[str]:
    return next((kind for kind, pattern in PART_PATTERNS.items() if pattern.fullmatch(part)), None)


def _commodity(part: str) -> str:
    return part.split("-", 1)[0] if _part_type(part) == "RM" else (_part_type(part) or "")


def _number(value: str) -> Optional[float]:
    value = value.strip()
    if not NUMERIC.fullmatch(value):
        return None
    try:
        result = float(value)
    except (ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _integer(value: str, positive: bool = False) -> Optional[int]:
    value = value.strip()
    if not re.fullmatch(r"[+-]?[0-9]+", value):
        return None
    try:
        result = int(value)
    except ValueError:
        return None
    return result if not positive or result > 0 else None


def _positive(value: str) -> Optional[float]:
    result = _number(value)
    return result if result is not None and result > 0 else None


def _date(value: str) -> Optional[str]:
    value = value.strip()
    if re.fullmatch(r"[0-9]{8}", value):
        value = value[:4] + "-" + value[4:6] + "-" + value[6:]
    elif re.fullmatch(r"[0-9]{4}/[0-9]{2}/[0-9]{2}", value):
        value = value.replace("/", "-")
    elif not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        return None
    try:
        return dt.date.fromisoformat(value).isoformat()
    except ValueError:
        return None


def _enum(value: str, choices: tuple) -> Optional[str]:
    value = value.strip().lower()
    return next((choice for choice in choices if choice.lower() == value), None)


def _plant(value: str) -> Optional[str]:
    return PLANTS.get("".join(value.split()).upper())


def _unit(value: str, target: str, allowed: tuple) -> Optional[float]:
    value = value.strip().upper()
    units = {"PCS": ("PCS", 1), "EA": ("PCS", 1), "KPCS": ("PCS", 1000),
             "M": ("M", 1), "CM": ("M", .01), "MM": ("M", .001)}
    if value not in allowed or value not in units or units[value][0] != target:
        return None
    return units[value][1]


def _attr_unit(code: str, unit: str) -> Optional[Decimal]:
    """回傳換算到標準單位的乘數；不將不相容的單位默認為標準單位。"""
    unit = "".join(unit.split()).replace("℃", "°C").replace("Ω", "Ω")
    if not unit:
        return Decimal(1)
    if code in ("TempMin", "TempMax"):
        return Decimal(1) if unit in ("°C", "C") else None
    if code == "Tolerance":
        return Decimal(1) if unit == "%" else None
    if code == "ShelfLifeMonths":
        return Decimal(1) if unit == "月" else None
    base, standard_scale = {
        "Capacitance": ("F", Decimal("1e-6")), "Resistance": ("Ω", Decimal(1)),
        "Inductance": ("H", Decimal("1e-6")), "RatedVoltage": ("V", Decimal(1)),
        "RatedCurrent": ("A", Decimal(1)), "PowerRating": ("W", Decimal(1)),
    }[code]
    unit = re.sub("ohm", "Ω", unit, flags=re.IGNORECASE)
    if code == "Resistance" and unit in ("k", "K", "M"):
        unit += "Ω"
    if not unit.endswith(base):
        return None
    prefix = unit[:-len(base)]
    return PREFIXES[prefix] / standard_scale if prefix in PREFIXES else None


def _attribute(code: str, value: str, unit: str):
    value = value.strip()
    if not value or value.upper() in ("TBD", "N/A"):
        return None
    if code == "Package":
        return re.sub(r"[\s-]+", "", value).upper() or None
    if code == "RoHS":
        value = value.upper()
        return value if value in ("Y", "N") else None
    if code == "SafetyCert":
        return sorted({re.sub(r"\s+", "", item).upper() for item in value.split(";") if item.strip()}) or None
    if code == "MSL":
        return value
    value = value.replace("−", "-")
    if code == "Tolerance" and value.startswith("±"):
        value = value[1:].lstrip()
    match = re.fullmatch(r"(" + NUMBER + r")\s*(.*?)", value)
    if not match:
        return None
    inline = match.group(2)
    multiplier = _attr_unit(code, inline or unit)
    # 兩欄都寫單位時，兩者也都必須能換算且一致。
    if inline and unit.strip() and _attr_unit(code, unit) != multiplier:
        return None
    if multiplier is None:
        return None
    try:
        result = float(Decimal(match.group(1)) * multiplier)
    except (DecimalException, OverflowError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _review_id(kind: str, key: str) -> str:
    return hashlib.sha1((kind + key).encode("utf-8")).hexdigest()[:10]


def _cycle_edges(rows: list) -> set:
    """以兩次迭代 DFS 找強連通分量，回傳所有位於環上的行（包括自環）。"""
    graph, reverse = defaultdict(list), defaultdict(list)
    nodes = set()
    for row in rows:
        parent, child = row["Parent"], row["Child"]
        graph[parent].append(child)
        reverse[child].append(parent)
        nodes.update((parent, child))
    seen, order = set(), []
    for start in sorted(nodes):
        if start in seen:
            continue
        seen.add(start)
        stack = [(start, iter(graph[start]))]
        while stack:
            node, children = stack[-1]
            child = next(children, None)
            if child is None:
                order.append(node)
                stack.pop()
            elif child not in seen:
                seen.add(child)
                stack.append((child, iter(graph[child])))
    components, sizes = {}, Counter()
    for start in reversed(order):
        if start in components:
            continue
        component = len(sizes)
        components[start] = component
        stack = [start]
        while stack:
            node = stack.pop()
            sizes[component] += 1
            for parent in reverse[node]:
                if parent not in components:
                    components[parent] = component
                    stack.append(parent)
    return {i for i, row in enumerate(rows)
            if components[row["Parent"]] == components[row["Child"]]
            and (sizes[components[row["Parent"]]] > 1 or row["Parent"] == row["Child"])}


class _Cleaner:
    def __init__(self, raw_dir: str, approvals: Optional[dict]):
        self.raw_dir = Path(raw_dir)
        self.approvals = approvals or {}
        self.problems = Counter()
        self.actions = {}
        self.inputs = Counter()
        self.data = {"meta": {"base_date": BASE_DATE}, "parts": {}, "attrs": {}, "bom": [],
                     "alternates": [], "inventory": [], "po": [], "review": [], "issues": [],
                     "excluded": {key: 0 for key in FILES.values()}}
        self.blocked = set()

    def issue(self, file: str, issue: str, action: str, count: int = 1):
        self.problems[file, issue] += count
        self.actions[file, issue] = action

    def drop(self, file: str, issue: str, count: int = 1):
        self.issue(file, issue, "排除該列，不參與計算", count)
        self.data["excluded"][FILES[file]] += count

    def rows(self, file: str):
        seen = set()
        with (self.raw_dir / file).open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                self.inputs[file] += 1
                if None in row or any(value is None for value in row.values()):
                    self.drop(file, "invalid_csv_row")
                    continue
                signature = tuple(row.items())
                if signature in seen:
                    self.drop(file, "duplicate_row")
                    continue
                seen.add(signature)
                yield row

    def choose(self, file: str, kind: str, key: str, commodity: str, options: list, reason: str):
        review = {"ReviewID": _review_id(kind, key), "Kind": kind, "Commodity": commodity,
                  "Key": key, "Reason": reason, "Options": options}
        self.issue(file, kind, "依 component 確認採用指定候選或全部拒絕；未確認前不生效", len(options))
        approval = self.approvals.get(review["ReviewID"], {})
        if isinstance(approval, dict):
            if approval.get("decision") == "reject":
                return None
            choice = approval.get("choice")
            if (approval.get("decision") == "approve" and type(choice) is int
                    and 0 <= choice < len(options)):
                return options[choice]
        self.data["review"].append(review)
        return None

    def reference(self, file: str, *part_nos: str) -> bool:
        if any(_part_type(part) is None for part in part_nos):
            self.drop(file, "invalid_part_no")
            return False
        if any(part in self.blocked for part in part_nos):
            self.drop(file, "part_conflict_reference")
            return False
        if any(part not in self.data["parts"] for part in part_nos):
            self.drop(file, "orphan_part")
            return False
        return True

    def parts(self):
        file = "parts_master.csv"
        groups = defaultdict(list)
        for row in self.rows(file):
            part = normalize_part(row["PartNo"])
            if _part_type(part) is None:
                self.drop(file, "invalid_part_no")
            else:
                groups[part].append(row)
        for part, options in groups.items():
            row = options[0]
            if len(options) > 1:
                row = self.choose(file, "part_conflict", part, _commodity(part), options, "同一料號的主檔內容不同")
                self.data["excluded"]["parts"] += len(options) - (row is not None)
                if row is None:
                    self.blocked.add(part)
                    continue
            kind = _part_type(part)
            if row["PartType"].strip() != kind:
                self.drop(file, "invalid_part_type")
                continue
            uom = row["UoM"].strip().upper()
            if uom == "EA":
                uom = "PCS"
            if uom not in ("PCS", "M"):
                self.drop(file, "invalid_uom")
                continue
            result = {key: row[key].strip() for key in ("PartDesc", "MakeBuy", "SupplierName", "MfrPartNo")}
            result.update(PartNo=part, PartType=kind, UoM=uom)
            commodity = row["Commodity"].strip()
            result["Commodity"] = commodity if kind == "RM" and commodity in COMMODITIES else None
            if kind == "RM" and commodity not in COMMODITIES:
                self.issue(file, "invalid_commodity", "類別設為 null；審查分工依料號類別碼")
            product_line = row["ProductLine"].strip()
            if kind == "FG":
                result["ProductLine"] = "DC" if part.startswith("FGD") else "AC"
                if product_line != result["ProductLine"]:
                    self.issue(file, "product_line_mismatch", "以成品料號判定產品線")
            else:
                result["ProductLine"] = product_line if kind == "SA" and product_line in ("DC", "AC") else None
                if kind == "SA" and product_line not in ("DC", "AC"):
                    self.issue(file, "invalid_product_line", "產品線設為 null")
            result["Lifecycle"] = _enum(row["Lifecycle"], ("Active", "NRND", "EOL", "Obsolete"))
            if result["Lifecycle"] is None:
                self.issue(file, "invalid_lifecycle", "生命週期設為 null")
            if result["MakeBuy"] not in ("M", "B"):
                result["MakeBuy"] = None
                self.issue(file, "invalid_make_buy", "自製／外購設為 null")
            for field in ("LeadTimeDays", "MOQ", "UnitCostUSD"):
                result[field] = _positive(row[field]) if field == "UnitCostUSD" else _integer(row[field], positive=True)
                if result[field] is None:
                    self.issue(file, "invalid_" + field, "無效欄位設為 null")
            self.data["parts"][part] = result

    def attributes(self):
        file = "part_attributes.csv"
        groups = defaultdict(list)
        signatures = defaultdict(set)
        for row in self.rows(file):
            part = normalize_part(row["PartNo"])
            if not self.reference(file, part):
                continue
            code = ATTR_CODES.get(row["AttrCode"].strip().lower())
            if code is None:
                self.drop(file, "invalid_attr_code")
                continue
            value = _attribute(code, row["AttrValue"], row["Unit"])
            if value is None:
                self.drop(file, "invalid_attr_value")
                if code == "ShelfLifeMonths":
                    # 有保存期限屬性但值無效：仍需檢查效期卻無法確認 → 標記 None，庫存計算時不可用
                    self.data["attrs"].setdefault(part, {}).setdefault("ShelfLifeMonths", None)
                continue
            self.data["attrs"].setdefault(part, {})
            key = (part, code)
            signature = json.dumps(value, ensure_ascii=False, sort_keys=True)
            if signature in signatures[key]:
                self.drop(file, "duplicate_attribute")
                continue
            signatures[key].add(signature)
            groups[key].append({"PartNo": part, "AttrCode": code, "AttrValue": value,
                                "Unit": STANDARD_UNITS.get(code, "")})
        for (part, code), options in groups.items():
            selected = options[0]
            if len(options) > 1:
                selected = self.choose(file, "attr_conflict", part + "|" + code, _commodity(part), options,
                                       "同一屬性換算至標準單位後數值不同")
                self.data["excluded"]["attrs"] += len(options) - (selected is not None)
            if selected is not None:
                self.data["attrs"][part][code] = selected["AttrValue"]
            elif code == "ShelfLifeMonths":
                # 保存期限待確認：仍需檢查效期但無法確認 → None，確認前該料庫存不可用
                self.data["attrs"].setdefault(part, {})["ShelfLifeMonths"] = None

    @staticmethod
    def bom_key(row: dict) -> str:
        return "|".join((row["Parent"], row["Rev"], str(row["LineNo"])))

    def bom(self):
        file = "bom.csv"
        groups = defaultdict(list)
        for row in self.rows(file):
            parent, child = normalize_part(row["ParentPartNo"]), normalize_part(row["ChildPartNo"])
            if not self.reference(file, parent, child):
                continue
            if _part_type(parent) not in ("FG", "SA") or _part_type(child) not in ("SA", "RM"):
                self.drop(file, "invalid_bom_part_type")
                continue
            line, qty = _integer(row["LineNo"]), _positive(row["QtyPer"])
            factor = _unit(row["UoM"], self.data["parts"][child]["UoM"], ("PCS", "EA", "KPCS", "M", "CM", "MM"))
            if line is None or qty is None:
                self.drop(file, "invalid_line_or_qty")
                continue
            if factor is None or not math.isfinite(qty * factor):
                self.drop(file, "invalid_uom")
                continue
            start, end = _date(row["EffectiveFrom"]), _date(row["EffectiveTo"])
            if start is None or (row["EffectiveTo"].strip() and end is None) or (end and end < start):
                self.drop(file, "invalid_dates")
                continue
            text = row["ScrapRate"].strip()
            scrap = _number(text[:-1]) if text.endswith("%") else _number(text)
            if text.endswith("%") and scrap is not None:
                scrap /= 100
            if not text:
                scrap = 0.0
            elif scrap is None or not 0 <= scrap <= .18:
                scrap = 0.0
                self.issue(file, "invalid_scrap_rate", "依損耗率預設規則將損耗率設為 0")
            result = {"Parent": parent, "Child": child, "Rev": row["BOMRev"].strip(), "LineNo": line,
                      "QtyPer": qty * factor, "Scrap": scrap, "RefDes": row["RefDes"].strip(),
                      "EffFrom": start, "EffTo": end}
            groups[self.bom_key(result)].append(result)
        active = []
        for key, options in groups.items():
            unique = []
            for cand in options:
                if cand not in unique:
                    unique.append(cand)
            if len(unique) < len(options):
                # 標準化後完全相同（例如 2% 與 0.02、日期寫法不同）→ 視為重複，只留一筆
                self.issue(file, "duplicate_after_normalize", "標準化後所有欄位相同，視為重複只保留一筆")
                self.data["excluded"]["bom"] += len(options) - len(unique)
                options = unique
            selected = options[0]
            if len(options) > 1:
                selected = self.choose(file, "bom_conflict", key, _commodity(options[0]["Child"]), options,
                                       "同一上階、版次、行號的內容不同")
                self.data["excluded"]["bom"] += len(options) - (selected is not None)
            if selected is not None:
                active.append(selected)
        # 拒絕已知環上的行可切斷循環；核准本身不能使仍有循環的圖生效。
        cycles = _cycle_edges(active)
        remaining = []
        for i, row in enumerate(active):
            approval = self.approvals.get(_review_id("bom_cycle", self.bom_key(row)), {})
            if i in cycles and isinstance(approval, dict) and approval.get("decision") == "reject":
                self.drop(file, "bom_cycle_rejected")
            else:
                remaining.append(row)
        cycles = _cycle_edges(remaining) if len(remaining) != len(active) else cycles
        for i, row in enumerate(remaining):
            if i in cycles:
                key = self.bom_key(row)
                self.data["review"].append({"ReviewID": _review_id("bom_cycle", key), "Kind": "bom_cycle",
                    "Commodity": _commodity(row["Child"]), "Key": key, "Reason": "此行位於循環參照；需修正或拒絕環上紀錄後才能展開",
                    "Options": [row]})
                self.drop(file, "bom_cycle")
            else:
                self.data["bom"].append(row)

    def alternates(self):
        file = "alternates.csv"
        groups = defaultdict(list)
        for row in self.rows(file):
            primary, alt = normalize_part(row["PrimaryPartNo"]), normalize_part(row["AltPartNo"])
            if not self.reference(file, primary, alt):
                continue
            if primary == alt or _part_type(primary) != "RM" or _part_type(alt) != "RM":
                self.drop(file, "invalid_alternate_parts")
                continue
            record_id, scope = row["AltRecID"].strip(), row["Scope"].strip()
            value = row["ScopeValue"].strip()
            if not re.fullmatch(r"ALT[0-9]{7}", record_id):
                self.drop(file, "invalid_alt_record_id")
                continue
            if scope == "PARENT":
                value = normalize_part(value)
                if not self.reference(file, value):
                    continue
                valid_scope = _part_type(value) in ("SA", "FG")
            elif scope == "PRODUCTLINE":
                valid_scope = value in ("DC", "AC")
            else:
                valid_scope = scope == "GLOBAL" and not value
            if not valid_scope:
                self.drop(file, "invalid_scope")
                continue
            priority, ratio = _integer(row["Priority"]), _positive(row["QtyRatio"])
            if priority is None or ratio is None:
                self.drop(file, "invalid_priority_or_ratio")
                continue
            bidirectional, mix = row["Bidirectional"].strip() or "N", row["MixAllowed"].strip() or "N"
            bundle = row["BundleID"].strip()
            if bidirectional not in ("Y", "N") or mix not in ("Y", "N") or (bundle and not re.fullmatch(r"BND-[0-9]{4}", bundle)):
                self.drop(file, "invalid_flags_or_bundle")
                continue
            status = _enum(row["ApprovalStatus"], ("Approved", "Pending", "Rejected"))
            if status is None:
                self.drop(file, "invalid_approval_status")
                continue
            start, end = _date(row["ValidFrom"]), _date(row["ValidTo"])
            if start is None or (row["ValidTo"].strip() and end is None) or (end and end < start):
                self.drop(file, "invalid_dates")
                continue
            result = {"AltRecID": record_id, "Primary": primary, "Alt": alt, "Scope": scope, "ScopeValue": value or None,
                      "Priority": priority, "QtyRatio": ratio, "Bidirectional": bidirectional == "Y", "MixAllowed": mix == "Y",
                      "BundleID": bundle or None, "Status": status, "ValidFrom": start, "ValidTo": end, "Remark": row["Remark"]}
            key = "|".join((primary, alt, scope, value))
            groups[key].append(result)
        for key, options in groups.items():
            unique, seen_content = [], []
            for cand in options:
                content = {k: v for k, v in cand.items() if k != "AltRecID"}
                if content not in seen_content:
                    seen_content.append(content)
                    unique.append(cand)
            if len(unique) < len(options):
                # 只差紀錄編號、內容完全相同 → 重複，保留第一筆（alt_invalid_or_conflicting 只把「內容不同」列為待確認）
                self.issue(file, "duplicate_except_id", "只有替代料紀錄編號不同、內容相同，視為重複保留一筆")
                self.data["excluded"]["alternates"] += len(options) - len(unique)
                options = unique
            selected = options[0]
            if len(options) > 1:
                selected = self.choose(file, "alt_conflict", key, _commodity(options[0]["Primary"]), options,
                                       "同一主料、替代料、範圍與範圍值的內容不同")
                self.data["excluded"]["alternates"] += len(options) - (selected is not None)
            if selected is None:
                continue
            if selected["Status"] == "Pending":
                review = {"ReviewID": _review_id("alt_pending", selected["AltRecID"]), "Kind": "alt_pending",
                          "Commodity": _commodity(selected["Primary"]), "Key": selected["AltRecID"],
                          "Reason": "Pending 替代料須經負責主料類別的 component 核准", "Options": [dict(selected)]}
                self.issue(file, "alt_pending", "未確認保留 Pending；依 component 決定改為 Approved／Rejected")
                approval = self.approvals.get(review["ReviewID"], {})
                decision = approval.get("decision") if isinstance(approval, dict) else None
                if decision in ("approve", "reject"):
                    selected["Status"] = "Approved" if decision == "approve" else "Rejected"
                else:
                    self.data["review"].append(review)
            self.data["alternates"].append(selected)

    def inventory(self):
        file = "inventory.csv"
        for row in self.rows(file):
            part = normalize_part(row["PartNo"])
            if not self.reference(file, part):
                continue
            if _part_type(part) != "RM":
                self.drop(file, "invalid_part_type")
                continue
            if _date(row["SnapshotDate"]) != BASE_DATE:
                self.drop(file, "invalid_snapshot_date")
                continue
            plant = _plant(row["Plant"])
            if plant is None:
                self.drop(file, "invalid_plant")
                continue
            qty = _number(row["QtyOnHand"])
            allocated = _number(row["QtyAllocated"]) if row["QtyAllocated"].strip() else 0.0
            if qty is None or allocated is None or qty < 0 or not 0 <= allocated <= qty:
                self.drop(file, "invalid_quantity")
                continue
            factor = _unit(row["UoM"], self.data["parts"][part]["UoM"], ("PCS", "KPCS", "M"))
            if factor is None or not math.isfinite((qty - allocated) * factor):
                self.drop(file, "invalid_uom")
                continue
            code = row["DateCode"].strip()
            try:
                if not re.fullmatch(r"[0-9]{4}", code):
                    raise ValueError
                dt.date.fromisocalendar(2000 + int(code[:2]), int(code[2:]), 1)
            except ValueError:
                code = None
                self.issue(file, "invalid_datecode", "DateCode 設為 null；需檢查保存期限時不可用")
            qc = row["QCStatus"].strip()
            if qc not in ("合格", "待驗", "凍結", "MRB"):
                qc = None
                self.issue(file, "invalid_qc_status", "QCStatus 設為 null；不可計入可用庫存")
            self.data["inventory"].append({"Plant": plant, "PartNo": part, "LotNo": row["LotNo"].strip(),
                "DateCode": code, "QCStatus": qc, "Qty": (qty - allocated) * factor, "StorageLoc": row["StorageLoc"].strip()})

    def po(self):
        file = "open_po.csv"
        for row in self.rows(file):
            part = normalize_part(row["PartNo"])
            if not self.reference(file, part):
                continue
            if _part_type(part) != "RM":
                self.drop(file, "invalid_part_type")
                continue
            no, line = row["PONo"].strip(), _integer(row["POLine"])
            if not re.fullmatch(r"PO[0-9]{8}", no) or line is None:
                self.drop(file, "invalid_po_number_or_line")
                continue
            plant = _plant(row["Plant"])
            if plant is None:
                self.drop(file, "invalid_plant")
                continue
            status = _enum(row["Status"], ("Open", "Closed", "Cancelled"))
            if status is None:
                self.drop(file, "invalid_status")
                continue
            qty, received = _number(row["OrderQty"]), _number(row["ReceivedQty"])
            if qty is None or received is None or qty < 0 or not 0 <= received <= qty:
                self.drop(file, "invalid_quantity")
                continue
            uom = row["UoM"].strip().upper()
            if _unit(uom, self.data["parts"][part]["UoM"], ("PCS", "M")) is None:
                self.drop(file, "invalid_uom")
                continue
            order_date, eta = _date(row["OrderDate"]), _date(row["ETA"])
            if order_date is None:
                self.drop(file, "invalid_order_date")
                continue
            if eta and eta < order_date:
                self.drop(file, "eta_before_order_date")
                continue
            if eta is None:
                self.issue(file, "invalid_eta", "ETA 設為 null；保留紀錄，不計入在途")
            price = _positive(row["UnitPriceUSD"])
            if price is None:
                self.issue(file, "invalid_unit_price", "UnitPriceUSD 設為 null")
            self.data["po"].append({"PONo": no, "POLine": line, "Plant": plant, "PartNo": part,
                "SupplierName": row["SupplierName"].strip(), "OpenQty": qty - received, "UoM": uom,
                "UnitPriceUSD": price, "OrderDate": order_date, "ETA": eta, "Status": status})

    def run(self):
        self.parts()
        self.attributes()
        self.bom()
        self.alternates()
        self.inventory()
        self.po()
        self.data["review"].sort(key=lambda r: (r["Kind"], r["Key"]))
        self.data["issues"] = [{"File": file, "Issue": issue, "Count": count, "Action": self.actions[file, issue]}
                               for (file, issue), count in sorted(self.problems.items()) if count > 0]
        return self.data

    def report(self) -> str:
        def cell(value):
            return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")
        lines = ["# 資料清理報告", "", "資料基準日：" + BASE_DATE, "", "## 總覽", "",
                 "| 檔案 | 原始列數 | 清理後筆數 | 排除列數 |", "| --- | ---: | ---: | ---: |"]
        for file, key in FILES.items():
            count = sum(len(a) for a in self.data["attrs"].values()) if key == "attrs" else len(self.data[key])
            lines.append(f"| {file} | {self.inputs[file]} | {count} | {self.data['excluded'][key]} |")
        lines += ["", "排除列數包含重複與未確認衝突；設為 null 或保留 Pending 的列仍保留於清理資料。",
                  "同列可有多個欄位問題，因此問題筆數不可直接相加作為排除列數。"]
        for file in FILES:
            lines += ["", "## " + file, "", "| 問題 | 筆數 | 處理方式 |", "| --- | ---: | --- |"]
            problems = [i for i in self.data["issues"] if i["File"] == file]
            for problem in problems:
                issue = problem["Issue"]
                label = ISSUE_LABELS.get(issue, issue)
                lines.append("| {}（{}） | {} | {} |".format(cell(label), cell(issue), problem["Count"], cell(problem["Action"])))
            if not problems:
                lines.append("| 無 | — | 無需處理 |")
        lines += ["", "## 待人工確認清單摘要", "", "由負責該物料類別的 component 零件工程師確認；已有效確認的項目不列入待確認清單。",
                  "", "| Kind | 筆數 |", "| --- | ---: |"]
        counts = Counter(r["Kind"] for r in self.data["review"])
        lines += [f"| {kind} | {count} |" for kind, count in sorted(counts.items())]
        if not counts:
            lines.append("| 無 | — |")
        lines += ["", "| Kind | ReviewID | Key |", "| --- | --- | --- |"]
        lines += ["| {} | {} | {} |".format(cell(r["Kind"]), r["ReviewID"], cell(r["Key"])) for r in self.data["review"]]
        return "\n".join(lines) + "\n"


def run_clean(raw_dir: str, out_dir: str, approvals: Optional[dict] = None) -> dict:
    """清理並產出 clean.json、中文報告及 review_queue.json；回傳不含料號的計數摘要。"""
    cleaner = _Cleaner(raw_dir, approvals)
    data = cleaner.run()
    output = Path(out_dir)
    output.mkdir(parents=True, exist_ok=True)
    for name, value in (("clean.json", data), ("review_queue.json", data["review"])):
        with (output / name).open("w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
    (output / "cleaning_report.md").write_text(cleaner.report(), encoding="utf-8")
    return {"counts": {key: len(data[key]) for key in ("parts", "bom", "alternates", "inventory", "po", "review")},
            "issues": len(data["issues"])}


def load_clean(out_dir: str) -> dict:
    """讀取 out_dir/clean.json。"""
    with (Path(out_dir) / "clean.json").open(encoding="utf-8") as handle:
        return json.load(handle)
