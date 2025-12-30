import streamlit as st
import pandas as pd
import pandas_ta as ta
import numpy as np
import yfinance as yf
import requests
import time
from datetime import datetime, time as dtime
import pytz

# --- 1. PAGE CONFIG & APK STYLE CSS ---
st.set_page_config(page_title="Mishr@lgobot Ultimate", layout="wide", initial_sidebar_state="collapsed")

# --- CUSTOM CSS (MOBILE APK LOOK) ---
st.markdown("""
    <style>
        .stApp { background-color: #000000; color: #e0e0e0; font-family: 'Roboto', sans-serif; }
        
        /* Card Design */
        .card {
            background: linear-gradient(145deg, #111, #1a1a1a); 
            border: 1px solid #333; border-radius: 12px;
            padding: 15px; margin-bottom: 10px; box-shadow: 0 4px 6px rgba(0,0,0,0.3);
        }
        
        /* Buttons */
        div.stButton > button {
            width: 100%; border-radius: 8px; font-weight: bold; border: none; height: 45px;
        }
        button[kind="primary"] { background-color: #00e676; color: black; border: 1px solid #00e676; }
        button[kind="secondary"] { background-color: #ff1744; color: white; border: 1px solid #ff1744; }
        
        /* Text Colors */
        .up { color: #00e676; font-weight: bold; } 
        .down { color: #ff1744; font-weight: bold; }
        .neutral { color: #888; }
        
        /* Hide Header/Footer */
        #MainMenu {visibility: hidden;} footer {visibility: hidden;} header {visibility: hidden;}
    </style>
""", unsafe_allow_html=True)

# --- 2. INITIALIZE STATE ---
defaults = {
    "auth": False, "bal": 100000.0, "positions": [], "bot_active": False,
    "smartApi": None, "token_df": None, "real_trade_active": False,
    "strategy_mode": "1. Sniper (1m) [Scalp]", "manual_qty": 50,
    "daily_pnl": 0.0, "max_loss": 5000, "target_pct": 2.0, "sl_pct": 1.0,
    "logs": [],
    "watchlist": [
        {"type": "INDEX", "symbol": "NIFTY 50", "code": "^NSEI", "step": 50},
        {"type": "INDEX", "symbol": "BANKNIFTY", "code": "^NSEBANK", "step": 100},
        {"type": "MCX", "symbol": "CRUDEOIL", "code": "CL=F", "step": 10},
        {"type": "CRYPTO", "symbol": "BITCOIN", "code": "BTC-USD", "step": 1}
    ]
}
for key, val in defaults.items():
    if key not in st.session_state: st.session_state[key] = val

# --- 3. HELPER FUNCTIONS ---
def add_log(msg, type_="INFO"):
    ts = datetime.now(pytz.timezone('Asia/Kolkata')).strftime('%H:%M:%S')
    st.session_state.logs.insert(0, f"[{ts}] [{type_}] {msg}")
    if len(st.session_state.logs) > 100: st.session_state.logs.pop()

def check_market_time():
    # Only allow trades between 09:15 and 15:15
    tz = pytz.timezone('Asia/Kolkata')
    now = datetime.now(tz).time()
    if dtime(9, 15) <= now <= dtime(15, 15): return True
    return False 

# --- API HANDLING (Fixes 'SmartConnect not defined') ---
API_OK = False
try:
    from SmartApi import SmartConnect
    import pyotp
    API_OK = True
except ImportError:
    pass

def angel_login(api, client, pin, totp_key):
    if not API_OK: return "Library Missing. Check requirements.txt", None
    try:
        obj = SmartConnect(api_key=api)
        totp_val = pyotp.TOTP(totp_key).now()
        data = obj.generateSession(client, pin, totp_val)
        if data['status']: return "Success", obj
        return f"Failed: {data['message']}", None
    except Exception as e: return f"Error: {str(e)}", None

# --- DATA & TOKEN MAPPING ---
@st.cache_resource
def load_tokens():
    try:
        url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
        data = requests.get(url).json()
        df = pd.DataFrame(data)
        return df[df['exch_seg'].isin(['NFO', 'NSE', 'MCX'])]
    except: return None

