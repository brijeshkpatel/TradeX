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
    value=500,
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
        
        # Extract Greeks
        ce_greeks = ce.get("greeks", {}) if isinstance(ce, dict) else {}
        pe_greeks = pe.get("greeks", {}) if isinstance(pe, dict) else {}
        ce_delta = extract_val(ce_greeks, ["delta"]) or 0.0
        pe_delta = extract_val(pe_greeks, ["delta"]) or 0.0
        
        # Calculate Delta Exposures (in Crores)
        ce_exp = (ce_oi * ce_delta * strike) / 10000000
        pe_exp = (pe_oi * pe_delta * strike) / 10000000
        net_exp = ce_exp + pe_exp

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
            "net_writer_delta": pe_chg - ce_chg,
            "ce_delta": ce_delta,
            "pe_delta": pe_delta,
            "ce_exp": ce_exp,
            "pe_exp": pe_exp,
            "net_exp": net_exp
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

# Robust Fallback
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
    if not df_history.empty:
        oi_max_row = df_history.loc[df_history['pcr_oi'].idxmax()]
        oi_min_row = df_history.loc[df_history['pcr_oi'].idxmin()]
        chg_max_row = df_history.loc[df_history['pcr_chg'].idxmax()]
        chg_min_row = df_history.loc[df_history['pcr_chg'].idxmin()]
        
        t_oi_max, t_oi_min = f"{oi_max_row['pcr_oi']} ({oi_max_row['timestamp'].strftime('%H:%M')})", f"{oi_min_row['pcr_oi']} ({oi_min_row['timestamp'].strftime('%H:%M')})"
        t_chg_max, t_chg_min = f"{chg_max_row['pcr_chg']} ({chg_max_row['timestamp'].strftime('%H:%M')})", f"{chg_min_row['pcr_chg']} ({chg_min_row['timestamp'].strftime('%H:%M')})"
    else:
        now_str = display_data['timestamp'].strftime("%H:%M")
        t_oi_max = t_oi_min = f"{display_data['pcr_oi']} ({now_str})"
        t_chg_max = t_chg_min = f"{display_data['pcr_chg']} ({now_str})"

    m1, m2 = st.columns(2)
    m1.metric("Nifty Spot", f"{display_data['spot']:.2f}")
    m2.metric("Session Writer Delta", f"{display_data['net_writer_delta']/100000:+.2f}L")

    # ==========================================
    # DELTA EXPOSURE SECTION (NEW)
    # ==========================================
    st.markdown("---")
    st.markdown("### 🎯 Institutional Delta Exposure Levels")
    
    df_strikes = display_data["strike_df"].copy()
    
    # Calculate peaks safely if net_exp exists in current dataframe
    if not df_strikes.empty and 'net_exp' in df_strikes.columns:
        peak_pos = df_strikes.loc[df_strikes['net_exp'].idxmax()]
        peak_neg = df_strikes.loc[df_strikes['net_exp'].idxmin()]
        call_res = df_strikes.loc[df_strikes['ce_exp'].idxmax()]
        put_sup = df_strikes.loc[df_strikes['pe_exp'].idxmin()]
        
        df_strikes['prev_net'] = df_strikes['net_exp'].shift(1)
        flip_rows = df_strikes[(df_strikes['prev_net'] < 0) & (df_strikes['net_exp'] > 0)]
        delta_flip_strike = flip_rows.iloc[0]['strike'] if not flip_rows.empty else "None"
        
        d1, d2, d3, d4, d5 = st.columns(5)
        d1.metric("Peak Positive (+Delta exp)", f"{peak_pos['strike']}", f"{peak_pos['net_exp']:,.2f} Cr")
        d2.metric("Peak Negative (-Delta exp)", f"{peak_neg['strike']}", f"{peak_neg['net_exp']:,.2f} Cr", delta_color="inverse")
        d3.metric("Call Resistance", f"{call_res['strike']}", f"{call_res['ce_exp']:,.2f} Cr")
        d4.metric("Put Support", f"{put_sup['strike']}", f"{put_sup['pe_exp']:,.2f} Cr", delta_color="inverse")
        d5.metric("Delta Flip Strike", f"{delta_flip_strike}", "Trend Reversal", delta_color="off")
        
        # Exposure Chart
        fig_exp = go.Figure()
        fig_exp.add_trace(go.Bar(x=df_strikes['strike'], y=df_strikes['ce_exp'], name='Call Exposure', marker_color='#F44336'))
        fig_exp.add_trace(go.Bar(x=df_strikes['strike'], y=df_strikes['pe_exp'], name='Put Exposure', marker_color='#4CAF50'))
        fig_exp.add_trace(go.Scatter(x=df_strikes['strike'], y=df_strikes['net_exp'], mode='lines+markers', name='Net Exposure', line=dict(color='#FFB300', width=3)))
        
        fig_exp.add_vline(x=display_data["spot"], line_dash="dash", line_color="#E0E0E0", annotation_text=f"Spot {display_data['spot']:.2f}", annotation_position="top")
        
        fig_exp.update_layout(
            barmode='relative', height=400, template="plotly_dark",
            margin=dict(l=40, r=40, t=20, b=20),
            xaxis=dict(title="Strike", showgrid=True, gridcolor="#263238"),
            yaxis=dict(title="Exposure (Cr)", showgrid=True, gridcolor="#263238"),
            hovermode="x unified", legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1)
        )
        st.plotly_chart(fig_exp, use_container_width=True)

    st.markdown("---")
    # Broad Market Order Flow
    st.markdown(f"### Broad Market Order Flow (±{strike_depth} Active Zone)")
    o1, o2 = st.columns(2)
    
    with o1:
        st.metric("Total PCR (OI)", display_data['pcr_oi'])
        st.markdown(f"<span style='color:#4CAF50; font-size: 0.8rem;'>High: {t_oi_max}</span> &nbsp;|&nbsp; <span style='color:#F44336; font-size: 0.8rem;'>Low: {t_oi_min}</span>", unsafe_allow_html=True)
        
    with o2:
        st.metric("Intraday PCR (OI Change)", display_data['pcr_chg'])
        st.markdown(f"<span style='color:#4CAF50; font-size: 0.8rem;'>High: {t_chg_max}</span> &nbsp;|&nbsp; <span style='color:#F44336; font-size: 0.8rem;'>Low: {t_chg_min}</span>", unsafe_allow_html=True)

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

        with col_pcr1:
            fig_pcr_oi = go.Figure()
            fig_pcr_oi.add_trace(go.Scatter(x=df_plot["timestamp"], y=df_plot["pcr_oi"], mode="lines+markers", line=dict(color="#00E5FF", width=2.5)))
            fig_pcr_oi.add_hline(y=1.0, line_dash="dash", line_color="#E0E0E0")
            fig_pcr_oi.update_layout(title="Macro PCR (Total OI)", height=300, template="plotly_dark", margin=dict(l=40, r=40, t=50, b=30))
            st.plotly_chart(fig_pcr_oi, use_container_width=True)

        with col_pcr2:
            fig_pcr_chg = go.Figure()
            fig_pcr_chg.add_trace(go.Scatter(x=df_plot["timestamp"], y=df_plot["pcr_chg_clamped"], mode="lines+markers", line=dict(color="#FFB300", width=2.5)))
            fig_pcr_chg.add_hline(y=1.0, line_dash="dash", line_color="#E0E0E0")
            fig_pcr_chg.update_layout(title="Intraday Flow PCR (OI Change)", height=300, template="plotly_dark", margin=dict(l=40, r=40, t=50, b=30))
            st.plotly_chart(fig_pcr_chg, use_container_width=True)

    # ==========================================
    # 7. STRIKE DISTRIBUTION CHARTS
    # ==========================================
    spot_val = display_data["spot"]
    
    st.markdown("---")
    st.markdown(f"### 📌 Current Strike-Wise Writer Dominance")
    fig_strike = go.Figure()
    bar_colors = ["#4CAF50" if x >= 0 else "#F44336" for x in df_strikes["net_writer_delta"]]
    fig_strike.add_trace(go.Bar(x=df_strikes["strike"], y=df_strikes["net_writer_delta"] / 100000.0, marker_color=bar_colors, name="Net Put - Call Add"))
    fig_strike.add_vline(x=spot_val, line_dash="dash", line_color="#424242", annotation_text=f"NIFTY {spot_val:.2f}")
    fig_strike.update_layout(height=380, template="plotly_white", margin=dict(l=40, r=40, t=20, b=30))
    st.plotly_chart(fig_strike, use_container_width=True)

# ==========================================
# 8. REFRESH LOOP
# ==========================================
if auto_refresh and is_market_open:
    time.sleep(60)
    st.rerun()
