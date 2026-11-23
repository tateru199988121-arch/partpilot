"""計算引擎。只讀 clean.json 格式的 dict，不碰權限。

所有數量：PCS 無條件進位到整數、M 無條件進位到小數第二位（只在最後彙總時進位）。
每個回傳都要帶 "rules"（描述性規則名稱 list）與 "excluded"（被排除的資料：原因→筆數）。
"""
from __future__ import annotations

import calendar
import datetime as dt
import unicodedata
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Iterable, Optional


_ZERO = Fraction(0)
_ONE = Fraction(1)

ALT_RULES = {
    "alt_approval": "替代料：核准狀態",
    "alt_validity": "替代料：有效期間",
    "alt_bidirectional": "替代料：雙向互換",
    "alt_no_chaining": "替代料：禁止串接",
    "alt_scope": "替代料：適用範圍",
    "alt_lifecycle": "替代料：生命週期",
    "alt_attribute_compatibility": "替代料：屬性相容",
    "alt_quantity_ratio": "替代料：比例用量",
    "alt_no_mixing": "替代料：整片不混用",
    "alt_bundle": "替代料：成組切換",
    "alt_invalid_or_conflicting": "替代料：無效資料與衝突確認",
    "alt_priority": "替代料：使用優先序",
}


def _normal(value):
    value = unicodedata.normalize("NFKC", str(value)).strip().upper()
    return "".join("-" if unicodedata.category(c) == "Pd" or c == "−" else c for c in value)


def _decimal(value):
    """Parse finite decimal input exactly; rational arithmetic keeps 1/QtyRatio exact."""
    try:
        result = Decimal(str(value))
        return Fraction(result) if result.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _date(value):
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if not isinstance(value, str) or len(value) != 10 or value[4] != "-" or value[7] != "-":
        return None
    try:
        return dt.date.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def _number(value):
    return int(value) if value.denominator == 1 else float(value)


