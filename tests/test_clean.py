"""資料清理（用 data/sample 的自產資料 + 小型手做 CSV）。"""
import csv, json, os
import pytest
from partpilot.clean import run_clean, load_clean
from tests.conftest import RAW

HDR = {
 "parts_master.csv": "PartNo,PartDesc,PartType,Commodity,ProductLine,UoM,Lifecycle,MakeBuy,SupplierName,MfrPartNo,LeadTimeDays,MOQ,UnitCostUSD",
 "part_attributes.csv": "PartNo,AttrCode,AttrValue,Unit",
 "bom.csv": "ParentPartNo,BOMRev,LineNo,ChildPartNo,QtyPer,UoM,ScrapRate,RefDes,EffectiveFrom,EffectiveTo",
 "alternates.csv": "AltRecID,PrimaryPartNo,AltPartNo,Scope,ScopeValue,Priority,QtyRatio,Bidirectional,MixAllowed,BundleID,ApprovalStatus,ValidFrom,ValidTo,Remark",
 "inventory.csv": "SnapshotDate,Plant,PartNo,LotNo,DateCode,QCStatus,QtyOnHand,QtyAllocated,UoM,StorageLoc",
 "open_po.csv": "PONo,POLine,Plant,PartNo,SupplierName,OrderQty,ReceivedQty,UoM,UnitPriceUSD,OrderDate,ETA,Status",
}

def write(d, rows):
    for f, h in HDR.items():
        with open(os.path.join(d, f), "w", encoding="utf-8") as fh:
            fh.write(h + "\n" + "".join(r + "\n" for r in rows.get(f, [])))

@pytest.fixture
def small(tmp_path):
    raw = tmp_path / "raw"; raw.mkdir(); out = tmp_path / "out"
    write(str(raw), {
     "parts_master.csv": [
      "FGD010-01,10kW DC,FG,,AC,PCS,Active,M,,,7,1,100",          # ProductLine 與料號不符 → DC
      "SA100001,板,SA,,DC,PCS,Active,M,,,7,1,10",
      " cap–100001 ,電容,RM,CAP,,pcs,active,B,供應商-01,X1,14,100,0.5",  # 標準化
      "CAP-100002,電容2,RM,CAP,,EA,量產,B,供應商-02,X2,14,100,0.6",    # Lifecycle 無效
      "RES-1O0003,壞料號,RM,RES,,PCS,Active,B,供應商-03,X3,14,100,0.1", # 無效料號（字母 O）
      "IND-100004,電感,RM,IND,,PCS,Active,B,供應商-04,X4,14,100,1",
      "IND-100004,電感,RM,IND,,PCS,Active,B,供應商-05,X4,14,100,1",   # 主檔衝突 → 待人工確認
     ],
     "part_attributes.csv": [
      "CAP-100001,Capacitance,100nF,", "CAP-100001,RatedVoltage,0.8kV,", "CAP-100001,ratedvoltage,800,V",
      "CAP-100002,Capacitance,0.1,uF", "CAP-100002,TempMax,105,℃", "CAP-100002,Tolerance,±5%,",
      "CAP-100002,RatedVoltage,630,V", "CAP-100002,RatedVoltage,450,V",   # 衝突 → 待人工確認、缺值
      "CAP-100002,Package,TBD,",
     ],
     "bom.csv": [
      "FGD010-01,A,10,SA100001,1,PCS,,,2024-01-01,",
      "SA100001,A,10,CAP-100001,2,PCS,2%,C1,2024/01/01,",
      "SA100001,A,10,CAP-100001,2,PCS,2%,C1,2024/01/01,",        # 完全重複
      "SA100001,A,20,CAP-100002,0.004,KPCS,0.5,C2,20240101,",    # KPCS → 4 PCS；損耗 0.5 超範圍 → 0
      "SA100001,A,30,CAP-100002,1,PCS,,C3,01/01/2024,",           # 日期格式無效
      "SA100001,A,40,IND-100004,1,PCS,,L1,2024-01-01,",           # 主檔衝突料號 → 暫不使用
     ],
     "alternates.csv": [
      "ALT0000001,CAP-100001,CAP-100002,GLOBAL,,1,1,N,Y,,Pending,2025-01-01,,",
      "ALT0000002,CAP-100001,CAP-100002,GLOBAL,,1,1,n, ,,APPROVED ,2025-01-01,2024-01-01,",  # 日期顛倒 → 無效
      "ALT0000003,CAP-100002,CAP-100001,GLOBAL,,1,1,N,Y,,核准中,2025-01-01,,",                 # 無效狀態
     ],
     "inventory.csv": [
      "2026-11-06,plantc,CAP-100001,L1,2620, 合格,1000,100,PCS,A",
      "2026-11-06,PLANT-C,CAP-100001,L2,2620,OK,1000,0,PCS,A",       # QCStatus 無效
      "2026-11-06,XX-YY,CAP-100001,L3,2620,合格,1000,0,PCS,A",     # 工廠無效
      "2026-11-06,PLANT-C,CAP-100002,L4,2620,合格,2,0,KPCS,A",       # 2000 PCS
      "2026-11-06,PLANT-C,CAP-100002,L5,2620,合格,10,20,PCS,A",      # 保留>在庫 無效
     ],
     "open_po.csv": [
      "PO00000001,1,PLANT-C,CAP-100001,供應商-01,500,100,PCS,0.5,2026-09-01,2026-11-10,open ",
      "PO00000002,1,PLANT-C,CAP-100001,供應商-01,500,0,PCS,0.5,2026-09-01,2026-08-01,Open",  # ETA 早於下單
      "PO00000003,1,PLANT-C,CAP-100001,供應商-01,500,0,PCS,0.5,2026-09-01,2026-11-10,Hold",  # 狀態無效
     ],
    })
    summary = run_clean(str(raw), str(out))
    return summary, load_clean(str(out)), out

