# app.py — ONE-FILE Streamlit: APR Matrix (Binance Dual Investment)
# - Прямое подключение к Binance с ротацией эндпоинтов
# - Автопереход на PROXY_BASE при 451 (restricted location)
# - Подсветка максимума и стрелки тренда по pid
import json, time, hmac, hashlib
from urllib import request, parse, error
import streamlit as st

# ========= Secrets / Настройки =========
BINANCE_KEY    = st.secrets.get("BINANCE_KEY", "")
BINANCE_SECRET = st.secrets.get("BINANCE_SECRET", "")
PROXY_BASE     = st.secrets.get("PROXY_BASE", "").rstrip("/")
# можно переопределить список хостов одной строкой через запятую:
BASES = [u.strip() for u in st.secrets.get("BINANCE_BASES", "").split(",") if u.strip()] or [
    "https://api-gcp.binance.com",
    "https://api4.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
    "https://api.binance.com",  # последним
]
UA = "apr-onefile/1.1"

st.set_page_config(page_title="APR Matrix (Binance Dual Investment)", layout="wide")
st.title("APR Matrix — Binance Dual Investment (ETH)")

# ========= HTTP helpers =========
def http_get(url, headers=None, timeout=15):
    req = request.Request(url, headers=headers or {"User-Agent": UA})
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            return resp.getcode(), resp.read().decode("utf-8")
    except error.HTTPError as e:
        body = ""
        try: body = e.read().decode("utf-8", "ignore")
        except: pass
        return e.code, body

def http_get_any(path_with_query: str, headers=None, timeout=15):
    """
    Перебираем альтернативные базы Binance. Пропускаем 451 и ответы с текстом
    про Eligibility/restricted location.
    """
    last = None
    for base in BASES:
        code, body = http_get(base + path_with_query, headers=headers, timeout=timeout)
        txt = body or ""
        restricted = (code == 451) or ("Eligibility" in txt) or ("restricted location" in txt)
        if 200 <= (code or 0) < 300 and not restricted:
            return base, code, body
        last = f"{base} → HTTP {code}; {txt[:160].replace(chr(10),' ')}"
    raise RuntimeError(last or "All Binance endpoints failed")

def sign_params(params: dict):
    qs  = parse.urlencode(params)
    sig = hmac.new(BINANCE_SECRET.encode(), qs.encode(), hashlib.sha256).hexdigest()
    headers = {"X-MBX-APIKEY": BINANCE_KEY, "User-Agent": UA}
    return f"{qs}&signature={sig}", headers

# ========= Binance fetch (raw) =========
@st.cache_data(ttl=2)
def fetch_products_all_direct(option_type: str, exercised: str, invest: str, pages=3):
    # time
    base, code, body = http_get_any("/api/v3/time")
    if code != 200:
        raise RuntimeError(f"time error: {code} {body[:200]}")
    ts = json.loads(body)["serverTime"]

    out = []
    chosen_base = base
    for idx in range(1, pages+1):
        p = {
            "optionType": option_type,
            "exercisedCoin": exercised,
            "investCoin": invest,
            "pageSize": 100,
            "pageIndex": idx,
            "timestamp": ts,
            "recvWindow": 60000
        }
        qs, hdr = sign_params(p)
        base, code, body = http_get_any(f"/sapi/v1/dci/product/list?{qs}", headers=hdr)
        chosen_base = base
        if code != 200:
            raise RuntimeError(f"HTTP {code}: {body[:300]}")
        data = json.loads(body)
        page = data.get("list") or data.get("data") or []
        out.extend(page)
        if len(page) < 100:
            break
    return {"items": out, "endpoint": chosen_base}

# ========= Proxy fetch (normalized or raw) =========
@st.cache_data(ttl=2)
def fetch_via_proxy(params: dict):
    if not PROXY_BASE:
        raise RuntimeError("PROXY_BASE is not set")
    url = f"{PROXY_BASE}/api/matrix?{parse.urlencode(params)}"
    code, body = http_get(url, headers={"User-Agent": UA}, timeout=15)
    if code != 200:
        raise RuntimeError(f"Proxy error {code}: {body[:300]}")
    return json.loads(body)

# ========= Normalize to matrix =========
def normalize(items, min_apr_pct, duration_set, max_strikes, strike_prec=2):
    """
    items: список raw продуктов Binance
    -> { strikes, days, cells, max_apr }
    """
    mp, strikes_set, days_set = {}, set(), set()
    for it in items:
        try:
            apr = float(it["apr"]) * 100.0
            if apr < min_apr_pct: 
                continue
            strike = round(float(it.get("strikePrice", 0)), strike_prec)
            days   = int(it.get("duration", 0))
            pid    = str(it.get("id", "n/a"))
            key = (strike, days)
            prev = mp.get(key)
            if prev is None or apr > prev[0]:
                mp[key] = (round(apr, 2), pid)
                strikes_set.add(strike); days_set.add(days)
        except Exception:
            continue
    strikes = sorted(strikes_set, reverse=True)[:max_strikes]
    days    = sorted([d for d in (duration_set or sorted(days_set)) if (not duration_set) or (d in days_set)])
    max_apr = max((v[0] for v in mp.values()), default=0)
    cells = {str(s): {} for s in strikes}
    for (s, d), (apr, pid) in mp.items():
        if s in strikes and d in days:
            cells[str(s)][str(d)] = {"apr": apr, "pid": pid}
    return {"strikes": strikes, "days": days, "cells": cells, "max_apr": max_apr}

