# 觀察清單 Web 系統

前端(GitHub Pages) + 後端(Render)分離部署的個股觀察工具。

## 結構

```
.
├── frontend/          # 純靜態前端 → GitHub Pages
│   └── index.html
├── backend/           # Flask API → Render
│   ├── app.py
│   ├── requirements.txt
│   └── .gitignore
├── render.yaml        # Render 服務設定(Infrastructure as Code)
└── .github/workflows/pages.yml   # 自動發佈 frontend/ 到 Pages
```

## 架構重點

- 後端**無狀態**:只負責抓資料(股價、三大法人、融資融券、本益比、搜尋、熱門股)。
- 使用者的觀察清單/分頁存在**瀏覽器 localStorage**,不進資料庫。
- 後端的 SQLite(`cache.db`)只當快取;Render 免費方案休眠/重部署後會被清掉,但快取會自己重建。

## 後端環境變數

| 變數 | 說明 | 範例 |
| --- | --- | --- |
| `CORS_ORIGINS` | 允許的前端來源,逗號分隔 | `https://你的帳號.github.io` |
| `PORT` | Render 自動注入,本機開發預設 5000 | — |

## 本機開發

```bash
cd backend
python -m venv .venv && . .venv/Scripts/activate   # Windows
pip install -r requirements.txt
python app.py                                       # http://127.0.0.1:5000
```

前端本機測試:直接用瀏覽器開 `frontend/index.html`,或 `cd frontend && python -m http.server 8080`。
前端會自動判斷 localhost 走本機後端、線上走 Render。
