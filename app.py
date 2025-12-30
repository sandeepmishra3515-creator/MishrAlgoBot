import streamlit as st
import pandas as pd
import pandas_ta as ta
import numpy as np
import yfinance as yf
import requests
import time
from datetime import datetime, time as dtime
import pytz

# --- 1. PAGE CONFIG & APK STYLE ---
st.set_page_config(page_title="Mishr@lgobot Final", layout="wide", initial_sidebar_state="collapsed")

st.markdown("""
    <style>
        .stApp { background-color: #000000; color: #e0e0e0; font-family: 'Roboto', sans-serif; }
        .card { 
            background: linear-gradient(145deg, #111, #1a1a1a); 
            border: 1px solid #333; border-radius: 12px; padding: 15px; margin-bottom: 10px; 
        }
        div.stButton > button { width: 100%; border-radius: 8px; font-weight: bold; border: none; height: 45px; }
        button[kind="primary"] { background-color: #00e676; color: black; }
        button[kind="secondary"] { background-color: #ff1744; color: white; }
        .bull { color: #00e676; font-weight: bold; } 
        .bear { color: #ff1744; font-weight: bold; }
        #MainMenu {visibility: hidden;} footer {visibility: hidden;} header {visibility: hidden;}
    </style>
""", unsafe_allow_html=True)

# --- 2. STATE MANAGEMENT ---
defaults = {
    "auth": False, "bal": 100000.0, "positions": [], "bot_active": False,
    "smartApi": None, "token_df": None, "real_trade_active": False,
    "strategy_mode": "1. Sniper (1m)", "manual_qty": 50,
    "daily_pnl": 0.0, "max_loss": 5000, "target_pct": 2.0, "sl_pct": 1.0,
    "logs": [],
    "watchlist": [
        {"type": "INDEX", "symbol": "NIFTY 50", "code": "^NSEI", "step": 50},
        {"type": "INDEX", "symbol": "BANKNIFTY", "code": "^NSEBANK", "step": 100},
        {"type": "MCX", "symbol": "CRUDEOIL", "code": "CL=F", "step": 10},
        {"type": "CRYPTO", "symbol": "BITCOIN", "code": "BTC-USD", "step": 1},
        {"type": "EQUITY", "symbol": "RELIANCE", "code": "RELIANCE.NS", "step": 1}
    ]
}
for key, val in defaults.items():
    if key not in st.session_state: st.session_state[key] = val

# --- 3. HELPERS ---
def add_log(msg, type_="INFO"):
    ts = datetime.now(pytz.timezone('Asia/Kolkata')).strftime('%H:%M:%S')
    st.session_state.logs.insert(0, f"[{ts}] [{type_}] {msg}")
    if len(st.session_state.logs) > 100: st.session_state.logs.pop()

def check_market_time(exch_type):
    tz = pytz.timezone('Asia/Kolkata')
    now = datetime.now(tz).time()
    if exch_type == "CRYPTO": return True
    elif exch_type == "MCX": return dtime(9, 0) <= now <= dtime(23, 30)
    elif exch_type in ["INDEX", "EQUITY"]: return dtime(9, 15) <= now <= dtime(15, 30)
    return False

# --- 4. API & TOKENS ---
API_OK = False
try:
    from SmartApi import SmartConnect
    import pyotp
    API_OK = True
except ImportError: pass

def angel_login(api, client, pin, totp_key):
    if not API_OK: return "Library Missing", None
    try:
        obj = SmartConnect(api_key=api)
        totp_val = pyotp.TOTP(totp_key).now()
        data = obj.generateSession(client, pin, totp_val)
        if data['status']: return "Success", obj
        return f"Failed: {data['message']}", None
    except Exception as e: return f"Error: {str(e)}", None

@st.cache_resource
def load_tokens():
    try:
        url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
        data = requests.get(url).json()
        df = pd.DataFrame(data)
        return df[df['exch_seg'].isin(['NFO', 'NSE', 'MCX'])]
    except: return None

