"""
觀察清單 Web 系統 — 後端 (Flask + SQLite + yfinance)
=====================================================
在能連網的電腦上:
    pip install flask yfinance numpy
    python app.py
然後瀏覽器打開  http://127.0.0.1:5000

功能:
  - 網頁上切換個股,後端「即時」用 yfinance 抓近兩年真實股價(60 秒快取,對來源禮貌一點)。
  - 觀察清單(保留/刪除)存進 SQLite watchlist.db,重開仍在。
  - 台股輸入 4 碼數字(如 2453)會自動補 .TW;輸入字母(如 AAPL)則當美股。
"""
from __future__ import annotations
import os, sqlite3, datetime as dt, time, json, sys, difflib, csv, io, ssl, re
from urllib.request import urlopen, Request
from urllib.parse import urlencode
from flask import Flask, jsonify, request, abort
from flask_cors import CORS

BASE = os.path.dirname(os.path.abspath(__file__))
# 這個 SQLite 現在「只當快取」用(股價/法人/本益比),不再存任何使用者清單。
# 在 Render 免費方案上這個檔案會在休眠/重部署後被清掉,但快取本來就會自己重建,沒關係。
DB   = os.path.join(BASE, "cache.db")
YEARS = 2
app = Flask(__name__)

# ---------- CORS ----------
# 前端在 GitHub Pages、後端在 Render,屬於跨來源請求,必須放行前端網域。
# CORS_ORIGINS 用逗號分隔;預設 "*" 方便一開始測試,正式上線後改成你的 Pages 網址。
_origins = os.environ.get("CORS_ORIGINS", "*").strip()
if _origins == "*":
    CORS(app, resources={r"/api/*": {"origins": "*"}})
else:
    CORS(app, resources={r"/api/*": {"origins": [o.strip() for o in _origins.split(",") if o.strip()]}})

# ---------- SQLite ----------
def db():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row; return c

def init_db():
    """只建立『快取表』。使用者的清單/分頁已改由前端 localStorage 管理,
       後端完全無狀態,不再有 tabs / watchlist 資料表。"""
    with db() as c:
        # 日收盤價快取:歷史資料一旦入庫就不再重抓,只補最近缺的交易日。
        c.execute("CREATE TABLE IF NOT EXISTS prices("
                  "symbol TEXT, date TEXT, close REAL, PRIMARY KEY(symbol, date))")
        c.execute("CREATE INDEX IF NOT EXISTS idx_prices_sym ON prices(symbol)")
        # 個股 info(名稱/本益比等會變動的欄位)快取,附抓取時間戳,用短 TTL。
        c.execute("CREATE TABLE IF NOT EXISTS stock_info("
                  "symbol TEXT PRIMARY KEY, json TEXT, fetched_at REAL)")
        # 全市場資金面日報表快取(三大法人 T86 / 融資融券 MARGN 等)。
        # 一天一列、包含當天全市場所有個股,查任何一檔都能命中。歷史交易日定案後不再重抓。
        c.execute("CREATE TABLE IF NOT EXISTS market_reports("
                  "report_key TEXT, date TEXT, json TEXT, fetched_at REAL, "
                  "PRIMARY KEY(report_key, date))")


def yf_symbol(code: str) -> str:
    code = code.strip().upper()
    return code + ".TW" if code.isdigit() else code   # 4 碼數字 = 台股

def _yf_download(code: str, period: str):
    import yfinance as yf
    df = yf.download(yf_symbol(code), period=period, interval="1d",
                     auto_adjust=True, progress=False)
    if df is None or df.empty:
        return []
    out = []
    closes = df["Close"].dropna()
    for d, x in zip(closes.index, closes.to_numpy().ravel()):
        out.append((d.strftime("%Y-%m-%d"), round(float(x), 2)))
    return out

def _prices_from_db(code: str, years: int):
    cutoff = (dt.date.today() - dt.timedelta(days=int(years * 365.25) + 5)).strftime("%Y-%m-%d")
    with db() as c:
        rows = c.execute(
            "SELECT date, close FROM prices WHERE symbol=? AND date>=? ORDER BY date",
            (code.upper(), cutoff)).fetchall()
    return [(r["date"], r["close"]) for r in rows]

def _save_prices(code: str, rows):
    if not rows:
        return
    with db() as c:
        c.executemany("INSERT OR REPLACE INTO prices(symbol,date,close) VALUES(?,?,?)",
                      [(code.upper(), d, x) for d, x in rows])

def _latest_trading_day_guess():
    """粗略推估「最近應該有的交易日」:往前找到第一個非週末的日期。
       (不含國定假日判斷,但配合『資料只差幾天就補抓』的邏輯已足夠。)"""
    d = dt.date.today()
    while d.weekday() >= 5:      # 5=六, 6=日
        d -= dt.timedelta(days=1)
    return d.strftime("%Y-%m-%d")

def fetch_prices(code: str, years: int = YEARS):
    """價格資料以 SQLite 為主要來源。歷史資料一旦入庫就不再重抓,
       只有當 DB 裡最新一筆落後於最近交易日時,才向 yfinance 補抓近期缺口。
       完全沒有資料時才會整段(period=15y)抓一次。"""
    code_u = code.upper()

    with db() as c:
        row = c.execute("SELECT MAX(date) md, MIN(date) mnd, COUNT(*) n FROM prices WHERE symbol=?",
                        (code_u,)).fetchone()
    have_max, have_min, have_n = (row["md"], row["mnd"], row["n"]) if row else (None, None, 0)

    need_earliest = (dt.date.today() - dt.timedelta(days=int(years * 365.25))).strftime("%Y-%m-%d")
    latest_needed = _latest_trading_day_guess()

    # 決定要不要打外部 API,以及打多久的區間
    fetch_period = None
    if have_n == 0 or have_min is None:
        fetch_period = "15y"                       # 全新股票:一次抓滿(供十年線用)
    else:
        if have_max is None or have_max < latest_needed:
            # 只缺最近幾天 → 抓短區間補齊即可(留裕度抓 5 天)
            gap_days = (dt.date.today() - dt.datetime.strptime(have_max, "%Y-%m-%d").date()).days if have_max else 9999
            if gap_days <= 7:      fetch_period = "5d"
            elif gap_days <= 30:   fetch_period = "1mo"
            elif gap_days <= 180:  fetch_period = "6mo"
            else:                  fetch_period = "2y"
        if have_min is not None and have_min > need_earliest:
            # 使用者要更久以前的資料,而 DB 裡最早的還不夠早 → 需要抓更長區間
            fetch_period = "15y"

    if fetch_period:
        try:
            fetched = _yf_download(code, fetch_period)
            _save_prices(code, fetched)
        except Exception as e:
            print(f"{code} 價格補抓失敗({fetch_period}):{e}", file=sys.stderr)

    # 從 DB 取出要用的範圍
    rows = _prices_from_db(code, years)
    if len(rows) < 60:
        # DB 還是不夠(可能剛抓失敗或本來就資料少):最後再嘗試整段抓一次
        try:
            fetched = _yf_download(code, "15y")
            _save_prices(code, fetched)
            rows = _prices_from_db(code, years)
        except Exception:
            pass
    if len(rows) < 60:
        raise ValueError("資料太少,無法畫趨勢線")
    dates  = [d for d, _ in rows]
    closes = [x for _, x in rows]
    return dates, closes

NAMES_FILE = os.path.join(BASE, "names.json")
def load_names():
    try:
        with open(NAMES_FILE, encoding="utf-8") as f:
            return {k.upper(): v for k, v in json.load(f).items()}
    except Exception:
        return {}
NAMES = load_names()

