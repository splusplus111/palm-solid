import time
import threading

# Simple in-memory analytics store
class TradeAnalytics:
    def __init__(self):
        self.trades = []
        self.lock = threading.Lock()

    def log_trade(self, mint, action, amount, price, timestamp=None):
        with self.lock:
            self.trades.append({
                "mint": mint,
                "action": action,  # 'buy' or 'sell'
                "amount": amount,
                "price": price,
                "timestamp": timestamp or time.time()
            })

    def get_profit(self):
        profit = 0.0
        buys = {}
        for t in self.trades:
            if t["action"] == "buy":
                buys.setdefault(t["mint"], 0.0)
                buys[t["mint"]] += t["amount"] * t["price"]
            elif t["action"] == "sell":
                profit += t["amount"] * t["price"]
        total_buy = sum(buys.values())
        return profit - total_buy

    def summary(self):
        return {
            "total_trades": len(self.trades),
            "profit": self.get_profit(),
            "trades": self.trades[-10:]  # last 10 trades
        }

analytics = TradeAnalytics()