# ========= UI controls =========
c1, c2, c3, c4 = st.columns([1.1,1.1,1.1,2])
with c1: option_type = st.selectbox("Option", ["PUT","CALL"], index=0)
with c2: exercised   = st.text_input("Exercised", "ETH")
with c3: invest      = st.text_input("Invest", "USDT")
with c4: durations_s = st.text_input("Durations (дни, через запятую)", "3,7,14")
duration_set = [int(x) for x in durations_s.split(",") if x.strip().isdigit()]
min_apr     = st.number_input("Min APR, %", value=0.0, step=0.1)
max_strikes = st.number_input("Max strikes", 1, 20, 5)
refresh_sec = st.slider("Автообновление, сек", 2, 30, 5)

# ========= Fetch strategy =========
endpoint_used = ""
data = None

try:
    if BINANCE_KEY and BINANCE_SECRET:
        # Пытаемся напрямую (может сработать, если IP не в геофенсе)
        raw = fetch_products_all_direct(option_type, exercised, invest)
        endpoint_used = f"Direct: {raw['endpoint']}"
        data = normalize(raw["items"], min_apr, duration_set, int(max_strikes))
    else:
        endpoint_used = "Direct: disabled (no keys)"
        raise RuntimeError("No direct keys; using proxy")

except Exception as direct_err:
    # Если прямой доступ не удался — пробуем прокси (если задан)
    if not PROXY_BASE:
        st.error(f"Ошибка прямого запроса: {direct_err}\n"
                 f"Решение: задать PROXY_BASE (ЕС-хост) в Secrets или запустить Streamlit в разрешённом регионе.")
        st.stop()
    try:
        params = {
            "optionType": option_type,
            "exercisedCoin": exercised,
            "investCoin": invest,
            "minAPR": float(min_apr),
            "maxStrikes": int(max_strikes),
            "durations": ",".join(str(d) for d in duration_set) if duration_set else ""
        }
        proxy_res = fetch_via_proxy(params)
        # Прокси может отдавать уже нормализованную структуру
        if {"strikes","days","cells","max_apr"}.issubset(proxy_res.keys()):
            data = proxy_res
        else:
            # или список raw items
            items = proxy_res.get("items") or proxy_res.get("list") or proxy_res.get("data") or []
            data = normalize(items, min_apr, duration_set, int(max_strikes))
        endpoint_used = f"Proxy: {PROXY_BASE}"
    except Exception as proxy_err:
        st.error(f"Ошибка прокси: {proxy_err}")
        st.stop()

st.caption(endpoint_used)

# ========= Render matrix =========
strikes, days, cells, max_apr = data["strikes"], data["days"], data["cells"], data["max_apr"]

# Держим прошлые APR по pid для стрелок
prev = st.session_state.setdefault("prev_apr_by_pid", {})

def cell_html(cell):
    apr = float(cell["apr"])
    pid = str(cell["pid"])
    delta = None
    if pid in prev:
        delta = apr - prev[pid]
    prev[pid] = apr
    # стрелки при |Δ| >= 0.05 п.п.
    arrow = ""
    if delta is not None and abs(delta) >= 0.05:
        arrow = "&#9650;" if delta > 0 else "&#9660;"
    style = 'background:#fff3cd;' if apr == max_apr else ''
    return f'<td style="border:1px solid #ddd;padding:6px;text-align:right;{style}"><span style="font-variant-numeric:tabular-nums">{apr:0.1f}{arrow}</span></td>'

rows = []
rows.append('<table style="border-collapse:collapse;width:100%;table-layout:fixed">')
# header
rows.append('<thead><tr><th style="position:sticky;left:0;background:#fafafa;border:1px solid #ddd;padding:6px;text-align:center">Days\\Str</th>')
for s in strikes:
    rows.append(f'<th style="border:1px solid #ddd;padding:6px;text-align:center">{s}</th>')
rows.append('</tr></thead><tbody>')
# body
for d in days:
    rows.append(f'<tr><th style="position:sticky;left:0;background:#fafafa;border:1px solid #ddd;padding:6px;text-align:center">{d}</th>')
    for s in strikes:
        cell = cells.get(str(s), {}).get(str(d))
        rows.append('<td style="border:1px solid #eee;padding:6px;"></td>' if not cell else cell_html(cell))
    rows.append('</tr>')
rows.append('</tbody></table>')

st.markdown("".join(rows), unsafe_allow_html=True)
st.caption(f"max APR: {max_apr:0.1f}% • updated {time.strftime('%H:%M:%S')} • автообновление {refresh_sec}s")

# Автообновление (JS)
st.markdown(f"<script>setTimeout(()=>location.reload(), {int(refresh_sec)*1000});</script>", unsafe_allow_html=True)