if st.session_state.token_df is None:
    with st.spinner("System Initializing..."):
        st.session_state.token_df = load_tokens()

def get_angel_token(symbol, strike=None, opt_type=None, type_="EQUITY"):
    df = st.session_state.token_df
    if df is None: return None, None, "NSE"
    if type_ == "MCX":
        res = df[(df['name'] == symbol) & (df['instrumenttype'] == 'FUTCOM')]
        if not res.empty: return res.sort_values('expiry').iloc[0]['token'], res.iloc[0]['symbol'], "MCX"
    elif type_ == "INDEX" and strike:
        s_str = str(int(strike))
        name = "NIFTY" if "NIFTY" in symbol else "BANKNIFTY"
        res = df[(df['name'] == name) & (df['symbol'].str.endswith(opt_type)) & (df['symbol'].str.contains(s_str))]
        if not res.empty: return res.sort_values('expiry').iloc[0]['token'], res.iloc[0]['symbol'], "NFO"
    elif type_ == "EQUITY":
        res = df[(df['name'] == symbol) & (df['exch_seg'] == 'NSE') & (df['symbol'].str.endswith('-EQ'))]
        if not res.empty: return res.iloc[0]['token'], res.iloc[0]['symbol'], "NSE"
    return None, None, "NSE"

def get_live_ltp(token, exch):
    if st.session_state.smartApi and token:
        try:
            d = st.session_state.smartApi.ltpData(exch, symbolToken=token, symbol=token)
            if d['status']: return d['data']['ltp']
        except: pass
    return 0.0

# --- 5. STRATEGY ENGINE ---
def calculate_signals(df, strategy):
    last = df.iloc[-1]
    sig = "HOLD"
    df['EMA9'] = df.ta.ema(length=9)
    df['EMA21'] = df.ta.ema(length=21)
    df['RSI'] = df.ta.rsi(length=14)
    df['VWAP'] = df.ta.vwap()
    
    if "Sniper" in strategy:
        if last['EMA9'] > last['EMA21'] and last['RSI'] > 55: sig = "BUY"
        elif last['EMA9'] < last['EMA21'] and last['RSI'] < 45: sig = "SELL"
    elif "Momentum" in strategy:
        if last['Close'] > last['EMA9']: sig = "BUY"
        else: sig = "SELL"
    elif "Golden" in strategy:
        if last['EMA9'] > last['EMA21']: sig = "BUY"
        else: sig = "SELL"
    elif "Supertrend" in strategy:
        st_data = df.ta.supertrend(length=10, multiplier=3)
        if st_data is not None:
             df = pd.concat([df, st_data], axis=1)
             if df.iloc[-1]['Close'] > df.iloc[-1][df.columns[-2]]: sig = "BUY"
             else: sig = "SELL"
    elif "VWAP" in strategy:
        macd = df.ta.macd(fast=12, slow=26, signal=9)
        if macd is not None:
            df = pd.concat([df, macd], axis=1)
            if last['Close'] > last['VWAP'] and last[df.columns[-3]] > last[df.columns[-1]]: sig = "BUY"
            elif last['Close'] < last['VWAP'] and last[df.columns[-3]] < last[df.columns[-1]]: sig = "SELL"
    elif "Volume" in strategy:
        vol_avg = df['Volume'].rolling(20).mean().iloc[-1]
        if last['Volume'] > (vol_avg * 2):
            if last['Close'] > last['Open']: sig = "BUY"
            else: sig = "SELL"
    return sig

