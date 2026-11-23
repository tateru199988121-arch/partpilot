"""CLI、網頁與離線回答共用的摘要；只使用已授權的工具結果。"""
import json

from .engine import ALT_RULES


def _reason(text):
    key, _, detail = text.partition(" ")
    return ALT_RULES.get(key, key) + ("：" + detail if detail else "")


def summary_text(answer, fallback="已完成授權範圍內的查詢。"):
    """讓模型摘要與離線摘要共用相同呈現，JSON 由介面另行顯示。"""
    text = answer.split("原始 JSON：", 1)[0].split("```", 1)[0].split("\n{", 1)[0].strip()
    if text.startswith(("{", "[")):
        text = ""
    lines = [line.strip() for line in (text or fallback).splitlines() if line.strip()]
    if len(lines) > 5:
        lines = lines[:4] + [" ".join(lines[4:])]
    if len(lines) == 1:
        lines.append("詳細資料請查看原始 JSON。")
    return "\n".join(lines)


def summarize(name, result, fallback=""):
    """產生二至五行中文摘要，完整明細由原始 JSON 保留。"""
    if result.get("error"):
        return "查詢拒絕：" + str(result.get("message", result["error"])) + "\n請依權限或輸入限制調整查詢。"
    part = result.get("part", result.get("primary", ""))
    lines = []
    if name == "shortage" and "max_buildable" in result:
        lines.append(f"{result.get('plant')} 做 {result.get('qty')} 台 {part}（{result.get('prod_date')}）：最多可做 {result['max_buildable']} 台。")
        shortages = result.get("shortages", [])
        lines.append(f"缺 {len(shortages)} 種料。" if shortages else "本次需求無缺料。")
        if shortages:
            items = [f"{row['PartNo']} 缺 {row['short']} {row.get('uom', '')}".strip() for row in shortages]
            # 最多兩行缺料明細，完整料號與數量保留於 JSON。
            lines.extend("；".join(items[i:i + 3]) + "。" for i in range(0, min(len(items), 6), 3))
            if len(items) > 6:
                lines[-1] += f"另有 {len(items) - 6} 種料，詳見原始 JSON。"
        else:
            covered = result.get("covered_by_inbound_or_alt", [])
            lines.append(f"其中 {len(covered)} 種料由在途或替代料補足；採購單與替代用量詳見原始 JSON。")
    elif name == "explode_bom" and "rm" in result:
        lines = [f"{part} 的 BOM 展開（生產 {result.get('qty', 1)} 台），共 {len(result['rm'])} 種原料。",
                 "原料需求：" + "；".join(f"{no} {row['qty']} {row.get('uom', '')}" for no, row in list(result['rm'].items())[:5]) + "。"]
    elif name == "where_used" and "direct_parents" in result:
        parents = [row['Parent'] for row in result["direct_parents"]]
        lines = [f"{part} 使用上階：" + ("、".join(parents) or "無") + "。",
                 "受影響成品：" + ("、".join(result.get("finished_goods", [])) or "無") + "。"]
    elif name in {"get_inventory", "get_inbound"} and "qty" in result:
        label = "可用庫存" if name == "get_inventory" else "生產日前可用在途"
        lines = [f"{part}（{result.get('plant') or '全部授權工廠'}）{label}：{result['qty']} {result.get('uom', '')}".rstrip() + "。"]
        if name == "get_inbound":
            lines.append(f"如期可用採購單 {len(result.get('pos', []))} 筆；逾期採購單 {len(result.get('late_pos', []))} 筆，未計入可用量。")
        else:
            lines.append(f"共 {result.get('lots', 0)} 筆可用庫存，已依品檢狀態與保存期限篩選。")
    elif name == "get_alternates" and "usable" in result:
        usable, rejected = result["usable"], result.get("rejected", [])
        lines = [f"{part} 在上階 {result.get('parent')} 的替代料判斷：可用 {len(usable)} 筆，不可用 {len(rejected)} 筆。",
                 "可用替代料：" + ("、".join(row['Alt'] for row in usable) or "無") + "。"]
        if rejected:
            lines.append("不可用原因：" + "；".join(row['Alt'] + "：" + "、".join(_reason(reason) for reason in row.get('reasons', [])) for row in rejected[:2]) + "。")
        lines.append(f"適用成品：{result['fg']}；僅適用上述上階與成品範圍。" if result.get('fg') else "僅適用指定上階範圍；產品線規則須提供成品後確認。")
    elif name == "alternate_records" and "as_primary" in result:
        lines = [f"{part} 作為主料有 {len(result['as_primary'])} 筆替代紀錄，作為替代料有 {len(result.get('as_alt', []))} 筆。",
                 "尚未指定使用上階，不能視為可直接使用；須另查替代料適用性。"]
    else:
        # 其他 real 查詢保留模型摘要；提示詞要求只輸出中文摘要。
        lines = summary_text(fallback).splitlines()
    if len(lines) < 5 and result.get("rules"):
        rules = result["rules"]
        lines.append("依據：" + "、".join(rules[:3]) + ("等規則" if len(rules) > 3 else "") + "；排除原因與筆數詳見原始 JSON。")
    return "\n".join(lines[:5])


def render(summary, raw):
    return summary + "\n\n原始 JSON：\n" + json.dumps(raw, ensure_ascii=False, indent=2)
