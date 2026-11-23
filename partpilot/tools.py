"""LLM 工具層：把 Engine 包成工具，所有輸出經 Policy 過濾。"""
from __future__ import annotations

import datetime as dt
import math
import re

from .authz import PermissionDenied, Policy, _part_no
from .engine import ALT_RULES


_BASE_DATE = dt.date(2026, 11, 6)
_STRING = {"type": "string", "minLength": 1}
_DATE = {"type": "string", "format": "date", "pattern": r"^\d{4}-\d{2}-\d{2}$",
         "description": "生產日 YYYY-MM-DD；省略時為 2026-11-06。"}
_PLANT = {"type": "string", "minLength": 1,
          "description": "工廠代碼；可省略時彙總全部工廠，plant_control 省略時使用自己的工廠。"}
_PARTS = {"type": "array", "items": _STRING, "minItems": 1, "uniqueItems": True}


def _spec(name, description, properties, required):
    return {"name": name, "description": description,
            "input_schema": {"type": "object", "properties": properties,
                             "required": required, "additionalProperties": False}}


_PLANT_ALIAS = {"廠區C": "PLANT-C", "PLANTC": "PLANT-C",
                "廠區D": "PLANT-D", "PLANTD": "PLANT-D",
                "廠區A": "PLANT-A", "PLANTA": "PLANT-A",
                "廠區B": "PLANT-B", "PLANTB": "PLANT-B"}


def _plant(value):
    key = "".join(str(value).split())
    return _PLANT_ALIAS.get(key, _PLANT_ALIAS.get(key.upper(), key.upper()))


def _compact_explode(result, commodity=None):
    """只留每個原料的數量、單位、類別、位置與直接上階，避免工具輸出過長。"""
    rm = {}
    for part, row in result.get("rm", {}).items():
        if commodity and row.get("commodity") != commodity:
            continue
        pos = row.get("positions", [])
        rm[part] = {"qty": row.get("qty"), "uom": row.get("uom"), "commodity": row.get("commodity"),
                    "parents": sorted({p.get("parent") for p in pos if p.get("parent")}),
                    "refdes": sorted({p.get("refdes") for p in pos if p.get("refdes")})}
    out = dict(result)
    out["rm"] = rm
    out["rm_count"] = len(rm)
    return out


def _compact_shortage(result, keep_all=False):
    rows = result.get("rows", [])
    out = {k: v for k, v in result.items() if k not in ("rows", "shortages")}
    out["rm_checked"] = len(rows)
    out["shortages"] = result.get("shortages", [])
    out["covered_by_inbound_or_alt"] = [r for r in rows if r.get("short", 0) <= 0 and
                                        (r.get("alt_cover") or (r.get("inbound") or 0) > 0) and
                                        r.get("need", 0) > (r.get("available") or 0)]
    if keep_all:
        out["rows"] = rows
    return out


_CLEAN_NOTE = "資料清理階段整份檔案被排除的列數（全部資料，不是本次查詢專屬）；本次查詢排除的筆數在 excluded。"


def _split_clean(obj):
    """把引擎 excluded 裡的 clean_* 全域清理排除數移到 cleaning_excluded_global，避免和本次查詢的排除筆數混淆。"""
    if isinstance(obj, list):
        return [_split_clean(x) for x in obj]
    if not isinstance(obj, dict):
        return obj
    out = {k: _split_clean(v) for k, v in obj.items()}
    exc = out.get("excluded")
    if isinstance(exc, dict) and any(k.startswith("clean_") for k in exc):
        glob = {k[len("clean_"):]: v for k, v in exc.items() if k.startswith("clean_")}
        out["excluded"] = {k: v for k, v in exc.items() if not k.startswith("clean_")}
        out["cleaning_excluded_global"] = dict(glob, 說明=_CLEAN_NOTE)
    return out


