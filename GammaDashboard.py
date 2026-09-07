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
    "sslmode": st.secrets.get("DB_SSLMODE", "require")
}

ist = pytz.timezone("Asia/Kolkata")

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

def fetch_today_records():
    """Fetches all snapshot rows recorded today, converted to Asia/Kolkata timezone."""
    try:
        conn = get_db_connection()
        query = """
            SELECT * FROM market_snapshots_1m 
            WHERE timestamp::date = CURRENT_DATE 
            ORDER BY timestamp ASC;
        """
        df = pd.read_sql_query(query, conn)
        conn.close()
        if not df.empty:
            df['timestamp'] = pd.to_datetime(df['timestamp'], utc=True).dt.tz_convert('Asia/Kolkata')
        return df
    except Exception as e:
        st.sidebar.error(f"DB Read Error: {e}")
        return pd.DataFrame()

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
# 3. SIDEBAR PARAMETERS & DATABASE UTILITY
# ==========================================
st.sidebar.title("⚙️ Strategy Parameters")

strike_depth = st.sidebar.slider(
    "🎯 Strike Range (± Points)",
    min_value=100,
    max_value=1000,
    value=400,
    step=50,
    help="Restricts ALL calculations to this specific zone."
)

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
if "daily_baseline_oi" not in st.session_state:
    st.session_state.daily_baseline_oi = {}

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
        else:
            st.sidebar.error(f"Dhan API [{response.status_code}]: {response.text[:120]}")
    except Exception as e:
        st.sidebar.error(f"Dhan Connection Error: {e}")
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
    atm_ce_iv, atm_pe_iv = 14.0, 14.0

    for strike_str, details in option_chain.items():
        strike = float(strike_str)
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

    atm_iv = (atm_ce_iv + atm_pe_iv) / 2 if (atm_ce_iv + atm_pe_iv) > 0 else 14.0
    daily_1sd = spot_price * (atm_iv / 100) * math.sqrt(1 / 252)

    sd1_upper = round(spot_price + daily_1sd, 2)
    sd1_lower = round(spot_price - daily_1sd, 2)
    sd2_upper = round(spot_price + (2 * daily_1sd), 2)
    sd2_lower = round(spot_price - (2 * daily_1sd), 2)

    pcr_oi = round(total_pe_oi / total_ce_oi, 2) if total_ce_oi > 0 else 1.0
    pcr_vol = round(total_pe_vol / total_ce_vol, 2) if total_ce_vol > 0 else 1.0
    pcr_chg = round(total_pe_oi_chg / (total_ce_oi_chg if total_ce_oi_chg != 0 else 1), 2)

    total_abs_macro_oi = total_ce_oi + total_pe_oi
    vol_oi_velocity = round((total_ce_vol + total_pe_vol) / (total_abs_macro_oi if total_abs_macro_oi != 0 else 1), 2)
    
    total_abs_oi_chg = abs(total_ce_oi_chg) + abs(total_pe_oi_chg)
    intra_vol_oi_velocity = round((total_ce_vol + total_pe_vol) / (total_abs_oi_chg if total_abs_oi_chg != 0 else 1), 2)

    now = datetime.now(ist)

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
# 5. MARKET STATUS & FALLBACK RESOLUTION
# ==========================================
current_time_ist = datetime.now(ist)
market_open = current_time_ist.replace(hour=9, minute=15, second=0, microsecond=0)
market_close = current_time_ist.replace(hour=15, minute=30, second=0, microsecond=0)
is_market_open = (current_time_ist.weekday() < 5) and (market_open <= current_time_ist <= market_close)

st.title("⚡ Institutional Delta Flow & 1-Minute Logger")

metrics = process_data()

if metrics and is_market_open:
    log_snapshot_to_db(metrics)

# Fetch chronological data straight from DB for today's session
df_history = fetch_today_records()

# Robust Fallback: Determine whether to display Live API data or PostgreSQL snapshot
display_data = None
is_fallback = False

if metrics:
    display_data = metrics
