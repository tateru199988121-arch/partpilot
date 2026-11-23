"""權限與工具（用手做資料 + 真 Engine 的部分方法以 FakeEngine 模擬）。"""
import datetime as dt
import json
import pytest
from partpilot.authz import Policy, PermissionDenied
from partpilot.tools import tool_specs, ToolExecutor


class FakeEngine:
    """最小資料：DC 成品 FGD010-01 → SA100001 → CAP-000001, MOS-000001；AC 成品 FGA010-01 → SA100002 → CAP-000001, RLY-000001。
    CAP-000001 有替代料 CAP-000002（GLOBAL）。"""
    def __init__(self):
        self.data = {
            "parts": {k: {"PartNo": k, "PartType": t, "Commodity": c, "ProductLine": pl, "SupplierName": "供應商-0" + str(i),
                          "MfrPartNo": "M" + str(i), "UnitCostUSD": 1.5 + i, "UoM": "PCS", "Lifecycle": "Active", "PartDesc": k}
                      for i, (k, t, c, pl) in enumerate([
                          ("FGD010-01", "FG", None, "DC"), ("FGA010-01", "FG", None, "AC"), ("SA100001", "SA", None, "DC"),
                          ("SA100002", "SA", None, "AC"), ("CAP-000001", "RM", "CAP", None), ("CAP-000002", "RM", "CAP", None),
                          ("MOS-000001", "RM", "MOS", None), ("RLY-000001", "RM", "RLY", None), ("RES-000001", "RM", "RES", None)])},
            "bom": [{"Parent": "FGD010-01", "Child": "SA100001"}, {"Parent": "SA100001", "Child": "CAP-000001"},
                    {"Parent": "SA100001", "Child": "MOS-000001"}, {"Parent": "FGA010-01", "Child": "SA100002"},
                    {"Parent": "SA100002", "Child": "CAP-000001"}, {"Parent": "SA100002", "Child": "RLY-000001"}],
            "alternates": [{"AltRecID": "ALT0000001", "Primary": "CAP-000001", "Alt": "CAP-000002", "Bidirectional": False}],
        }
    def part(self, p):
        return self.data["parts"].get(p.strip().upper())
    def where_used(self, p, prod_date=None):
        fg = {"CAP-000001": ["FGD010-01", "FGA010-01"], "MOS-000001": ["FGD010-01"], "RLY-000001": ["FGA010-01"]}.get(p, [])
        return {"part": p, "direct_parents": [{"Parent": "SA100001" if f.startswith("FGD") else "SA100002"} for f in fg],
                "finished_goods": fg, "paths": [[p, "SA100001" if f.startswith("FGD") else "SA100002", f] for f in fg]}
    def explode(self, p, qty, d):
        rm = {"FGD010-01": ["CAP-000001", "MOS-000001"], "FGA010-01": ["CAP-000001", "RLY-000001"]}[p]
        return {"part": p, "qty": qty, "rm": {r: {"qty": qty, "uom": "PCS", "commodity": r[:3], "positions": []} for r in rm}, "rules": [], "excluded": {}}
    def available(self, p, plant, d):
        return {"part": p, "plant": plant, "qty": 10, "uom": "PCS", "lots": 1, "excluded": {}, "rules": []}
    def inbound(self, p, plant, d):
        return {"part": p, "plant": plant, "qty": 5, "pos": [{"PONo": "PO00000001", "POLine": 1, "OpenQty": 5, "ETA": "2026-11-01",
                "SupplierName": "供應商-09", "UnitPriceUSD": 2.5}], "late_pos": [], "excluded": {}, "rules": []}
    def alternates(self, primary, parent, fg, d):
        return {"primary": primary, "parent": parent, "usable": [{"AltRecID": "ALT0000001", "Alt": "CAP-000002", "QtyRatio": 1}], "rejected": [], "rules": []}
    def shortage(self, p, qty, plant, d, include_po=False, include_alt=False, only_parts=None):
        return {"part": p, "qty": qty, "plant": plant, "rows": [], "shortages": [], "max_buildable": qty, "rules": [], "excluded": {}, "notes": []}