# ---------- 證交所上市公司清單(中文名稱 + 產業別),抓一次存檔 ----------
TWSE_FILE     = os.path.join(BASE, "twse_listed.json")
TWSE_URL_JSON = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"        # 主要來源
TWSE_URL_CSV  = "https://mopsfin.twse.com.tw/opendata/t187ap03_L.csv"       # 備援來源(不同網域)
TWSE_TTL      = 7 * 86400                                                  # 7 天內用快取
TWSE_RETRY_BACKOFF = 120                                                    # 抓取失敗後,至少間隔幾秒才再試一次
# 上市產業別代號 -> 中文(t187ap03_L 的「產業別」常是代號)
IND_CODE = {"01":"水泥","02":"食品","03":"塑膠","04":"紡織纖維","05":"電機機械","06":"電器電纜",
    "08":"玻璃陶瓷","09":"造紙","10":"鋼鐵","11":"橡膠","12":"汽車","14":"建材營造","15":"航運",
    "16":"觀光餐旅","17":"金融保險","18":"貿易百貨","19":"綜合","20":"其他","21":"化學工業",
    "22":"生技醫療","23":"油電燃氣","24":"半導體","25":"電腦及週邊設備","26":"光電","27":"通信網路",
    "28":"電子零組件","29":"電子通路","30":"資訊服務","31":"其他電子","32":"文化創意","33":"農業科技",
    "34":"電子商務","35":"綠能環保","36":"數位雲端","37":"運動休閒","38":"居家生活"}

def _twse_row_to_entry(get):
    """get: 給欄位中文名稱,回傳 (代號, 條目) 或 None。集中容錯,JSON/CSV 共用。"""
    code = (get("公司代號") or get("Code") or "").strip()
    if not code:
        return None
    name = get("公司簡稱") or get("公司名稱") or get("Name")
    ind  = (get("產業別") or get("Industry") or "").strip()
    ind  = IND_CODE.get(ind, ind) or None                  # 代號 -> 中文(已是中文則原樣)
    return code, {"name": name, "industry": ind}

# 已知問題:*.twse.com.tw 系列網域的憑證鏈缺少 Subject Key Identifier 擴展欄位,
# 在較新版 Python/OpenSSL 下會被嚴格驗證擋下(CERTIFICATE_VERIFY_FAILED),
# 與你的網路或防火牆無關,是對方伺服器憑證設定的瑕疵(不少開發者都回報過)。
# 這裡只針對「這兩個證交所網域、抓公開無敏感性的開放資料」停用憑證驗證,
# 不影響其他連線(例如 yfinance/Yahoo 仍走正常驗證)。
_TWSE_SSL_CTX = None
def _twse_ssl_context():
    global _TWSE_SSL_CTX
    if _TWSE_SSL_CTX is None:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        _TWSE_SSL_CTX = ctx
    return _TWSE_SSL_CTX

def _fetch_twse_json() -> dict:
    req = Request(TWSE_URL_JSON, headers={"User-Agent": "Mozilla/5.0"})
    rows = json.loads(urlopen(req, timeout=20, context=_twse_ssl_context()).read().decode("utf-8"))
    m = {}
    for r in rows:
        entry = _twse_row_to_entry(r.get)
        if entry: m[entry[0]] = entry[1]
    return m

def _fetch_twse_csv() -> dict:
    req = Request(TWSE_URL_CSV, headers={"User-Agent": "Mozilla/5.0"})
    text = urlopen(req, timeout=20, context=_twse_ssl_context()).read().decode("utf-8-sig")   # 該檔常帶 BOM
    m = {}
    for row in csv.DictReader(io.StringIO(text)):
        entry = _twse_row_to_entry(row.get)
        if entry: m[entry[0]] = entry[1]
    return m

_twse_map = None          # 成功抓到的結果(in-memory 正向快取,不快取失敗)
_twse_fail_until = 0.0     # 失敗後的退避時間點,避免每個請求都重打外部 API
def twse_listed() -> dict:
    """{代號: {name, industry}}。抓不到就回空 dict(自動退回 yfinance 名稱)。
       重要:失敗不會被永久記住——只會短暫退避,過陣子(或重啟)會自動重試。"""
    global _twse_map, _twse_fail_until
    if _twse_map:                                            # 已有「非空」成功結果,直接用
        return _twse_map

    try:                                                      # 先看本地快取檔(7 天內且非空才採信)
        if (time.time() - os.path.getmtime(TWSE_FILE) < TWSE_TTL):
            cached = json.load(open(TWSE_FILE, encoding="utf-8"))
            if cached:                                         # 空字典視為無效快取,不採信
                _twse_map = cached
                return _twse_map
    except Exception:
        pass

    if time.time() < _twse_fail_until:                         # 還在退避期間,先不重打
        return {}

    m, last_err = {}, None
    for label, fetcher in (("JSON", _fetch_twse_json), ("CSV", _fetch_twse_csv)):
        try:
            m = fetcher()
            if m:
                break
        except Exception as e:
            last_err = e
            print(f"證交所上市清單({label})抓取失敗:{e}", file=sys.stderr)

    if m:
        json.dump(m, open(TWSE_FILE, "w", encoding="utf-8"), ensure_ascii=False)
        _twse_map = m
        print(f"證交所上市清單抓取成功,共 {len(m)} 檔", file=sys.stderr)
        return m

    # 兩個來源都失敗:不快取這個結果,短暫退避後允許重試
    _twse_fail_until = time.time() + TWSE_RETRY_BACKOFF
    print(f"證交所上市清單暫時抓不到({TWSE_RETRY_BACKOFF}秒後會再試),改用 yfinance 英文名:{last_err}", file=sys.stderr)
    return {}

INFO_TTL = 12 * 3600     # info(本益比/名稱等)快取 12 小時

def fetch_info(code: str) -> dict:
    """對外入口:優先讀 DB 快取(12 小時內),過期或沒有才真正打 API。
       info 這類欄位(本益比、市值)雖然會變,但盤中頻繁重抓意義不大,快取半天足夠。"""
    code_u = code.upper()
    with db() as c:
        row = c.execute("SELECT json, fetched_at FROM stock_info WHERE symbol=?", (code_u,)).fetchone()
    if row and (time.time() - row["fetched_at"] < INFO_TTL):
        try:
            return json.loads(row["json"])
        except Exception:
            pass
    data = _fetch_info_raw(code)
    try:
        with db() as c:
            c.execute("INSERT OR REPLACE INTO stock_info(symbol,json,fetched_at) VALUES(?,?,?)",
                      (code_u, json.dumps(data, ensure_ascii=False), time.time()))
    except Exception:
        pass
    return data

def _stock_valuation_from_twse(code: str) -> dict:
    """從證交所 BWIBBU 全市場報表取單一台股的 本益比 / 殖利率 / 股價淨值比。
       這是官方開放資料,不像 yfinance 的 .info 會在雲端 IP 被 Yahoo 限流回空。
       BWIBBU 整張表已被 _get_market_report 快取(記憶體 4 小時 + DB),個股查詢很便宜。"""
    if not code.isdigit():
        return {}
    try:
        _, data = _get_market_report("BWIBBU", _fetch_bwibbu_for_date)
        return (data or {}).get(code.strip()) or {}
    except Exception as e:
        print(f"BWIBBU 個股估值({code})取得失敗:{e}", file=sys.stderr)
        return {}

