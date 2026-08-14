import streamlit as st
import requests
import pandas as pd
import math
import time
from datetime import datetime
import pytz
import psycopg2
import plotly.graph_objects as go
# ==========================================
# 1. CONFIGURATION & DATABASE    streamlit run GammaDashboard.py
# ==========================================
# st.set_page_config(page_title="Institutional Gamma Command Center", layout="wide")

CLIENT_ID = "1107312463"
ACCESS_TOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzUxMiJ9.eyJpc3MiOiJkaGFuIiwicGFydG5lcklkIjoiIiwiZXhwIjoxNzg2NzE4OTIyLCJpYXQiOjE3ODY2MzI1MjIsInRva2VuQ29uc3VtZXJUeXBlIjoiU0VMRiIsIndlYmhvb2tVcmwiOiIiLCJkaGFuQ2xpZW50SWQiOiIxMTA3MzEyNDYzIn0.k1vcpQc4-T-U411MxZV3CmoJuoZB53znydCnn5B2Ii67G_Sqb_gkwex6jKBFt-ItglJzgrPzz8nMQ39-IXwZnQ"

UNDERLYING_SCRIP = 13  # Nifty 50
UNDERLYING_SEG = "IDX_I" 
EXPIRY_DATE = "2026-08-18" 
LOT_SIZE = 65 

DB_CONFIG = {
    "dbname": "gamma_db",
    "user": "postgres",
    "password": "root",
    "host": "localhost",
    "port": "5432"
}


def init_db():
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS gamma_metrics (
                id SERIAL PRIMARY KEY,
                timestamp TIMESTAMP NOT NULL,
                spot_price FLOAT,
                zero_gamma FLOAT,
                call_wall FLOAT,
                put_wall FLOAT,
                cw_velocity FLOAT,
                pw_velocity FLOAT,
                vol_skew FLOAT,
                iv_vwap FLOAT
            )
        """)
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        st.sidebar.warning(f"PostgreSQL Offline: DB Logging Disabled ({e})")

def log_to_postgres(metrics):
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor()
        query = """
            INSERT INTO gamma_metrics 
            (timestamp, spot_price, zero_gamma, call_wall, put_wall, cw_velocity, pw_velocity, vol_skew, iv_vwap)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        values = (
            datetime.now(), metrics['spot'], metrics['zero_gamma'], 
            metrics['call_wall'], metrics['put_wall'], 0.0, 
            0.0, metrics['vol_skew'], metrics['iv_vwap']
        )
        cur.execute(query, values)
        conn.commit()
        cur.close()
        conn.close()
    except Exception:
        pass

init_db()

# ==========================================
# 2. SESSION STATE FOR ROLLING & DAILY METRICS
# ==========================================
ist = pytz.timezone('Asia/Kolkata')
current_date = datetime.now(ist).date()

if 'oi_snapshots' not in st.session_state:
    st.session_state.oi_snapshots = {}
if 'vol_snapshots' not in st.session_state:
    st.session_state.vol_snapshots = {}

# Daily PCR & Velocity Extrema Tracker + Dynamic OI Baseline
if 'pcr_tracker' not in st.session_state or st.session_state.pcr_tracker.get('date') != current_date:
    st.session_state.pcr_tracker = {
        'date': current_date,
        'oi_max': {'val': 0.0, 'time': '--:--'},
        'oi_min': {'val': 999.0, 'time': '--:--'},
        'vol_max': {'val': 0.0, 'time': '--:--'},
        'vol_min': {'val': 999.0, 'time': '--:--'},
        'chg_max': {'val': 0.0, 'time': '--:--'},
        'chg_min': {'val': 999.0, 'time': '--:--'},
        'vel_max': {'val': 0.0, 'time': '--:--'},
        'vel_min': {'val': 9999.0, 'time': '--:--'},
        'intra_vel_max': {'val': 0.0, 'time': '--:--'},
        'intra_vel_min': {'val': 9999.0, 'time': '--:--'}
    }
    st.session_state.daily_baseline_oi = {}

def get_rolling_metric(snapshot_dict, strike_key, current_val, window_minutes=15):
    current_time = time.time()
    snapshots = snapshot_dict.setdefault(strike_key, [])
    
    snapshots.append({'time': current_time, 'val': current_val})
    cutoff_time = current_time - (window_minutes * 60)
    
    snapshot_dict[strike_key] = [s for s in snapshots if s['time'] >= cutoff_time]
    oldest_val = snapshot_dict[strike_key][0]['val']
    
    return max(0, current_val - oldest_val)

