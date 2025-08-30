# app.py — Streamlit: APR Matrix с обходом 451 через ротацию эндпоинтов Binance
import os, time, hmac, hashlib, json
from urllib import request, parse, error
import streamlit as st

# ===== Настройка ключей =====
KEY    = st.secrets.get("BINANCE_KEY", "")
SECRET = st.secrets.get("BINANCE_SECRET", "")
# Переопределить список баз можно секретом BINANCE_BASES="https://api-gcp.binance.com,https://api4.binance.com"
BASES  = [u.strip() for u in st.secrets.get("BINANCE_BASES", "").split(",") if u.strip()] or [
    "https://api-gcp.binance.com",
    "https://api4.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
    "https://api.binance.com",  # оставим последним
]

USER_AGENT = "apr-onefile-rotating/1.0"

st.set_page_config(page_title="APR Matrix (Binance Dual Investment)", layout="wide")
st.title("APR Matrix — Binance Dual Investment (ETH)")

if not KEY or not SECRET:
    st.error("Добавь BINANCE_KEY и BINANCE_SECRET в Settings → Secrets (только read-only).")
    st.stop()

# Служебная переменная: какой базовый URL сработал
if "LAST_BASE" not in st.session_state:
    st.session_state["LAST_BASE"] = ""

# ===== HTTP helpers с ротацией баз =====
def http_get(url, headers=None, timeout=15):
    req = request.Request(url, headers=headers or {"User-Agent": USER_AGENT})
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            return resp.getcode(), resp.read().decode("utf-8")
    except error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "ignore")
        except Exception:
            pass
        return e.code, body
    except Exception as e:
        raise

def http_get_any(path_with_query: str, headers=None, timeout=15):
    """
    Перебирает BASES и возвращает (code, body) с первого успешного ответа (2xx).
    Пропускает 451 и ответы, где в тексте упомянуто 'Eligibility'/'restricted location'.
    """
    last_msg = None
    for base in BASES:
        try:
            code, body = http_get(base + path_with_query, headers=headers, timeout=timeout)
            # Binance иногда отдаёт 403 с текстом про Eligibility — считаем как блокировку
            restricted = ("Eligibility" in (body or "")) or ("restricted location" in (body or "") ) or (code == 451)
            if 200 <= (code or 0) < 300 and not restricted:
                st.session_state["LAST_BASE"] = base
                return code, body
            # сохраняем последнюю ошибку и пробуем следующий base
            last_msg = f"{base} → HTTP {code}; {body[:160].replace(chr(10),' ')}"
        except Exception as e:
            last_msg = f"{base} → {type(e).__name__}: {e}"
    raise RuntimeError(last_msg or "All endpoints failed")

def server_time_ms():
    code, body = http_get_any("/api/v3/time")
    if code != 200:
        raise RuntimeError(f"time error: {code} {body[:200]}")
    return json.loads(body)["serverTime"]

def sign_params(params: dict):
    qs  = parse.urlencode(params)
    sig = hmac.new(SECRET.encode(), qs.encode(), hashlib.sha256).hexdigest()
    headers = {"X-MBX-APIKEY": KEY, "User-Agent": USER_AGENT}
    return f"{qs}&signature={sig}", headers

@st.cache_data(ttl=2)  # кэш на 2 секунды, чтобы не долбить API
def fetch_products_all(option_type: str, exercised: str, invest: str, max_pages=3):
    ts = server_time_ms()
    out = []
    for idx in range(1, max_pages+1):
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
        code, body = http_get_any(f"/sapi/v1/dci/product/list?{qs}", headers=hdr)
        if code != 200:
            raise RuntimeError(f"HTTP {code}: {body[:300]}")
        data = json.loads(body)
        page = data.get("list") or data.get("data") or []
        out.extend(page)
        if len(page) < 100:
            break
    return out

def normalize(items, min_apr_pct, duration_set, max_strikes, strike_prec=2):
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

# ===== UI =====
c1, c2, c3, c4 = st.columns([1.1,1.1,1.1,2])
with c1: option_type = st.selectbox("Option", ["PUT","CALL"], index=0)
with c2: exercised   = st.text_input("Exercised", "ETH")
with c3: invest      = st.text_input("Invest", "USDT")
with c4: durations_s = st.text_input("Durations (дни, через запятую)", "3,7,14")
duration_set = [int(x) for x in durations_s.split(",") if x.strip().isdigit()]
min_apr = st.number_input("Min APR, %", value=0.0, step=0.1)
max_strikes = st.number_input("Max strikes", 1, 20, 5)
refresh_sec = st.slider("Автообновление, сек", 2, 30, 5)

status = st.empty()
try:
    items = fetch_products_all(option_type, exercised, invest)
    data  = normalize(items, min_apr, duration_set, max_strikes)
    if st.session_state["LAST_BASE"]:
        status.caption(f"Endpoint: {st.session_state['LAST_BASE']}")
except Exception as e:
    st.error(f"Ошибка запроса: {e}")
    st.stop()

strikes, days, cells, max_apr = data["strikes"], data["days"], data["cells"], data["max_apr"]

# Рендерим HTML-таблицу (подсветка максимума)
rows = []
rows.append('<table style="border-collapse:collapse;width:100%;table-layout:fixed">')
rows.append('<thead><tr><th style="position:sticky;left:0;background:#fafafa;border:1px solid #ddd;padding:6px;text-align:center">Days\\Str</th>')
for s in strikes:
    rows.append(f'<th style="border:1px solid #ddd;padding:6px;text-align:center">{s}</th>')
rows.append('</tr></thead><tbody>')
for d in days:
    rows.append(f'<tr><th style="position:sticky;left:0;background:#fafafa;border:1px solid #ddd;padding:6px;text-align:center">{d}</th>')
    for s in strikes:
        cell = cells.get(str(s), {}).get(str(d))
        if not cell:
            rows.append('<td style="border:1px solid #eee;padding:6px;"></td>')
        else:
            apr = float(cell["apr"])
            style = 'background:#fff3cd;' if apr == max_apr else ''
            rows.append(f'<td style="border:1px solid #ddd;padding:6px;text-align:right;{style}">{apr:0.1f}</td>')
    rows.append('</tr>')
rows.append('</tbody></table>')
st.markdown("".join(rows), unsafe_allow_html=True)
st.caption(f"max APR: {max_apr:0.1f}% • updated {time.strftime('%H:%M:%S')} • автообновление {refresh_sec}s")

# Автообновление страницы
st.markdown(f"<script>setTimeout(()=>location.reload(), {int(refresh_sec)*1000});</script>", unsafe_allow_html=True)