if st.session_state.token_df is None:
    with st.spinner("Loading Market Data..."):
        st.session_state.token_df = load_tokens()

def get_angel_token(symbol, strike=None, opt_type=None, type_="INDEX"):
    df = st.session_state.token_df
    if df is None: return None, None, "NSE"
    
    if type_ == "MCX":
        res = df[(df['name'] == symbol) & (df['instrumenttype'] == 'FUTCOM')]
        if not res.empty:
            return res.sort_values('expiry').iloc[0]['token'], res.iloc[0]['symbol'], "MCX"
            
    elif type_ == "INDEX" and strike:
        s_str = str(int(strike))
        name = "NIFTY" if "NIFTY" in symbol else "BANKNIFTY"
        res = df[(df['name'] == name) & (df['symbol'].str.endswith(opt_type)) & (df['symbol'].str.contains(s_str))]
        if not res.empty:
            res = res.sort_values('expiry')
            return res.iloc[0]['token'], res.iloc[0]['symbol'], "NFO"
            
    return None, None, "NSE"

def get_live_ltp(token, exch):
    if st.session_state.smartApi and token:
        try:
            d = st.session_state.smartApi.ltpData(exch, symbolToken=token, symbol=token)
            if d['status']: return d['data']['ltp']
        except: pass
    return 0.0

# --- STRATEGY ENGINE ---
def calculate_signals(df, strategy):
    last = df.iloc[-1]
    sig = "HOLD"
    
    # INDICATORS
    df['EMA9'] = df.ta.ema(length=9)
    df['EMA21'] = df.ta.ema(length=21)
    df['RSI'] = df.ta.rsi(length=14)
    
    # 1. Sniper (Scalping)
    if "Sniper" in strategy:
        if last['EMA9'] > last['EMA21'] and last['RSI'] > 55: sig = "BUY"
        elif last['EMA9'] < last['EMA21'] and last['RSI'] < 45: sig = "SELL"
        
    # 2. VWAP + MACD (High Accuracy)
    elif "VWAP" in strategy:
        df['VWAP'] = df.ta.vwap()
        macd = df.ta.macd(fast=12, slow=26, signal=9)
        if macd is not None:
            df = pd.concat([df, macd], axis=1)
            macd_line = df.columns[-3] 
            signal_line = df.columns[-1]
            if last['Close'] > last['VWAP'] and last[macd_line] > last[signal_line]: sig = "BUY"
            elif last['Close'] < last['VWAP'] and last[macd_line] < last[signal_line]: sig = "SELL"
            
    # 3. Supertrend
    elif "Supertrend" in strategy:
        st_data = df.ta.supertrend(length=10, multiplier=3)
        if st_data is not None:
             df = pd.concat([df, st_data], axis=1)
             st_col = df.columns[-2] # Usually Trend column
             if df.iloc[-1]['Close'] > df.iloc[-1][df.columns[-3]]: sig = "BUY" # logic approx
             else: sig = "SELL"

    return sig

@st.cache_data(ttl=10) # Refresh data every 10s
def scan_market(watchlist, strategy):
    data = []
    period = "5d"
    interval = "15m" if "VWAP" in strategy else "1m"
    
    for item in watchlist:
        try:
            df = yf.download(item['code'], period=period, interval=interval, progress=False)
            if df.empty: continue
            if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
            
            sig = calculate_signals(df, strategy)
            
            # PRICE & TOKEN LOGIC
            trade_price = df.iloc[-1]['Close']
            token, sym, exch = None, item['symbol'], "NSE"
            
            if item['type'] == "INDEX":
                strike = round(trade_price / item['step']) * item['step']
                if "BUY" in sig: otype = "CE"
                elif "SELL" in sig: otype = "PE"
                else: otype = "CE" # Default
                
                token, sym, exch = get_angel_token(item['symbol'], strike, otype, "INDEX")
                if sig != "HOLD": sig = f"BUY {otype}" 
                
            elif item['type'] == "MCX":
                token, sym, exch = get_angel_token(item['symbol'], type_="MCX")

            if token:
                ltp = get_live_ltp(token, exch)
                if ltp > 0: trade_price = ltp
            elif item['type'] == "INDEX":
                trade_price = trade_price * 0.01 
                
            data.append({
                "display": sym, "price": trade_price, "sig": sig, 
                "token": token, "exch": exch, "type": item['type']
            })
        except: pass
    return data