U = {
 "supply_chain": {"username": "scm", "role": "supply_chain", "product_line": None, "plant": None, "commodity_group": None},
 "LINE_DC": {"username": "line_dc", "role": "product_line", "product_line": "DC", "plant": None, "commodity_group": None},
 "LINE_AC": {"username": "line_ac", "role": "product_line", "product_line": "AC", "plant": None, "commodity_group": None},
 "PLANT_C": {"username": "plant_c", "role": "plant_control", "product_line": None, "plant": "PLANT-C", "commodity_group": None},
 "COMPONENT_PASSIVE": {"username": "component_passive", "role": "component", "product_line": None, "plant": None, "commodity_group": "被動元件"},
 "admin": {"username": "admin", "role": "admin", "product_line": None, "plant": None, "commodity_group": None},
}

def ex(u):
    return ToolExecutor(U[u], FakeEngine())

def dumps(x):
    return json.dumps(x, ensure_ascii=False)

def test_product_line_scope_dynamic():
    p = Policy(U["LINE_DC"], FakeEngine())
    s = p.scope_parts()
    assert {"FGD010-01", "SA100001", "CAP-000001", "MOS-000001", "CAP-000002"} <= s
    assert "FGA010-01" not in s and "RLY-000001" not in s
    with pytest.raises(PermissionDenied):
        p.check_part("FGA010-01")

def test_engineer_scope_commodity_and_where_used():
    s = Policy(U["COMPONENT_PASSIVE"], FakeEngine()).scope_parts()
    assert {"CAP-000001", "CAP-000002", "RES-000001", "SA100001", "FGD010-01", "FGA010-01"} <= s
    assert "MOS-000001" not in s and "RLY-000001" not in s

def test_product_line_other_line_denied_and_no_shared_leak():
    r = ex("LINE_DC")("explode_bom", {"part_no": "FGA010-01", "qty": 1, "prod_date": "2026-11-20"})
    assert r["error"] == "permission_denied"
    w = ex("LINE_DC")("where_used", {"part_no": "CAP-000001"})
    assert "FGA010-01" not in dumps(w) and "SA100002" not in dumps(w)

def test_price_only_component():
    for u in ("supply_chain", "LINE_DC", "PLANT_C"):
        out = dumps(ex(u)("get_part", {"part_no": "CAP-000001"}))
        assert "UnitCostUSD" not in out and "2.5" not in out and "5.5" not in out
        out = dumps(ex(u)("get_inbound", {"part_no": "CAP-000001", "plant": "PLANT-C", "prod_date": "2026-11-20"}))
        assert "UnitPriceUSD" not in out
    out = dumps(ex("COMPONENT_PASSIVE")("get_part", {"part_no": "CAP-000001"}))
    assert "UnitCostUSD" in out

def test_sensitive_fields():
    for u in ("supply_chain", "LINE_DC"):
        out = dumps(ex(u)("get_part", {"part_no": "CAP-000001"}))
        assert "SupplierName" not in out and "供應商-04" not in out
    assert "供應商-04" in dumps(ex("PLANT_C")("get_part", {"part_no": "CAP-000001"}))
    assert "供應商-09" in dumps(ex("PLANT_C")("get_inbound", {"part_no": "CAP-000001", "plant": "PLANT-C", "prod_date": "2026-11-20"}))

def test_plant_only():
    r = ex("PLANT_C")("get_inventory", {"part_no": "CAP-000001", "plant": "PLANT-D", "prod_date": "2026-11-20"})
    assert r["error"] == "permission_denied"
    r = ex("PLANT_C")("get_inventory", {"part_no": "CAP-000001", "plant": "PLANT-C", "prod_date": "2026-11-20"})
    assert r["qty"] == 10
    assert ex("PLANT_C")("explode_bom", {"part_no": "FGA010-01", "qty": 1, "prod_date": "2026-11-20"}).get("error") is None

def test_admin_no_data_tools():
    assert tool_specs(U["admin"]) == []
    assert ex("admin")("get_part", {"part_no": "CAP-000001"})["error"] == "permission_denied"

def test_engineer_out_of_group_denied():
    assert ex("COMPONENT_PASSIVE")("get_part", {"part_no": "MOS-000001"})["error"] == "permission_denied"
    e = ex("COMPONENT_PASSIVE")("explode_bom", {"part_no": "FGD010-01", "qty": 1, "prod_date": "2026-11-20"})
    assert "CAP-000001" in dumps(e) and "MOS-000001" not in dumps(e)