def test_parts_normalized_and_invalid(small):
    s, d, out = small
    assert "CAP-100001" in d["parts"]
    assert d["parts"]["CAP-100001"]["UoM"] == "PCS"
    assert d["parts"]["CAP-100001"]["Lifecycle"] == "Active"
    assert d["parts"]["CAP-100002"]["Lifecycle"] is None
    assert d["parts"]["FGD010-01"]["ProductLine"] == "DC"
    assert not any(k.startswith("RES-") for k in d["parts"])
    assert "IND-100004" not in d["parts"]
    assert any(r["Kind"] == "part_conflict" and "IND-100004" in r["Key"] for r in d["review"])

def test_attrs_units_and_conflict(small):
    s, d, out = small
    a1 = d["attrs"]["CAP-100001"]
    assert a1["Capacitance"] == pytest.approx(0.1)
    assert a1["RatedVoltage"] == pytest.approx(800)
    a2 = d["attrs"]["CAP-100002"]
    assert a2["Capacitance"] == pytest.approx(0.1)
    assert a2["TempMax"] == pytest.approx(105)
    assert a2["Tolerance"] == pytest.approx(5)
    assert "RatedVoltage" not in a2 and "Package" not in a2
    assert any(r["Kind"] == "attr_conflict" for r in d["review"])

def test_bom_rules(small):
    s, d, out = small
    lines = [(b["Parent"], b["LineNo"]) for b in d["bom"]]
    assert lines.count(("SA100001", 10)) == 1
    l20 = [b for b in d["bom"] if b["Parent"] == "SA100001" and b["LineNo"] == 20][0]
    assert l20["QtyPer"] == pytest.approx(4) and l20["Scrap"] == 0
    l10 = [b for b in d["bom"] if b["Parent"] == "SA100001" and b["LineNo"] == 10][0]
    assert l10["Scrap"] == pytest.approx(0.02) and l10["EffFrom"] == "2024-01-01"
    assert ("SA100001", 30) not in lines and ("SA100001", 40) not in lines

def test_alternates_rules(small):
    s, d, out = small
    ids = {a["AltRecID"]: a for a in d["alternates"]}
    assert ids["ALT0000001"]["Status"] == "Pending"
    assert "ALT0000002" not in ids and "ALT0000003" not in ids
    assert any(r["Kind"] == "alt_pending" for r in d["review"])

def test_inventory_and_po(small):
    s, d, out = small
    inv = {i["LotNo"]: i for i in d["inventory"]}
    assert inv["L1"]["Plant"] == "PLANT-C" and inv["L1"]["Qty"] == 900 and inv["L1"]["QCStatus"] == "合格"
    assert inv.get("L2") is None or inv["L2"]["QCStatus"] is None
    assert "L3" not in inv and "L5" not in inv
    assert inv["L4"]["Qty"] == 2000
    po = {p["PONo"]: p for p in d["po"]}
    assert po["PO00000001"]["Status"] == "Open" and po["PO00000001"]["OpenQty"] == 400
    assert "PO00000002" not in po
    assert "PO00000003" not in po or po["PO00000003"]["Status"] not in ("Open",)