def _specs():
    return [
        _spec("get_part", "查詢權限範圍內的料號主檔及材料屬性；欄位依角色遮罩。",
              {"part_no": _STRING}, ["part_no"]),
        _spec("explode_bom", "依生產日與生產數量展開多階 BOM，取得原料需求與用料位置。",
              {"part_no": _STRING, "qty": {"type": "number", "exclusiveMinimum": 0},
               "prod_date": _DATE, "commodity": {"type": "string", "description": "只列此物料類別碼（例如 RLY）"}},
              ["part_no", "qty"]),
        _spec("alternate_records", "列出某料號作為主料或替代料的所有替代料紀錄原文（核准狀態、方向、範圍、有效期、BundleID）。料號不在任何 BOM、無法指定上階時，或要確認 A→C 是否有自己的紀錄（alt_no_chaining 不串接）時用。",
              {"part_no": _STRING}, ["part_no"]),
        _spec("impact_analysis", "料件交期延長、停產或缺料時的影響分析（一次算完）：反查受影響成品，逐一成品換算每台用量，依各廠可用庫存＋生產日前可到的在途算出『只看這顆料』可做台數，列出晚到的在途、直接上階的可用替代料，並依新交期週數推算新下單到貨日。",
              {"part_no": _STRING, "prod_date": _DATE, "plant": _PLANT,
               "new_lead_weeks": {"type": "integer", "minimum": 1, "description": "新的交期週數（查詢有給時填）"}},
              ["part_no"]),
        _spec("bom_children", "查詢某上階（成品或半成品）在生產日生效的直接下階：料號、用量、損耗率、零件位置 RefDes。找特定位置（例如 C1-C4）的料號時用。",
              {"part_no": _STRING, "prod_date": _DATE}, ["part_no"]),
        _spec("where_used", "反查料號的直接上階、成品與使用路徑；只顯示權限範圍內的結果。",
              {"part_no": _STRING, "prod_date": _DATE}, ["part_no"]),
        _spec("get_inventory", "查詢生產日可用的合格且未過期庫存；省略工廠時彙總可查看的工廠。",
              {"part_no": _STRING, "plant": _PLANT, "prod_date": _DATE}, ["part_no"]),
        _spec("get_inbound", "查詢生產日前可到貨的在途採購與晚到採購；省略工廠時彙總可查看的工廠。",
              {"part_no": _STRING, "plant": _PLANT, "prod_date": _DATE}, ["part_no"]),
        _spec("get_alternates", "依主料、直接上階、成品及生產日判斷替代料可用性與拒絕原因。",
              {"primary": _STRING, "parent": _STRING, "fg": _STRING, "prod_date": _DATE},
              ["primary", "parent"]),
        _spec("shortage", "依成品數量、指定工廠與生產日試算缺料及最多可做數量；可選擇納入在途與替代料或限定原料。",
              {"part_no": _STRING, "qty": {"type": "integer", "minimum": 1},
               "plant": _PLANT, "prod_date": _DATE, "include_po": {"type": "boolean"},
               "include_alt": {"type": "boolean"}, "only_parts": _PARTS},
              ["part_no", "qty", "plant"]),
        _spec("supplier_stats", "統計指定範圍內料號的供應商家數與料號數，不提供名稱；少於四筆時只顯示「少於 4」。",
              {"part_nos": _PARTS}, ["part_nos"]),
        _spec("price_stats", "單價統計（平均、最小、最大），不列出料號；有效單價少於四筆時只顯示「少於 4」。supply_chain 只能依維度查：commodity（物料類別碼，例如 CAP）及／或 product_line（DC／AC，依該產品線成品 BOM 展開涵蓋的原材料）；component 給 part_nos。",
              {"part_nos": _PARTS, "commodity": {"type": "string"}, "product_line": {"type": "string", "enum": ["DC", "AC"]}}, []),
    ]


def tool_specs(user: dict) -> list:
    """回傳 Anthropic tools 格式的 list（name/description/input_schema），依角色過濾；admin 回 []。"""
    role = user.get("role")
    if role not in {"supply_chain", "product_line", "plant_control", "component"}:
        return []
    return [spec for spec in _specs()
            if spec["name"] != "price_stats" or role in {"supply_chain", "component"}]