elif not df_history.empty:
    is_fallback = True
    latest_db_row = df_history.iloc[-1]
    raw_strike_data = latest_db_row["strike_details"]
    if isinstance(raw_strike_data, str):
        raw_strike_data = json.loads(raw_strike_data)
    strike_df_recovered = pd.DataFrame(raw_strike_data).sort_values("strike")

    display_data = {
        "timestamp": latest_db_row["timestamp"],
        "spot": float(latest_db_row["spot_price"]),
        "net_writer_delta": float(latest_db_row["net_writer_delta"]),
        "total_ce_oi": float(latest_db_row["total_ce_oi"]),
        "total_pe_oi": float(latest_db_row["total_pe_oi"]),
        "total_ce_oi_chg": float(latest_db_row["total_ce_oi_chg"]),
        "total_pe_oi_chg": float(latest_db_row["total_pe_oi_chg"]),
        "total_ce_vol": float(latest_db_row["total_ce_vol"]),
        "total_pe_vol": float(latest_db_row["total_pe_vol"]),
        "pcr_oi": float(latest_db_row["pcr_oi"]),
        "pcr_vol": float(latest_db_row["pcr_vol"]),
        "pcr_chg": float(latest_db_row["pcr_chg"]),
        "vol_oi_velocity": float(latest_db_row["macro_velocity"]),
        "intra_vol_oi_velocity": float(latest_db_row["intraday_velocity"]),
        "sd1_upper": float(latest_db_row["sd1_upper"]),
        "sd1_lower": float(latest_db_row["sd1_lower"]),
        "sd2_upper": float(latest_db_row["sd2_upper"]),
        "sd2_lower": float(latest_db_row["sd2_lower"]),
        "strike_df": strike_df_recovered
    }

# Status Banner
col_h1, col_h2 = st.columns([8, 2])
with col_h1:
    if display_data and not is_fallback:
        st.success(f"🟢 Live Dhan Feed Active | Snapshot: {display_data['timestamp'].strftime('%H:%M:%S')} IST")
    elif display_data and is_fallback:
        st.warning(f"🟡 Dhan API Offline | Showing Latest Database Snapshot from {display_data['timestamp'].strftime('%H:%M:%S')} IST")
    else:
        st.error("🔴 No Live Feed or Database Records Found. Verify Dhan credentials and DB connection.")

with col_h2:
    auto_refresh = st.toggle("Auto-Refresh (1 Min)", value=is_market_open)