@st.cache_data(ttl=10)
def scan_market(watchlist, strategy):
    data = []
    interval = "1m" if "Sniper" in strategy else "15m" if "VWAP" in strategy else "5m"
    for item in watchlist:
        try:
            df = yf.download(item['code'], period="5d", interval=interval, progress=False)
            if df.empty: continue
            if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
            sig = calculate_signals(df, strategy)
            trade_price = df.iloc[-1]['Close']
            token, sym, exch = None, item['symbol'], "NSE"
            
            if item['type'] == "INDEX":
                strike = round(trade_price / item['step']) * item['step']
                otype = "CE" if "BUY" in sig else "PE"
                token, sym, exch = get_angel_token(item['symbol'], strike, otype, "INDEX")
                if sig != "HOLD": sig = f"BUY {otype}"
                trade_price *= 0.01 
            elif item['type'] == "MCX":
                token, sym, exch = get_angel_token(item['symbol'], type_="MCX")
            elif item['type'] == "EQUITY":
                token, sym, exch = get_angel_token(item['symbol'], type_="EQUITY")

            if token:
                ltp = get_live_ltp(token, exch)
                if ltp > 0: trade_price = ltp
                
            data.append({
                "display": sym, "price": trade_price, "sig": sig, 
                "token": token, "exch": exch, "type": item['type'],
                "change": ((df.iloc[-1]['Close'] - df.iloc[0]['Open'])/df.iloc[0]['Open'])*100
            })
        except: pass
    return data

# --- 6. UI TABS ---
c1, c2 = st.columns([4, 1])
with c1: st.markdown("### 🤖 Mishr@lgobot <span style='color:gold'>FINAL</span>", unsafe_allow_html=True)
with c2: st.markdown(f"<small>Status:</small> {'🟢 ON' if st.session_state.bot_active else '🔴 OFF'}", unsafe_allow_html=True)

data_list = scan_market(st.session_state.watchlist, st.session_state.strategy_mode)

tab1, tab2, tab3, tab4 = st.tabs(["🏠 DASHBOARD", "🔍 SCREENER & FII", "⚙️ CONFIG", "📜 LOGS"])

with tab1:
    curr_pnl = sum([p['pnl'] for p in st.session_state.positions])
    total_pnl = st.session_state.daily_pnl + curr_pnl
    cls = "bull" if total_pnl >= 0 else "bear"
    
    c1, c2, c3 = st.columns(3)
    c1.markdown(f"<div class='card'>Wallet<br><b>₹{st.session_state.bal:,.0f}</b></div>", unsafe_allow_html=True)
    c2.markdown(f"<div class='card'>P&L<br><span class='{cls}'>₹{total_pnl:.2f}</span></div>", unsafe_allow_html=True)
    c3.markdown(f"<div class='card'>Active<br><b>{len(st.session_state.positions)}</b></div>", unsafe_allow_html=True)
    
    if st.button("🚨 PANIC: EXIT ALL", type="secondary"):
        st.session_state.bot_active = False
        st.session_state.positions = []
        add_log("PANIC EXIT TRIGGERED", "ALERT")
        st.rerun()

    st.write("### Signals")
    if data_list:
        for d in data_list:
            col = "#00e676" if "BUY" in d['sig'] else "#ff1744" if "SELL" in d['sig'] else "#333"
            st.markdown(f"<div class='card' style='border-left:4px solid {col}'><b>{d['display']}</b>: {d['price']:.2f} | {d['sig']}</div>", unsafe_allow_html=True)
    else:
        st.info("⏳ Waiting for Market Data...")

with tab2:
    st.info("FII/DII Data & Screener")
    
    # --- ERROR FIX: CHECK IF DATA EXISTS BEFORE DATAFRAME ---
    if data_list:
        df_screen = pd.DataFrame(data_list)
        if not df_screen.empty:
            st.dataframe(df_screen[['display', 'price', 'sig', 'type', 'change']], use_container_width=True)
            
            st.write("#### FII/DII Trend Simulation")
            nifty_chg = df_screen.iloc[0]['change']
            sentiment = "BULLISH (Buying)" if nifty_chg > 0 else "BEARISH (Selling)"
            st.metric("FII Sentiment", sentiment, f"{nifty_chg:.2f}%")
        else:
            st.warning("Data loading...")
    else:
        st.warning("⏳ Data is loading... please wait.")

