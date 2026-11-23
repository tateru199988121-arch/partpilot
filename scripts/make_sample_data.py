"""以固定種子建立完全虛構的示範資料；不讀取任何外部資料。"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path
import random

ROOT = Path(__file__).resolve().parent.parent
SEED = 420
HEADERS = {
    "parts_master.csv": "PartNo,PartDesc,PartType,Commodity,ProductLine,UoM,Lifecycle,MakeBuy,SupplierName,MfrPartNo,LeadTimeDays,MOQ,UnitCostUSD",
    "bom.csv": "ParentPartNo,BOMRev,LineNo,ChildPartNo,QtyPer,UoM,ScrapRate,RefDes,EffectiveFrom,EffectiveTo",
    "part_attributes.csv": "PartNo,AttrCode,AttrValue,Unit",
    "inventory.csv": "SnapshotDate,Plant,PartNo,LotNo,DateCode,QCStatus,QtyOnHand,QtyAllocated,UoM,StorageLoc",
    "open_po.csv": "PONo,POLine,Plant,PartNo,SupplierName,OrderQty,ReceivedQty,UoM,UnitPriceUSD,OrderDate,ETA,Status",
    "alternates.csv": "AltRecID,PrimaryPartNo,AltPartNo,Scope,ScopeValue,Priority,QtyRatio,Bidirectional,MixAllowed,BundleID,ApprovalStatus,ValidFrom,ValidTo,Remark",
}


def make_sample_data(output_dir=ROOT / "data/sample"):
    """每檔 50–300 列；回傳列數。相同種子與輸出內容不受日期影響。"""
    rng = random.Random(SEED)
    rows = {name: [] for name in HEADERS}
    parts, bom, attrs, inventory, po, alts = (rows[name] for name in HEADERS)
    fgs = ["FGD101-01", "FGD102-01", "FGA101-01", "FGA102-01"]
    assemblies = [f"SA100{i:03}" for i in range(101, 113)]
    commodities = ("CAP", "RES", "MOS", "CON", "IND")
    materials = [f"{code}-{i:06}" for code in commodities for i in range(101, 121)]
    for i, part in enumerate(fgs + assemblies + materials):
        kind = "FG" if part in fgs else "SA" if part in assemblies else "RM"
        line = ("AC" if part.startswith("FGA") or part in assemblies[4:8] else "DC") if kind != "RM" else ""
        code = part[:3] if kind == "RM" else ""
        parts.append([part, f"Sample {kind} {i + 1:03}", kind, code, line, "PCS", "Active",
                      "B" if kind == "RM" else "M", f"Supplier-{i % 8 + 1:02}",
                      f"MP-{i + 1:05}", rng.choice([14, 21, 35]), 10, round(rng.uniform(.2, 8), 2)])
    # 兩筆全形料號，清理後仍為原定的獨立料號。
    for index in (16, 17):
        parts[index][0] = " " + "".join(chr(ord(c) + 0xFEE0) for c in parts[index][0]) + " "
    for index in (114, 115):
        parts[index][-1] = ""  # 缺值兩筆，單價設為 null。

    for part in materials:
        code = part[:3]
        values = {
            "CAP": [("Capacitance", "10", "uF"), ("RatedVoltage", "50", "V"),
                    ("Package", "0603", ""), ("RoHS", "Y", ""), ("SafetyCert", "UL", ""), ("Tolerance", "5", "%")],
            "RES": [("Resistance", "10", "kΩ"), ("PowerRating", ".25", "W"),
                    ("Tolerance", "1", "%"), ("Package", "0603", ""), ("RoHS", "Y", "")],
            "MOS": [("RatedVoltage", "100", "V"), ("RatedCurrent", "10", "A"), ("RoHS", "Y", "")],
        }.get(code, [])
        attrs.extend([part, *value] for value in values)
    attrs.extend([["IND-000119", "Inductance", "", "uH"], ["IND-000120", "Inductance", "TBD", "uH"]])

    dates = ("2026-01-01", "2026/01/01", "20260101")
    for i, fg in enumerate(fgs):
        for j in range(2):
            bom.append([fg, "A", (j + 1) * 10, assemblies[i * 2 + j], 1, "PCS", "0", "", dates[i % 3], ""])
    for i, assembly in enumerate(assemblies[:10]):
        for j, code in enumerate(commodities):
            bom.append([assembly, "A", (j + 1) * 10, f"{code}-{101 + i:06}",
                        (2, 3, 1, 2, 1)[j], "PCS", "2%" if code == "CAP" else "0",
                        f"{code[0]}1", dates[i % 3], ""])
    # 兩筆循環邊：隔離於可查詢的正常產品之外，清理器須移入待確認。
    bom.extend([[assemblies[10], "A", 10, assemblies[11], 1, "PCS", "0", "", dates[0], ""],
                [assemblies[11], "A", 10, assemblies[10], 1, "PCS", "0", "", dates[1], ""]])
    for i in range(2):
        bom.append([assemblies[i], "A", 90, f"CAP-{119 + i:06}", 1, "PCS", "0", "C9", "2026-02-30", ""])

    for i, part in enumerate(materials):
        qty = 160 if i == 0 else 60 if i == 10 else rng.randint(80, 450)
        allocated = 20 if i == 0 else 0
        inventory.append([("2026-11-06", "2026/11/06", "20261106")[i % 3], "PLANT-A", part,
                          f"LOT-{i + 1:04}", "2640", "合格", qty, allocated, "PCS", "A-01"])
        po.append([f"PO{i + 1:08}", 1, "PLANT-A", part, f"Supplier-{i % 8 + 1:02}",
                   100, 10, "PCS", "1.25", "2026/10/01", "2026-11-10" if i % 2 == 0 else "20261119", "Open"])
    # 兩筆待驗、兩筆缺 ETA：可查到，但不得算入可用供給。
    for i in range(2):
        inventory.append(["2026-11-06", "PLANT-B", materials[i], f"QC-{i}", "2640", "待驗", 900, 0, "PCS", "B-01"])
        po.append([f"PO{201 + i:08}", 1, "PLANT-B", materials[i], "Supplier-01", 500, 0,
                   "PCS", "1.25", "2026-10-01", "", "Open"])

    for code in commodities:
        for i in range(10):
            alts.append([f"ALT{len(alts) + 1:07}", f"{code}-{101 + i:06}", f"{code}-{111 + i:06}",
                         "GLOBAL", "", 1, 1, "N", "Y", "", "Approved", "2026/01/01", "", "Synthetic sample"])
    for i in range(2):
        # 主替料相同的無效紀錄，以及須由 component 核准的有效 Pending 紀錄。
        alts.append([f"ALT{101 + i:07}", materials[i], materials[i], "GLOBAL", "", 1, 1, "N", "Y", "",
                     "Approved", "20260101", "", "Invalid self alternate"])
        alts.append([f"ALT{201 + i:07}", materials[i], f"CAP-{119 + i:06}", "PARENT", assemblies[i],
                     2, 1, "N", "Y", "", "Pending", "2026-01-01", "", "Review required"])

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, records in rows.items():
        records.extend([list(records[0]), list(records[1])])  # 每檔兩筆完整重複列。
        with (output_dir / name).open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream, lineterminator="\n")
            writer.writerow(HEADERS[name].split(","))
            writer.writerows(records)
    return {name: len(records) for name, records in rows.items()}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data/sample")
    for name, count in make_sample_data(parser.parse_args().output_dir).items():
        print(f"{name}: {count} rows")