def _fetch_info_raw(code: str) -> dict:
    """公司基本資料(防呆:抓不到就回空值,不影響畫圖)。

    重要效能取捨:
      - 台股(數字代號)在 Render 這種雲端 IP 上,yfinance 的 .info 幾乎必被 Yahoo 擋,
        而且不是立刻失敗、是卡十幾秒才逾時 —— 一整排預載會把單一 worker 拖到逾時被砍。
        所以台股「完全不打」yfinance,基本面改用證交所官方 BWIBBU(本益比/殖利率/淨值比)
        + 上市清單(名稱/產業);52 週高低則在 stock_payload 用收盤價自算。
      - 美股維持原本的 yfinance .info(在美股上較常成功)。
    """
    is_tw = code.isdigit()
    ov = NAMES.get(code.upper(), {})
    tw = (twse_listed().get(code.strip()) if is_tw else {}) or {}

    info = {}
    if not is_tw:
        try:
            import yfinance as yf
            info = yf.Ticker(yf_symbol(code)).info or {}
        except Exception:
            info = {}

    val = _stock_valuation_from_twse(code) if is_tw else {}   # 台股官方本益比 / 殖利率 / 淨值比(已快取,很便宜)

    name   = ov.get("name")   or tw.get("name")     or info.get("longName") or info.get("shortName") or code.upper()
    sector = ov.get("sector") or tw.get("industry") or info.get("sector")

    def first(*vals):
        for v in vals:
            if v is not None:
                return v
        return None

    return {
        "name":       name,
        "sector":     sector,
        "industry":   info.get("industry"),
        "currency":   first(info.get("currency"), "TWD" if is_tw else None),
        "market_cap": info.get("marketCap"),            # 台股此表無市值,會是 None(可接受)
        "pe":         first(info.get("trailingPE"), val.get("pe")),
        "forward_pe": info.get("forwardPE"),
        "peg":        info.get("pegRatio"),
        "pb":         first(info.get("priceToBook"), val.get("pb")),
        # yfinance 的 dividendYield 有時是小數(0.019)有時是百分比(1.9);BWIBBU 的殖利率是百分比。
        # 前端 fmtPct 兩種都能正確顯示,所以直接取第一個非空即可。
        "div_yield":  first(info.get("dividendYield"), val.get("yield")),
        "earnings_growth": info.get("earningsQuarterlyGrowth"),
        "wk_high":    info.get("fiftyTwoWeekHigh"),      # 台股 → None,交給 stock_payload 用收盤價自算
        "wk_low":     info.get("fiftyTwoWeekLow"),
    }

def search_index() -> dict:
    """合併證交所上市清單與 names.json,當作名稱搜尋索引 {代號:{name,industry}}。"""
    idx = {}
    for code, v in twse_listed().items():
        idx[code] = {"name": v.get("name"), "industry": v.get("industry")}
    for code, v in NAMES.items():
        e = idx.setdefault(code, {})
        if v.get("name"):   e["name"] = v["name"]
        if v.get("sector"): e["industry"] = e.get("industry") or v.get("sector")
    return idx

def search_stocks(q: str, limit: int = 10) -> list:
    """打代號或名稱(可只打一部分)都能找;含簡單模糊比對。"""
    q = q.strip()
    if not q:
        return []
    ql = q.lower()
    scored = []
    for code, info in search_index().items():
        name = info.get("name") or ""
        nl = name.lower()
        if   code == q:                            s = 100
        elif code.upper().startswith(q.upper()):   s = 90
        elif q in code:                            s = 75
        elif ql and nl.startswith(ql):             s = 80
        elif ql and ql in nl:                      s = 65
        else:
            r = difflib.SequenceMatcher(None, ql, nl).ratio() if nl else 0
            s = 40 + r * 15 if r >= 0.6 else None
        if s is not None:
            scored.append((s, code, name, info.get("industry")))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [{"symbol": c, "name": n, "industry": ind} for _, c, n, ind in scored[:limit]]

# ---------- 熱門股(台股/美股)──────────────────────────────────────────
# 台股:用證交所「每日所有證券交易資訊」官方端點,依「成交金額」排序取前幾名 ── 真實資料,非猜測。
# 美股:沒有同等可靠的免費官方「熱門股」API,改用 Yahoo 一個業界常用、但非官方文件化的
#       screener 端點;若失敗則退回一份*明確標示*的「常見高流通量代表股」靜態清單當保底,
#       絕不把保底清單偽裝成即時熱門股(回傳結果會帶 source 欄位讓前端可以誠實標示)。
HOT_TW_LIMIT = 10
HOT_US_LIMIT = 10
US_FALLBACK_STATIC = [
    ("AAPL","Apple"), ("MSFT","Microsoft"), ("NVDA","NVIDIA"), ("AMZN","Amazon"),
    ("GOOGL","Alphabet"), ("META","Meta Platforms"), ("TSLA","Tesla"),
    ("AVGO","Broadcom"), ("AMD","AMD"), ("NFLX","Netflix"),
]

def _fetch_tw_hot_for_date(date_str: str):
    """抓某一天的台股全市場交易資訊,回傳依成交金額排序的清單;當天無資料回傳 []。"""
    url = f"https://www.twse.com.tw/exchangeReport/MI_INDEX?response=json&date={date_str}&type=ALLBUT0999"
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    raw = json.loads(urlopen(req, timeout=20, context=_twse_ssl_context()).read().decode("utf-8"))

    fields, data = raw.get("fields9"), raw.get("data9")           # 常見的「每日收盤行情」表格鍵名
    if not data:                                                   # 退而求其次:在 tables 陣列裡找對應表格
        for t in raw.get("tables", []):
            if "證券代號" in (t.get("fields") or []) and "成交金額" in (t.get("fields") or []):
                fields, data = t.get("fields"), t.get("data"); break
    if not fields or not data:
        return []

    try:
        i_code, i_name, i_val = fields.index("證券代號"), fields.index("證券名稱"), fields.index("成交金額")
    except ValueError:
        return []

    rows = []
    for r in data:
        code = (r[i_code] or "").strip()
        if not code.isdigit():                                    # 只留一般股票代號,排除權證/ETF等雜訊
            continue
        try:
            val = int(str(r[i_val]).replace(",", ""))
        except (ValueError, IndexError):
            continue
        rows.append((val, code, (r[i_name] or "").strip()))
    rows.sort(reverse=True)
    return [{"symbol": c, "name": n} for _, c, n in rows[:HOT_TW_LIMIT]]

def fetch_tw_hot():
    """從今天往前最多找 10 個日曆天,抓到第一個有資料的交易日就回傳(處理假日/休市)。"""
    today = dt.date.today()
    for back in range(10):
        date_str = (today - dt.timedelta(days=back)).strftime("%Y%m%d")
        try:
            rows = _fetch_tw_hot_for_date(date_str)
            if rows:
                return rows, date_str
        except Exception as e:
            print(f"台股熱門股({date_str})抓取失敗:{e}", file=sys.stderr)
    return [], None

def fetch_us_hot():
    """回傳 (清單, 來源字串)。來源 'yahoo_screener'=即時抓取成功;'fallback_static'=保底清單。"""
    try:
        url = ("https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved"
               f"?lang=en-US&region=US&formatted=true&count={HOT_US_LIMIT}&scrIds=most_actives")
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        raw = json.loads(urlopen(req, timeout=15).read().decode("utf-8"))
        quotes = raw["finance"]["result"][0]["quotes"]
        rows = [{"symbol": q["symbol"], "name": q.get("shortName") or q.get("longName") or q["symbol"]}
                for q in quotes[:HOT_US_LIMIT]]
        if rows:
            return rows, "yahoo_screener"
    except Exception as e:
        print(f"美股熱門股(Yahoo screener)抓取失敗,改用保底清單:{e}", file=sys.stderr)
    return [{"symbol": s, "name": n} for s, n in US_FALLBACK_STATIC], "fallback_static"