with tab3:
    st.write("#### 🔐 Login")
    if not st.session_state.smartApi:
        with st.form("log"):
            ak = st.text_input("API Key")
            cid = st.text_input("Client ID")
            pin = st.text_input("PIN", type="password")
            totp = st.text_input("TOTP Secret")
            if st.form_submit_button("CONNECT"):
                msg, api = angel_login(ak, cid, pin, totp)
                if api: st.session_state.smartApi = api; st.rerun()
                else: st.error(msg)
    else: st.success("Logged In"); st.button("LOGOUT", on_click=lambda: st.session_state.update(smartApi=None))

    st.write("#### 🎮 Strategy")
    st.session_state.strategy_mode = st.selectbox("Mode", [
        "1. Sniper (1m) [Scalp]", "2. Momentum (5m) [Trend]", "3. Supertrend (Pro)", 
        "4. Golden Cross (Pro)", "5. VWAP + MACD (High Acc)", "6. Volume Shock"
    ])
    
    c1, c2 = st.columns([3,1])
    new = c1.text_input("Add Stock")
    if c2.button("Add") and new:
        st.session_state.watchlist.append({"type": "EQUITY", "symbol": new.upper(), "code": f"{new.upper()}.NS", "step": 1})
        st.rerun()
        
    rem = st.selectbox("Remove", [x['symbol'] for x in st.session_state.watchlist])
    if st.button("Delete"):
        st.session_state.watchlist = [x for x in st.session_state.watchlist if x['symbol'] != rem]
        st.rerun()
    
    st.write("---")
    st.session_state.real_trade_active = st.toggle("REAL TRADING", value=st.session_state.real_trade_active)
    if st.button("▶ START", type="primary"): st.session_state.bot_active = True; st.rerun()
    if st.button("🛑 STOP"): st.session_state.bot_active = False; st.rerun()

with tab4:
    st.download_button("Download Logs", "\n".join(st.session_state.logs), "logs.txt")
    st.text_area("Logs", "\n".join(st.session_state.logs), height=300)

# --- 7. BOT LOOP ---
if st.session_state.bot_active:
    for d in data_list:
        if not check_market_time(d['type']): continue
        
        # Entry
        if "BUY" in d['sig'] and not any(p['display'] == d['display'] for p in st.session_state.positions):
            qty = st.session_state.manual_qty
            mode = "PAPER"
            if st.session_state.real_trade_active and d['token'] and st.session_state.smartApi:
                try:
                    p = {"variety":"NORMAL", "tradingsymbol":d['display'], "symboltoken":d['token'], "transactiontype":"BUY", "exchange":d['exch'], "ordertype":"MARKET", "producttype":"INTRADAY", "duration":"DAY", "quantity":str(qty)}
                    st.session_state.smartApi.placeOrder(p); mode = "REAL"
                except: mode = "FAIL"
            
            if mode != "FAIL":
                st.session_state.positions.append({"display":d['display'], "entry":d['price'], "qty":qty, "pnl":0.0, "type":mode})
                add_log(f"Entry: {d['display']}", mode)
    
    # Exit (SL/Target)
    for p in st.session_state.positions[:]:
        curr = p['entry']
        match = next((x for x in data_list if x['display'] == p['display']), None)
        if match: curr = match['price']
        
        p['pnl'] = (curr - p['entry']) * p['qty']
        pct = ((curr - p['entry']) / p['entry']) * 100
        
        if pct <= -st.session_state.sl_pct or pct >= st.session_state.target_pct:
            st.session_state.daily_pnl += p['pnl']
            st.session_state.positions.remove(p)
            add_log(f"Exit {p['display']} PnL: {p['pnl']}", "EXIT")

    time.sleep(5)
    st.rerun()