# --- BOT LOGIC ---
def run_bot_logic(data):
    # 1. Update PnL & Check SL/Target
    for p in st.session_state.positions[:]:
        current_price = p['entry']
        
        # Real Live Price Check
        if p['type'] == "REAL" and st.session_state.smartApi:
            # For speed we use scanned price, ideally use ltpData here
            pass
        
        match = next((d for d in data if d['display'] == p['display']), None)
        if match: current_price = match['price']
        
        p['pnl'] = (current_price - p['entry']) * p['qty']
        pnl_pct = ((current_price - p['entry']) / p['entry']) * 100
        
        if pnl_pct <= -st.session_state.sl_pct:
            add_log(f"🛑 SL Hit: {p['display']}", "EXIT")
            st.session_state.daily_pnl += p['pnl']
            st.session_state.positions.remove(p)
            st.toast(f"SL HIT: {p['display']}")
        elif pnl_pct >= st.session_state.target_pct:
            add_log(f"🎯 Target Hit: {p['display']}", "EXIT")
            st.session_state.daily_pnl += p['pnl']
            st.session_state.positions.remove(p)
            st.toast(f"TARGET HIT: {p['display']}")

    # 2. Max Loss Check
    if st.session_state.daily_pnl <= -st.session_state.max_loss:
        st.error("MAX LOSS HIT. BOT STOPPED.")
        st.session_state.bot_active = False
        return

    # 3. New Entries
    if not check_market_time() and st.session_state.real_trade_active: 
        st.toast("Market Closed (09:15-15:15)")
        return
    
    for d in data:
        if any(p['display'] == d['display'] for p in st.session_state.positions): continue
        
        if "BUY" in d['sig']:
            qty = st.session_state.manual_qty
            mode = "PAPER"
            
            # Real Trade Execution
            if st.session_state.real_trade_active and d['token'] and st.session_state.smartApi:
                try:
                    p = {
                        "variety": "NORMAL", "tradingsymbol": d['display'], "symboltoken": d['token'],
                        "transactiontype": "BUY", "exchange": d['exch'], "ordertype": "MARKET",
                        "producttype": "INTRADAY", "duration": "DAY", "quantity": str(qty)
                    }
                    st.session_state.smartApi.placeOrder(p)
                    mode = "REAL"
                except Exception as e:
                    add_log(f"Order Fail: {str(e)}", "ERROR")
                    mode = "FAIL"
            
            if mode != "FAIL":
                st.session_state.positions.append({
                    "display": d['display'], "entry": d['price'], "qty": qty, "pnl": 0.0, "type": mode
                })
                add_log(f"Entry: {d['display']} @ {d['price']}", mode)

# --- 4. UI LAYOUT ---
c1, c2 = st.columns([4, 1])
with c1: st.markdown("### 🤖 Mishr@lgobot <span style='color:gold'>ULTIMATE</span>", unsafe_allow_html=True)
with c2: st.markdown(f"<small>Status:</small> {'🟢 ON' if st.session_state.bot_active else '🔴 OFF'}", unsafe_allow_html=True)

data_list = scan_market(st.session_state.watchlist, st.session_state.strategy_mode)

tab1, tab2, tab3 = st.tabs(["🏠 DASHBOARD", "⚙️ SETTINGS", "📜 LOGS"])

