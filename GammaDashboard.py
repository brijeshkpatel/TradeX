import streamlit as st
import requests
import pandas as pd
import numpy as np
import json
import math
import time
from datetime import datetime
import pytz
import psycopg2
from psycopg2.extras import Json
import plotly.graph_objects as go

# ==========================================
# 1. CONFIGURATION & DATABASE (VIA SECRETS)
# ==========================================
st.set_page_config(page_title="Institutional Gamma Command Center", layout="wide")

# Read from Streamlit Secrets with fallback to defaults
CLIENT_ID = st.secrets.get("DHAN_CLIENT_ID", "DHAN_CLIENT_ID")
ACCESS_TOKEN = st.secrets.get("DHAN_ACCESS_TOKEN", "DHAN_ACCESS_TOKEN")
EXPIRY_DATE = st.secrets.get("EXPIRY_DATE", "EXPIRY_DATE")

UNDERLYING_SCRIP = 13  # Nifty 50
UNDERLYING_SEG = "IDX_I" 
LOT_SIZE = 65

DB_CONFIG = {
    "dbname": st.secrets.get("DB_NAME", "DB_NAME"),
    "user": st.secrets.get("DB_USER", "DB_USER"),
    "password": st.secrets.get("DB_PASSWORD", "DB_PASSWORD"),
    "host": st.secrets.get("DB_HOST", "DB_HOST"),
    "port": st.secrets.get("DB_PORT", "5432"),
    "sslmode": "require" # Required for Cloud DBs like Neon/Supabase
}


# ==========================================
# 2. DATABASE MANAGEMENT & INITIALIZATION
# ==========================================
def get_db_connection():
    return psycopg2.connect(**DB_CONFIG)

