# PartPilot 5 分鐘試用指南

以下料號都存在於 `data/sample/` 的範例資料。

## 1. 啟動（約 1 分鐘）

選一種方式啟動：

- **本機 demo（不需 API key）**：

  ```bash
  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && bash scripts/demo.sh
  ```

  `scripts/demo.sh` 會依序產生範例資料、建立 13 個示範帳號（沒有設定 `PARTPILOT_INIT_PASSWORD` 時，首次啟動會在終端顯示隨機密碼）、以 `sys_admin` 執行資料清理，再以 demo 模式啟動網頁 `http://127.0.0.1:18765`。

- **Docker**：執行 `docker compose up --build`，打開 `http://127.0.0.1:18765`。

## 2. 登入

先用產品線帳號 `line_dc` 登入（使用啟動時顯示的隨機密碼，或你設定的 `PARTPILOT_INIT_PASSWORD`）。這個帳號只能看 DC 產品線的料件，而且看不到供應商名稱和單價，最適合展示權限過濾。

再次啟動時設定 `PARTPILOT_PASSWORD` 為原密碼；既有帳號不會被重設。也可以用命令列問，未設定密碼環境變數時依提示輸入：

```bash
LLM_MODE=demo .venv/bin/python -m partpilot ask --user line_dc "SA100101 的 BOM"
```

## 3. 依序問這 5 個問題（demo 模式都能回答）

| # | 帳號 | 問題 | 應該看到 |
|---|---|---|---|
| 1 | `line_dc` | 「SA100101 的 BOM」 | 「BOM 展開（生產 1 台）」：5 顆原料（CAP-000101、RES-000101、MOS-000101、CON-000101、IND-000101），每顆有用量與零件位置；沒有供應商名稱、沒有單價。 |
| 2 | `line_dc` | 「2026-11-20 PLANT-A 做 100 台 FGD101-01 缺什麼料」 | 「缺料試算；最多可做 53 台」，列出 4 顆缺料（例如 CAP-000101 需要 204、可用 140、缺 64）。再問一次並在句尾加「，納入在途與替代料」，會變成最多可做 100 台：缺口由在途採購和替代料補上。 |
| 3 | `line_dc` | 「FGA101-01 的 BOM」 | **被拒絕**：FGA 開頭是 AC 產品線的成品，回覆「查詢拒絕：依權限規則不可查看此料件資料。」，不透露任何內容。 |
| 4 | `plant_c` | 「PLANT-A CAP-000101 庫存」 | **被拒絕**：廠區帳號只能看自己廠區（PLANT-C），回覆「查詢拒絕：依權限規則不可查看此工廠資料。」改用 `plant_a` 問同一句，會看到「可用庫存：140 PCS」。 |
| 5 | `engineer_passive` | 「CAP-000101 的替代料，上階 SA100101，成品 FGD101-01」 | 替代料判斷：CAP-000111 可用；CAP-000119 不可用，原因是「替代料：核准狀態：Pending 未核准」。 |

以上結果是用 `scripts/make_sample_data.py` 產生的範例資料、未指定日期時以資料基準日 2026-11-06 計算的。範例資料重新產生後，數字可能不同。

## 4. 加碼看後台（選做）

- 用 `engineer_passive` 執行 `.venv/bin/python -m partpilot review --user engineer_passive`，可以看到被動元件群組的待確認資料，並用 `--approve ID` 或 `--reject ID` 處理。
- 用 `sys_admin` 執行 `.venv/bin/python -m partpilot audit --user sys_admin`，可以看到剛才每次提問、工具呼叫與 token 用量的紀錄，包含被拒絕的那兩題。

## 注意

demo 模式不呼叫 LLM，只認得固定句型（BOM、用在哪裡、庫存、在途、替代料、做 N 台缺什麼料）。比較口語的問法、相對日期（例如「下週一」）和交期影響分析，需接真實 LLM（`LLM_MODE=real`）。