def get_hot():
    """回傳熱門股清單(純資料,不寫任何 DB)。
       前端拿到後自己顯示成唯讀的『熱門股』分頁,並可存進 localStorage 當快取。"""
    tw_rows, tw_date = fetch_tw_hot()
    us_rows, us_source = fetch_us_hot()
    return {
        "tw": tw_rows,
        "us": us_rows,
        "tw_asof": tw_date,
        "us_source": us_source,
        "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
    }


# ---------- 資金面資料(三大法人 / 融資融券 / 大盤本益比 / VIX)──────────────────
# 全部是台股官方開放資料(證交所),免費、無需金鑰。
# 欄位名稱用「包含關鍵字」模糊比對而非精確比對:證交所欄位常帶括號註記、用詞偶有調整,
# 模糊比對較不容易因為一點點格式差異就整批解析失敗。

def _strip_html_text(s):
    """證交所/櫃買 JSON 有時會在欄位或數字內夾 HTML 標籤;解析前先清掉。"""
    if s is None:
        return ""
    return re.sub(r"<[^>]*>", "", str(s)).replace("\xa0", " ").strip()

def _norm_field_name(s):
    """欄位名稱標準化:去 HTML、空白、全形空白與換行,讓欄位比對更耐改版。"""
    return re.sub(r"\s+", "", _strip_html_text(s).replace("　", ""))

def _find_field(fields, must_contain, exclude=()):
    """在欄位名稱清單裡找出符合條件的索引。
       先找「以第一個關鍵字開頭」的欄位以降低歧義,找不到再用任意位置比對。"""
    nf = [_norm_field_name(f) for f in (fields or [])]
    first = must_contain[0]
    for i, f in enumerate(nf):
        if f.startswith(first) and all(k in f for k in must_contain) and not any(k in f for k in exclude):
            return i
    for i, f in enumerate(nf):
        if all(k in f for k in must_contain) and not any(k in f for k in exclude):
            return i
    return None

def _find_any_field(fields, needles, exclude=()):
    nf = [_norm_field_name(f) for f in (fields or [])]
    for i, f in enumerate(nf):
        if any(n in f for n in needles) and not any(x in f for x in exclude):
            return i
    return None

def _cell(row, idx):
    """安全取列欄位。TWSE 報表偶爾混入短列/註解列,不能直接 r[idx]。"""
    if idx is None:
        return None
    if isinstance(row, dict):
        # dict row:idx 可能是欄位名稱,也可能仍是 index。兩種都支援。
        if isinstance(idx, str):
            return row.get(idx)
        keys = list(row.keys())
        return row.get(keys[idx]) if 0 <= idx < len(keys) else None
    if not isinstance(row, (list, tuple)):
        return None
    return row[idx] if 0 <= idx < len(row) else None

def _to_int(s):
    try:
        t = _strip_html_text(s).replace(",", "").replace("＋", "+").strip()
        t = t.replace("−", "-").replace("－", "-")
        if t in ("", "-", "--", "—", "N/A", "nan", "None"):
            return None
        # 有些報表會有小數或百分比註記,只取數字本體。
        t = t.replace("%", "")
        return int(float(t))
    except (ValueError, TypeError):
        return None

def _to_float(s):
    try:
        t = _strip_html_text(s).replace(",", "").strip()
        if t in ("", "-", "--", "—", "N/A"):
            return None
        return float(t)
    except (ValueError, TypeError):
        return None

def _extract_table(raw, code_hint, value_hint):
    """從日報 JSON 抓出最像資料表的 (fields, data)。支援頂層 fields/data 與 tables 多表格式。"""
    candidates = []
    if isinstance(raw, dict) and raw.get("fields") and raw.get("data"):
        candidates.append({"title": raw.get("title", ""), "fields": raw.get("fields") or [], "data": raw.get("data") or []})
    for t in (raw.get("tables", []) or []) if isinstance(raw, dict) else []:
        if isinstance(t, dict) and t.get("fields") and t.get("data"):
            candidates.append(t)
    for t in candidates:
        f = [_norm_field_name(x) for x in (t.get("fields") or [])]
        if any(code_hint in x for x in f) and any(value_hint in x for x in f):
            return t.get("fields") or [], t.get("data") or []
    if candidates:
        # fallback:若欄位名改版,至少回第一張有資料的表,後續解析器會安全失敗。
        return candidates[0].get("fields") or [], candidates[0].get("data") or []
    return None, None

def _fetch_json_url(url: str) -> dict:
    req = Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json,text/plain,*/*",
        "Referer": "https://www.twse.com.tw/",
    })
    return json.loads(urlopen(req, timeout=20, context=_twse_ssl_context()).read().decode("utf-8"))

def _fetch_text_url(url: str) -> str:
    """抓文字/CSV 用。TWSE CSV 常見 Big5/UTF-8 BOM,所以做多編碼 fallback。"""
    req = Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/csv,text/plain,*/*",
        "Referer": "https://www.twse.com.tw/",
    })
    b = urlopen(req, timeout=20, context=_twse_ssl_context()).read()
    for enc in ("utf-8-sig", "big5hkscs", "cp950", "utf-8"):
        try:
            return b.decode(enc)
        except Exception:
            pass
    return b.decode("utf-8", errors="ignore")

def _fetch_t86_for_date(date_str: str) -> dict:
    """三大法人(外資/投信/自營商)當日對個股買賣超股數。"""
    urls = [
        f"https://www.twse.com.tw/rwd/zh/fund/T86?response=json&date={date_str}&selectType=ALL",
        f"https://www.twse.com.tw/fund/T86?response=json&date={date_str}&selectType=ALL",
    ]
    last_err = None
    for url in urls:
        try:
            raw = _fetch_json_url(url)
            fields, data = _extract_table(raw, "證券代號", "買賣超")
            if not fields or not data:
                continue
            i_code   = _find_field(fields, ["證券代號"])
            i_foreign= _find_field(fields, ["外", "買賣超"])
            i_trust  = _find_field(fields, ["投信", "買賣超"])
            i_dealer = _find_field(fields, ["自營商", "買賣超"], exclude=["自行", "避險"])
            i_total  = _find_field(fields, ["三大法人", "買賣超"])
            out = {}
            for r in data:
                code = _strip_html_text(_cell(r, i_code))
                m = re.match(r"^(\d{4,6})", code)
                if not m:
                    continue
                code = m.group(1)
                foreign = _to_int(_cell(r, i_foreign)) if i_foreign is not None else None
                trust   = _to_int(_cell(r, i_trust))   if i_trust   is not None else None
                dealer  = _to_int(_cell(r, i_dealer))  if i_dealer  is not None else None
                total   = _to_int(_cell(r, i_total))   if i_total   is not None else None
                if total is None:
                    parts = [x for x in (foreign, trust, dealer) if x is not None]
                    total = sum(parts) if parts else None
                if foreign is None and trust is None and dealer is None and total is None:
                    continue
                out[code] = {
                    "foreign_net": foreign,
                    "trust_net":   trust,
                    "dealer_net":  dealer,
                    "total_net":   total,
                }
            if out:
                return out
        except Exception as e:
            last_err = e
    if last_err:
        print(f"T86({date_str}) 抓取或解析失敗:{last_err}", file=sys.stderr)
    return {}