def init_db():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS market_snapshots_1m (
                id SERIAL PRIMARY KEY,
                timestamp TIMESTAMP WITH TIME ZONE NOT NULL,
                spot_price NUMERIC(10, 2),
                net_writer_delta NUMERIC(14, 2),
                total_ce_oi NUMERIC(16, 2),
                total_pe_oi NUMERIC(16, 2),
                total_ce_oi_chg NUMERIC(16, 2),
                total_pe_oi_chg NUMERIC(16, 2),
                total_ce_vol NUMERIC(16, 2),
                total_pe_vol NUMERIC(16, 2),
                pcr_oi NUMERIC(6, 2),
                pcr_vol NUMERIC(6, 2),
                pcr_chg NUMERIC(6, 2),
                macro_velocity NUMERIC(10, 2),
                intraday_velocity NUMERIC(10, 2),
                sd1_upper NUMERIC(10, 2),
                sd1_lower NUMERIC(10, 2),
                sd2_upper NUMERIC(10, 2),
                sd2_lower NUMERIC(10, 2),
                strike_details JSONB
            );
            CREATE INDEX IF NOT EXISTS idx_snapshots_time ON market_snapshots_1m (timestamp);
        """)
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        st.sidebar.error(f"DB Init Warning: {e}")

init_db()

def log_snapshot_to_db(m):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        query = """
            INSERT INTO market_snapshots_1m (
                timestamp, spot_price, net_writer_delta, total_ce_oi, total_pe_oi,
                total_ce_oi_chg, total_pe_oi_chg, total_ce_vol, total_pe_vol,
                pcr_oi, pcr_vol, pcr_chg, macro_velocity, intraday_velocity,
                sd1_upper, sd1_lower, sd2_upper, sd2_lower, strike_details
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        strike_json = m["strike_df"].to_dict(orient="records")
        
        # Strip timezone info so it writes pure IST into TIMESTAMP WITHOUT TIME ZONE
        ist_naive_timestamp = m["timestamp"].replace(tzinfo=None)

        values = (
            ist_naive_timestamp, m["spot"], m["net_writer_delta"], m["total_ce_oi"], m["total_pe_oi"],
            m["total_ce_oi_chg"], m["total_pe_oi_chg"], m["total_ce_vol"], m["total_pe_vol"],
            m["pcr_oi"], m["pcr_vol"], m["pcr_chg"], m["vol_oi_velocity"], m["intra_vol_oi_velocity"],
            m["sd1_upper"], m["sd1_lower"], m["sd2_upper"], m["sd2_lower"], Json(strike_json)
        )
        cur.execute(query, values)
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        st.sidebar.error(f"DB Write Error: {e}")

def get_db_row_count():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM market_snapshots_1m;")
        count = cur.fetchone()[0]
        cur.close()
        conn.close()
        return count
    except Exception:
        return 0

def fetch_all_db_records():
    try:
        conn = get_db_connection()
        df = pd.read_sql_query("SELECT * FROM market_snapshots_1m ORDER BY timestamp ASC;", conn)
        conn.close()
        return df
    except Exception:
        return pd.DataFrame()

def flush_database():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("TRUNCATE TABLE market_snapshots_1m RESTART IDENTITY;")
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception:
        return False

# ==========================================
# 3. SESSION STATE & SIDEBAR PARAMETERS
# ==========================================
ist = pytz.timezone("Asia/Kolkata")
current_date = datetime.now(ist).date()

if "daily_baseline_oi" not in st.session_state:
    st.session_state.daily_baseline_oi = {}

# Time-series buffer for PCR charting
if "pcr_history" not in st.session_state:
    st.session_state.pcr_history = []

# Daily Extrema Tracker
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

st.sidebar.title("⚙️ Strategy Parameters")

strike_depth = st.sidebar.slider(
    "🎯 Strike Range (± Points)",
    min_value=100,
    max_value=1000,
    value=400,
    step=50,
    help="Restricts ALL calculations (PCRs, Velocities, Net Delta) to this specific zone."
)

# Database Management in Sidebar
st.sidebar.markdown("---")
st.sidebar.title("💾 Expiry Data Management")
db_count = get_db_row_count()
st.sidebar.info(f"📊 Stored Rows: **{db_count:,}** minutes")

if st.sidebar.button("📥 Prepare Expiry Backup (CSV)"):
    all_data = fetch_all_db_records()
    if not all_data.empty:
        all_data["strike_details"] = all_data["strike_details"].apply(json.dumps)
        csv_data = all_data.to_csv(index=False).encode("utf-8")
        st.sidebar.download_button(
            label="💾 Download CSV Backup",
            data=csv_data,
            file_name=f"nifty_expiry_backup_{datetime.now(ist).strftime('%Y%m%d_%H%M')}.csv",
            mime="text/csv"
        )
    else:
        st.sidebar.warning("Database is currently empty.")

st.sidebar.markdown("#### 🗑️ Flush Expiry Database")
confirm_flush = st.sidebar.checkbox("Confirm: I have downloaded my backup")
if st.sidebar.button("Flush Server DB", disabled=not confirm_flush):
    if flush_database():
        st.sidebar.success("Database truncated successfully!")
        st.rerun()
    else:
        st.sidebar.error("Failed to flush database.")

# ==========================================
# 4. DATA ENGINE (DHAN API FETCH)
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
        response = requests.post(url, json=payload, headers=headers, timeout=8)
        if response.status_code == 200:
            return response.json()
    except Exception:
        pass
    return None

def extract_val(opt_dict, keys):
    for k in keys:
        if k in opt_dict and opt_dict[k] is not None:
            return float(opt_dict[k])
    return None

def process_data():
    raw_data = fetch_option_chain()
    if not raw_data or not raw_data.get("data"):
        return None

    data = raw_data.get("data", {})
    spot_price = float(data.get("last_price", 0))
    option_chain = data.get("oc", {})

    total_ce_oi, total_pe_oi = 0.0, 0.0
    total_ce_oi_chg, total_pe_oi_chg = 0.0, 0.0
    total_ce_vol, total_pe_vol = 0.0, 0.0
    strike_rows = []

    atm_strike = round(spot_price / 50) * 50
    atm_ce_iv = 0.0
    atm_pe_iv = 0.0

    for strike_str, details in option_chain.items():
        strike = float(strike_str)
        
        # 🎯 STRICT FILTER: Only calculate metrics for strikes within the selected range
        if not (spot_price - strike_depth <= strike <= spot_price + strike_depth):
            continue

        ce = details.get("ce", {})
        pe = details.get("pe", {})

        ce_oi = extract_val(ce, ["oi", "open_interest"]) or 0.0
        pe_oi = extract_val(pe, ["oi", "open_interest"]) or 0.0
        ce_vol = extract_val(ce, ["volume"]) or 0.0
        pe_vol = extract_val(pe, ["volume"]) or 0.0

        ce_prev_oi = extract_val(ce, ["previous_oi", "prev_oi", "p_oi"]) or 0.0
        pe_prev_oi = extract_val(pe, ["previous_oi", "prev_oi", "p_oi"]) or 0.0
        api_ce_chg = extract_val(ce, ["change_in_oi", "chng_in_oi", "oi_change"])
        api_pe_chg = extract_val(pe, ["change_in_oi", "chng_in_oi", "oi_change"])

        if api_ce_chg is not None and api_ce_chg != 0:
            ce_chg = api_ce_chg
        elif ce_prev_oi > 0:
            ce_chg = ce_oi - ce_prev_oi
        else:
            baseline = st.session_state.daily_baseline_oi.setdefault(f"CE_{strike}", ce_oi)
            ce_chg = ce_oi - baseline

        if api_pe_chg is not None and api_pe_chg != 0:
            pe_chg = api_pe_chg
        elif pe_prev_oi > 0:
            pe_chg = pe_oi - pe_prev_oi
        else:
            baseline = st.session_state.daily_baseline_oi.setdefault(f"PE_{strike}", pe_oi)
            pe_chg = pe_oi - baseline

        # Add to totals (now strictly constrained to the selected strike range)
        total_ce_oi += ce_oi
        total_pe_oi += pe_oi
        total_ce_oi_chg += ce_chg
        total_pe_oi_chg += pe_chg
        total_ce_vol += ce_vol
        total_pe_vol += pe_vol

        if strike == atm_strike:
            atm_ce_iv = extract_val(ce, ["implied_volatility", "iv"]) or 14.0
            atm_pe_iv = extract_val(pe, ["implied_volatility", "iv"]) or 14.0

        strike_rows.append({
            "strike": strike,
            "ce_oi": ce_oi,
            "pe_oi": pe_oi,
            "ce_oi_chg": ce_chg,
            "pe_oi_chg": pe_chg,
            "ce_vol": ce_vol,
            "pe_vol": pe_vol,
            "net_writer_delta": pe_chg - ce_chg
        })

    # Standard Deviation Calculation
    atm_iv = (atm_ce_iv + atm_pe_iv) / 2 if (atm_ce_iv + atm_pe_iv) > 0 else 14.0
    daily_1sd = spot_price * (atm_iv / 100) * math.sqrt(1 / 252)

    sd1_upper = round(spot_price + daily_1sd, 2)
    sd1_lower = round(spot_price - daily_1sd, 2)
    sd2_upper = round(spot_price + (2 * daily_1sd), 2)
    sd2_lower = round(spot_price - (2 * daily_1sd), 2)

    # PCRs & Velocities (derived from the active zone)
    pcr_oi = round(total_pe_oi / total_ce_oi, 2) if total_ce_oi > 0 else 1.0
    pcr_vol = round(total_pe_vol / total_ce_vol, 2) if total_ce_vol > 0 else 1.0
    pcr_chg = round(total_pe_oi_chg / (total_ce_oi_chg if total_ce_oi_chg != 0 else 1), 2)

    total_abs_macro_oi = total_ce_oi + total_pe_oi
    vol_oi_velocity = round((total_ce_vol + total_pe_vol) / (total_abs_macro_oi if total_abs_macro_oi != 0 else 1), 2)
    
    total_abs_oi_chg = abs(total_ce_oi_chg) + abs(total_pe_oi_chg)
    intra_vol_oi_velocity = round((total_ce_vol + total_pe_vol) / (total_abs_oi_chg if total_abs_oi_chg != 0 else 1), 2)

    now = datetime.now(ist)

    # Append to rolling in-memory buffer for real-time PCR line plots
    st.session_state.pcr_history.append({
        "time": now,
        "pcr_oi": pcr_oi,
        "pcr_chg": pcr_chg,
        "pcr_vol": pcr_vol,
        "spot": spot_price
    })
    st.session_state.pcr_history = st.session_state.pcr_history[-375:]  # Keep full trading day

    return {
        "timestamp": now,
        "spot": spot_price,
        "net_writer_delta": total_pe_oi_chg - total_ce_oi_chg,
        "total_ce_oi": total_ce_oi,
        "total_pe_oi": total_pe_oi,
        "total_ce_oi_chg": total_ce_oi_chg,
        "total_pe_oi_chg": total_pe_oi_chg,
        "total_ce_vol": total_ce_vol,
        "total_pe_vol": total_pe_vol,
        "pcr_oi": pcr_oi,
        "pcr_vol": pcr_vol,
        "pcr_chg": pcr_chg,
        "vol_oi_velocity": vol_oi_velocity,
        "intra_vol_oi_velocity": intra_vol_oi_velocity,
        "sd1_upper": sd1_upper,
        "sd1_lower": sd1_lower,
        "sd2_upper": sd2_upper,
        "sd2_lower": sd2_lower,
        "strike_df": pd.DataFrame(strike_rows).sort_values("strike")
    }

# ==========================================
# 5. MARKET STATUS & DASHBOARD UI
# ==========================================
current_time_ist = datetime.now(ist)
market_open = current_time_ist.replace(hour=9, minute=15, second=0, microsecond=0)
market_close = current_time_ist.replace(hour=15, minute=30, second=0, microsecond=0)
is_market_open = (current_time_ist.weekday() < 5) and (market_open <= current_time_ist <= market_close)

st.title("⚡ Institutional Delta Flow & 1-Minute Logger")

col_h1, col_h2 = st.columns([8, 2])
with col_h1:
    if is_market_open:
        st.success("🟢 Live Dhan Feed Active | Auto-Logging 1-Min Records to PostgreSQL")
    else:
        st.warning("⏸️ Market is Closed. Database logging paused.")

with col_h2:
    auto_refresh = st.toggle("Auto-Refresh (1 Min)", value=is_market_open)

metrics = process_data()

if metrics:
    # Auto-log to PostgreSQL every minute
    if is_market_open:
        log_snapshot_to_db(metrics)

    # Update Extrema Tracker
    now_str = datetime.now(ist).strftime("%H:%M")
    tracker = st.session_state.pcr_tracker
    
    if metrics['pcr_oi'] > tracker['oi_max']['val']: tracker['oi_max'] = {'val': metrics['pcr_oi'], 'time': now_str}
    if metrics['pcr_oi'] < tracker['oi_min']['val']: tracker['oi_min'] = {'val': metrics['pcr_oi'], 'time': now_str}
        
    if metrics['pcr_vol'] > tracker['vol_max']['val']: tracker['vol_max'] = {'val': metrics['pcr_vol'], 'time': now_str}
    if metrics['pcr_vol'] < tracker['vol_min']['val']: tracker['vol_min'] = {'val': metrics['pcr_vol'], 'time': now_str}
        
    if metrics['pcr_chg'] > tracker['chg_max']['val']: tracker['chg_max'] = {'val': metrics['pcr_chg'], 'time': now_str}
    if metrics['pcr_chg'] < tracker['chg_min']['val']: tracker['chg_min'] = {'val': metrics['pcr_chg'], 'time': now_str}
        
    if metrics['vol_oi_velocity'] > tracker['vel_max']['val']: tracker['vel_max'] = {'val': metrics['vol_oi_velocity'], 'time': now_str}
    if metrics['vol_oi_velocity'] < tracker['vel_min']['val']: tracker['vel_min'] = {'val': metrics['vol_oi_velocity'], 'time': now_str}
        
    if metrics['intra_vol_oi_velocity'] > tracker['intra_vel_max']['val']: tracker['intra_vel_max'] = {'val': metrics['intra_vol_oi_velocity'], 'time': now_str}
    if metrics['intra_vol_oi_velocity'] < tracker['intra_vel_min']['val']: tracker['intra_vel_min'] = {'val': metrics['intra_vol_oi_velocity'], 'time': now_str}

    # Metrics Overview
    m1, m2 = st.columns(2)
    m1.metric("Nifty Spot", f"{metrics['spot']:.2f}")
    m2.metric("Session Writer Delta", f"{metrics['net_writer_delta']/100000:+.2f}L")

    st.markdown("---")
    # ==========================================
    # BROAD MARKET ORDER FLOW & TRACKERS
    # ==========================================
    st.markdown(f"### Broad Market Order Flow (±{strike_depth} Active Zone)")
    o1, o2, o3, o4, o5 = st.columns(5)
    
    with o1:
        st.metric("Total PCR (OI)", metrics['pcr_oi'])
        st.markdown(f"<span style='color:grey; font-size: 0.85rem;'>Calls: {metrics['total_ce_oi']:,.0f} <br> Puts: {metrics['total_pe_oi']:,.0f}</span>", unsafe_allow_html=True)
        st.markdown(f"<span style='color:#4CAF50; font-size: 0.8rem;'>High: {tracker['oi_max']['val']} ({tracker['oi_max']['time']})</span> &nbsp;|&nbsp; <span style='color:#F44336; font-size: 0.8rem;'>Low: {tracker['oi_min']['val']} ({tracker['oi_min']['time']})</span>", unsafe_allow_html=True)
        
    with o2:
        st.metric("Intraday PCR (OI Change)", metrics['pcr_chg'])
        st.markdown(f"<span style='color:grey; font-size: 0.85rem;'>Call Add: {metrics['total_ce_oi_chg']:,.0f} <br> Put Add: {metrics['total_pe_oi_chg']:,.0f}</span>", unsafe_allow_html=True)
        st.markdown(f"<span style='color:#4CAF50; font-size: 0.8rem;'>High: {tracker['chg_max']['val']} ({tracker['chg_max']['time']})</span> &nbsp;|&nbsp; <span style='color:#F44336; font-size: 0.8rem;'>Low: {tracker['chg_min']['val']} ({tracker['chg_min']['time']})</span>", unsafe_allow_html=True)
        
    with o3:
        st.metric("Volume PCR", metrics['pcr_vol'])
        st.markdown(f"<span style='color:grey; font-size: 0.85rem;'>Call Vol: {metrics['total_ce_vol']:,.0f} <br> Put Vol: {metrics['total_pe_vol']:,.0f}</span>", unsafe_allow_html=True)
        st.markdown(f"<span style='color:#4CAF50; font-size: 0.8rem;'>High: {tracker['vol_max']['val']} ({tracker['vol_max']['time']})</span> &nbsp;|&nbsp; <span style='color:#F44336; font-size: 0.8rem;'>Low: {tracker['vol_min']['val']} ({tracker['vol_min']['time']})</span>", unsafe_allow_html=True)
        
    with o4:
        st.metric("Macro Vol/OI Velocity", f"{metrics['vol_oi_velocity']}x")
        st.markdown(f"<span style='color:grey; font-size: 0.85rem;'>Overall Market Direction</span><br>", unsafe_allow_html=True)
        st.markdown(f"<span style='color:#4CAF50; font-size: 0.8rem;'>High: {tracker['vel_max']['val']}x ({tracker['vel_max']['time']})</span> &nbsp;|&nbsp; <span style='color:#F44336; font-size: 0.8rem;'>Low: {tracker['vel_min']['val']}x ({tracker['vel_min']['time']})</span>", unsafe_allow_html=True)

    with o5:
        st.metric("Intraday Vol/OI Velocity", f"{metrics['intra_vol_oi_velocity']}x")
        st.markdown(f"<span style='color:grey; font-size: 0.85rem;'>Pure Intraday Intensity</span><br>", unsafe_allow_html=True)
        st.markdown(f"<span style='color:#4CAF50; font-size: 0.8rem;'>High: {tracker['intra_vel_max']['val']}x ({tracker['intra_vel_max']['time']})</span> &nbsp;|&nbsp; <span style='color:#F44336; font-size: 0.8rem;'>Low: {tracker['intra_vel_min']['val']}x ({tracker['intra_vel_min']['time']})</span>", unsafe_allow_html=True)

    st.markdown("---")
    st.markdown("### Statistical Boundaries")
    s1, s2, s3, s4 = st.columns(4)
    s1.success(f"**-2.0 SD:** {metrics['sd2_lower']}")
    s2.success(f"**-1.0 SD:** {metrics['sd1_lower']}")
    s3.warning(f"**+1.0 SD:** {metrics['sd1_upper']}")
    s4.warning(f"**+2.0 SD:** {metrics['sd2_upper']}")

    # ==========================================
    # PCR TRENDS OVER TIME (NEW CHARTS)
    # ==========================================
    st.markdown("---")
    st.markdown("### 📈 PCR Trends Over Time")

    df_pcr = pd.DataFrame(st.session_state.pcr_history)
    
    if not df_pcr.empty:
        col_pcr1, col_pcr2 = st.columns(2)

        # 1. Total PCR (OI) Chart
        with col_pcr1:
            fig_pcr_oi = go.Figure()
            fig_pcr_oi.add_trace(go.Scatter(
                x=df_pcr["time"],
                y=df_pcr["pcr_oi"],
                mode="lines+markers",
                name="Total PCR (OI)",
                line=dict(color="#2196F3", width=2.5),
                marker=dict(size=4),
                hovertemplate="Time: %{x|%H:%M}<br>Total PCR: %{y:.2f}<extra></extra>"
            ))
            fig_pcr_oi.add_hline(
                y=1.0, line_dash="dash", line_color="#757575",
                annotation_text="Neutral (1.0)", annotation_position="bottom right"
            )
            fig_pcr_oi.update_layout(
                title="Total PCR (OI Macro)",
                height=340,
                template="plotly_white",
                margin=dict(l=40, r=40, t=40, b=30),
                xaxis=dict(title="Time", showgrid=True, gridcolor="#f0f0f0"),
                yaxis=dict(title="PCR (OI)", showgrid=True, gridcolor="#f0f0f0"),
                hovermode="x unified"
            )
            st.plotly_chart(fig_pcr_oi, use_container_width=True)

        # 2. Intraday PCR (OI Change) Chart
        with col_pcr2:
            fig_pcr_chg = go.Figure()
            fig_pcr_chg.add_trace(go.Scatter(
                x=df_pcr["time"],
                y=df_pcr["pcr_chg"],
                mode="lines+markers",
                name="Intraday PCR (Chg)",
                line=dict(color="#FF9800", width=2.5),
                marker=dict(size=4),
                hovertemplate="Time: %{x|%H:%M}<br>Intraday PCR: %{y:.2f}<extra></extra>"
            ))
            fig_pcr_chg.add_hline(
                y=1.0, line_dash="dash", line_color="#757575",
                annotation_text="Neutral (1.0)", annotation_position="bottom right"
            )
            fig_pcr_chg.update_layout(
                title="Intraday PCR (OI Change Flow)",
                height=340,
                template="plotly_white",
                margin=dict(l=40, r=40, t=40, b=30),
                xaxis=dict(title="Time", showgrid=True, gridcolor="#f0f0f0"),
                yaxis=dict(title="PCR (OI Change)", showgrid=True, gridcolor="#f0f0f0"),
                hovermode="x unified"
            )
            st.plotly_chart(fig_pcr_chg, use_container_width=True)

    # ==========================================
    # CHART RENDERING HELPERS
    # ==========================================
    df_strikes = metrics["strike_df"]
    spot_val = metrics["spot"]
    today_str = datetime.now(ist).strftime("%a, %d %b")

    def render_grouped_bar_chart(title, y_col_pe, y_col_ce, y_label):
        fig = go.Figure()
        fig.add_trace(go.Bar(
            x=df_strikes['strike'], y=df_strikes[y_col_pe] / 100000.0, name=f'Put {y_label}', marker_color='#4CAF50',
            hovertemplate=f'Strike: %{{x}}<br>Put {y_label}: %{{y:.2f}}L<extra></extra>'
        ))
        fig.add_trace(go.Bar(
            x=df_strikes['strike'], y=df_strikes[y_col_ce] / 100000.0, name=f'Call {y_label}', marker_color='#F44336',
            hovertemplate=f'Strike: %{{x}}<br>Call {y_label}: %{{y:.2f}}L<extra></extra>'
        ))
        fig.add_vline(
            x=spot_val, line_width=2, line_dash="dash", line_color="#424242",
            annotation_text=f"NIFTY {spot_val:.2f}", annotation_position="top",
            annotation_font=dict(size=13, color="black"), annotation_bgcolor="#EEEEEE"
        )
        fig.update_layout(
            barmode='group', height=400, template="plotly_white", margin=dict(l=40, r=40, t=40, b=40),
            xaxis=dict(title="Strike", tickmode='array', tickvals=df_strikes['strike'], tickangle=-45, showgrid=True, gridcolor='#f0f0f0'),
            yaxis=dict(title=f"{y_label} (Lakhs)", ticksuffix="L", showgrid=True, gridcolor='#f0f0f0'),
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
            hovermode="x unified"
        )
        st.plotly_chart(fig, use_container_width=True)

    # Strike-Wise Net Writing Distribution Chart
    st.markdown("---")
    st.markdown(f"### 📌 Current Strike-Wise Writer Dominance (±{strike_depth} Points)")
    fig_strike = go.Figure()
    bar_colors = ["#4CAF50" if x >= 0 else "#F44336" for x in df_strikes["net_writer_delta"]]

    fig_strike.add_trace(go.Bar(
        x=df_strikes["strike"],
        y=df_strikes["net_writer_delta"] / 100000.0,
        marker_color=bar_colors,
        name="Net Put - Call Add",
        hovertemplate="Strike: %{x}<br>Net Delta: %{y:.2f}L<extra></extra>"
    ))

    fig_strike.add_vline(
        x=spot_val, line_width=2, line_dash="dash", line_color="#424242",
        annotation_text=f"NIFTY {spot_val:.2f}", annotation_position="top"
    )

    fig_strike.update_layout(
        height=380, template="plotly_white", margin=dict(l=40, r=40, t=20, b=30),
        xaxis=dict(title="Strike", tickmode="array", tickvals=df_strikes["strike"], tickangle=-45, showgrid=True, gridcolor="#f0f0f0"),
        yaxis=dict(title="Net Writer Delta (Lakhs)", showgrid=True, gridcolor="#f0f0f0", zeroline=True, zerolinecolor="#000000"),
        hovermode="x unified"
    )
    st.plotly_chart(fig_strike, use_container_width=True)

    # Restored Bar Charts for OI Change & Volume
    st.markdown("---")
    st.markdown(f"### 📊 OI Change on {today_str}")
    render_grouped_bar_chart(f"OI Change on {today_str}", 'pe_oi_chg', 'ce_oi_chg', 'OI Change')

    st.markdown(f"### 📊 Total Volume on {today_str}")
    render_grouped_bar_chart(f"Total Volume on {today_str}", 'pe_vol', 'ce_vol', 'Volume')

# ==========================================
# 6. REFRESH LOOP
# ==========================================
if auto_refresh and is_market_open:
    time.sleep(60)
    st.rerun()