# ==========================================
# 3. LIVE DHAN API FETCHING & PROCESSING
# ==========================================
def fetch_option_chain():
    url = "https://api.dhan.co/v2/optionchain"
    headers = {
        "access-token": ACCESS_TOKEN,
        "client-id": CLIENT_ID,
        "Content-Type": "application/json"
    }
    payload = {
        "UnderlyingScrip": UNDERLYING_SCRIP, 
        "UnderlyingSeg": UNDERLYING_SEG, 
        "Expiry": EXPIRY_DATE
    }
    
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        if response.status_code == 200:
            return response.json()
        else:
            st.error(f"Dhan API Error ({response.status_code}): {response.text}")
    except Exception as e:
        st.error(f"API Connection Exception: {e}")
    return None

def process_data():
    raw_data = fetch_option_chain()
    if not raw_data or not raw_data.get('data'):
        return None
        
    data = raw_data.get('data', {})
    spot_price = data.get('last_price', 0)
    option_chain = data.get('oc', {})
    
    chain_data = []
    iv_sum = 0
    iv_weighted_strike_sum = 0
    
    total_ce_oi, total_pe_oi = 0, 0
    total_ce_oi_chg, total_pe_oi_chg = 0, 0
    total_ce_vol, total_pe_vol = 0, 0
    
    total_ce_vol_15m, total_pe_vol_15m = 0, 0
    
    def extract_api_val(opt_dict, keys):
        for key in keys:
            if key in opt_dict:
                return float(opt_dict[key])
        return None
    
    for strike_str, details in option_chain.items():
        strike = float(strike_str)
        
        if not (spot_price - 500 <= strike <= spot_price + 500):
            continue
            
        ce = details.get('ce', {})
        pe = details.get('pe', {})
        
        ce_oi = ce.get('oi', 0)
        ce_gamma = ce.get('greeks', {}).get('gamma', 0) if ce.get('greeks') else 0
        ce_iv = ce.get('implied_volatility', 0)
        
        pe_oi = pe.get('oi', 0)
        pe_gamma = pe.get('greeks', {}).get('gamma', 0) if pe.get('greeks') else 0
        pe_iv = pe.get('implied_volatility', 0)
        
        ce_vol, pe_vol = ce.get('volume', 0), pe.get('volume', 0)
        
        # --- DYNAMIC OI CHANGE & PREVIOUS OI FALLBACK LOGIC ---
        api_ce_chg = extract_api_val(ce, ['change_in_oi', 'chng_in_oi', 'oi_change', 'change_oi', 'chg_oi'])
        api_pe_chg = extract_api_val(pe, ['change_in_oi', 'chng_in_oi', 'oi_change', 'change_oi', 'chg_oi'])
        
        ce_prev_oi = extract_api_val(ce, ['previous_oi', 'prev_oi', 'p_oi', 'previous_open_interest']) or 0
        pe_prev_oi = extract_api_val(pe, ['previous_oi', 'prev_oi', 'p_oi', 'previous_open_interest']) or 0
        
        # Call OI Change Calculation
        if api_ce_chg is not None and api_ce_chg != 0:
            ce_oi_chg = api_ce_chg
        elif ce_prev_oi > 0:
            ce_oi_chg = ce_oi - ce_prev_oi
        else:
            if f"CE_{strike}" not in st.session_state.daily_baseline_oi:
                st.session_state.daily_baseline_oi[f"CE_{strike}"] = ce_oi
            ce_oi_chg = ce_oi - st.session_state.daily_baseline_oi[f"CE_{strike}"]
            
        # Put OI Change Calculation
        if api_pe_chg is not None and api_pe_chg != 0:
            pe_oi_chg = api_pe_chg
        elif pe_prev_oi > 0:
            pe_oi_chg = pe_oi - pe_prev_oi
        else:
            if f"PE_{strike}" not in st.session_state.daily_baseline_oi:
                st.session_state.daily_baseline_oi[f"PE_{strike}"] = pe_oi
            pe_oi_chg = pe_oi - st.session_state.daily_baseline_oi[f"PE_{strike}"]
        # ----------------------------------------

        ce_vol_15m = get_rolling_metric(st.session_state.vol_snapshots, f"CE_{strike}", ce_vol)
        pe_vol_15m = get_rolling_metric(st.session_state.vol_snapshots, f"PE_{strike}", pe_vol)
        
        total_ce_oi += ce_oi
        total_pe_oi += pe_oi
        total_ce_oi_chg += ce_oi_chg
        total_pe_oi_chg += pe_oi_chg
        total_ce_vol += ce_vol
        total_pe_vol += pe_vol
        total_ce_vol_15m += ce_vol_15m
        total_pe_vol_15m += pe_vol_15m
        
        net_gex = (ce_gamma * ce_oi * LOT_SIZE) - (pe_gamma * pe_oi * LOT_SIZE)
        
        total_iv_at_strike = ce_iv + pe_iv
        if total_iv_at_strike > 0:
            iv_sum += total_iv_at_strike
            iv_weighted_strike_sum += (strike * total_iv_at_strike)
            
        chain_data.append({
            'Strike': strike, 
            'CE_OI_CHG_LAKHS': ce_oi_chg / 100000.0,
            'PE_OI_CHG_LAKHS': pe_oi_chg / 100000.0,
            'CE_VOL_LAKHS': ce_vol / 100000.0,
            'PE_VOL_LAKHS': pe_vol / 100000.0,
            'CE_VOL_15M_LAKHS': ce_vol_15m / 100000.0,
            'PE_VOL_15M_LAKHS': pe_vol_15m / 100000.0,
            'Net_GEX': net_gex
        })
        
    df = pd.DataFrame(chain_data)
    if df.empty: 
        return None
        
    df = df.sort_values('Strike').reset_index(drop=True)
    
    call_wall = df.loc[df['Net_GEX'].idxmax()]['Strike']
    put_wall = df.loc[df['Net_GEX'].idxmin()]['Strike']
    zero_gamma = df.iloc[(df['Net_GEX'].abs()).argsort()[:1]]['Strike'].values[0]
    
    atm_strike = min(df['Strike'], key=lambda x: abs(x - spot_price))
    atm_ce_iv = option_chain.get(str(atm_strike), {}).get('ce', {}).get('implied_volatility', 0)
    atm_pe_iv = option_chain.get(str(atm_strike), {}).get('pe', {}).get('implied_volatility', 0)
    vol_skew = atm_pe_iv - atm_ce_iv 
    
    atm_iv_avg = (atm_ce_iv + atm_pe_iv) / 2 if (atm_ce_iv + atm_pe_iv) > 0 else 15.0
    daily_1sd = spot_price * (atm_iv_avg / 100) * math.sqrt(1 / 252)
    iv_vwap = round(iv_weighted_strike_sum / iv_sum, 2) if iv_sum > 0 else spot_price
    
    pcr_oi = round(total_pe_oi / total_ce_oi, 2) if total_ce_oi > 0 else 1.0
    pcr_vol = round(total_pe_vol / total_ce_vol, 2) if total_ce_vol > 0 else 1.0
    
    total_abs_macro_oi = total_ce_oi + total_pe_oi
    vol_oi_velocity = round((total_ce_vol + total_pe_vol) / (total_abs_macro_oi if total_abs_macro_oi != 0 else 1), 2)
    
    # Intraday Check Capping
    total_abs_oi_chg = abs(total_ce_oi_chg) + abs(total_pe_oi_chg)
    if total_abs_oi_chg == 0:
        pcr_chg = 1.0
        intra_vol_oi_velocity = 0.0
    else:
        pcr_chg = round(total_pe_oi_chg / (total_ce_oi_chg if total_ce_oi_chg != 0 else 1), 2)
        intra_vol_oi_velocity = round((total_ce_vol + total_pe_vol) / total_abs_oi_chg, 2)
        
    # ==========================================
    # INSTITUTIONAL SMART MONEY TRIGGER LOGIC
    # ==========================================
    alert_status = "warning"
    alert_msg = "⚪ VOLATILITY CONTRACTION: Volume is quiet. Institutions are accumulating. Wait for the breakout trigger."
    trade_target = "N/A"
    trade_stop = "N/A"

    vol_minimum = 50000 
    
    if (total_pe_vol_15m > total_ce_vol_15m * 1.5) and (total_pe_oi_chg > total_ce_oi_chg * 1.2) and (total_pe_vol_15m > vol_minimum):
        alert_status = "success"
        alert_msg = "🟢 BULLISH TRIGGER FIRED: Massive Put Writing Divergence Detected. Institutions are aggressively building a floor."
        trade_target = f"{call_wall} (Call Wall Resistance) or {spot_price + daily_1sd:.1f} (+1 SD)"
        trade_stop = f"Strict close below {put_wall} (Structural Failure)"
        
    elif (total_ce_vol_15m > total_pe_vol_15m * 1.5) and (total_ce_oi_chg > total_pe_oi_chg * 1.2) and (total_ce_vol_15m > vol_minimum):
        alert_status = "error"
        alert_msg = "🔴 BEARISH TRIGGER FIRED: Massive Call Writing Divergence Detected. Institutions are aggressively building a ceiling."
        trade_target = f"{put_wall} (Put Wall Support) or {spot_price - daily_1sd:.1f} (-1 SD)"
        trade_stop = f"Strict close above {call_wall} (Structural Failure)"

    return {
        'spot': spot_price, 'zero_gamma': zero_gamma, 'call_wall': call_wall, 'put_wall': put_wall,
        'vol_skew': vol_skew, 'iv_vwap': iv_vwap, 'sd': daily_1sd,
        'total_ce_oi': total_ce_oi, 'total_pe_oi': total_pe_oi, 'pcr_oi': pcr_oi,
        'total_ce_oi_chg': total_ce_oi_chg, 'total_pe_oi_chg': total_pe_oi_chg, 'pcr_chg': pcr_chg,
        'total_ce_vol': total_ce_vol, 'total_pe_vol': total_pe_vol, 'pcr_vol': pcr_vol,
        'vol_oi_velocity': vol_oi_velocity,
        'intra_vol_oi_velocity': intra_vol_oi_velocity,
        'alert_status': alert_status,
        'alert_msg': alert_msg,
        'trade_target': trade_target,
        'trade_stop': trade_stop,
        'strike_df': df
    }