def _candidate_table_items(raw: dict):
    """回傳所有可能含資料的 table。

    TWSE/TPEx JSON 不一定只有 fields/data。融資融券 MI_MARGN 常會把
    全市場彙總表放在 fields/data,再把「個股明細表」放在 fields1/data1、
    fields2/data2...。舊版只掃 fields/data,因此會完全漏掉個股融資融券餘額。
    """
    items = []

    def add_item(fields, data, title=""):
        if fields and data:
            items.append({"title": title or "", "fields": fields or [], "data": data or []})

    def walk(obj, title=""):
        if isinstance(obj, dict):
            cur_title = obj.get("title", title) or title

            # 1) 標準表格。
            add_item(obj.get("fields"), obj.get("data"), cur_title)
            add_item(obj.get("fields"), obj.get("aaData"), cur_title)
            add_item(obj.get("columns"), obj.get("data"), cur_title)

            # 2) TWSE 常見多表格式: fields1/data1、fields2/data2...。
            #    這是本次融資融券抓不到個股資料的主要原因。
            for k, fields in obj.items():
                m = re.fullmatch(r"(?:fields|columns)(\d+)", str(k))
                if not m or not fields:
                    continue
                suf = m.group(1)
                for data_key in (f"data{suf}", f"aaData{suf}"):
                    add_item(fields, obj.get(data_key), f"{cur_title}:{k}/{data_key}")

            # 3) 如果未來改成 table1:{fields,data} 這種巢狀格式,也一起掃。
            for k, v in obj.items():
                if isinstance(v, (dict, list)):
                    walk(v, cur_title or str(k))
        elif isinstance(obj, list):
            for v in obj:
                walk(v, title)

    walk(raw)

    # 去重:相同 fields+前幾筆資料+title 只保留一次,避免 tables 與遞迴掃描重複。
    seen, uniq = set(), []
    for t in items:
        fields = t.get("fields") or []
        data = t.get("data") or []
        sample = data[:3] if isinstance(data, list) else []
        key = (tuple(map(str, fields)), len(data or []), str(sample), t.get("title", ""))
        if key not in seen:
            seen.add(key)
            uniq.append(t)
    return uniq

def _norm_fields(fields):
    return [_norm_field_name(f) for f in (fields or [])]

def _find_balance_field(fields, side: str):
    """找融資/融券的餘額欄位。side='融資' or '融券'。"""
    # 最準:欄位名直接包含「融資/融券」與「今日餘額」
    for keys in ([side, "今日餘額"], [side, "餘額"], [side, "餘"]):
        idx = _find_field(fields, keys)
        if idx is not None:
            return idx
    return None

def _parse_margin_table(fields, data) -> dict:
    """解析單張融資融券表。
       支援 TWSE 個股表、二層表頭造成的重複「今日餘額」,以及 TPEx aaData 類型。"""
    fields = _norm_fields(fields)
    if not fields or not data:
        return {}

    i_code = _find_any_field(fields, ["股票代號", "證券代號", "代號", "有價證券代號", "證券代碼"])
    if i_code is None:
        return {}

    i_margin = _find_balance_field(fields, "融資")
    i_short  = _find_balance_field(fields, "融券")

    # 有些 JSON 會把群組表頭(融資/融券)拿掉,欄位只剩重複的「今日餘額」。
    # 標準順序通常第一個今日餘額=融資,第二個今日餘額=融券。
    if i_margin is None or i_short is None:
        today_balance = [i for i, f in enumerate(fields) if "今日餘額" in f or f in ("餘額", "餘")]
        if len(today_balance) >= 2:
            if i_margin is None:
                i_margin = today_balance[0]
            if i_short is None:
                i_short = today_balance[1]

    # 再退一步:標準 MI_MARGN 欄位位置為
    # 代號,名稱,融資買進,融資賣出,融資現金償還,融資前日餘額,融資今日餘額,融資限額,
    # 融券買進,融券賣出,融券現券償還,融券前日餘額,融券今日餘額,融券限額,資券互抵,註記
    if i_margin is None and len(fields) > i_code + 6:
        i_margin = i_code + 6
    if i_short is None and len(fields) > i_code + 12:
        i_short = i_code + 12

    if i_margin is None and i_short is None:
        return {}

    out = {}
    for r in data:
        code = _strip_html_text(_cell(r, i_code))
        m = re.match(r"^(\d{4,6})", code)
        if not m:
            continue
        code = m.group(1)
        mb = _to_int(_cell(r, i_margin)) if i_margin is not None else None
        sb = _to_int(_cell(r, i_short))  if i_short  is not None else None
        if mb is None and sb is None:
            continue
        out[code] = {"margin_balance": mb, "short_balance": sb}
    return out

def _parse_margin_csv_for_stock(code: str, text: str) -> dict | None:
    """直接掃 TWSE CSV 文字,找指定股票的融資/融券餘額。

    這是給 MI_MARGN JSON 解析失敗時的保險作法:CSV 下載格式通常仍保留表格原始欄位,
    即使 JSON 變成多層表頭或欄位名稱異動,逐列掃股票代號仍可抓到標準位置。
    """
    if not text:
        return None
    rows = []
    for row in csv.reader(io.StringIO(text)):
        cleaned = [_strip_html_text(x) for x in row]
        if any(cleaned):
            rows.append(cleaned)

    # 找最像欄位標題的列,優先用欄名定位「今日餘額」。
    header = None
    for row in rows[:20]:
        joined = "".join(_norm_field_name(x) for x in row)
        if ("融資" in joined and "融券" in joined and ("餘額" in joined or "今日餘額" in joined)):
            header = row
            break

    h_margin = h_short = None
    if header:
        nf = [_norm_field_name(x) for x in header]
        # 若 CSV 欄名已帶融資/融券,直接定位。
        for i, f in enumerate(nf):
            if h_margin is None and "融資" in f and "餘額" in f and ("今日" in f or "本日" in f or f.endswith("餘額")):
                h_margin = i
            if h_short is None and "融券" in f and "餘額" in f and ("今日" in f or "本日" in f or f.endswith("餘額")):
                h_short = i
        # 多層表頭被攤平時可能只有重複「今日餘額」。標準順序第一個=融資,第二個=融券。
        bals = [i for i, f in enumerate(nf) if "今日餘額" in f or "本日餘額" in f or f == "餘額"]
        if h_margin is None and len(bals) >= 1:
            h_margin = bals[0]
        if h_short is None and len(bals) >= 2:
            h_short = bals[1]

    for row in rows:
        code_idx = None
        for i, cell in enumerate(row[:4]):
            m = re.match(r"^(\d{4,6})(?:\D|$)", cell)
            if m and m.group(1) == code:
                code_idx = i
                break
        if code_idx is None:
            continue

        candidates = []
        # 1) 欄名定位。
        if h_margin is not None or h_short is not None:
            mb = _to_int(row[h_margin]) if h_margin is not None and h_margin < len(row) else None
            sb = _to_int(row[h_short])  if h_short  is not None and h_short  < len(row) else None
            if mb is not None or sb is not None:
                candidates.append((mb, sb, "header"))

        # 2) TWSE MI_MARGN 標準相對位置。
        pos_margin = code_idx + 6
        pos_short  = code_idx + 12
        mb = _to_int(row[pos_margin]) if pos_margin < len(row) else None
        sb = _to_int(row[pos_short])  if pos_short  < len(row) else None
        if mb is not None or sb is not None:
            candidates.append((mb, sb, "std-pos"))

        # 3) 最後保險:抓該列所有整數欄位,用標準欄序推估。
        #    code/name 之後通常依序為 融資買進/賣出/償還/前日餘額/今日餘額/限額/融券買進/...
        nums = []
        for i, cell in enumerate(row[code_idx+1:], start=code_idx+1):
            v = _to_int(cell)
            if v is not None:
                nums.append((i, v))
        if len(nums) >= 11:
            # 排除股票名稱後,第 5 個數值通常是融資今日餘額,第 11 個是融券今日餘額。
            candidates.append((nums[4][1], nums[10][1], "numeric-order"))

        for mb, sb, how in candidates:
            if mb is not None or sb is not None:
                return {"margin_balance": mb, "short_balance": sb, "source": f"TWSE CSV:{how}"}
    return None


