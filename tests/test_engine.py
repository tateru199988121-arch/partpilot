"""計算引擎（手做 clean.json 格式資料，數字手算）。"""
import datetime as dt
import pytest
from partpilot.engine import Engine

D = dt.date(2026, 11, 20)

def P(no, typ, com=None, pl=None, uom="PCS", life="Active"):
    return {"PartNo": no, "PartDesc": no, "PartType": typ, "Commodity": com, "ProductLine": pl, "UoM": uom,
            "Lifecycle": life, "MakeBuy": "B", "SupplierName": "供應商-01", "MfrPartNo": "X", "LeadTimeDays": 56,
            "MOQ": 1, "UnitCostUSD": 1.0}

def B(p, c, q, scrap=0.0, ref="", f="2024-01-01", t=None, line=10):
    return {"Parent": p, "Child": c, "Rev": "A", "LineNo": line, "QtyPer": q, "Scrap": scrap, "RefDes": ref, "EffFrom": f, "EffTo": t}

def A(i, pri, alt, scope="GLOBAL", sv=None, prio=1, ratio=1.0, bi=False, mix=True, bnd=None, st="Approved", vf="2025-01-01", vt=None):
    return {"AltRecID": i, "Primary": pri, "Alt": alt, "Scope": scope, "ScopeValue": sv, "Priority": prio, "QtyRatio": ratio,
            "Bidirectional": bi, "MixAllowed": mix, "BundleID": bnd, "Status": st, "ValidFrom": vf, "ValidTo": vt, "Remark": ""}

def I(plant, part, lot, qty, qc="合格", dc="2620"):
    return {"Plant": plant, "PartNo": part, "LotNo": lot, "DateCode": dc, "QCStatus": qc, "Qty": qty, "StorageLoc": "X"}

def PO(no, plant, part, q, eta, st="Open"):
    return {"PONo": no, "POLine": 1, "Plant": plant, "PartNo": part, "SupplierName": "供應商-02", "OpenQty": q, "UoM": "PCS",
            "UnitPriceUSD": 1.0, "OrderDate": "2026-09-01", "ETA": eta, "Status": st}

CAPATTR = {"Capacitance": 100.0, "RatedVoltage": 500.0, "Package": "RADIAL", "RoHS": "Y", "SafetyCert": ["UL"]}

@pytest.fixture
def eng():
    parts = [P("FGD010-01", "FG", pl="DC"), P("FGA010-01", "FG", pl="AC"), P("SA100001", "SA", pl="DC"), P("SA100002", "SA", pl="DC"),
             P("CAP-000001", "RM", "CAP"), P("CAP-000002", "RM", "CAP"), P("CAP-000003", "RM", "CAP"), P("CAP-000004", "RM", "CAP", life="EOL"),
             P("CBL-000001", "RM", "CBL", uom="M"), P("MOS-000001", "RM", "MOS"), P("MOS-000002", "RM", "MOS"),
             P("DIO-000001", "RM", "DIO"), P("DIO-000002", "RM", "DIO"), P("CAP-000005", "RM", "CAP")]
    data = {
     "meta": {"base_date": "2026-11-06"},
     "parts": {p["PartNo"]: p for p in parts},
     "attrs": {"CAP-000001": dict(CAPATTR), "CAP-000002": dict(CAPATTR), "CAP-000003": dict(CAPATTR),
               "CAP-000004": dict(CAPATTR), "CAP-000005": dict(CAPATTR, RatedVoltage=400.0),
               "MOS-000001": {}, "MOS-000002": {"RoHS": "Y"}, "DIO-000001": {}, "DIO-000002": {"RoHS": "Y"}},
     "bom": [B("FGD010-01", "SA100001", 2, line=10), B("FGD010-01", "CBL-000001", 1.5, 0.1, line=20),
             B("SA100001", "CAP-000001", 3, 0.02, "C1-C3", line=10),
             B("SA100001", "MOS-000001", 1, ref="Q1", line=20), B("SA100001", "DIO-000001", 1, ref="D1", line=30),
             B("SA100001", "CAP-000003", 1, ref="C9", f="2026-12-01", line=40),       # 11/20 未生效
             B("FGA010-01", "SA100002", 1), B("SA100002", "CAP-000001", 1)],
     "alternates": [A("ALT0000001", "CAP-000001", "CAP-000002", prio=1),
                    A("ALT0000002", "CAP-000002", "CAP-000003"),                          # 不得串接 1→3
                    A("ALT0000003", "CAP-000001", "CAP-000004", prio=2),                  # alt_lifecycle EOL
                    A("ALT0000004", "CAP-000001", "CAP-000005", prio=3),                  # alt_attribute_compatibility 電壓較低
                    A("ALT0000005", "CAP-000001", "CAP-000003", st="Pending", prio=4),    # alt_approval
                    A("ALT0000006", "MOS-000001", "MOS-000002", "PARENT", "SA100001", mix=False, bnd="BND-0001"),
                    A("ALT0000007", "DIO-000001", "DIO-000002", "PARENT", "SA100001", mix=False, bnd="BND-0001"),
                    A("ALT0000008", "CAP-000001", "CAP-000002", "PRODUCTLINE", "AC", prio=5)],
     "inventory": [I("PLANT-C", "CAP-000001", "L1", 1000), I("PLANT-C", "CAP-000001", "L2", 500, qc="待驗"),
                   I("PLANT-D", "CAP-000001", "L3", 9999), I("PLANT-C", "CAP-000002", "L4", 120, dc="2640"),
                   I("PLANT-C", "MOS-000001", "L5", 150), I("PLANT-C", "MOS-000002", "L6", 40),
                   I("PLANT-C", "DIO-000001", "L7", 1000), I("PLANT-C", "DIO-000002", "L8", 30),
                   I("PLANT-C", "CBL-000001", "L9", 500.0)],
     "po": [PO("PO00000001", "PLANT-C", "CAP-000001", 300, "2026-11-15"), PO("PO00000002", "PLANT-C", "CAP-000001", 700, "2026-11-18"),
            PO("PO00000003", "PLANT-C", "CAP-000001", 50, "2026-11-01", st="Closed")],
     "review": [], "issues": [], "excluded": {},
    }
    return Engine(data)

