import time
import threading
import sys
import logging

# Simple in-memory analytics store
class TradeAnalytics:
    def __init__(self):
        self.trades = []
        self.missed = []  # missed buys/sells
        self.logs = []    # real-time logs
        self.metrics = {
            "detection_to_buy_ms": 0,
            "buy_to_sell_ms": 0,
            "rpc_latency_ms": 0
        }
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

    def log_missed(self, token, detected, buy_attempted, buy_time, sell_attempted, sell_time, reason):
        with self.lock:
            self.missed.append({
                "token": token,
                "detected": detected,
                "buy_attempted": buy_attempted,
                "buy_time": buy_time,
                "sell_attempted": sell_attempted,
                "sell_time": sell_time,
                "reason": reason
            })

    def log_event(self, message):
        with self.lock:
            ts = time.strftime('%H:%M:%S')
            self.logs.append(f'[{ts}] {message}')
            # Keep only last 100 logs
            self.logs = self.logs[-100:]

    def set_metrics(self, detection_to_buy, buy_to_sell, rpc_latency):
        with self.lock:
            self.metrics = {
                "detection_to_buy_ms": detection_to_buy,
                "buy_to_sell_ms": buy_to_sell,
                "rpc_latency_ms": rpc_latency
            }

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

    def get_missed(self):
        with self.lock:
            return self.missed[-10:]  # last 10 missed

    def get_logs(self):
        with self.lock:
            return self.logs[-20:]  # last 20 logs

    def get_metrics(self):
        with self.lock:
            return self.metrics

class AnalyticsLogHandler(logging.Handler):
    def emit(self, record):
        msg = self.format(record)
        analytics.log_event(msg)

class StdoutInterceptor:
    def __init__(self, orig_stdout, analytics):
        self.orig_stdout = orig_stdout
        self.analytics = analytics
    def write(self, msg):
        self.orig_stdout.write(msg)
        if msg.strip():
            self.analytics.log_event(msg.strip())
    def flush(self):
        self.orig_stdout.flush()

analytics = TradeAnalytics()

# Pipe all logging to analytics
log_handler = AnalyticsLogHandler()
log_handler.setLevel(logging.INFO)
logging.getLogger().addHandler(log_handler)

# Pipe all print statements (stdout) to analytics
sys.stdout = StdoutInterceptor(sys.stdout, analytics)
sys.stderr = StdoutInterceptor(sys.stderr, analytics)

# Example: populate with some dummy data for dashboard testing
analytics.log_missed('NEWCOIN', '12:01:02', False, None, False, None, 'Slippage too high')
analytics.log_event('Detected NEWCOIN...')
analytics.log_event('Missed buy: slippage too high')
analytics.set_metrics(120, 3500, 80)