def _fetch_twse_margin_csv_for_date(code: str, date_str: str) -> dict | None:
    """用 CSV 直接抓單一股票融資融券餘額。JSON 解析不到時最有用。"""
    urls = [
        f"https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?response=csv&date={date_str}&selectType=MS",
        f"https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?response=csv&date={date_str}&selectType=ALL",
        f"https://www.twse.com.tw/exchangeReport/MI_MARGN?response=csv&date={date_str}&selectType=MS",
        f"https://www.twse.com.tw/exchangeReport/MI_MARGN?response=csv&date={date_str}&selectType=ALL",
    ]
    last_err = None
    for url in urls:
        try:
            mar = _parse_margin_csv_for_stock(code, _fetch_text_url(url))
            if mar:
                return mar
        except Exception as e:
            last_err = e
            continue
    if last_err:
        print(f"MI_MARGN CSV({code},{date_str}) 抓取或解析失敗:{last_err}", file=sys.stderr)
    return None

def _roc_date(date_str: str) -> str:
    """YYYYMMDD -> 民國年/MM/DD,供 TPEx 備援端點使用。"""
    y = int(date_str[:4]) - 1911
    return f"{y}/{date_str[4:6]}/{date_str[6:8]}"

def _fetch_margin_for_date(date_str: str) -> dict:
    """融資融券當日餘額。

    修正重點:
    1) TWSE 以新版 rwd/zh/marginTrading/MI_MARGN + selectType=MS 優先。
    2) 加上舊版 exchangeReport 與 TWSE OpenAPI fallback。
    3) 加上 TPEx fallback,避免上櫃股票完全沒資料。
    4) 解析器不再假設欄位/列長度固定,欄位缺失時安全略過。
    """
    twse_urls = [
        f"https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?response=json&date={date_str}&selectType=MS",
        f"https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?response=json&date={date_str}&selectType=ALL",
        f"https://www.twse.com.tw/exchangeReport/MI_MARGN?response=json&date={date_str}&selectType=MS",
        f"https://www.twse.com.tw/exchangeReport/MI_MARGN?response=json&date={date_str}&selectType=ALL",
    ]
    # TPEx 用民國年日期,作為上櫃股票或 TWSE 暫時回空時的備援。
    tpex_urls = [
        f"https://www.tpex.org.tw/web/stock/margin_trading/margin_balance/margin_bal_result.php?l=zh-tw&o=json&d={_roc_date(date_str)}&s=0,asc",
    ]

    last_err = None
    for url in twse_urls + tpex_urls:
        try:
            raw = _fetch_json_url(url)
            stat = str(raw.get("stat") or raw.get("reportDate") or "").strip()
            if stat.upper() in ("FAIL", "很抱歉，沒有符合條件的資料!", "查詢日期小於民國88年1月5日，請重新查詢!"):
                continue
            merged = {}
            for t in _candidate_table_items(raw):
                parsed = _parse_margin_table(t.get("fields") or [], t.get("data") or [])
                if parsed:
                    merged.update(parsed)
            if merged:
                return merged
        except Exception as e:
            last_err = e
            continue
    if last_err:
        print(f"MI_MARGN({date_str}) 抓取或解析失敗:{last_err}", file=sys.stderr)
    return {}


def _ymd_dash(date_str: str) -> str:
    """YYYYMMDD -> YYYY-MM-DD。"""
    if not date_str or len(date_str) != 8:
        return date_str
    return f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"

def _ymd_plain(date_str: str) -> str:
    """YYYY-MM-DD / YYYYMMDD -> YYYYMMDD。"""
    t = str(date_str or "").strip()
    return t.replace("-", "")[:8]

def _fetch_finmind_margin_history(code: str, start_date: str, end_date: str) -> dict:
    """FinMind 個股融資融券備援。支援 v4/v3/v2 端點與多種欄位命名。"""
    if not code or not code.isdigit():
        return {}

    start_dash, end_dash = _ymd_dash(start_date), _ymd_dash(end_date)
    url_specs = []
    # v4:現行文件常用格式。
    url_specs.append(("FinMind-v4", "https://api.finmindtrade.com/api/v4/data?" + urlencode({
        "dataset": "TaiwanStockMarginPurchaseShortSale",
        "data_id": code,
        "start_date": start_dash,
        "end_date": end_dash,
    })))
    # v3/v2:舊版與 web domain 備援,有些環境會只允許其中一個 domain。
    url_specs.append(("FinMind-v3", "https://api.finmindtrade.com/api/v3/data?" + urlencode({
        "dataset": "TaiwanStockMarginPurchaseShortSale",
        "stock_id": code,
        "date": start_dash,
    })))
    url_specs.append(("FinMind-v2", "https://api.web.finmindtrade.com/v2/api?" + urlencode({
        "dataset": "TaiwanStockMarginPurchaseShortSale",
        "stock_id": code,
        "date": start_dash,
    })))

    def pick(row, names):
        for n in names:
            if isinstance(row, dict) and n in row:
                return row.get(n)
        if isinstance(row, dict):
            norm = {re.sub(r"[^a-z0-9一-龥]", "", k.lower()): v for k, v in row.items()}
            for n in names:
                key = re.sub(r"[^a-z0-9一-龥]", "", n.lower())
                if key in norm:
                    return norm[key]
        return None

    out, errors = {}, []
    for source, url in url_specs:
        try:
            raw = _fetch_json_url(url)
            rows = raw.get("data") if isinstance(raw, dict) else None
            if not isinstance(rows, list):
                errors.append(f"{source}: no data list")
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                d = _ymd_plain(pick(row, ["date", "Date", "日期"]))
                if not d or d < start_date or d > end_date:
                    continue
                stock_id = str(pick(row, ["stock_id", "StockID", "stockID", "證券代號", "股票代號"]) or code).strip()
                if stock_id != code:
                    continue
                mb = _to_int(pick(row, [
                    "MarginPurchaseTodayBalance", "MarginPurchaseTodayBalanceShares",
                    "margin_purchase_today_balance", "margin_purchase_balance",
                    "MarginPurchaseBalance", "MarginPurchaseRemain", "MarginPurchase", "融資今日餘額", "融資餘額", "融資餘額股數"
                ]))
                sb = _to_int(pick(row, [
                    "ShortSaleTodayBalance", "ShortSaleTodayBalanceShares",
                    "short_sale_today_balance", "short_sale_balance",
                    "ShortSaleBalance", "ShortSaleRemain", "ShortSale", "融券今日餘額", "融券餘額", "融券餘額股數"
                ]))
                if mb is None and sb is None:
                    continue
                out[d] = {"margin_balance": mb, "short_balance": sb, "source": source}
            if out:
                return out
        except Exception as e:
            errors.append(f"{source}: {e}")
    if errors:
        print(f"FinMind margin({code},{start_date}-{end_date}) 無法取得:{' | '.join(errors[:3])}", file=sys.stderr)
    return out