def _ceil(value, uom):
    scale = 100 if uom == "M" else 1
    scaled = max(_ZERO, value) * scale
    return Fraction(-(-scaled.numerator // scaled.denominator), scale)


def _floor(value):
    value = max(_ZERO, value)
    return value.numerator // value.denominator


def _yes(value):
    return value is True or (isinstance(value, str) and value.strip().upper() == "Y")


class Engine:
    def __init__(self, data: dict):
        self.data = data
        self._parts = data.get("parts", {})
        self._bom_parent = defaultdict(list)
        self._bom_child = defaultdict(list)
        self._alt_primary = defaultdict(list)
        self._alt_alt = defaultdict(list)
        self._alt_signatures = defaultdict(set)
        self._alt_records = {}
        self._bundles = defaultdict(list)
        self._inventory = defaultdict(list)
        self._po = defaultdict(list)
        self._indexed = {}
        self._sync_indexes()

    def _sync_indexes(self):
        """Index once; also accept appended clean rows without rescanning old rows."""
        for name in ("bom", "alternates", "inventory", "po"):
            rows = self.data.get(name, [])
            previous, count = self._indexed.get(name, (None, 0))
            if previous is not rows or len(rows) < count:
                indexes = {"bom": (self._bom_parent, self._bom_child),
                           "alternates": (self._alt_primary, self._alt_alt, self._alt_signatures,
                                          self._alt_records, self._bundles),
                           "inventory": (self._inventory,), "po": (self._po,)}[name]
                for index in indexes:
                    index.clear()
                count = 0
            for row in rows[count:]:
                if name == "bom":
                    self._bom_parent[row["Parent"]].append(row)
                    self._bom_child[row["Child"]].append(row)
                elif name == "alternates":
                    self._alt_primary[row["Primary"]].append(row)
                    self._alt_alt[row["Alt"]].append(row)
                    key = tuple(row.get(k) for k in ("Primary", "Alt", "Scope", "ScopeValue"))
                    self._alt_signatures[key].add(repr(sorted(row.items())))
                    self._alt_records[row["AltRecID"]] = row
                    if row.get("BundleID"):
                        self._bundles[row["BundleID"]].append(row)
                else:
                    index = self._inventory if name == "inventory" else self._po
                    index[(row["PartNo"], row["Plant"])].append(row)
            self._indexed[name] = (rows, len(rows))

    def _require(self, part_no):
        no = _normal(part_no)
        if no not in self._parts:
            raise ValueError("找不到料號：" + no)
        return no

    @staticmethod
    def _plant(plant):
        key = "".join(_normal(plant).split())
        for code, alias in (("PLANT-C", "廠區C"), ("PLANT-D", "廠區D"),
                            ("PLANT-A", "廠區A"), ("PLANT-B", "廠區B")):
            if key in (code, code.replace("-", ""), alias):
                return code
        raise ValueError("無效的工廠：" + str(plant))

    @staticmethod
    def _production_date(value):
        result = _date(value)
        if result is None:
            raise ValueError("生產日必須是有效日期")
        return result

    def _clean_excluded(self, *tables):
        return Counter({"clean_" + key: count for key, count in self.data.get("excluded", {}).items()
                        if key in tables and isinstance(count, int) and count})

    @staticmethod
    def _effective(row, day):
        start, end = _date(row.get("EffFrom")), _date(row.get("EffTo"))
        if start is None or (row.get("EffTo") and (end is None or end < start)):
            return "bom_invalid_date"
        if day < start or (end is not None and day > end):
            return "bom_not_effective"
        return None

    # ---- 主檔 / 屬性 ----
    def part(self, part_no: str) -> Optional[dict]:
        """料號標準化後查主檔（去空白、轉大寫、破折號轉 -），找不到回 None。"""
        record = self._parts.get(_normal(part_no))
        return dict(record) if record is not None else None

    # ---- BOM ----
    def bom_children(self, parent: str, prod_date: dt.date) -> list:
        """直接下階（生效中的行）：[{"Child","QtyPer","Scrap","RefDes","LineNo","Rev"}]。"""
        parent = self._require(parent)
        day = self._production_date(prod_date)
        self._sync_indexes()
        fields = ("Child", "QtyPer", "Scrap", "RefDes", "LineNo", "Rev")
        return [{key: row.get(key) for key in fields} for row in self._bom_parent.get(parent, ())
                if self._effective(row, day) is None and row["Child"] in self._parts]

    def _requirements(self, part_no, day):
        """Unrounded per-top requirements and per-board positions for allocation."""
        rm = {}
        excluded = self._clean_excluded("bom")
        # A shared SA may be reached through several paths. Count an excluded row once.
        seen_exclusions = set()

        def reject(row, reason):
            key = (id(row), reason)
            if key not in seen_exclusions:
                seen_exclusions.add(key)
                excluded[reason] += 1

        def walk(no, factor, path, context):
            if self._parts[no].get("PartType") == "RM":
                entry = rm.setdefault(no, {"raw": _ZERO, "positions": []})
                entry["raw"] += factor
                entry["positions"].append({"parent": no, "path": path, "qty_per_top": factor,
                                           "refdes": "", "per_board": _ONE,
                                           "boards_per_top": _ONE, "context": context})
                return
            for index, row in enumerate(self._bom_parent.get(no, ())):
                reason = self._effective(row, day)
                if reason:
                    reject(row, reason)
                    continue
                child = row["Child"]
                if child not in self._parts:
                    reject(row, "bom_orphan")
                    continue
                if child in path:
                    reject(row, "bom_cycle")
                    continue
                amount = _decimal(row.get("QtyPer"))
                if amount is None or amount <= 0:
                    reject(row, "bom_invalid_qty")
                    continue
                scrap = _decimal(row.get("Scrap", 0))
                if scrap is None or not _ZERO <= scrap <= Fraction(9, 50):
                    scrap = _ZERO  # 損耗率缺值或無效時採零
                per_board = amount * (_ONE + scrap)
                usage = factor * per_board
                if self._parts[child].get("PartType") == "RM":
                    entry = rm.setdefault(child, {"raw": _ZERO, "positions": []})
                    entry["raw"] += usage
                    entry["positions"].append({"parent": no, "path": list(path),
                                               "qty_per_top": usage, "refdes": row.get("RefDes", ""),
                                               "per_board": per_board, "boards_per_top": factor,
                                               "context": context})
                else:
                    walk(child, usage, path + [child], context + (index,))

        walk(part_no, _ONE, [part_no], ())
        return rm, excluded

    def explode(self, part_no: str, qty: float, prod_date: dt.date) -> dict:
        """多階展開到原料。毛需求 = qty × Π(QtyPer×(1+Scrap))，依 RM 彙總後再進位。
        回傳 {"part","qty","prod_date","rm": {RM: {"qty": 進位後, "uom", "commodity",
                 "positions": [{"parent": 直接上階, "path": [頂層,..,直接上階], "qty_per_top": 未進位每 1 頂層用量, "refdes"}]}},
               "rules": [...], "excluded": {...}}
        """
        part_no = self._require(part_no)
        day = self._production_date(prod_date)
        quantity = _decimal(qty)
        if quantity is None or quantity < 0:
            raise ValueError("生產數量必須是大於或等於零的數字")
        self._sync_indexes()
        requirements, excluded = self._requirements(part_no, day)
        result = {}
        for no, entry in requirements.items():
            part = self._parts[no]
            result[no] = {"qty": _number(_ceil(entry["raw"] * quantity, part["UoM"])),
                          "uom": part["UoM"], "commodity": part.get("Commodity"),
                          "positions": [{"parent": p["parent"], "path": list(p["path"]),
                                         "qty_per_top": _number(p["qty_per_top"]), "refdes": p["refdes"]}
                                        for p in entry["positions"]]}
        return {"part": part_no, "qty": _number(quantity), "prod_date": day.isoformat(), "rm": result,
                "rules": ["半成品一律展開", "BOM 生效與毛需求彙總進位"],
                "excluded": dict(excluded)}

    def where_used(self, part_no: str, prod_date: Optional[dt.date] = None) -> dict:
        """反查上階（prod_date=None 時用基準日 2026-11-06 判斷生效）。
        回傳 {"part","direct_parents": [{"Parent","QtyPer","RefDes"}], "finished_goods": [FG...], "paths": [[part, 上階.., FG]]}"""
        part_no = self._require(part_no)
        day = self._production_date(prod_date if prod_date is not None else
                                    self.data.get("meta", {}).get("base_date", "2026-11-06"))
        self._sync_indexes()
        excluded = self._clean_excluded("bom")
        parents, paths, finished = [], [], set()
        seen_exclusions, seen_paths = set(), set()

        def walk(no, path):
            if self._parts[no].get("PartType") == "FG":
                finished.add(no)
                if tuple(path) not in seen_paths:
                    paths.append(path)
                    seen_paths.add(tuple(path))
                return
            for row in self._bom_child.get(no, ()):
                reason = self._effective(row, day)
                parent = row["Parent"]
                if not reason and parent not in self._parts:
                    reason = "bom_orphan"
                if not reason and parent in path:
                    reason = "bom_cycle"
                if reason:
                    key = (id(row), reason)
                    if key not in seen_exclusions:
                        excluded[reason] += 1
                        seen_exclusions.add(key)
                    continue
                if len(path) == 1:
                    parents.append({k: row.get(k) for k in ("Parent", "QtyPer", "RefDes")})
                walk(parent, path + [parent])

        walk(part_no, [part_no])
        return {"part": part_no, "direct_parents": parents, "finished_goods": sorted(finished),
                "paths": paths, "rules": ["生產日與基準日", "BOM 生效與反查上階"],
                "excluded": dict(excluded)}

    # ---- 庫存 / 在途 ----
    def available(self, part_no: str, plant: str, prod_date: dt.date) -> dict:
        """可用庫存：QCStatus=合格、效期（有 ShelfLifeMonths 才檢查；DateCode 該 ISO 週星期一＋月數，月底對齊；到期日 < 生產日 不可用；DateCode 無效且需檢查效期 → 不可用）。
        回傳 {"part","plant","qty","uom","lots": n可用批數, "excluded": {"qc_not_pass": n, "expired": n, "datecode_invalid": n, ...}, "rules": [...]}"""
        part_no, plant = self._require(part_no), self._plant(plant)
        day = self._production_date(prod_date)
        self._sync_indexes()
        excluded = self._clean_excluded("inventory")
        attrs = self.data.get("attrs", {}).get(part_no, {})
        months = _decimal(attrs.get("ShelfLifeMonths"))
        quantity, lots = _ZERO, 0
        for row in self._inventory.get((part_no, plant), ()):
            reasons = []
            if row.get("QCStatus") != "合格":
                reasons.append("qc_not_pass")
            amount = _decimal(row.get("Qty"))
            if amount is None or amount < 0:
                reasons.append("inventory_invalid_qty")
            if "ShelfLifeMonths" in attrs:
                dc = row.get("DateCode")
                try:
                    if not isinstance(dc, str) or len(dc) != 4 or not dc.isascii() or not dc.isdigit():
                        raise ValueError
                    made = dt.date.fromisocalendar(2000 + int(dc[:2]), int(dc[2:]), 1)
                    if months is None or months < 0 or months != int(months):
                        reasons.append("shelf_life_invalid")
                    else:
                        serial = made.year * 12 + made.month - 1 + int(months)
                        year, month = serial // 12, serial % 12 + 1
                        expiry = dt.date(year, month, min(made.day, calendar.monthrange(year, month)[1]))
                        if expiry < day:
                            reasons.append("expired")
                except (ValueError, OverflowError):
                    reasons.append("datecode_invalid")
            if reasons:
                excluded.update(reasons)
            else:
                quantity += amount
                lots += 1
        uom = self._parts[part_no]["UoM"]
        return {"part": part_no, "plant": plant, "qty": _number(round(quantity, 6)), "uom": uom,
                "lots": lots, "excluded": dict(excluded), "rules": ["可用庫存、品檢與保存期限"]}

    def inbound(self, part_no: str, plant: str, prod_date: dt.date) -> dict:
        """在途：Status=Open、OpenQty>0、ETA 有效且 ETA+4 天 ≤ 生產日。
        回傳 {"part","plant","qty","pos": [{"PONo","POLine","OpenQty","ETA","SupplierName","UnitPriceUSD"}],
               "late_pos": [同格式，ETA+4 > 生產日的 Open 單], "excluded": {...}, "rules": [...]}"""
        part_no, plant = self._require(part_no), self._plant(plant)
        day = self._production_date(prod_date)
        self._sync_indexes()
        excluded = self._clean_excluded("po")
        pos, late_pos, quantity = [], [], _ZERO
        fields = ("PONo", "POLine", "OpenQty", "ETA", "SupplierName", "UnitPriceUSD")
        for row in self._po.get((part_no, plant), ()):
            reasons = []
            if row.get("Status") != "Open":
                reasons.append("po_not_open")
            amount = _decimal(row.get("OpenQty"))
            if amount is None or amount <= 0:
                reasons.append("po_nonpositive_qty")
            eta, order = _date(row.get("ETA")), _date(row.get("OrderDate"))
            if eta is None:
                reasons.append("eta_invalid")
            if order is None:
                reasons.append("order_date_invalid")
            if eta is not None and order is not None and eta < order:
                reasons.append("po_date_conflict")
            if reasons:
                excluded.update(reasons)
                continue
            detail = {key: row.get(key) for key in fields}
            detail["ETA"] = eta.isoformat()
            # Subtraction avoids overflowing date.max when adding the inspection buffer.
            if (day - eta).days < 4:
                late_pos.append(detail)
                excluded["po_late"] += 1
            else:
                pos.append(detail)
                quantity += amount
        return {"part": part_no, "plant": plant, "uom": self._parts[part_no]["UoM"],
                "qty": _number(round(quantity, 6)),
                "pos": pos, "late_pos": late_pos, "excluded": dict(excluded),
                "rules": ["在途未交量、到廠後四天進料檢驗與日期矛盾排除"]}

    # ---- 替代料 ----
    def _alt_check(self, row, reverse, parent, product_line, day):
        primary, alt = (row["Alt"], row["Primary"]) if reverse else (row["Primary"], row["Alt"])
        reasons = []
        if row.get("Status") != "Approved":
            reasons.append("alt_approval " + str(row.get("Status")) + " 未核准")
        start, end = _date(row.get("ValidFrom")), _date(row.get("ValidTo"))
        valid_dates = start is not None and (not row.get("ValidTo") or (end is not None and end >= start))
        if not valid_dates or day < start or (end is not None and day > end):
            reasons.append("alt_validity 生產日不在有效期間或日期無效")
        if reverse and not _yes(row.get("Bidirectional")):
            reasons.append("alt_bidirectional 未允許反向替代")
        scope, value = row.get("Scope"), row.get("ScopeValue")
        valid_scope = ((scope == "GLOBAL" and not value) or
                       (scope == "PRODUCTLINE" and value in ("AC", "DC")) or
                       (scope == "PARENT" and value in self._parts and
                        self._parts[value].get("PartType") in ("FG", "SA")))
        if (not valid_scope or (scope == "PARENT" and value != parent) or
                (scope == "PRODUCTLINE" and value != product_line)):
            reasons.append("alt_scope 不符合直接上階或成品產品線範圍")
        target = self._parts.get(alt, {})
        if target.get("Lifecycle") not in ("Active", "NRND"):
            reasons.append("alt_lifecycle 替代料生命週期須為 Active 或 NRND")
        ratio = _decimal(row.get("QtyRatio"))
        if ratio is not None and ratio > 0 and reverse:
            ratio = _ONE / ratio
        main_attrs = self.data.get("attrs", {}).get(primary, {})
        alt_attrs = self.data.get("attrs", {}).get(alt, {})
        minimum = ("RatedVoltage", "RatedCurrent", "PowerRating", "TempMax")
        maximum = ("TempMin", "Tolerance")
        equal = ("Capacitance", "Resistance", "Inductance") if ratio == 1 else ()
        for code in minimum + maximum + equal:
            if main_attrs.get(code) is None:
                continue
            a, b = _decimal(main_attrs[code]), _decimal(alt_attrs.get(code))
            if b is None or a is None:
                reasons.append("alt_attribute_compatibility " + code + " 替代料屬性缺值或無效")
            elif ((code in minimum and b < a) or (code in maximum and b > a) or
                  (code in equal and b != a)):
                reasons.append("alt_attribute_compatibility " + code + " 屬性不相容")
        if main_attrs.get("Package") is not None and main_attrs["Package"] != alt_attrs.get("Package"):
            reasons.append("alt_attribute_compatibility Package 封裝不相同或缺值")
        if alt_attrs.get("RoHS") != "Y":  # alt_attribute_compatibility：替代料 RoHS 須為 Y（不論主料是否有此屬性）
            reasons.append("alt_attribute_compatibility RoHS 替代料須為 Y")
        if main_attrs.get("SafetyCert") is not None:
            if not set(main_attrs["SafetyCert"]).issubset(set(alt_attrs.get("SafetyCert") or [])):
                reasons.append("alt_attribute_compatibility SafetyCert 未涵蓋主料所有認證")
        if (primary == alt or primary not in self._parts or alt not in self._parts or
                ratio is None or ratio <= 0 or not valid_dates or not valid_scope):
            reasons.append("alt_invalid_or_conflicting 相同料號、用量比、日期、範圍或主檔無效")
        # Clean data already resolves conflicts; the indexed signatures also guard
        # hand-made inputs without rescanning a primary's alternate records.
        key = tuple(row.get(k) for k in ("Primary", "Alt", "Scope", "ScopeValue"))
        if len(self._alt_signatures[key]) > 1:
            reasons.append("alt_invalid_or_conflicting 同組替代紀錄衝突，待人工確認")
        return primary, alt, ratio, reasons

    def alternates(self, primary: str, parent: str, fg: Optional[str], prod_date: dt.date) -> dict:
        """在「成品 fg → 直接上階 parent → 主料 primary」位置判斷所有替代料紀錄（含 Bidirectional=Y 的反向紀錄，反向用量比 1/QtyRatio）。
        fg=None 時 PRODUCTLINE 範圍依 parent 的產品線（SA 主檔 ProductLine）判斷。
        回傳 {"primary","parent","usable": [{"AltRecID","Alt","QtyRatio"(實際比),"Priority","MixAllowed","BundleID","Reverse"(bool),"bundle_members":[{"Primary","Alt","AltRecID","QtyRatio"}]}] (依 Priority 排序),
               "rejected": [{"AltRecID","Alt","Status","reasons": ["alt_approval Pending 未核准", "alt_attribute_compatibility RatedVoltage 較低", ...]}], "rules": [...]}
        成組切換：bundle 的所有成員都要在同一 parent 下可用（成員主料須在該 parent 的 BOM 中且成員紀錄本身通過核准、有效期、方向、範圍、生命週期、屬性、用量比、不混用與資料有效性檢查），否則整組不可用（原因寫 alt_bundle）。
        alt_no_chaining：不串接，只看 primary 自己的紀錄。"""
        primary, parent = self._require(primary), self._require(parent)
        fg = self._require(fg) if fg is not None else None
        day = self._production_date(prod_date)
        self._sync_indexes()
        product_line = ("DC" if fg.startswith("FGD") else "AC" if fg.startswith("FGA") else None) if fg else self._parts[parent].get("ProductLine")
        active_children = {r["Child"] for r in self._bom_parent.get(parent, ())
                           if self._effective(r, day) is None}
        usable, rejected = [], []
        options = [(r, False) for r in self._alt_primary.get(primary, ())]
        options.extend((r, True) for r in self._alt_alt.get(primary, ()) if r["Primary"] != primary)
        seen = set()
        for row, reverse in options:
            signature = (repr(sorted(row.items())), reverse)
            if signature in seen:
                continue
            seen.add(signature)
            _, alt, ratio, reasons = self._alt_check(row, reverse, parent, product_line, day)
            members = []
            if row.get("BundleID"):
                bundle_ok = True
                for member in self._bundles[row["BundleID"]]:
                    mp, ma, mr, failures = self._alt_check(member, reverse, parent, product_line, day)
                    if failures or mp not in active_children:
                        bundle_ok = False
                    if mr is not None and mr > 0:
                        item = {"Primary": mp, "Alt": ma, "AltRecID": member["AltRecID"], "QtyRatio": _number(mr)}
                        if item not in members:
                            members.append(item)
                if not bundle_ok:
                    reasons.append("alt_bundle 成組成員未在同一直接上階生效或未通過替代規則")
            if reasons:
                rejected.append({"AltRecID": row["AltRecID"], "Alt": alt,
                                 "Status": row.get("Status"), "reasons": reasons})
            else:
                usable.append({"AltRecID": row["AltRecID"], "Alt": alt, "QtyRatio": _number(ratio),
                               "Priority": row["Priority"], "MixAllowed": _yes(row.get("MixAllowed")),
                               "BundleID": row.get("BundleID"), "Reverse": reverse, "bundle_members": members})
        usable.sort(key=lambda r: (r["Priority"], r["AltRecID"], r["Reverse"]))
        excluded = self._clean_excluded("alternates")
        for row in rejected:
            for rule in {reason.split()[0] for reason in row["reasons"]}:
                excluded[rule] += 1
        return {"primary": primary, "parent": parent, "fg": fg, "usable": usable, "rejected": rejected,
                "rules": list(ALT_RULES.values()),
                "excluded": dict(excluded)}

    # ---- 缺料試算 ----
    def shortage(self, part_no: str, qty: int, plant: str, prod_date: dt.date,
                 include_po: bool = False, include_alt: bool = False,
                 only_parts: Optional[Iterable[str]] = None) -> dict:
        """缺料試算。需求 = explode；供給 = available（include_po 時加 inbound）。
        include_alt 時：對短缺的主料，依 positions（直接上階）找 usable 替代料，依 Priority 用替代料的（可用庫存＋在途若 include_po）剩餘量補，
        替代料自己也是本單需求料時先扣掉自己的需求；MixAllowed=N（alt_no_mixing）以「每一片上階組件」為單位：同一片板子該位置全用主料或全用替代料（不同片可以不同）；MixAllowed=Y 可在同一片混用；Bundle（alt_bundle）同一片板子整組成員一起換，可換片數 = min(各成員替代料可用量 ÷ 每片替代用量)。
        only_parts：只計算這些 RM（例如查詢只問某些位置）。
        回傳 {"part","qty","plant","prod_date","rows": [{"PartNo","commodity","need","available","inbound","alt_cover": [{"Alt","AltRecID","qty_used"}],"short"}],
               "shortages": [短缺的 rows], "max_buildable": int(用同一套供給、二分搜尋可完整做幾台), "rules": [...], "excluded": {...}, "notes": [...]}"""
        part_no, plant = self._require(part_no), self._plant(plant)
        day = self._production_date(prod_date)
        quantity = _decimal(qty)
        if quantity is None or quantity < 0 or quantity.denominator != 1:
            raise ValueError("生產台數必須是大於或等於零的整數")
        quantity = int(quantity)
        selected = None if only_parts is None else {self._require(p) for p in only_parts}
        self._sync_indexes()
        requirements, excluded = self._requirements(part_no, day)
        focused = {no for no in requirements if selected is None or no in selected}
        stocks, alt_cache = {}, {}

        def stock(no):
            if no not in stocks:
                inv = self.available(no, plant, day)
                incoming = self.inbound(no, plant, day) if include_po else {"qty": 0, "excluded": {}}
                stocks[no] = (_decimal(inv["qty"]), _decimal(incoming["qty"]))
                # Global cleaning counts refer to rows, not to the number of lookups.
                for report in (inv, incoming):
                    for reason, count in report["excluded"].items():
                        if reason.startswith("clean_"):
                            excluded[reason] = count
                        else:
                            excluded[reason] += count
            return stocks[no]

        def choices(no, parent):
            key = no, parent
            if key not in alt_cache:
                result = self.alternates(no, parent, part_no if self._parts[part_no].get("PartType") == "FG" else None, day)
                alt_cache[key] = result["usable"]
                # Public JSON ratios are floats. Allocation uses the original exact
                # ratio so a reverse 1/13 cannot turn thirteen parts into a shortage.
                for alt in alt_cache[key]:
                    for member in [alt] + alt["bundle_members"]:
                        ratio = _decimal(self._alt_records[member["AltRecID"]]["QtyRatio"])
                        member["_ratio"] = _ONE / ratio if alt["Reverse"] else ratio
                for reason, count in result["excluded"].items():
                    if reason.startswith("clean_"):
                        excluded[reason] = count
                    else:
                        excluded[reason] += count
            return alt_cache[key]

        def simulate(count):
            pools, states, positions, position_copies = {}, {}, defaultdict(list), {}
            alt_consumed = defaultdict(lambda: _ZERO)
            notes = []

            def pool(no):
                if no not in pools:
                    inv, incoming = stock(no)
                    pools[no] = inv + incoming
                return pools[no]

            def allowance(no):
                # Carry a rounded unit's unused fraction to other positions using
                # the same RM; rounding every BOM position would inflate demand.
                raw = alt_consumed[no]
                return pool(no) + _ceil(raw, self._parts[no]["UoM"]) - raw

            def consume(no, raw):
                previous = alt_consumed[no]
                alt_consumed[no] += raw
                used = (_ceil(alt_consumed[no], self._parts[no]["UoM"]) -
                        _ceil(previous, self._parts[no]["UoM"]))
                pools[no] -= used
                return used

            # Reserve the needs of BOM materials before any is offered as an alternate.
            # Unselected materials still reserve their stock and participate in bundles,
            # but do not themselves trigger substitution or constrain the answer.
            for no, entry in requirements.items():
                raw = entry["raw"] * count
                need = _ceil(raw, self._parts[no]["UoM"])
                reserved = min(need, pool(no))
                pools[no] -= reserved
                states[no] = {"raw": raw, "need": need, "main": reserved,
                              "covered": _ZERO, "alt_cover": {}}
                for template in entry["positions"]:
                    position = dict(template, covered=_ZERO, capacity=template["qty_per_top"] * count)
                    positions[(position["context"], no)].append(position)
                    position_copies[id(template)] = position

            def missing(no):
                s = states[no]
                return max(_ZERO, s["raw"] - s["covered"] - s["main"])

            def cover(no, pos, amount, alternate, used):
                s = states[no]
                pos["covered"] += amount
                s["covered"] += amount
                # Whole-board substitution may also replace already reserved main stock.
                required_main = _ceil(s["raw"] - s["covered"], self._parts[no]["UoM"])
                released = max(_ZERO, s["main"] - required_main)
                s["main"] -= released
                pools[no] += released
                key = alternate["Alt"], alternate["AltRecID"]
                s["alt_cover"][key] = s["alt_cover"].get(key, _ZERO) + used

            if include_alt and count:
                # Retry after a substitution releases main stock needed by another
                # position. Every successful step consumes previously unswitched demand.
                progress = True
                while progress:
                    progress = False
                    for no in requirements:
                        if no not in focused or missing(no) <= 0:
                            continue
                        for template in requirements[no]["positions"]:
                            context = template["context"]
                            group = positions[(context, no)]
                            pos = position_copies[id(template)]
                            for alt in choices(no, pos["parent"]):
                                if missing(no) <= 0 or pos["covered"] >= pos["capacity"]:
                                    break
                                ratio = alt["_ratio"]
                                if alt["BundleID"]:
                                    members = []
                                    for member in alt["bundle_members"]:
                                        member_positions = positions.get((context, member["Primary"]), [])
                                        if not member_positions:
                                            members = []
                                            break
                                        members.append((member, member_positions))
                                    if not members:
                                        continue
                                    per_alt = defaultdict(lambda: _ZERO)
                                    limit = None
                                    for member, member_positions in members:
                                        per_board = sum((p["per_board"] for p in member_positions), _ZERO)
                                        boards = min(_floor((p["capacity"] - p["covered"]) / p["per_board"])
                                                     for p in member_positions)
                                        limit = boards if limit is None else min(limit, boards)
                                        per_alt[member["Alt"]] += per_board * member["_ratio"]
                                    for alt_no, usage in per_alt.items():
                                        limit = min(limit, _floor(allowance(alt_no) / usage))
                                    trigger_usage = sum((p["per_board"] for p in group), _ZERO)
                                    boards = min(limit, int(_ceil(missing(no) / trigger_usage, "PCS")))
                                    if boards <= 0:
                                        continue
                                    # Consume cumulatively, including members sharing an alt.
                                    for member, member_positions in members:
                                        alt_no = member["Alt"]
                                        for p in member_positions:
                                            amount = p["per_board"] * boards
                                            raw_used = amount * member["_ratio"]
                                            used = consume(alt_no, raw_used)
                                            cover(member["Primary"], p, amount, member, used)
                                    notes.append("上階 %s 的成組替代 %s 一起切換 %s 片" % (pos["parent"], alt["BundleID"], boards))
                                else:
                                    alt_no = alt["Alt"]
                                    room = pos["capacity"] - pos["covered"]
                                    if alt["MixAllowed"]:
                                        amount = min(missing(no), room, allowance(alt_no) / ratio)
                                    else:
                                        step = pos["per_board"]
                                        boards = min(_floor(room / step), _floor(allowance(alt_no) / (step * ratio)),
                                                     int(_ceil(missing(no) / step, "PCS")))
                                        amount = step * boards
                                    if amount <= 0:
                                        continue
                                    used = consume(alt_no, amount * ratio)
                                    cover(no, pos, amount, alt, used)
                                    detail = ("（整片替換 %s 片）" % _number(amount / pos["per_board"])) if not alt["MixAllowed"] else ""
                                    notes.append("上階 %s：%s 改用替代料 %s，使用 %s %s%s" %
                                                 (pos["parent"], no, alt_no, _number(used), self._parts[alt_no]["UoM"], detail))
                                progress = True
            rows = []
            for no in requirements:
                if no not in focused:
                    continue
                inv, incoming = stock(no)
                s = states[no]
                rows.append({"PartNo": no, "commodity": self._parts[no].get("Commodity"),
                             "uom": self._parts[no]["UoM"],
                             "need": _number(s["need"]), "available": _number(inv), "inbound": _number(incoming),
                             "alt_cover": [{"Alt": key[0], "AltRecID": key[1], "qty_used": _number(used)}
                                           for key, used in s["alt_cover"].items()],
                             "short": _number(_ceil(missing(no), self._parts[no]["UoM"]))})
            return rows, notes

        rows, notes = simulate(quantity)
        # Capture evidence for the requested quantity, not repeated binary-search probes.
        reported_excluded = dict(excluded)
        shortages = [row for row in rows if row["short"] > 0]
        if not shortages:
            maximum = quantity
        else:
            low, high = 0, quantity
            while low < high:
                mid = (low + high + 1) // 2
                probe, _ = simulate(mid)
                if any(row["short"] > 0 for row in probe):
                    high = mid - 1
                else:
                    low = mid
            maximum = low
        rules = ["半成品一律展開", "BOM 生效日、損耗率與彙總進位", "可用庫存計算"]
        if include_po:
            rules.append("在途與四天進料檢驗")
        if include_alt:
            rules.extend(ALT_RULES.values())
            rules.append("替代料：優先滿足自身需求")
        return {"part": part_no, "qty": quantity, "plant": plant, "prod_date": day.isoformat(),
                "rows": rows, "shortages": shortages, "max_buildable": maximum,
                "rules": rules, "excluded": reported_excluded, "notes": notes}