# ==========================================
# 6. DASHBOARD METRICS & CHARTS RENDERING
# ==========================================
if display_data:
    # Compute high/low extrema dynamically from DB records if present
    if not df_history.empty:
        oi_max_row = df_history.loc[df_history['pcr_oi'].idxmax()]
        oi_min_row = df_history.loc[df_history['pcr_oi'].idxmin()]
        chg_max_row = df_history.loc[df_history['pcr_chg'].idxmax()]
        chg_min_row = df_history.loc[df_history['pcr_chg'].idxmin()]
        vol_max_row = df_history.loc[df_history['pcr_vol'].idxmax()]
        vol_min_row = df_history.loc[df_history['pcr_vol'].idxmin()]
        vel_max_row = df_history.loc[df_history['macro_velocity'].idxmax()]
        vel_min_row = df_history.loc[df_history['macro_velocity'].idxmin()]
        intra_max_row = df_history.loc[df_history['intraday_velocity'].idxmax()]
        intra_min_row = df_history.loc[df_history['intraday_velocity'].idxmin()]

        t_oi_max, t_oi_min = f"{oi_max_row['pcr_oi']} ({oi_max_row['timestamp'].strftime('%H:%M')})", f"{oi_min_row['pcr_oi']} ({oi_min_row['timestamp'].strftime('%H:%M')})"
        t_chg_max, t_chg_min = f"{chg_max_row['pcr_chg']} ({chg_max_row['timestamp'].strftime('%H:%M')})", f"{chg_min_row['pcr_chg']} ({chg_min_row['timestamp'].strftime('%H:%M')})"
        t_vol_max, t_vol_min = f"{vol_max_row['pcr_vol']} ({vol_max_row['timestamp'].strftime('%H:%M')})", f"{vol_min_row['pcr_vol']} ({vol_min_row['timestamp'].strftime('%H:%M')})"
        t_vel_max, t_vel_min = f"{vel_max_row['macro_velocity']}x ({vel_max_row['timestamp'].strftime('%H:%M')})", f"{vel_min_row['macro_velocity']}x ({vel_min_row['timestamp'].strftime('%H:%M')})"
        t_intra_max, t_intra_min = f"{intra_max_row['intraday_velocity']}x ({intra_max_row['timestamp'].strftime('%H:%M')})", f"{intra_min_row['intraday_velocity']}x ({intra_min_row['timestamp'].strftime('%H:%M')})"
    else:
        now_str = display_data['timestamp'].strftime("%H:%M")
        t_oi_max = t_oi_min = f"{display_data['pcr_oi']} ({now_str})"
        t_chg_max = t_chg_min = f"{display_data['pcr_chg']} ({now_str})"
        t_vol_max = t_vol_min = f"{display_data['pcr_vol']} ({now_str})"
        t_vel_max = t_vel_min = f"{display_data['vol_oi_velocity']}x ({now_str})"
        t_intra_max = t_intra_min = f"{display_data['intra_vol_oi_velocity']}x ({now_str})"

    # Spot & Writer Overview
    m1, m2 = st.columns(2)
    m1.metric("Nifty Spot", f"{display_data['spot']:.2f}")
    m2.metric("Session Writer Delta", f"{display_data['net_writer_delta']/100000:+.2f}L")

    st.markdown("---")
    # Broad Market Order Flow
    st.markdown(f"### Broad Market Order Flow (±{strike_depth} Active Zone)")
    o1, o2, o3, o4, o5 = st.columns(5)
    
    with o1:
        st.metric("Total PCR (OI)", display_data['pcr_oi'])
        st.markdown(f"<span style='color:grey; font-size: 0.85rem;'>Calls: {display_data['total_ce_oi']:,.0f} <br> Puts: {display_data['total_pe_oi']:,.0f}</span>", unsafe_allow_html=True)
        st.markdown(f"<span style='color:#4CAF50; font-size: 0.8rem;'>High: {t_oi_max}</span> &nbsp;|&nbsp; <span style='color:#F44336; font-size: 0.8rem;'>Low: {t_oi_min}</span>", unsafe_allow_html=True)
        
    with o2:
        st.metric("Intraday PCR (OI Change)", display_data['pcr_chg'])
        st.markdown(f"<span style='color:grey; font-size: 0.85rem;'>Call Add: {display_data['total_ce_oi_chg']:,.0f} <br> Put Add: {display_data['total_pe_oi_chg']:,.0f}</span>", unsafe_allow_html=True)
        st.markdown(f"<span style='color:#4CAF50; font-size: 0.8rem;'>High: {t_chg_max}</span> &nbsp;|&nbsp; <span style='color:#F44336; font-size: 0.8rem;'>Low: {t_chg_min}</span>", unsafe_allow_html=True)
        
    with o3:
        st.metric("Volume PCR", display_data['pcr_vol'])
        st.markdown(f"<span style='color:grey; font-size: 0.85rem;'>Call Vol: {display_data['total_ce_vol']:,.0f} <br> Put Vol: {display_data['total_pe_vol']:,.0f}</span>", unsafe_allow_html=True)
        st.markdown(f"<span style='color:#4CAF50; font-size: 0.8rem;'>High: {t_vol_max}</span> &nbsp;|&nbsp; <span style='color:#F44336; font-size: 0.8rem;'>Low: {t_vol_min}</span>", unsafe_allow_html=True)
        
    with o4:
        st.metric("Macro Vol/OI Velocity", f"{display_data['vol_oi_velocity']}x")
        st.markdown(f"<span style='color:grey; font-size: 0.85rem;'>Overall Market Direction</span><br>", unsafe_allow_html=True)
        st.markdown(f"<span style='color:#4CAF50; font-size: 0.8rem;'>High: {t_vel_max}</span> &nbsp;|&nbsp; <span style='color:#F44336; font-size: 0.8rem;'>Low: {t_vel_min}</span>", unsafe_allow_html=True)

    with o5:
        st.metric("Intraday Vol/OI Velocity", f"{display_data['intra_vol_oi_velocity']}x")
        st.markdown(f"<span style='color:grey; font-size: 0.85rem;'>Pure Intraday Intensity</span><br>", unsafe_allow_html=True)
        st.markdown(f"<span style='color:#4CAF50; font-size: 0.8rem;'>High: {t_intra_max}</span> &nbsp;|&nbsp; <span style='color:#F44336; font-size: 0.8rem;'>Low: {t_intra_min}</span>", unsafe_allow_html=True)

    st.markdown("---")
    st.markdown("### Statistical Boundaries")
    s1, s2, s3, s4 = st.columns(4)
    s1.success(f"**-2.0 SD:** {display_data['sd2_lower']}")
    s2.success(f"**-1.0 SD:** {display_data['sd1_lower']}")
    s3.warning(f"**+1.0 SD:** {display_data['sd1_upper']}")
    s4.warning(f"**+2.0 SD:** {display_data['sd2_upper']}")

    # ==========================================
    # PCR TRENDS OVER TIME (FROM POSTGRESQL)
    # ==========================================
    st.markdown("---")
    st.markdown("### 📈 PCR Sentiment & Flow Trends")

    if not df_history.empty:
        df_plot = df_history.copy()
        df_plot["pcr_chg_clamped"] = df_plot["pcr_chg"].astype(float).clip(lower=-0.5, upper=3.0)

        col_pcr1, col_pcr2 = st.columns(2)

        # 1. Total PCR (OI Macro)
        with col_pcr1:
            fig_pcr_oi = go.Figure()

            fig_pcr_oi.add_hrect(
                y0=1.0, y1=2.5, fillcolor="rgba(76, 175, 80, 0.08)",
                layer="below", line_width=0, annotation_text="BULLISH SUPPORT (>1.0)",
                annotation_position="top right", annotation_font=dict(color="#4CAF50", size=10)
            )
            fig_pcr_oi.add_hrect(
                y0=0.0, y1=1.0, fillcolor="rgba(244, 67, 54, 0.08)",
                layer="below", line_width=0, annotation_text="BEARISH RESISTANCE (<1.0)",
                annotation_position="bottom right", annotation_font=dict(color="#F44336", size=10)
            )

            fig_pcr_oi.add_trace(go.Scatter(
                x=df_plot["timestamp"],
                y=df_plot["pcr_oi"],
                mode="lines+markers",
                name="Total PCR",
                line=dict(color="#00E5FF", width=2.5),
                marker=dict(size=4, color="#FFFFFF", line=dict(color="#00E5FF", width=1.5)),
                hovertemplate="Time: %{x|%H:%M}<br>Total PCR: <b>%{y:.2f}</b><extra></extra>"
            ))

            fig_pcr_oi.add_hline(y=1.0, line_dash="dash", line_color="#E0E0E0", line_width=1.5)

            fig_pcr_oi.update_layout(
                title=dict(text="<b>Macro PCR (Total OI)</b>", font=dict(size=15, color="#FFFFFF")),
                height=360,
                template="plotly_dark",
                paper_bgcolor="rgba(15, 23, 42, 0.6)",
                plot_bgcolor="rgba(15, 23, 42, 0.6)",
                margin=dict(l=40, r=40, t=50, b=30),
                xaxis=dict(title="Time (IST)", tickformat="%H:%M", showgrid=True, gridcolor="#263238"),
                yaxis=dict(title="PCR (OI)", range=[0.4, 2.0], showgrid=True, gridcolor="#263238"),
                hovermode="x unified"
            )
            st.plotly_chart(fig_pcr_oi, use_container_width=True)

        # 2. Intraday PCR (OI Change Flow)
        with col_pcr2:
            fig_pcr_chg = go.Figure()

            fig_pcr_chg.add_hrect(
                y0=1.0, y1=3.0, fillcolor="rgba(76, 175, 80, 0.08)",
                layer="below", line_width=0, annotation_text="AGGRESSIVE PUT WRITING (>1.0)",
                annotation_position="top right", annotation_font=dict(color="#4CAF50", size=10)
            )
            fig_pcr_chg.add_hrect(
                y0=-0.5, y1=1.0, fillcolor="rgba(244, 67, 54, 0.08)",
                layer="below", line_width=0, annotation_text="AGGRESSIVE CALL WRITING (<1.0)",
                annotation_position="bottom right", annotation_font=dict(color="#F44336", size=10)
            )

            fig_pcr_chg.add_trace(go.Scatter(
                x=df_plot["timestamp"],
                y=df_plot["pcr_chg_clamped"],
                mode="lines+markers",
                name="Intraday PCR",
                line=dict(color="#FFB300", width=2.5),
                marker=dict(size=4, color="#FFFFFF", line=dict(color="#FFB300", width=1.5)),
                hovertemplate="Time: %{x|%H:%M}<br>Intraday PCR: <b>%{y:.2f}</b><extra></extra>"
            ))

            fig_pcr_chg.add_hline(y=1.0, line_dash="dash", line_color="#E0E0E0", line_width=1.5)
            fig_pcr_chg.add_hline(y=0.0, line_dash="dot", line_color="#78909C", line_width=1)

            fig_pcr_chg.update_layout(
                title=dict(text="<b>Intraday Flow PCR (OI Change)</b>", font=dict(size=15, color="#FFFFFF")),
                height=360,
                template="plotly_dark",
                paper_bgcolor="rgba(15, 23, 42, 0.6)",
                plot_bgcolor="rgba(15, 23, 42, 0.6)",
                margin=dict(l=40, r=40, t=50, b=30),
                xaxis=dict(title="Time (IST)", tickformat="%H:%M", showgrid=True, gridcolor="#263238"),
                yaxis=dict(title="PCR (OI Change)", range=[-0.2, 2.5], showgrid=True, gridcolor="#263238"),
                hovermode="x unified"
            )
            st.plotly_chart(fig_pcr_chg, use_container_width=True)
    else:
        st.info("No recorded database snapshots found for today's session yet.")

    # ==========================================
    # 7. STRIKE DISTRIBUTION CHARTS
    # ==========================================
    df_strikes = display_data["strike_df"]
    spot_val = display_data["spot"]
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

    # Grouped Bar Charts for OI Change & Volume
    st.markdown("---")
    st.markdown(f"### 📊 OI Change on {today_str}")
    render_grouped_bar_chart(f"OI Change on {today_str}", 'pe_oi_chg', 'ce_oi_chg', 'OI Change')

    st.markdown(f"### 📊 Total Volume on {today_str}")
    render_grouped_bar_chart(f"Total Volume on {today_str}", 'pe_vol', 'ce_vol', 'Volume')

# ==========================================
# 8. REFRESH LOOP
# ==========================================
if auto_refresh and is_market_open:
    time.sleep(60)
    st.rerun()
