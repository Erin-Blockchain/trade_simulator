import streamlit as st
import math
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import io

def binomial_pmf(n, k, p):
    return math.comb(n, k) * (p**k) * ((1 - p) ** (n - k))

# --- HTML/CSS Helpers for Neon UI ---
def create_neon_card(title, value, color):
    return f"""
    <div style="background-color: #0a0e17; border: 1px solid {color}; border-radius: 8px; padding: 15px; text-align: center; box-shadow: 0 0 12px {color}40; margin-bottom: 20px;">
        <div style="color: #a0aec0; font-size: 14px; margin-bottom: 5px; font-family: sans-serif;">{title}</div>
        <div style="color: {color}; font-size: 28px; font-weight: bold; font-family: monospace;">{value}</div>
    </div>
    """

# --- Page Config ---
st.set_page_config(page_title="Trading Model Analysis", layout="wide", initial_sidebar_state="expanded")

# Inject global CSS for the terminal feel
st.markdown("""
    <style>
    .stApp { background-color: #05070a; }
    h1 { color: #ffffff; text-align: center; font-family: sans-serif; }
    </style>
""", unsafe_allow_html=True)

st.markdown("<h1>✨ Trading Strategy Performance Analysis 📈</h1>", unsafe_allow_html=True)

# --- Interactive Sidebar ---
st.sidebar.markdown("<h2 style='color: white;'>Parameters</h2>", unsafe_allow_html=True)

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
    top_2_wins = [top_1["wins"], top_2["wins"]]
    combined_prob = top_1["prob"] + top_2["prob"]

    wins_list = [item["wins"] for item in outcomes]
    probs_list = [item["prob"] * 100 for item in outcomes]
    pnl_list = [item["pnl"] for item in outcomes]

    # --- 1. Top Metrics Row ---
    col1, col2, col3 = st.columns(3)
    with col1:
        st.markdown(create_neon_card("EV Per Trade", f"${ev_per_trade:,.2f}", "#00ff88"), unsafe_allow_html=True)
    with col2:
        st.markdown(create_neon_card(f"Total EV ({n_trades} Trades)", f"${total_ev:,.2f}", "#00e5ff"), unsafe_allow_html=True)
    with col3:
        st.markdown(create_neon_card("Win Rate", f"{win_rate_pct}%", "#ff00aa"), unsafe_allow_html=True)

    # --- 2. Discrete Outcomes HTML Table ---
    st.markdown(f"<h3 style='color: white; font-size: 16px; margin-top: 10px;'>Discrete Outcome Probabilities for {n_trades} Trades</h3>", unsafe_allow_html=True)
    
    # Fix: Formatted as a flat string without indentation so Markdown doesn't mistake it for a code block
    table_html = "<div style='background-color: #0a0e17; padding: 20px; border-radius: 8px; border: 1px solid #1f2937; margin-bottom: 30px;'>"
    table_html += "<table style='width: 100%; border-collapse: collapse; color: white; font-family: monospace; text-align: center;'>"
    table_html += "<tr style='border-bottom: 1px solid #333; color: #a0aec0;'><th style='padding: 10px;'>Wins</th><th style='padding: 10px;'>Losses</th><th style='padding: 10px;'>Probability</th><th style='padding: 10px;'>Total PnL</th></tr>"
    
    for item in outcomes:
        pnl_color = "#ff3366" if item['pnl'] < 0 else "#00ff88"
        table_html += f"<tr style='border-bottom: 1px solid #1a202c;'><td style='padding: 10px;'>{item['wins']}</td><td style='padding: 10px;'>{item['losses']}</td><td style='padding: 10px;'>{item['prob']*100:.2f}%</td><td style='padding: 10px; color: {pnl_color}; font-weight: bold;'>${item['pnl']:,.2f}</td></tr>"
    
    table_html += "</table></div>"
    st.markdown(table_html, unsafe_allow_html=True)

    # --- 3. Interactive Plotly Charts (UI) ---
    col_chart1, col_chart2 = st.columns(2)
    
    with col_chart1:
        # Probability Bar Chart (Logic fix: Top 2 outcomes highlighted cyan)
        bar_colors = ['#00e5ff' if w in top_2_wins else '#1f4287' for w in wins_list]
        fig_bar = go.Figure(data=[go.Bar(x=wins_list, y=probs_list, marker_color=bar_colors, opacity=0.9)])
        fig_bar.update_layout(
            title="Probability Distribution", title_font=dict(color="white"),
            paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)',
            xaxis=dict(title="Number of Wins", color="#a0aec0", tickmode='linear', dtick=1, showgrid=False),
            yaxis=dict(title="Probability (%)", color="#a0aec0", showgrid=True, gridcolor="#1a202c"),
            margin=dict(l=20, r=20, t=40, b=20)
        )
        st.plotly_chart(fig_bar, use_container_width=True)

    with col_chart2:
        # PnL Line Chart (Logic fix: Line neutral, points red/green, break-even matched)
        marker_colors = ['#ff3366' if pnl < 0 else '#00ff88' for pnl in pnl_list]
        fig_line = go.Figure()
        fig_line.add_trace(go.Scatter(
            x=wins_list, y=pnl_list, mode='lines+markers',
            line=dict(color='#3a4a5a', width=2),
            marker=dict(size=10, color=marker_colors, line=dict(width=1, color='white'))
        ))
        fig_line.add_hline(y=0, line_dash="dash", line_color="#ff3366", annotation_text="Break-Even ($0)", annotation_font_color="#ff3366")
        fig_line.update_layout(
            title="Total PnL vs. Number of Wins", title_font=dict(color="white"),
            paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)',
            xaxis=dict(title="Number of Wins", color="#a0aec0", tickmode='linear', dtick=1, showgrid=False),
            yaxis=dict(title="Total PnL ($)", color="#a0aec0", showgrid=True, gridcolor="#1a202c"),
            margin=dict(l=20, r=20, t=40, b=20),
            showlegend=False
        )
        st.plotly_chart(fig_line, use_container_width=True)

    # --- 4. Conclusion Card ---
    st.markdown(f"""
    <div style="background-color: #0a0e17; border: 1px solid #00e5ff; border-radius: 8px; padding: 20px; box-shadow: 0 0 15px #00e5ff30; margin-top: 10px;">
        <div style="color: #ff00aa; font-weight: bold; font-size: 18px; margin-bottom: 8px;">🎯 Most Likely Outcomes</div>
        <div style="color: #e2e8f0; font-size: 16px; line-height: 1.5;">
            There is a combined <b style="color: #00ff88;">{combined_prob * 100:.2f}%</b> chance that you will hit 
            either <b style="color: white;">{top_1['wins']} wins</b> (yielding <span style="color: {'#00ff88' if top_1['pnl'] >= 0 else '#ff3366'};">${top_1['pnl']:,.2f}</span>) 
            or <b style="color: white;">{top_2['wins']} wins</b> (yielding <span style="color: {'#00ff88' if top_2['pnl'] >= 0 else '#ff3366'};">${top_2['pnl']:,.2f}</span>) 
            out of your {n_trades} trades.
        </div>
    </div>
    """, unsafe_allow_html=True)

    st.divider()

    # --- 5. Generate Composite Plotly Image for Download ---
    with st.spinner("Generating downloadable composite report..."):
        # Build an invisible subplot figure holding ALL data to render as a single PNG
        comp_fig = make_subplots(
            rows=4, cols=6, vertical_spacing=0.08,
            specs=[
                [{"type": "indicator", "colspan": 2}, None, {"type": "indicator", "colspan": 2}, None, {"type": "indicator", "colspan": 2}, None],
                [{"type": "table", "colspan": 6}, None, None, None, None, None],
                [{"type": "xy", "colspan": 3}, None, None, {"type": "xy", "colspan": 3}, None, None],
                [{"type": "table", "colspan": 6}, None, None, None, None, None] # Dummy row for text annotation spacing
            ],
            row_heights=[0.1, 0.4, 0.4, 0.1],
            subplot_titles=(None, None, None, "Discrete Outcome Probabilities", "Probability Distribution", "Total PnL vs. Number of Wins", None)
        )

        # 5a. Indicators
        comp_fig.add_trace(go.Indicator(mode="number", value=ev_per_trade, number={'prefix': "$", 'valueformat': ".2f", 'font': {'color': '#00ff88', 'size': 40}}, title={"text": "EV Per Trade", "font": {"color": "#a0aec0"}}), row=1, col=1)
        comp_fig.add_trace(go.Indicator(mode="number", value=total_ev, number={'prefix': "$", 'valueformat': ".2f", 'font': {'color': '#00e5ff', 'size': 40}}, title={"text": "Total EV", "font": {"color": "#a0aec0"}}), row=1, col=3)
        comp_fig.add_trace(go.Indicator(mode="number", value=win_rate_pct, number={'suffix': "%", 'valueformat': ".2f", 'font': {'color': '#ff00aa', 'size': 40}}, title={"text": "Win Rate", "font": {"color": "#a0aec0"}}), row=1, col=5)

        # 5b. Table
        table_font_colors = [['white']*len(wins_list), ['white']*len(wins_list), ['white']*len(wins_list), ['#ff3366' if p < 0 else '#00ff88' for p in pnl_list]]
        comp_fig.add_trace(go.Table(
            header=dict(values=["Wins", "Losses", "Probability", f"Total PnL ({n_trades} Trades)"], fill_color='#1a202c', font=dict(color='white', size=14)),
            cells=dict(values=[wins_list, [n_trades - w for w in wins_list], [f"{p:.2f}%" for p in probs_list], [f"${p:,.2f}" for p in pnl_list]], fill_color='#0a0e17', font=dict(color=table_font_colors, size=13), height=28)
        ), row=2, col=1)

        # 5c. Bar Chart
        comp_fig.add_trace(go.Bar(x=wins_list, y=probs_list, marker_color=bar_colors, showlegend=False), row=3, col=1)

        # 5d. Line Chart
        comp_fig.add_trace(go.Scatter(x=wins_list, y=pnl_list, mode='lines+markers', line=dict(color='#3a4a5a', width=2), marker=dict(size=8, color=marker_colors), showlegend=False), row=3, col=4)
        
        # Fix: Replaced add_hline with a robust scatter trace to avoid PlotlyKeyError on complex subplots
        comp_fig.add_trace(go.Scatter(x=[min(wins_list), max(wins_list)], y=[0, 0], mode='lines', line=dict(dash="dash", color="#ff3366", width=2), showlegend=False), row=3, col=4)

        # 5e. Layout & Annotations
        comp_fig.update_layout(
            template='plotly_dark', paper_bgcolor='#05070a', plot_bgcolor='#05070a',
            height=1200, width=1000, title="Trading Strategy Performance Analysis", title_x=0.5, title_font=dict(size=24),
            margin=dict(b=120),
            annotations=[
                dict(
                    x=0.5, y=-0.1, xref="paper", yref="paper",
                    text=f"Most Likely Outcomes: {combined_prob * 100:.2f}% chance of hitting either {top_1['wins']} wins (${top_1['pnl']:,.2f}) or {top_2['wins']} wins (${top_2['pnl']:,.2f}).",
                    showarrow=False, font=dict(size=16, color="#00e5ff"), bgcolor="#0a0e17", bordercolor="#00e5ff", borderwidth=1, borderpad=15
                )
            ]
        )
        comp_fig.update_xaxes(tickmode='linear', dtick=1, title_text="Number of Wins", showgrid=False, row=3, col=1)
        comp_fig.update_xaxes(tickmode='linear', dtick=1, title_text="Number of Wins", showgrid=False, row=3, col=4)
        comp_fig.update_yaxes(title_text="Probability (%)", gridcolor="#1a202c", row=3, col=1)
        comp_fig.update_yaxes(title_text="Total PnL ($)", gridcolor="#1a202c", row=3, col=4)

        # Generate bytes via Kaleido
        try:
            img_bytes = comp_fig.to_image(format="png", engine="kaleido", scale=2)
            st.download_button(
                label="📥 Download Full Report as PNG",
                data=img_bytes,
                file_name="trading_analysis_terminal.png",
                mime="image/png"
            )
        except ValueError as e:
            st.error("Error generating PNG. Make sure 'kaleido' is installed in your Python environment (`pip install kaleido`).")