def test_specs_and_bad_args():
    names = {t["name"] for t in tool_specs(U["supply_chain"])}
    assert {"get_part", "explode_bom", "where_used", "get_inventory", "get_inbound", "get_alternates", "shortage"} <= names
    assert all("input_schema" in t for t in tool_specs(U["supply_chain"]))
    assert ex("supply_chain")("nope", {})["error"] == "bad_request"
    assert ex("supply_chain")("shortage", {"part_no": "FGD010-01"})["error"] == "bad_request"

def test_aggregate_supplier_count_less_than_4():
    r = ex("supply_chain")("supplier_stats", {"part_nos": ["CAP-000001", "CAP-000002"]})
    assert r["supplier_count"] == "少於 4"

def test_impact_analysis_role_limits():
    r = ex("PLANT_C")("impact_analysis", {"part_no": "CAP-000001", "prod_date": "2026-12-01", "new_lead_weeks": 20})
    assert list(r["supply_by_plant"]) == ["PLANT-C"]
    assert r["new_order_eta"] == "2027-04-20"
    assert ex("admin")("impact_analysis", {"part_no": "CAP-000001"})["error"] == "permission_denied"
    w = ex("LINE_DC")("impact_analysis", {"part_no": "CAP-000001", "prod_date": "2026-12-01"})
    assert "FGA010-01" not in dumps(w)

def test_product_line_alternate_records_hides_other_line_scope_value():
    eng = FakeEngine()
    eng.data["alternates"] = [
        {"AltRecID": "ALT0000001", "Primary": "CAP-000001", "Alt": "CAP-000002", "Scope": "PARENT", "ScopeValue": "SA100002", "Bidirectional": False},
        {"AltRecID": "ALT0000002", "Primary": "CAP-000001", "Alt": "CAP-000002", "Scope": "PRODUCTLINE", "ScopeValue": "AC", "Bidirectional": False},
        {"AltRecID": "ALT0000003", "Primary": "CAP-000001", "Alt": "CAP-000002", "Scope": "PARENT", "ScopeValue": "SA100001", "Bidirectional": False}]
    out = dumps(ToolExecutor(U["LINE_DC"], eng)("alternate_records", {"part_no": "CAP-000001"}))
    assert "SA100002" not in out and "ALT0000001" not in out
    assert "ALT0000002" not in out          # 只限 AC 產品線的紀錄也不給 DC 的 product_line（避免透露共用料）
    assert "ALT0000003" in out

def test_supply_chain_price_stats_only_by_commodity():
    r = ex("supply_chain")("price_stats", {"part_nos": ["CAP-000001", "CAP-000002", "MOS-000001"]})
    assert r["error"] in ("permission_denied", "bad_request")
    r = ex("supply_chain")("price_stats", {"commodity": "CAP"})
    assert r["count"] == "少於 4" and "CAP-000001" not in dumps(r)


def test_free_text_notes_filtered_by_scope():
    eng = FakeEngine()
    eng.shortage = lambda *a, **k: {"part": "FGD010-01", "rows": [], "shortages": [], "max_buildable": 1, "rules": [], "excluded": {},
                                    "notes": ["上階 SA100001：MOS-000001 改用替代料", "上階 SA100001：CAP-000001 改用替代料 CAP-000002"]}
    out = dumps(ToolExecutor(U["COMPONENT_PASSIVE"], eng)("shortage", {"part_no": "FGD010-01", "qty": 1, "plant": "PLANT-C", "include_alt": True}))
    assert "MOS-000001" not in out and "CAP-000002" in out

def test_supply_chain_price_stats_by_product_line():
    r = ex("supply_chain")("price_stats", {"product_line": "DC"})
    assert "error" not in r and r["count"] == "少於 4"      # DC 涵蓋的 RM：CAP-000001、MOS-000001、CAP-000002（替代料不算 BOM 用料）
    assert "CAP-000001" not in dumps(r)

def test_clean_excluded_split_from_query_excluded():
    eng = FakeEngine()
    eng.available = lambda p, plant, d: {"part": p, "plant": plant, "qty": 10, "uom": "PCS", "lots": 1,
                                        "excluded": {"clean_inventory": 581, "qc_not_pass": 1}, "rules": []}
    r = ToolExecutor(U["supply_chain"], eng)("get_inventory", {"part_no": "CAP-000001", "plant": "PLANT-C"})
    assert r["excluded"] == {"qc_not_pass": 1}
    assert r["cleaning_excluded_global"]["inventory"] == 581 and "說明" in r["cleaning_excluded_global"]
