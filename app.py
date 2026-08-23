import streamlit as st
import matplotlib.pyplot as plt
from matplotlib.gridspec import GridSpec
import math
import io

def binomial_pmf(n, k, p):
    return math.comb(n, k) * (p**k) * ((1 - p) ** (n - k))

# --- Page Config ---
st.set_page_config(page_title="Trading Model Analysis", layout="wide")
st.title("Trading Model Analysis Generator")

# --- Interactive Sidebar ---
st.sidebar.header("Trade Parameters")
capital = st.sidebar.number_input("Capital per trade ($)", value=10.0)
sl_pct = st.sidebar.slider("Stop-Loss %", 1.0, 100.0, 50.0)
tp_mult = st.sidebar.number_input("Take-Profit Multiplier (x)", value=10.0)
win_rate_pct = st.sidebar.slider("Win Rate %", 1.0, 100.0, 27.0)
n_trades = st.sidebar.number_input("Number of Trades", value=10, step=1)

if st.sidebar.button("Run Simulation", type="primary"):
    # --- Core Calculations ---
    p = win_rate_pct / 100.0
    loss_amount = -1.0 * capital * (sl_pct / 100.0)
    profit_amount = (capital * tp_mult) - capital
    
    outcomes = []
    for wins in range(n_trades + 1):
        losses = n_trades - wins
        prob = binomial_pmf(n_trades, wins, p)
        total_pnl = (wins * profit_amount) + (losses * loss_amount)
        outcomes.append({"wins": wins, "prob": prob, "pnl": total_pnl})

    wins_list = [item["wins"] for item in outcomes]
    probs_list = [item["prob"] * 100 for item in outcomes]
    pnl_list = [item["pnl"] for item in outcomes]

    # --- Generate Chart Canvas ---
    fig = plt.figure(figsize=(10, 8))
    gs = GridSpec(2, 1, height_ratios=[1, 1], figure=fig)
    
    ax_bar = fig.add_subplot(gs[0])
    ax_bar.bar(wins_list, probs_list, color='#4C72B0', edgecolor='black')
    ax_bar.set_title("Probability Distribution", weight='bold')
    ax_bar.set_ylabel("Probability (%)")

    ax_line = fig.add_subplot(gs[1])
    ax_line.plot(wins_list, pnl_list, marker='o', color='#55A868', linewidth=2)
    ax_line.axhline(0, color='#C44E52', linestyle='--', label='Break-Even ($0)')
    ax_line.set_title("Total PnL vs. Number of Wins", weight='bold')
    ax_line.set_xlabel("Number of Wins")
    ax_line.set_ylabel("Total PnL ($)")
    ax_line.legend()

    plt.tight_layout()
    
    # Render in the browser
    st.pyplot(fig)

    # --- Create Download Button ---
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches='tight')
    st.download_button(
        label="Download Chart as PNG",
        data=buf.getvalue(),
        file_name="trading_analysis_report.png",
        mime="image/png"
    )