def test_report_and_approval(small, tmp_path):
    s, d, out = small
    rep = open(os.path.join(str(out), "cleaning_report.md"), encoding="utf-8").read()
    assert "|" in rep and len(d["issues"]) >= 8 and all(i["Count"] > 0 for i in d["issues"])
    pend = [r for r in d["review"] if r["Kind"] == "alt_pending"][0]
    out2 = tmp_path / "out2"
    run_clean(str(tmp_path / "raw"), str(out2), {pend["ReviewID"]: {"decision": "approve", "by": "engineer_user", "at": "x"}})
    d2 = load_clean(str(out2))
    assert [a for a in d2["alternates"] if a["AltRecID"] == "ALT0000001"][0]["Status"] == "Approved"
    # 摘要不含料件內容
    assert "CAP-" not in json.dumps(s, ensure_ascii=False)

def test_real_data_runs(tmp_path):
    s = run_clean(RAW, str(tmp_path))
    d = load_clean(str(tmp_path))
    assert s["counts"]["parts"] > 100 and s["counts"]["bom"] > 50
    assert all(p["UoM"] in ("PCS", "M") for p in d["parts"].values())
    assert all(i["Plant"] in ("PLANT-C", "PLANT-D", "PLANT-A", "PLANT-B") for i in d["inventory"])

def test_bom_normalized_duplicates_not_conflict(tmp_path):
    raw = tmp_path / "raw"; raw.mkdir()
    write(str(raw), {
     "parts_master.csv": ["FGD010-01,FG,FG,,DC,PCS,Active,M,,,7,1,100", "SA100001,板,SA,,DC,PCS,Active,M,,,7,1,10",
                          "CAP-100001,電容,RM,CAP,,PCS,Active,B,供應商-01,X1,14,100,0.5",
                          "CAP-100009,電容,RM,CAP,,PCS,Active,B,供應商-01,X1,14,100,0.5"],
     "part_attributes.csv": ["CAP-100009,ShelfLifeMonths,TBD,", "CAP-100001,ShelfLifeMonths,12,", "CAP-100001,ShelfLifeMonths,24,"],
     "bom.csv": ["FGD010-01,A,10,SA100001,1,PCS,,,2024-01-01,",
                 "SA100001,A,10,CAP-100001,2,PCS,2%,C1,2024-01-01,",
                 "SA100001,A,10,cap-100001,2,PCS,0.02,C1,20240101,"],
    })
    run_clean(str(raw), str(tmp_path / "out"))
    d = load_clean(str(tmp_path / "out"))
    assert [(b["Parent"], b["LineNo"]) for b in d["bom"]].count(("SA100001", 10)) == 1
    assert not any(r["Kind"] == "bom_conflict" for r in d["review"])
    assert "ShelfLifeMonths" in d["attrs"].get("CAP-100009", {}) and d["attrs"]["CAP-100009"]["ShelfLifeMonths"] is None
    assert d["attrs"]["CAP-100001"]["ShelfLifeMonths"] is None   # 衝突待確認 → 無法確認效期

def test_alternates_differ_only_in_id_are_duplicates(tmp_path):
    raw = tmp_path / "raw"; raw.mkdir()
    write(str(raw), {
     "parts_master.csv": ["CAP-100001,電容,RM,CAP,,PCS,Active,B,供應商-01,X1,14,100,0.5", "CAP-100002,電容,RM,CAP,,PCS,Active,B,供應商-01,X1,14,100,0.5"],
     "alternates.csv": ["ALT0000001,CAP-100001,CAP-100002,GLOBAL,,1,1,N,Y,,Approved,2025-01-01,,",
                        "ALT0000002,CAP-100001,CAP-100002,GLOBAL,,1,1,N,Y,,Approved,2025-01-01,,"],
    })
    run_clean(str(raw), str(tmp_path / "out"))
    d = load_clean(str(tmp_path / "out"))
    assert len(d["alternates"]) == 1 and not any(r["Kind"] == "alt_conflict" for r in d["review"])