# ==========================================
# 4. MARKET HOURS CHECK & STATUS
# ==========================================
current_time_ist = datetime.now(ist)
market_open = current_time_ist.replace(hour=9, minute=15, second=0, microsecond=0)
market_close = current_time_ist.replace(hour=15, minute=30, second=0, microsecond=0)
is_weekday = current_time_ist.weekday() < 5

is_market_open = is_weekday and (market_open <= current_time_ist <= market_close)

# ==========================================
# 5. DASHBOARD UI
# ==========================================
st.title("📊 Institutional Gamma Command Center")

col_head1, col_head2 = st.columns([8, 2])
with col_head1:
    if not is_market_open:
        st.warning("⏸️ Market is closed. Displaying latest API snapshot.")
    else:
        st.success("▶️ Live Dhan API Stream Active.")

with col_head2:
    auto_refresh = st.toggle("Auto-Refresh (1 Min)", value=is_market_open)

metrics = process_data()

if metrics:
    if is_market_open:
        log_to_postgres(metrics)
        
    # Update Extrema Tracker
    now_str = datetime.now(ist).strftime("%H:%M")
    tracker = st.session_state.pcr_tracker
    
    if metrics['pcr_oi'] > tracker['oi_max']['val']:
        tracker['oi_max'] = {'val': metrics['pcr_oi'], 'time': now_str}
    if metrics['pcr_oi'] < tracker['oi_min']['val']:
        tracker['oi_min'] = {'val': metrics['pcr_oi'], 'time': now_str}
        
    if metrics['pcr_vol'] > tracker['vol_max']['val']:
        tracker['vol_max'] = {'val': metrics['pcr_vol'], 'time': now_str}
    if metrics['pcr_vol'] < tracker['vol_min']['val']:
        tracker['vol_min'] = {'val': metrics['pcr_vol'], 'time': now_str}
        
    if metrics['pcr_chg'] > tracker['chg_max']['val']:
        tracker['chg_max'] = {'val': metrics['pcr_chg'], 'time': now_str}
    if metrics['pcr_chg'] < tracker['chg_min']['val']:
        tracker['chg_min'] = {'val': metrics['pcr_chg'], 'time': now_str}
        
    if metrics['vol_oi_velocity'] > tracker['vel_max']['val']:
        tracker['vel_max'] = {'val': metrics['vol_oi_velocity'], 'time': now_str}
    if metrics['vol_oi_velocity'] < tracker['vel_min']['val']:
        tracker['vel_min'] = {'val': metrics['vol_oi_velocity'], 'time': now_str}
        
    if metrics['intra_vol_oi_velocity'] > tracker['intra_vel_max']['val']:
        tracker['intra_vel_max'] = {'val': metrics['intra_vol_oi_velocity'], 'time': now_str}
    if metrics['intra_vol_oi_velocity'] < tracker['intra_vel_min']['val']:
        tracker['intra_vel_min'] = {'val': metrics['intra_vol_oi_velocity'], 'time': now_str}
    
    # ==========================================
    # INSTITUTIONAL TRADE ALERT BANNER
    # ==========================================
    st.markdown("---")
    st.markdown("### 🎯 Institutional Execution Engine")
    
    if metrics['alert_status'] == "success":
        st.success(metrics['alert_msg'])
        colA, colB = st.columns(2)
        colA.info(f"**🎯 Take Profit Target:** {metrics['trade_target']}")
        colB.error(f"**🛑 Maximum Risk Stop:** {metrics['trade_stop']}")
    elif metrics['alert_status'] == "error":
        st.error(metrics['alert_msg'])
        colA, colB = st.columns(2)
        colA.info(f"**🎯 Take Profit Target:** {metrics['trade_target']}")
        colB.error(f"**🛑 Maximum Risk Stop:** {metrics['trade_stop']}")
    else:
        st.warning(metrics['alert_msg'])
    
    st.markdown("---")
    
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Nifty Spot", f"{metrics['spot']:.2f}", delta=round(metrics['spot'] - metrics['zero_gamma'], 2))
    col2.metric("Zero Gamma (Regime)", metrics['zero_gamma'])
    col3.metric("IV VWAP Level", metrics['iv_vwap'])
    col4.metric("Volatility Skew (PE - CE)", f"{metrics['vol_skew']:.2f}%")

    st.markdown("### Broad Market Order Flow")

    m1, m2, m3, m4, m5 = st.columns(5)
    with m1:
        st.metric("Total PCR (OI)", metrics['pcr_oi'])
        st.markdown(f"<span style='color:grey; font-size: 0.85rem;'>Calls: {metrics['total_ce_oi']:,.0f} <br> Puts: {metrics['total_pe_oi']:,.0f}</span>", unsafe_allow_html=True)
        st.markdown(f"<span style='color:#4CAF50; font-size: 0.8rem;'>High: {tracker['oi_max']['val']} ({tracker['oi_max']['time']})</span> &nbsp;|&nbsp; <span style='color:#F44336; font-size: 0.8rem;'>Low: {tracker['oi_min']['val']} ({tracker['oi_min']['time']})</span>", unsafe_allow_html=True)
        
    with m2:
        st.metric("Intraday PCR (OI Change)", metrics['pcr_chg'])
        st.markdown(f"<span style='color:grey; font-size: 0.85rem;'>Call Add: {metrics['total_ce_oi_chg']:,.0f} <br> Put Add: {metrics['total_pe_oi_chg']:,.0f}</span>", unsafe_allow_html=True)
        st.markdown(f"<span style='color:#4CAF50; font-size: 0.8rem;'>High: {tracker['chg_max']['val']} ({tracker['chg_max']['time']})</span> &nbsp;|&nbsp; <span style='color:#F44336; font-size: 0.8rem;'>Low: {tracker['chg_min']['val']} ({tracker['chg_min']['time']})</span>", unsafe_allow_html=True)
        
    with m3:
        st.metric("Volume PCR", metrics['pcr_vol'])
        st.markdown(f"<span style='color:grey; font-size: 0.85rem;'>Call Vol: {metrics['total_ce_vol']:,.0f} <br> Put Vol: {metrics['total_pe_vol']:,.0f}</span>", unsafe_allow_html=True)
        st.markdown(f"<span style='color:#4CAF50; font-size: 0.8rem;'>High: {tracker['vol_max']['val']} ({tracker['vol_max']['time']})</span> &nbsp;|&nbsp; <span style='color:#F44336; font-size: 0.8rem;'>Low: {tracker['vol_min']['val']} ({tracker['vol_min']['time']})</span>", unsafe_allow_html=True)
        
    with m4:
        st.metric("Macro Vol/OI Velocity", f"{metrics['vol_oi_velocity']}x")
        st.markdown(f"<span style='color:grey; font-size: 0.85rem;'>Overall Market Direction</span><br>", unsafe_allow_html=True)
        st.markdown(f"<span style='color:#4CAF50; font-size: 0.8rem;'>High: {tracker['vel_max']['val']}x ({tracker['vel_max']['time']})</span> &nbsp;|&nbsp; <span style='color:#F44336; font-size: 0.8rem;'>Low: {tracker['vel_min']['val']}x ({tracker['vel_min']['time']})</span>", unsafe_allow_html=True)

    with m5:
        st.metric("Intraday Vol/OI Velocity", f"{metrics['intra_vol_oi_velocity']}x")
        st.markdown(f"<span style='color:grey; font-size: 0.85rem;'>Pure Intraday Intensity</span><br>", unsafe_allow_html=True)
        st.markdown(f"<span style='color:#4CAF50; font-size: 0.8rem;'>High: {tracker['intra_vel_max']['val']}x ({tracker['intra_vel_max']['time']})</span> &nbsp;|&nbsp; <span style='color:#F44336; font-size: 0.8rem;'>Low: {tracker['intra_vel_min']['val']}x ({tracker['intra_vel_min']['time']})</span>", unsafe_allow_html=True)


    st.markdown("---")
    
    today_str = datetime.now().strftime("%a, %d %b")
    df = metrics['strike_df']
    spot_val = metrics['spot']
    
    def render_bar_chart(title, df_col_pe, df_col_ce, y_title):
        fig = go.Figure()
        fig.add_trace(go.Bar(
            x=df['Strike'], y=df[df_col_pe], name=f'Put {y_title}', marker_color='#4CAF50',
            hovertemplate=f'Strike: %{{x}}<br>Put {y_title}: %{{y:.2f}}L<extra></extra>'
        ))
        fig.add_trace(go.Bar(
            x=df['Strike'], y=df[df_col_ce], name=f'Call {y_title}', marker_color='#F44336',
            hovertemplate=f'Strike: %{{x}}<br>Call {y_title}: %{{y:.2f}}L<extra></extra>'
        ))
        fig.add_vline(
            x=spot_val, line_width=2, line_dash="dash", line_color="#424242",
            annotation_text=f"NIFTY {spot_val:.2f}", annotation_position="top",
            annotation_font=dict(size=13, color="black"), annotation_bgcolor="#EEEEEE"
        )
        fig.update_layout(
            barmode='group', height=400, template="plotly_white", margin=dict(l=40, r=40, t=40, b=40),
            xaxis=dict(title="Strike", tickmode='array', tickvals=df['Strike'], tickangle=-45, showgrid=True, gridcolor='#f0f0f0'),
            yaxis=dict(title=f"Call / Put {y_title}", ticksuffix="L", showgrid=True, gridcolor='#f0f0f0'),
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
            hovermode="x unified"
        )
        st.plotly_chart(fig, use_container_width=True)

    # ==========================================
    # CHART RENDERING
    # ==========================================
    st.markdown(f"### OI Change on {today_str}")
    render_bar_chart(f"OI Change on {today_str}", 'PE_OI_CHG_LAKHS', 'CE_OI_CHG_LAKHS', 'OI Change')

    st.markdown(f"### Total Volume on {today_str}")
    render_bar_chart(f"Total Volume on {today_str}", 'PE_VOL_LAKHS', 'CE_VOL_LAKHS', 'Volume')

    st.markdown(f"### Active Pace: 15-Min Rolling Volume")
    if is_market_open:
        st.caption("Tracks immediate institutional volume flow generated in the last 15 minutes.")
        render_bar_chart("Active 15-Min Volume", 'PE_VOL_15M_LAKHS', 'CE_VOL_15M_LAKHS', '15m Vol')
    else:
        st.info("Market is closed. 15-Min Rolling Volume tracking requires live data updates.")

    st.markdown("---")
    
    # ==========================================
    # STRUCTURAL BOUNDARIES
    # ==========================================
    st.markdown("### Structural Boundaries")
    c1, c2, c3 = st.columns(3)
    with c1:
        st.info(f"**Call Wall (Resistance):** {metrics['call_wall']}")
        st.info(f"**Put Wall (Support):** {metrics['put_wall']}")
    with c2:
        st.warning(f"**+2.0 SD:** {metrics['spot'] + (2.0 * metrics['sd']):.1f}")
        st.warning(f"**+1.0 SD:** {metrics['spot'] + (1.0 * metrics['sd']):.1f}")
    with c3:
        st.success(f"**-1.0 SD:** {metrics['spot'] - (1.0 * metrics['sd']):.1f}")
        st.success(f"**-2.0 SD:** {metrics['spot'] - (2.0 * metrics['sd']):.1f}")

else:
    st.error("⚠️ **No Data Received from Dhan API.** Verify your Client ID, Access Token, and Expiry Date.")

# ==========================================
# 6. CONDITIONAL AUTO-REFRESH
# ==========================================
if auto_refresh and is_market_open:
    time.sleep(60)
    st.rerun()