def _fetch_bwibbu_for_date(date_str: str) -> dict:
    """全市場個股本益比 / 殖利率 / 股價淨值比 當日報表。"""
    url = f"https://www.twse.com.tw/exchangeReport/BWIBBU_d?response=json&date={date_str}&selectType=ALL"
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    raw = json.loads(urlopen(req, timeout=20, context=_twse_ssl_context()).read().decode("utf-8"))
    fields, data = _extract_table(raw, "證券代號", "本益比")
    if not fields or not data:
        return {}
    i_code  = _find_field(fields, ["證券代號"])
    i_pe    = _find_field(fields, ["本益比"])
    i_yield = _find_field(fields, ["殖利率"])
    i_pb    = _find_field(fields, ["股價淨值比"])
    out = {}
    for r in data:
        code = (r[i_code] or "").strip() if i_code is not None else None
        if not code or not code.isdigit():
            continue
        out[code] = {
            "pe":    _to_float(r[i_pe])    if i_pe    is not None else None,
            "yield": _to_float(r[i_yield]) if i_yield is not None else None,
            "pb":    _to_float(r[i_pb])    if i_pb    is not None else None,
        }
    return out

# 全市場日報表快取:同一天只抓一次(資料量大),之後查任何個股都直接從快取裡撈該股那一列。
# key 可能是「T86:latest」或「T86:20260701」;每個交易日報表都會暫存,避免同一批股票重複打官方 API。
_market_reports_cache: dict[str, tuple[str, dict, float]] = {}
MARKET_REPORT_TTL = 4 * 3600   # 4 小時
FUND_HISTORY_DAYS = 14         # 資金面至少顯示近兩週「交易日」資料
FUND_HISTORY_LOOKBACK = 45     # 遇到連假時,最多往前找 45 個日曆日

def _market_report_from_db(report_key: str, date_str: str):
    """回傳 (json_dict, fetched_at) 或 None。"""
    try:
        with db() as c:
            row = c.execute("SELECT json, fetched_at FROM market_reports WHERE report_key=? AND date=?",
                            (report_key, date_str)).fetchone()
        if row:
            return json.loads(row["json"]), row["fetched_at"]
    except Exception:
        pass
    return None

def _save_market_report(report_key: str, date_str: str, data: dict):
    try:
        with db() as c:
            c.execute("INSERT OR REPLACE INTO market_reports(report_key,date,json,fetched_at) VALUES(?,?,?,?)",
                      (report_key, date_str, json.dumps(data, ensure_ascii=False), time.time()))
    except Exception as e:
        print(f"market_reports 寫入失敗({report_key}:{date_str}):{e}", file=sys.stderr)

def _get_market_report_for_date(report_key: str, date_str: str, fetch_fn):
    """抓指定日期的全市場日報表。快取策略:
       - 過去的交易日:資料定案不再變 → DB 有就永遠用,不打外部 API。
       - 今天:盤中/盤後可能還在更新 → DB 資料若超過 TTL 才重抓。
       記憶體快取(_market_reports_cache)當作同一請求內的第一層,DB 當作跨請求/跨重啟的持久層。"""
    key = f"{report_key}:{date_str}"
    today_str = dt.date.today().strftime("%Y%m%d")
    is_today = (date_str == today_str)

    # 第一層:記憶體
    hit = _market_reports_cache.get(key)
    if hit and time.time() - hit[2] < MARKET_REPORT_TTL:
        return hit[0], hit[1]

    # 第二層:DB。過去交易日 → 直接用(不看 TTL);今天 → 僅在 TTL 內才用
    dbrow = _market_report_from_db(report_key, date_str)
    if dbrow is not None:
        data, fetched_at = dbrow
        if (not is_today) or (time.time() - fetched_at < MARKET_REPORT_TTL):
            _market_reports_cache[key] = (date_str, data, time.time())
            return date_str, data

    # 第三層:真正打外部 API
    data = fetch_fn(date_str) or {}
    _market_reports_cache[key] = (date_str, data, time.time())
    # 只把「有實際內容」的報表寫進 DB;空資料(假日/尚未公布)不落 DB,
    # 以免把某天永久記成空的、之後就再也不會補抓。
    if data:
        _save_market_report(report_key, date_str, data)
    return date_str, data

def _get_market_report(report_key: str, fetch_fn):
    """取得最近一個有資料的交易日報表;保留給大盤本益比/VIX等單日摘要使用。"""
    latest_key = f"{report_key}:latest"
    hit = _market_reports_cache.get(latest_key)
    if hit and time.time() - hit[2] < MARKET_REPORT_TTL:
        return hit[0], hit[1]
    today = dt.date.today()
    for back in range(10):                      # 往前找最近一個有資料的交易日(處理假日休市)
        date_str = (today - dt.timedelta(days=back)).strftime("%Y%m%d")
        try:
            _, data = _get_market_report_for_date(report_key, date_str, fetch_fn)
            if data:
                _market_reports_cache[latest_key] = (date_str, data, time.time())
                return date_str, data
        except Exception as e:
            print(f"{report_key}({date_str}) 抓取失敗:{e}", file=sys.stderr)
    return None, {}

def _get_market_report_history(report_key: str, fetch_fn, min_rows: int = FUND_HISTORY_DAYS,
                               max_back: int = FUND_HISTORY_LOOKBACK):
    """取得最近 min_rows 個有資料的交易日全市場報表,回傳由舊到新的 [(date, data), ...]。"""
    rows = []
    today = dt.date.today()
    for back in range(max_back):
        date_str = (today - dt.timedelta(days=back)).strftime("%Y%m%d")
        try:
            _, data = _get_market_report_for_date(report_key, date_str, fetch_fn)
            if data:
                rows.append((date_str, data))
                if len(rows) >= min_rows:
                    break
        except Exception as e:
            print(f"{report_key}({date_str}) 抓取失敗:{e}", file=sys.stderr)
    rows.reverse()
    return rows

def _latest_with_key(history: list[dict], section: str):
    for row in reversed(history):
        data = row.get(section)
        if data:
            return {**data, "asof": row.get("date")}
    return None