class ToolExecutor:
    def __init__(self, user: dict, engine, audit=None):
        self.user = user
        self.engine = engine
        self.audit = audit
        self.policy = Policy(user, engine)

    def __call__(self, name: str, args: dict) -> dict:
        """執行工具，回傳可 json.dumps 的 dict。權限不足 → {"error": "permission_denied", "message": 中文}；
        參數錯／找不到料號 → {"error": "bad_request", "message": ...}。每次呼叫寫稽核（若有 audit）。"""
        result = "bad_request"
        try:
            if self.user.get("role") not in {"supply_chain", "product_line", "plant_control", "component"}:
                raise PermissionDenied("依權限規則不可查看料件資料或執行資料工具。")
            params = self._validate(name, args)
            part_fields = ("part_no", "primary", "parent", "fg")
            for key in part_fields:
                if key in params:
                    self.policy.check_part(params[key])
            for key in ("part_nos", "only_parts"):
                for part in params.get(key, []):
                    self.policy.check_part(part)
            if name in {"get_inventory", "get_inbound", "shortage"}:
                self.policy.check_plant(params["plant"])
            if name == "price_stats" and self.user.get("role") not in {"supply_chain", "component"}:
                raise PermissionDenied("依權限規則不可查看單價統計。")
            response = _split_clean(self.policy.redact(self._execute(name, params)))
            result = "ok"
            return response
        except PermissionDenied as exc:
            result = "permission_denied"
            return {"error": result, "message": str(exc)}
        except ValueError as exc:
            result = "bad_request"
            return {"error": result, "message": str(exc)}
        finally:
            if self.audit is not None:
                self.audit.log(self.user, "tool", {"name": name, "args": args, "result": result})

    def _validate(self, name, args):
        specs = {spec["name"]: spec["input_schema"] for spec in _specs()}
        if not isinstance(name, str) or name not in specs:
            raise ValueError("未知的工具名稱。")
        if not isinstance(args, dict):
            raise ValueError("工具參數必須為物件。")
        schema = specs[name]
        if any(key not in args for key in schema["required"]):
            raise ValueError("缺少必要參數：" + "、".join(schema["required"]) + "。")
        if any(key not in schema["properties"] for key in args):
            raise ValueError("工具包含不支援的參數。")
        params = dict(args)
        for key, value in params.items():
            kind = schema["properties"][key]["type"]
            if kind == "string":
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(key + " 必須為非空字串。")
            elif kind in {"number", "integer"}:
                if (isinstance(value, bool) or not isinstance(value, (int, float))
                        or not math.isfinite(value) or value <= 0
                        or (kind == "integer" and not float(value).is_integer())):
                    raise ValueError(key + (" 必須為正整數。" if kind == "integer" else " 必須為正數。"))
            elif kind == "boolean" and not isinstance(value, bool):
                raise ValueError(key + " 必須為布林值。")
            elif kind == "array":
                if (not isinstance(value, list) or not value
                        or any(not isinstance(p, str) or not p.strip() for p in value)):
                    raise ValueError(key + " 必須為非空料號清單。")
        for key in ("part_no", "primary", "parent", "fg"):
            if key in params:
                params[key] = _part_no(params[key])
        for key in ("part_nos", "only_parts"):
            if key in params:
                params[key] = list(dict.fromkeys(_part_no(p) for p in params[key]))
        if "prod_date" in schema["properties"]:
            value = params.get("prod_date", _BASE_DATE.isoformat())
            if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
                raise ValueError("prod_date 必須為 YYYY-MM-DD 日期。")
            try:
                params["prod_date"] = dt.date.fromisoformat(value)
            except ValueError:
                raise ValueError("prod_date 必須為有效日期。") from None
        if "plant" in schema["properties"]:
            plant = params.get("plant")
            if plant is None and self.user.get("role") == "plant_control":
                plant = self.user.get("plant")
            params["plant"] = _plant(plant) if plant else None
        if name == "shortage":
            params["qty"] = int(params["qty"])
        return params

    def _part(self, part_no):
        part = self.engine.part(part_no)
        if part is None:
            raise ValueError("找不到指定料號。")
        return part

    def _impact(self, params):
        part, day = params["part_no"], params["prod_date"]
        wu = self.policy.redact(self.engine.where_used(part, day))
        fgs = wu.get("finished_goods", [])
        if self.user.get("role") == "plant_control":
            plants = [self.user.get("plant")]
        elif params.get("plant"):
            plants = [params["plant"]]
        else:
            plants = sorted({r["Plant"] for r in self.engine.data.get("inventory", []) if r.get("PartNo") == part} |
                            {r["Plant"] for r in self.engine.data.get("po", []) if r.get("PartNo") == part})
        supply = {}
        for pl in plants:
            av = self.engine.available(part, pl, day)
            ib = self.engine.inbound(part, pl, day)
            supply[pl] = {"available": av["qty"], "inbound_by_date": ib["qty"],
                          "pos_in_time": [{k: v for k, v in po.items() if k != "UnitPriceUSD"} for po in ib.get("pos", [])],
                          "late_pos": [{k: v for k, v in po.items() if k != "UnitPriceUSD"} for po in ib.get("late_pos", [])],
                          "excluded": {**av.get("excluded", {}), **ib.get("excluded", {})}}
        products = []
        for fg in fgs:
            ex = self.engine.explode(fg, 1, day)["rm"].get(part)
            if not ex:
                continue
            per_unit = sum(p["qty_per_top"] for p in ex.get("positions", []))
            parents = sorted({p["parent"] for p in ex.get("positions", [])})
            alts = {}
            for parent in parents:
                a = self.engine.alternates(part, parent, fg, day)
                scope = self.policy.scope_parts()
                visible = lambda alt: scope is None or alt in scope
                alts[parent] = {"usable": [u["Alt"] + "(" + u["AltRecID"] + ")" for u in a["usable"] if visible(u["Alt"])],
                                "rejected": [r["Alt"] + ":" + ";".join(r["reasons"]) for r in a["rejected"] if visible(r["Alt"])]}
            products.append({"fg": fg, "qty_per_unit": per_unit, "direct_parents": parents,
                             "max_units_by_plant_this_part_only": {pl: int((v["available"] + v["inbound_by_date"]) // per_unit) if per_unit else None
                                                                    for pl, v in supply.items()},
                             "alternates_by_parent": alts})
        out = {"part": part, "prod_date": day.isoformat(), "finished_goods": fgs, "supply_by_plant": supply,
               "products": products, "note": "各成品台數是『這批料全給該成品』的上限，彼此共用同一批料、不可相加。",
               "rules": ["BOM 生效與用量", *ALT_RULES.values(), "可用庫存與在途 ETA+4 天"]}
        if params.get("new_lead_weeks"):
            start = max(day, dt.date(2026, 11, 6))
            arrive = start + dt.timedelta(days=7 * params["new_lead_weeks"])
            out["new_order_eta"] = arrive.isoformat()
            out["new_order_usable_from"] = (arrive + dt.timedelta(days=4)).isoformat()
        return out

    def _shortage_detail(self, params, out):
        """補上採購單明細（include_po）與成組替代成員，讓回答能列出依據。"""
        rows = out.get("shortages", []) + out.get("covered_by_inbound_or_alt", [])
        if params.get("include_po"):
            for row in rows:
                if (row.get("inbound") or 0) > 0:
                    ib = self.engine.inbound(row["PartNo"], params["plant"], params["prod_date"])
                    row["pos"] = [{k: v for k, v in po.items() if k != "UnitPriceUSD"} for po in ib.get("pos", [])]
        if params.get("include_alt"):
            for row in rows:
                for cover in row.get("alt_cover") or []:
                    rec = next((a for a in self.engine.data.get("alternates", []) if a.get("AltRecID") == cover.get("AltRecID")), None)
                    if rec and rec.get("BundleID"):
                        cover["BundleID"] = rec["BundleID"]
                        cover["bundle_members"] = [{"Primary": a["Primary"], "Alt": a["Alt"], "AltRecID": a["AltRecID"]}
                                                   for a in self.engine.data.get("alternates", []) if a.get("BundleID") == rec["BundleID"]]
        return out

    def _execute(self, name, params):
        if name == "get_part":
            part = dict(self._part(params["part_no"]))
            part["attrs"] = self.engine.data.get("attrs", {}).get(params["part_no"], {})
            return part
        if name == "price_stats":
            if self.user.get("role") == "supply_chain":
                # supply_chain 只能看不指名料號的統計：只接受整個物料類別，避免用不同料號組合差分推回單價
                if params.get("part_nos") or not (params.get("commodity") or params.get("product_line")):
                    raise PermissionDenied("依權限規則，supply_chain 的單價只能以物料類別（commodity）或產品線（product_line）等維度查詢統計，不能指定料號。")
                parts = [p for p in self.engine.data.get("parts", {}).values() if p.get("PartType") == "RM"]
                if params.get("commodity"):
                    code = str(params["commodity"]).strip().upper()
                    parts = [p for p in parts if p.get("Commodity") == code]
                if params.get("product_line"):
                    line = str(params["product_line"]).strip().upper()
                    prefix = {"DC": "FGD", "AC": "FGA"}.get(line)
                    if not prefix:
                        raise ValueError("product_line 只能是 DC 或 AC。")
                    children = {}
                    for row in self.engine.data.get("bom", []):
                        children.setdefault(row["Parent"], set()).add(row["Child"])
                    reach = {k for k in self.engine.data.get("parts", {}) if k.startswith(prefix)}
                    todo = list(reach)
                    while todo:
                        for c in children.get(todo.pop(), ()):
                            if c not in reach:
                                reach.add(c)
                                todo.append(c)
                    parts = [p for p in parts if p["PartNo"] in reach]
            else:
                parts = [self._part(p) for p in params.get("part_nos") or []]
                if not parts:
                    raise ValueError("price_stats 需要 part_nos。")
        if name in {"supplier_stats", "price_stats"}:
            if name == "supplier_stats":
                parts = [self._part(p) for p in params["part_nos"]]
            if name == "supplier_stats":
                suppliers = {p["SupplierName"] for p in parts if p.get("SupplierName")}
                return {"supplier_count": self._count(len(suppliers)), "part_count": self._count(len(parts))}
            prices = [p["UnitCostUSD"] for p in parts
                      if isinstance(p.get("UnitCostUSD"), (int, float))
                      and not isinstance(p["UnitCostUSD"], bool) and math.isfinite(p["UnitCostUSD"])]
            if len(prices) < 4:
                return {"count": "少於 4", "average": "少於 4", "min": "少於 4", "max": "少於 4"}
            return {"count": len(prices), "average": sum(p / len(prices) for p in prices),
                    "min": min(prices), "max": max(prices)}
        # 先驗證所有輸入料號存在，再做引擎計算，避免部分執行。
        for key in ("part_no", "primary", "parent", "fg"):
            if key in params:
                self._part(params[key])
        for part in params.get("only_parts", []):
            self._part(part)
        if name == "explode_bom":
            return _compact_explode(self.engine.explode(params["part_no"], params["qty"], params["prod_date"]),
                                    (params.get("commodity") or "").strip().upper() or None)
        if name == "impact_analysis":
            return self._impact(params)
        if name == "alternate_records":
            part = params["part_no"]
            recs = [a for a in self.engine.data.get("alternates", []) if part in (a.get("Primary"), a.get("Alt"))]
            return {"part": part, "as_primary": [a for a in recs if a.get("Primary") == part],
                    "as_alt": [a for a in recs if a.get("Alt") == part],
                    "rules": [ALT_RULES[key] for key in ("alt_approval", "alt_bidirectional", "alt_no_chaining", "alt_invalid_or_conflicting")],
                    "note": "這是紀錄原文；能否在特定上階使用仍須以 get_alternates 判斷有效期間、適用範圍、生命週期、屬性相容、比例用量、整片不混用與成組切換。Pending 需負責主料類別的 component 核准後才可用。"}
        if name == "bom_children":
            return {"parent": params["part_no"], "children": self.engine.bom_children(params["part_no"], params["prod_date"]),
                    "rules": ["BOM 生效"]}
        if name == "where_used":
            return self.engine.where_used(params["part_no"], params["prod_date"])
        if name == "get_alternates":
            return self.engine.alternates(params["primary"], params["parent"], params.get("fg"), params["prod_date"])
        if name == "shortage":
            return self._shortage_detail(params, _compact_shortage(self.engine.shortage(params["part_no"], params["qty"], params["plant"], params["prod_date"],
                                        include_po=params.get("include_po", False),
                                        include_alt=params.get("include_alt", False),
                                        only_parts=params.get("only_parts")), bool(params.get("only_parts"))))
        method = self.engine.available if name == "get_inventory" else self.engine.inbound
        if params["plant"] is not None:
            return method(params["part_no"], params["plant"], params["prod_date"])
        return self._all_plants(name, method, params)

    @staticmethod
    def _count(value):
        return value if value >= 4 else "少於 4"

    def _all_plants(self, name, method, params):
        # Engine 的工廠參數是單一代碼；在工具層逐廠計算後合併。
        source = "inventory" if name == "get_inventory" else "po"
        plants = sorted({row["Plant"] for row in self.engine.data.get(source, []) if row.get("Plant")})
        result = {"part": params["part_no"], "plant": None, "qty": 0,
                  "uom": self._part(params["part_no"]).get("UoM"), "rules": [], "excluded": {}}
        if name == "get_inventory":
            result.update({"uom": self._part(params["part_no"]).get("UoM"), "lots": 0})
        else:
            result.update({"pos": [], "late_pos": []})
        for plant in plants:
            row = method(params["part_no"], plant, params["prod_date"])
            result["qty"] += row.get("qty", 0)
            for rule in row.get("rules", []):
                if rule not in result["rules"]:
                    result["rules"].append(rule)
            for reason, count in row.get("excluded", {}).items():
                result["excluded"][reason] = result["excluded"].get(reason, 0) + count
            if name == "get_inventory":
                result["lots"] += row.get("lots", 0)
            else:
                for key in ("pos", "late_pos"):
                    result[key].extend(dict(po, Plant=plant) for po in row.get(key, []))
        return result