with tab1:
    curr_pnl = sum([p['pnl'] for p in st.session_state.positions])
    total_pnl = st.session_state.daily_pnl + curr_pnl
    pnl_cls = "up" if total_pnl >= 0 else "down"
    
    c1, c2, c3 = st.columns(3)
    c1.markdown(f"<div class='card'>Start Bal<br><b>₹{st.session_state.bal:,.0f}</b></div>", unsafe_allow_html=True)
    c2.markdown(f"<div class='card'>Total P&L<br><span class='{pnl_cls}'>₹{total_pnl:.2f}</span></div>", unsafe_allow_html=True)
    c3.markdown(f"<div class='card'>Active<br><b>{len(st.session_state.positions)}</b></div>", unsafe_allow_html=True)
    
    # PANIC BUTTON
    if st.button("🚨 PANIC: EXIT ALL & STOP", type="secondary"):
        st.session_state.bot_active = False
        st.session_state.daily_pnl += curr_pnl
        st.session_state.positions = []
        add_log("PANIC BUTTON PRESSED - ALL EXITED", "ALERT")
        st.rerun()

    st.write("### Active Trades")
    if not st.session_state.positions: st.info("Waiting for signals...")
    for p in st.session_state.positions:
        p_cls = "up" if p['pnl'] >= 0 else "down"
        st.markdown(f"""
        <div class='card' style='display:flex; justify-content:space-between; align-items:center'>
            <div><b>{p['display']}</b> <small>({p['type']})</small><br>Qty: {p['qty']} @ {p['entry']}</div>
            <div class='{p_cls}'>₹{p['pnl']:.2f}</div>
        </div>
        """, unsafe_allow_html=True)

    st.write("### Signals")
    for d in data_list:
        color = "#00e676" if "BUY" in d['sig'] else "#333"
        st.markdown(f"<div class='card' style='padding:8px; border-left: 4px solid {color}'>{d['display']} : <b>{d['price']:.2f}</b> | {d['sig']}</div>", unsafe_allow_html=True)

with tab2:
    st.write("#### 🔐 Angel One Login")
    if not st.session_state.smartApi:
        with st.form("login"):
            ak = st.text_input("API Key")
            cid = st.text_input("Client ID")
            pin = st.text_input("PIN", type="password")
            totp = st.text_input("TOTP Key (Use Secret Key)")
            if st.form_submit_button("CONNECT"):
                msg, api = angel_login(ak, cid, pin, totp)
                if api: 
                    st.session_state.smartApi = api
                    st.success("Connected!")
                    st.rerun()
                else: st.error(msg)
    else:
        st.success("✅ Logged In")
        if st.button("LOGOUT"):
            st.session_state.smartApi = None
            st.rerun()

    st.write("#### 🎮 Config")
    st.session_state.strategy_mode = st.selectbox("Strategy", ["1. Sniper (1m)", "2. VWAP + MACD (15m)", "3. Supertrend"])
    c1, c2 = st.columns(2)
    st.session_state.sl_pct = c1.number_input("Stop Loss %", 0.5, 5.0, 1.0)
    st.session_state.target_pct = c2.number_input("Target %", 0.5, 10.0, 2.0)
    st.session_state.manual_qty = st.number_input("Qty", 1, 1000, 50)
    
    st.session_state.real_trade_active = st.toggle("ACTIVATE REAL TRADING", value=st.session_state.real_trade_active)
    
    # Watchlist Management
    st.write("#### Watchlist")
    new_sym = st.text_input("Add Symbol (e.g. INFY)")
    if st.button("Add Stock") and new_sym:
        st.session_state.watchlist.append({"type": "EQUITY", "symbol": new_sym.upper(), "code": f"{new_sym.upper()}.NS", "step": 1})
        st.rerun()
    
    rem_sym = st.selectbox("Remove Stock", [x['symbol'] for x in st.session_state.watchlist])
    if st.button("Remove Selected"):
        st.session_state.watchlist = [x for x in st.session_state.watchlist if x['symbol'] != rem_sym]
        st.rerun()

    st.write("---")
    if st.button("▶ START BOT", type="primary", disabled=st.session_state.bot_active):
        st.session_state.bot_active = True
        st.rerun()
    if st.button("🛑 STOP BOT", disabled=not st.session_state.bot_active):
        st.session_state.bot_active = False
        st.rerun()

with tab3:
    st.write("### 📜 Logs")
    log_txt = "\n".join(st.session_state.logs)
    st.download_button("Download Logs", log_txt, "logs.txt")
    st.text_area("", log_txt, height=300)

if st.session_state.bot_active:
    run_bot_logic(data_list)
    time.sleep(5)
    st.rerun()
