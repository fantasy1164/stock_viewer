# Stock Viewer — 個股觀察清單系統

台股 / 美股的長期趨勢觀察工具。前端部署於 **GitHub Pages**、後端部署於 **Render**,全免費運行。

> **定位:條件整理,不是投資建議。**
> 本工具把「趨勢方向 × 相對價位」等條件整理成一眼可讀的狀態(累積/持有/觀望/減碼),
> 幫助長期投資者做決策參考;它不產生買賣訊號、不做預測。實際決定請自行判斷。

---

## 功能總覽

### 📈 圖表分析
- **趨勢線 × 四象限**:對 15 年日收盤做對數線性回歸,依「趨勢向上/向下 × 價格高於/低於趨勢線」把個股歸入 **累積 / 持有 / 觀望 / 減碼** 四象限,附白話說明
- 顯示期間 1 月 ~ 5 年切換;季線 / 年線 / 十年線(2400 日均線)可獨立開關
- **本益比評估**:個股 P/E 對照大盤平均(證交所全市場)與自訂門檻滑桿
- **資金面**:近兩週三大法人買賣超(外資/投信/自營商)與融資融券變化,抓取時有進度條回饋

### 📋 清單管理
- 分頁式觀察清單:**熱門股(系統排行)→ 台股 → 美股 → 自訂清單 A / B**,可新增、改名、刪除、排序
- **熱門股每檔附「＋」收藏鈕**:瀏覽排行時看到有興趣的,一鍵收進自己的清單(複製語意,排行保持完整)
- 清單存於**瀏覽器 localStorage**:免帳號、免資料庫,重新整理與重開瀏覽器都在
- 清單徽章:各股的四象限狀態以顏色點顯示,背景漸進載入

### 📱 行動裝置體驗
- 手機(直式或橫屏)自動切換為**抽屜式清單**:圖表滿版,左下角「☰ 清單」開啟,選股自動收合
- 排序不靠拖曳手勢(行動瀏覽器上不可靠),改用「**↕ 排序**」編輯模式:分頁 ◀ ▶、個股 ▲ ▼ ⇄ 按鈕操作
- PC 維持滑鼠拖曳排序;兩種互動模型互不干擾
- 排序鈕上顯示前端版本號(目前 `v3.2`),快取除錯一眼定位

---

## 系統架構

```
┌─────────────────┐         ┌──────────────────┐         ┌───────────────┐
│  GitHub Pages    │  fetch  │  Render (免費)     │ requests │  資料來源       │
│  frontend/       │ ──────▶ │  backend/app.py   │ ───────▶ │  台灣證交所 TWSE │
│  index.html      │  CORS   │  Flask + gunicorn │          │  yfinance      │
│  (含 localStorage │         │  SQLite 快取       │          │  (Yahoo)       │
│   清單儲存)        │         │  (ephemeral)      │          └───────────────┘
└─────────────────┘         └──────────────────┘
```

### 設計原則:後端無狀態

後端**只做資料抓取與快取**,不儲存任何使用者資料。使用者的清單/分頁全部存在瀏覽器 localStorage。

這個選擇的原因:
- Render 免費方案的磁碟是 **ephemeral**(休眠/重部署即清空),不適合存使用者資料
- 免費 Postgres 有 30 天期限,持久磁碟要付費
- 個人工具不需要跨裝置同步時,localStorage 是零成本、零維護的正解
- 後端的 SQLite(`cache.db`)只當快取:被清掉沒關係,價格/法人/本益比資料會自動重建

### 資料來源策略

| 資料 | 來源 | 備註 |
| --- | --- | --- |
| 股價(台股/美股) | yfinance(Yahoo 圖表 API) | 抓 15 年日收盤,SQLite 快取、只補缺 |
| 三大法人買賣超 | 證交所 T86 開放資料 | 全市場日報快取,一次抓、查任何個股都命中 |
| 融資融券 | 證交所 MARGN 開放資料 | 同上 |
| 台股本益比/殖利率/淨值比 | 證交所 **BWIBBU** 官方報表 | 見下方「為什麼不用 yfinance .info」 |
| 52 週高低 | 自有收盤價快取自算 | 零外部依賴 |
| 美股基本面 | yfinance `.info` | 美股上較可靠 |
| 熱門股排行 | 證交所成交量排行 + 美股活躍榜 | |

