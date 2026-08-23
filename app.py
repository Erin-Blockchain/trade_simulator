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

# Replaced all sliders with number_input for direct value entry
capital = st.sidebar.number_input("Capital per trade ($)", value=100.00, format="%.2f")
sl_pct = st.sidebar.number_input("Stop-Loss %", value=70.00, format="%.2f")
tp_mult = st.sidebar.number_input("Take-Profit Multiplier (x)", value=10.00, format="%.2f")
win_rate_pct = st.sidebar.number_input("Win Rate %", value=66.66, format="%.2f")
n_trades = st.sidebar.number_input("Number of Trades", value=10, step=1)

if st.sidebar.button("Run Simulation", type="primary"):
    # --- Core Calculations ---
    p = win_rate_pct / 100.0
    loss_rate = 1.0 - p
    loss_amount = -1.0 * capital * (sl_pct / 100.0)
    profit_amount = (capital * tp_mult) - capital
    
    ev_per_trade = (p * profit_amount) + (loss_rate * loss_amount)
    total_ev = ev_per_trade * n_trades
    
    outcomes = []
    for wins in range(n_trades + 1):
        losses = n_trades - wins
        prob = binomial_pmf(n_trades, wins, p)
        total_pnl = (wins * profit_amount) + (losses * loss_amount)
        outcomes.append({"wins": wins, "losses": losses, "prob": prob, "pnl": total_pnl})

    # Find Top 2 Most Likely Outcomes
    sorted_outcomes = sorted(outcomes, key=lambda x: x["prob"], reverse=True)
    top_1, top_2 = sorted_outcomes[0], sorted_outcomes[1]
    combined_prob = top_1["prob"] + top_2["prob"]

    wins_list = [item["wins"] for item in outcomes]
    probs_list = [item["prob"] * 100 for item in outcomes]
    pnl_list = [item["pnl"] for item in outcomes]

    # --- Generate Master Image Canvas ---
    fig = plt.figure(figsize=(10, 16), facecolor='white')
    gs = GridSpec(4, 2, height_ratios=[1.2, 1.5, 2, 0.8], figure=fig)
    fig.suptitle("Trading Strategy Performance Analysis", fontsize=18, weight='bold', y=0.96)

    # 1. Text Info (Parameters & EV)
    ax_info = fig.add_subplot(gs[0, :])
    ax_info.axis('off')
    info_text = (
        f"1. Trade Parameters & Unit Breakdown\n"
        f"  • Capital per Trade: ${capital:.2f}\n"
        f"  • Stop-Loss ({sl_pct}%): -${abs(loss_amount):.2f} per losing trade\n"
        f"  • Take-Profit ({tp_mult}x): ${profit_amount:.2f} profit per winning trade\n"
        f"  • Win Rate: {win_rate_pct}%\n"
        f"  • Loss Rate: {100 - win_rate_pct:.2f}%\n\n"
        f"2. Expected Value (EV) Per Trade\n"
        f"  • EV Per Trade: ${ev_per_trade:.2f}\n"
        f"  • Total Expected Value for {n_trades} Trades: ${total_ev:.2f}"
    )
    ax_info.text(0, 1, info_text, va='top', ha='left', fontsize=12, family='monospace', linespacing=1.5)

    # 2. Discrete Outcomes Table
    ax_table = fig.add_subplot(gs[1, :])
    ax_table.axis('off')
    # Pad added to prevent overlap
    ax_table.set_title(f"3. Discrete Outcome Probabilities for {n_trades} Trades", loc='left', fontsize=12, weight='bold', family='monospace', pad=40)
    
    col_labels = ["Wins", "Losses", "Probability", f"Total PnL ({n_trades} Trades)"]
    cell_text = [
        [str(item["wins"]), str(item["losses"]), f"{item['prob']*100:.2f}%", f"${item['pnl']:.2f}"] 
        for item in outcomes
    ]
    
    table = ax_table.table(cellText=cell_text, colLabels=col_labels, loc='center', cellLoc='center')
    table.auto_set_font_size(False)
    table.set_fontsize(10)
    table.scale(1, 1.6)

    # 3a. Probability Bar Chart
    ax_bar = fig.add_subplot(gs[2, 0])
    ax_bar.bar(wins_list, probs_list, color='#4C72B0', edgecolor='black')
    ax_bar.set_title("Probability Distribution", weight='bold')
    ax_bar.set_xlabel("Number of Wins")
    ax_bar.set_ylabel("Probability (%)")
    ax_bar.grid(axis='y', linestyle='--', alpha=0.7)

    # 3b. PnL Line Chart
    ax_line = fig.add_subplot(gs[2, 1])
    ax_line.plot(wins_list, pnl_list, marker='o', color='#55A868', linewidth=2)
    ax_line.axhline(0, color='#C44E52', linestyle='--', label='Break-Even ($0)')
    ax_line.set_title("Total PnL vs. Number of Wins", weight='bold')
    ax_line.set_xlabel("Number of Wins")
    ax_line.set_ylabel("Total PnL ($)")
    ax_line.legend()
    ax_line.grid(True, linestyle='--', alpha=0.7)

    # 4. Conclusion
    ax_conc = fig.add_subplot(gs[3, :])
    ax_conc.axis('off')
    
    # \$ added to prevent matplotlib rendering the text as a math equation
    conc_text = (
        f"4. Conclusion & Most Likely Outcomes\n"
        f"Most Likely Outcomes: There is a combined {combined_prob * 100:.2f}% chance that you will hit\n"
        f"either {top_1['wins']} wins (yielding \${top_1['pnl']:.2f}) or {top_2['wins']} wins (yielding \${top_2['pnl']:.2f})\n"
        f"out of your {n_trades} trades."
    )
    ax_conc.text(0, 0.7, conc_text, va='top', ha='left', fontsize=12, family='monospace', weight='bold', linespacing=1.5)

    plt.tight_layout(pad=3.0)
    
    # Render full composite image in the browser
    st.pyplot(fig)

    # --- Create Download Button ---
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches='tight', dpi=200)
    st.download_button(
        label="Download Full Report as PNG",
        data=buf.getvalue(),
        file_name="trading_analysis_report.png",
        mime="image/png"
    )