def get_stock_funds(code: str, debug: bool = False) -> dict:
    """單一個股的資金面資料(三大法人 + 融資融券)。僅支援台股(數字代號)。"""
    if not code.isdigit():
        return {"available": False, "reason": "此資料目前僅支援台股,美股暫無對應免費官方來源"}

    t86_hist = _get_market_report_history("T86", _fetch_t86_for_date)
    margin_hist = _get_market_report_history("MARGN", _fetch_margin_for_date)

    # TWSE/TPEx 全市場日報表若沒有解析出指定個股,用 FinMind 的「個股+日期區間」資料補齊。
    # 這一步只在該股官方表缺漏太多時才打,避免每次切換股票都多打一個外部來源。
    official_margin_dates = [d for d, data in margin_hist if code in (data or {})]
    finmind_margin = {}
    csv_margin = {}
    date_pool = [d for d, _ in t86_hist] or [d for d, _ in margin_hist]
    if date_pool:
        start_date, end_date = min(date_pool), max(date_pool)
    else:
        end_date = dt.date.today().strftime("%Y%m%d")
        start_date = (dt.date.today() - dt.timedelta(days=FUND_HISTORY_LOOKBACK)).strftime("%Y%m%d")

    # 先用 FinMind 一次抓區間;若外部來源被擋或回空,再逐日用 TWSE CSV 掃指定股票列。
    if len(official_margin_dates) < max(2, len(date_pool) // 2):
        finmind_margin = _fetch_finmind_margin_history(code, start_date, end_date)

    missing_margin_dates = [d for d in date_pool if d not in official_margin_dates and d not in finmind_margin]
    # CSV 是每個交易日一個檔,只對缺漏日期打,避免整個畫面切換股票時過度請求。
    for d in missing_margin_dates[:FUND_HISTORY_DAYS]:
        mar = _fetch_twse_margin_csv_for_date(code, d)
        if mar:
            csv_margin[d] = mar

    by_date: dict[str, dict] = {}
    for date_str, data in t86_hist:
        inst = data.get(code)
        if inst:
            by_date.setdefault(date_str, {"date": date_str})["institutional"] = inst
    for date_str, data in margin_hist:
        mar = data.get(code)
        if mar:
            by_date.setdefault(date_str, {"date": date_str})["margin"] = mar
    # 補齊官方全市場表沒有解析到的融資融券資料。來源優先序:
    # TWSE/TPEx JSON > FinMind 區間 API > TWSE CSV 單日掃描。
    for date_str, mar in (finmind_margin or {}).items():
        if mar:
            by_date.setdefault(date_str, {"date": date_str}).setdefault("margin", mar)
    for date_str, mar in (csv_margin or {}).items():
        if mar:
            by_date.setdefault(date_str, {"date": date_str}).setdefault("margin", mar)

    # 只保留最近 target_trading_days 個有主要資金資料的日期;若 fallback 多回補資料,也不讓表格暴增。
    history = [by_date[d] for d in sorted(by_date)]
    if len(history) > FUND_HISTORY_DAYS:
        history = history[-FUND_HISTORY_DAYS:]

    debug_info = None
    if debug:
        debug_info = {
            "t86_dates": [d for d, _ in t86_hist],
            "margin_dates": [d for d, _ in margin_hist],
            "t86_row_counts": {d: len(data or {}) for d, data in t86_hist},
            "margin_row_counts": {d: len(data or {}) for d, data in margin_hist},
            "stock_in_t86_dates": [d for d, data in t86_hist if code in (data or {})],
            "stock_in_margin_dates": official_margin_dates,
            "finmind_margin_dates": sorted(finmind_margin.keys()),
            "finmind_margin_count": len(finmind_margin or {}),
            "csv_margin_dates": sorted(csv_margin.keys()),
            "csv_margin_count": len(csv_margin or {}),
            "margin_source": "TWSE/TPEx" if official_margin_dates else ("FinMind" if finmind_margin else ("TWSE CSV" if csv_margin else "none")),
            "cache_keys": sorted(_market_reports_cache.keys())[:80],
        }

    if not history:
        result = {
            "available": False,
            "reason": "最近兩週交易日內找不到此股票的三大法人或融資融券資料;可能不是上市股票,或證交所資料尚未更新",
            "history": [],
            "target_trading_days": FUND_HISTORY_DAYS,
        }
        if debug_info is not None:
            result["debug"] = debug_info
        return result

    inst = _latest_with_key(history, "institutional")
    mar = _latest_with_key(history, "margin")
    result = {
        "available": True,
        "target_trading_days": FUND_HISTORY_DAYS,
        "actual_days": len(history),
        "from": history[0]["date"],
        "to": history[-1]["date"],
        "institutional": inst,
        "margin": mar,
        "history": history,
    }
    if debug_info is not None:
        result["debug"] = debug_info
    elif mar is None:
        # 預設回傳精簡診斷,避免畫面沒資料時只能猜。前端會忽略這個欄位。
        result["margin_diagnostic"] = {
            "message": "後端未取得此股票近兩週融資融券餘額;請改用 ?debug=1 查看完整資料源狀態",
            "checked_dates": [d for d, _ in margin_hist],
            "stock_in_margin_dates": official_margin_dates,
            "finmind_margin_count": len(finmind_margin or {}),
            "csv_margin_count": len(csv_margin or {}),
            "candidate_fix": "v5 已支援 TWSE fields1/data1 多表格式",
        }
    return result

def fetch_market_pe():
    """大盤平均本益比:用證交所全市場個股本益比報表自行平均(注意:這是簡單算術平均,
       不是官方加權指數本益比,僅供「現在貴不貴」的粗略參考)。"""
    date_str, data = _get_market_report("BWIBBU", _fetch_bwibbu_for_date)
    pes = [v["pe"] for v in data.values() if v.get("pe") and v["pe"] > 0]
    if not pes:
        return None
    return {"avg_pe": round(sum(pes) / len(pes), 1), "n": len(pes), "asof": date_str}


def get_market_context():
    """全站共用的大盤參考資訊。
       已移除 VIX:平台同時支援台股/美股,VIX 不適合作為共通風險指標。
       目前只保留 PE 評估頁會用到的台股大盤平均本益比。"""
    pe = None
    try:
        pe = fetch_market_pe()
    except Exception as e:
        print(f"大盤本益比抓取失敗:{e}", file=sys.stderr)
    return {"market_pe": pe}


_cache: dict[str, tuple[float, dict]] = {}
def stock_payload(code: str, force: bool = False, years: int = YEARS) -> dict:
    years = max(1, min(15, int(years)))            # 安全範圍 1~15 年(十年線需 ~10年歷史+最長5年顯示視窗)
    key = f"{code.upper()}:{years}"
    hit = _cache.get(key)
    # DB 現在是主要來源,記憶體快取只是省去同一請求內的重算;
    # 資料本身是歷史(每天最多變一次),記憶體快取放到 10 分鐘也不會有鮮度問題。
    if hit and not force and time.time() - hit[0] < 600:
        return hit[1]
    if force:
        # 強制刷新:清掉這檔的 info 快取,讓價格與 info 都重抓最新
        try:
            with db() as c:
                c.execute("DELETE FROM stock_info WHERE symbol=?", (code.upper(),))
        except Exception:
            pass
    dates, closes = fetch_prices(code, years=years)
    info = fetch_info(code)
    # 最終保底:52 週高/低若前面來源都沒拿到,直接用近一年(約252個交易日)收盤價自算。
    # 這條一定成功,因為資料就在手上,不必再打任何外部 API。
    if info.get("wk_high") is None or info.get("wk_low") is None:
        recent = closes[-252:] if len(closes) >= 60 else closes
        if recent:
            if info.get("wk_high") is None:
                info["wk_high"] = round(max(recent), 2)
            if info.get("wk_low") is None:
                info["wk_low"] = round(min(recent), 2)
    payload = {"symbol": code.upper(), "market": "台股" if code.isdigit() else "美股",
               "asof": dates[-1], "last": closes[-1], "dates": dates, "closes": closes,
               "years": years, "info": info}
    _cache[key] = (time.time(), payload)
    return payload

# ---------- 路由 ----------
@app.route("/")
def index():
    # 前端已改由 GitHub Pages 提供,後端只當 API。這個根路由回傳簡單健康狀態,
    # 方便 Render 檢查、以及你用 UptimeRobot 之類的服務定時 ping(避免免費方案休眠)。
    return jsonify({"ok": True, "service": "stock-backend", "time": dt.datetime.now().isoformat(timespec="seconds")})

@app.route("/api/search")
def api_search():
    return jsonify(search_stocks(request.args.get("q", "")))

@app.route("/api/hot")
def api_hot():
    try:
        return jsonify(get_hot())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/stock/<symbol>")
def get_stock(symbol):
    force = request.args.get("force") == "1"
    years = request.args.get("years", YEARS)
    try:
        return jsonify(stock_payload(symbol, force=force, years=years))
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/stock/<symbol>/funds")
def api_stock_funds(symbol):
    try:
        return jsonify(get_stock_funds(symbol, debug=(request.args.get("debug") == "1")))
    except Exception as e:
        return jsonify({"available": False, "reason": str(e)}), 200

@app.route("/api/market")
def api_market():
    try:
        return jsonify(get_market_context())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# 在 module 載入時就建立快取表。
# 重要:Render 用 gunicorn 匯入本檔(app:app),不會執行下面的 __main__ 區塊,
# 所以 init_db() 必須放在這裡,否則線上第一次查詢會因為找不到表而爆掉。
init_db()

if __name__ == "__main__":
    # 本機開發用。Render 上是用 gunicorn 啟動(見 render.yaml 的 startCommand),不會走這裡。
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