**為什麼台股基本面不用 yfinance `.info`**:Yahoo 對雲端資料中心 IP(Render 即是)的 quoteSummary 端點常限流,`.info` 不是立刻失敗、而是卡十幾秒才逾時 —— 多檔預載時會把單一 worker 拖到 gunicorn 逾時被砍,造成全站 `Failed to fetch`。因此台股完全繞過 `.info`,改用證交所官方 BWIBBU(不限流、且已整表快取)。

---

## 專案結構

```
.
├── frontend/
│   └── index.html          # 單檔前端:UI + 圖表(ECharts)+ localStorage Store
├── backend/
│   ├── app.py              # Flask API(無狀態,6 個路由)
│   ├── requirements.txt
│   └── .gitignore
├── render.yaml             # Render Blueprint(Infrastructure as Code)
├── .github/workflows/
│   └── pages.yml           # push 到 main 自動發佈 frontend/ 到 GitHub Pages
└── README.md
```

## API 端點

| 路由 | 方法 | 說明 |
| --- | --- | --- |
| `/` | GET | 健康檢查 `{"ok":true}`(可供 UptimeRobot 保溫) |
| `/api/search?q=` | GET | 依代號/名稱搜尋(台股上市清單 + 美股) |
| `/api/hot` | GET | 熱門股排行(台股成交量 + 美股活躍),純資料不落地 |
| `/api/stock/<symbol>` | GET | 股價序列 + 基本面(`?years=15` 指定年數,`&force=1` 強制重抓) |
| `/api/stock/<symbol>/funds` | GET | 近兩週三大法人 + 融資融券 |
| `/api/market` | GET | 大盤平均本益比 |

## 環境變數(後端 / Render)

| 變數 | 說明 | 範例 |
| --- | --- | --- |
| `CORS_ORIGINS` | 允許的前端來源(逗號分隔;填網域根,不含子路徑、結尾不加斜線) | `https://yourname.github.io` |
| `PORT` | Render 自動注入;本機開發預設 5000 | — |

---

## 部署

### 後端 → Render
1. Fork / push 本 repo 到 GitHub
2. Render → **New → Blueprint** → 連接 repo(自動讀取 `render.yaml`)
3. 部署完成後,開 `https://<你的服務>.onrender.com/` 應看到 `{"ok":true}`
4. 到服務的 **Environment** 把 `CORS_ORIGINS` 從 `*` 改成你的 GitHub Pages 網域

### 前端 → GitHub Pages
1. 把 `frontend/index.html` 內的 `API_BASE` 改成你的 Render 網址
2. GitHub repo → **Settings → Pages** → Source 選 **GitHub Actions**
3. push 到 `main`(或 Actions 頁手動 **Run workflow**),`pages.yml` 會自動發佈 `frontend/`

### 已知營運特性
- **冷啟動**:Render 免費方案閒置 15 分鐘休眠,喚醒需 30–60 秒;個人使用可接受,介意者可用 UptimeRobot 每 5 分鐘 ping 健康檢查端點保溫(會佔用大部分每月 750 小時免費額度)
- **部署失敗排查**:Pages 部署遇 `Multiple artifacts named "github-pages"` 或 `Deployment failed, try again later` 多為 GitHub 端狀況 —— 不要按 Re-run,改用 **Run workflow** 觸發全新 run,並查 [githubstatus.com](https://www.githubstatus.com/)

---

## 本機開發

```bash
# 後端
cd backend
python -m venv .venv && . .venv/Scripts/activate    # Windows;macOS/Linux 用 source .venv/bin/activate
pip install -r requirements.txt
python app.py                                        # http://127.0.0.1:5000

# 前端(另開終端)
cd frontend && python -m http.server 8080            # http://localhost:8080
```

前端會自動判斷:`localhost` / `127.0.0.1` 走本機後端 5000 埠,其餘走線上 Render(`index.html` 內 `API_BASE`)。

## 清單備份 / 還原

清單只存在目前瀏覽器。換裝置或清瀏覽器資料前,可在瀏覽器 Console 備份:

```js
copy(Store.exportJSON())        // 匯出(已複製到剪貼簿,存成文字檔)
Store.importJSON(`貼上備份內容`); location.reload()   // 還原
```

---

## 免責聲明

本工具顯示的是真實歷史股價與公開市場資料的「條件整理」,不構成任何投資建議、買賣推薦或未來表現之預測。趨勢線是過去走勢的統計總結;「便宜」不代表值得買進(基本面可能已惡化)。所有投資決定請自行判斷並自負風險,建議以定期定額(DCA)作為對照基準。