def test_explode_multilevel_scrap_and_rounding(eng):
    r = eng.explode("FGD010-01", 100, D)
    rm = r["rm"]
    assert rm["CAP-000001"]["qty"] == 612          # 100×2×3×1.02
    assert rm["MOS-000001"]["qty"] == 200
    assert rm["CBL-000001"]["qty"] == pytest.approx(165.0) and rm["CBL-000001"]["uom"] == "M"
    assert "CAP-000003" not in rm                  # 未生效
    assert rm["CAP-000001"]["positions"][0]["parent"] == "SA100001"

def test_where_used(eng):
    r = eng.where_used("CAP-000001")
    assert set(r["finished_goods"]) == {"FGD010-01", "FGA010-01"}
    assert {p["Parent"] for p in r["direct_parents"]} == {"SA100001", "SA100002"}

def test_available_and_inbound(eng):
    a = eng.available("CAP-000001", "PLANT-C", D)
    assert a["qty"] == 1000 and a["excluded"].get("qc_not_pass") == 1
    b = eng.inbound("CAP-000001", "PLANT-C", D)
    assert b["qty"] == 300                         # 11/18+4 > 11/20 不計；Closed 不計
    assert [p["PONo"] for p in b["late_pos"]] == ["PO00000002"]

def test_shelf_life(eng):
    eng.data["attrs"]["CAP-000002"]["ShelfLifeMonths"] = 6.0
    eng.data["inventory"].append(I("PLANT-C", "CAP-000002", "L10", 1000, dc="2601"))   # 2025-12-29 +6 月 = 2026-06-29 已過期
    eng.data["inventory"].append(I("PLANT-C", "CAP-000002", "L11", 7, dc=None))
    a = eng.available("CAP-000002", "PLANT-C", D)
    assert a["qty"] == 120 and a["excluded"].get("expired") == 1 and a["excluded"].get("datecode_invalid") == 1

def test_alternates_rules(eng):
    r = eng.alternates("CAP-000001", "SA100001", "FGD010-01", D)
    assert [u["Alt"] for u in r["usable"]] == ["CAP-000002"]
    rej = {x["AltRecID"]: " ".join(x["reasons"]) for x in r["rejected"]}
    assert "alt_lifecycle" in rej["ALT0000003"] and "alt_attribute_compatibility" in rej["ALT0000004"] and "alt_approval" in rej["ALT0000005"] and "alt_scope" in rej["ALT0000008"]
    assert all(u["Alt"] != "CAP-000003" for u in r["usable"])   # alt_no_chaining 不串接

def test_bundle(eng):
    r = eng.alternates("MOS-000001", "SA100001", "FGD010-01", D)
    u = r["usable"][0]
    assert u["Alt"] == "MOS-000002" and u["BundleID"] == "BND-0001"
    assert {m["Primary"] for m in u["bundle_members"]} == {"MOS-000001", "DIO-000001"}

def test_shortage_basic(eng):
    r = eng.shortage("FGD010-01", 200, "PLANT-C", D)   # CAP 需 1224, 有 1000
    s = {x["PartNo"]: x["short"] for x in r["shortages"]}
    assert s["CAP-000001"] == 224 and s["MOS-000001"] == 250
    assert r["max_buildable"] == 75                  # MOS 150/2 = 75

def test_shortage_with_po_and_alt(eng):
    r = eng.shortage("FGD010-01", 200, "PLANT-C", D, include_po=True, include_alt=True)
    s = {x["PartNo"]: x["short"] for x in r["shortages"]}
    assert "CAP-000001" not in s                     # 1000+300+120 替代 ≥ 1224
    # MOS 位置 MixAllowed=N + bundle：每片 SA 要整片換，MOS-2 40 片、DIO-2 30 片 → bundle 最多 30 片 SA
    assert r["max_buildable"] == 90                  # (150 + 30) / 2

def test_shortage_errors(eng):
    with pytest.raises(ValueError):
        eng.shortage("FGX999-01", 1, "PLANT-C", D)

def test_shelf_life_unknown_not_available(eng):
    eng.data["attrs"]["CAP-000002"]["ShelfLifeMonths"] = None   # 屬性存在但值無效 → 無法確認效期
    a = eng.available("CAP-000002", "PLANT-C", D)
    assert a["qty"] == 0 and a["excluded"].get("shelf_life_invalid") == 1

def test_alternate_rohs_must_be_y_even_if_primary_missing(eng):
    eng.data["attrs"]["MOS-000002"] = {}   # 主料、替代料都沒有 RoHS
    r = eng.alternates("MOS-000001", "SA100001", "FGD010-01", D)
    assert not r["usable"] and any("RoHS" in " ".join(x["reasons"]) for x in r["rejected"])
