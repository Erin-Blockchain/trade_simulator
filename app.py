from flask import Flask, render_template, request, jsonify
import math

app = Flask(__name__)

def binomial_pmf(n, k, p):
    return math.comb(n, k) * (p**k) * ((1 - p) ** (n - k))

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/api/simulate', methods=['POST'])
def simulate():
    data = request.json
    capital = float(data.get('capital', 100))
    sl_pct = float(data.get('sl_pct', 70))
    tp_mult = float(data.get('tp_mult', 10))
    win_rate_pct = float(data.get('win_rate', 66.66))
    n_trades = int(data.get('n_trades', 10))

    p = win_rate_pct / 100.0
    loss_amount = -1.0 * capital * (sl_pct / 100.0)
    profit_amount = (capital * tp_mult) - capital
    
    ev_per_trade = (p * profit_amount) + ((1.0 - p) * loss_amount)
    
    outcomes = []
    for wins in range(n_trades + 1):
        losses = n_trades - wins
        prob = binomial_pmf(n_trades, wins, p)
        total_pnl = (wins * profit_amount) + (losses * loss_amount)
        outcomes.append({"wins": wins, "losses": losses, "prob": prob, "pnl": total_pnl})

    return jsonify({
        "ev_per_trade": ev_per_trade,
        "total_ev": ev_per_trade * n_trades,
        "outcomes": outcomes
    })

if __name__ == '__main__':
    app.run(debug=True)