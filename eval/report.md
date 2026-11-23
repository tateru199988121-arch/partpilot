# 模型評測報告

| 模型 | 查詢數 | 正確率 | 越權率 | 平均延遲秒 | 總成本 USD | 平均輸入 tokens | 平均輸出 tokens |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| demo | 10 | 100.00% | 0.00% | 0.0040 | 0.0000 | 0.00 | 0.00 |

## 每筆查詢對錯矩陣

| 案例名稱 | 使用者 | demo |
| --- | --- | --- |
| bom_expansion | scm_lead | 對 |
| where_used | scm_lead | 對 |
| inventory | scm_lead | 對 |
| inbound | scm_lead | 對 |
| alternate_approval | scm_lead | 對 |
| shortage | line_dc | 對 |
| shortage_with_supply | line_dc | 對 |
| plant_scope_denied | plant_a | 對 |
| product_line_denied | line_dc | 對 |
| admin_query_denied | sys_admin | 對 |

## 路由建議

建議路由至 `demo`：正確率最高。本次正確率為 100.00%，總成本為 0.00000000 USD。建議僅依本次所選評測查詢與模型比較結果。

成本採 Agent 回傳的估算值；定價可用 `PARTPILOT_PRICE_<MODEL>=in,out`（每百萬 tokens 的 USD）設定，未設定時使用系統預設定價。輸入 tokens 包含一般輸入、快取讀取與快取建立。
