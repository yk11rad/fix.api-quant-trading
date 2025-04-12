# Import libraries
import socket
import ssl
import datetime
import time
import pandas as pd
import numpy as np
import requests
from threading import Thread, Lock
from queue import Queue

# FIX Credentials
# Note: Replace with your own credentials securely (see README for instructions)
QUOTE_HOST = "YOUR_QUOTE_HOST_HERE"  # Placeholder; e.g., demo-uk-eqx-01.p.c-trader.com
QUOTE_PORT = 5211  # Typically unchanged for FIX
QUOTE_SENDER_COMP_ID = "YOUR_QUOTE_SENDER_COMP_ID_HERE"  # Placeholder
QUOTE_TARGET_COMP_ID = "YOUR_QUOTE_TARGET_COMP_ID_HERE"  # Placeholder
QUOTE_SENDER_SUB_ID = "QUOTE"  # Typically unchanged
QUOTE_PASSWORD = "YOUR_QUOTE_PASSWORD_HERE"  # Placeholder
QUOTE_ACCOUNT_NUMBER = "YOUR_QUOTE_ACCOUNT_NUMBER_HERE"  # Placeholder

TRADE_HOST = "YOUR_TRADE_HOST_HERE"  # Placeholder; e.g., demo-uk-eqx-01.p.c-trader.com
TRADE_PORT = 5212  # Typically unchanged for FIX
TRADE_SENDER_COMP_ID = "YOUR_TRADE_SENDER_COMP_ID_HERE"  # Placeholder
TRADE_TARGET_COMP_ID = "YOUR_TRADE_TARGET_COMP_ID_HERE"  # Placeholder
TRADE_SENDER_SUB_ID = "TRADE"  # Typically unchanged
TRADE_PASSWORD = "YOUR_TRADE_PASSWORD_HERE"  # Placeholder
TRADE_ACCOUNT_NUMBER = "YOUR_TRADE_ACCOUNT_NUMBER_HERE"  # Placeholder

# Telegram Credentials
# Note: Replace with your own credentials securely (see README for instructions)
TELEGRAM_BOT_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN_HERE"  # Placeholder
TELEGRAM_CHAT_ID = "YOUR_TELEGRAM_CHAT_ID_HERE"  # Placeholder

FIX_VERSION = "FIX.4.4"
HEARTBEAT_INTERVAL = 10

# Symbol mapping
symbol_id_map = {
    "GBP_JPY": 2  # Updated for GBP/JPY
}

# Strategy parameters
pairs_params = {
    "GBP_JPY": {
        "tolerance": 29.80,    # Pips tolerance for equal highs/lows
        "sl_pips": 49.01,      # Stop loss in pips
        "tp_pips": 149.35,     # Take profit in pips
        "pip_value": 0.01,     # GBP/JPY pip value
        "slippage": 0.02,      # Slippage in pips
        "spread": 0.02,        # Typical spread
        "commission": 0.5      # Commission per 0.01 lot
    }
}

class RealisticExecution:
    def __init__(self, params):
        self.params = params

    def adjust_price(self, price, trade_type):
        slippage = self.params['slippage'] if np.random.random() > 0.5 else -self.params['slippage']
        if trade_type == 'BUY':
            adjusted = price + self.params['spread'] + slippage
        else:
            adjusted = price - self.params['spread'] + slippage
        adjusted += self.params['commission'] * 0.01
        return round(adjusted, 3)

class LiveTrader:
    def __init__(self):
        self.quote_sock = None
        self.trade_sock = None
        self.context = ssl.create_default_context()
        self.quote_seq_num = 1
        self.trade_seq_num = 1
        self.quote_last_heartbeat = time.time()
        self.trade_last_heartbeat = time.time()
        self.quotes = {pair: [] for pair in symbol_id_map.keys()}  # H4 candles
        self.current_ticks = {pair: pd.DataFrame(columns=['time', 'bid', 'ask']) 
                            for pair in symbol_id_map.keys()}
        self.last_signal_times = {pair: datetime.datetime.min.replace(tzinfo=datetime.UTC) 
                               for pair in symbol_id_map.keys()}
        self.active_positions = {}
        self.lock = Lock()
        self.signal_queue = Queue()
        self.last_processed_h4_start = {pair: None for pair in symbol_id_map.keys()}

    def connect(self, host, port, label):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock = self.context.wrap_socket(sock, server_hostname=host)
        sock.settimeout(15)
        try:
            sock.connect((host, port))
            print(f"[CONNECTION] {label} connected to {host}:{port}")
            return sock
        except Exception as e:
            print(f"[ERROR] {label} connection failed: {e}")
            raise

    def get_timestamp(self):
        return datetime.datetime.now(datetime.UTC).strftime("%Y%m%d-%H:%M:%S.%f")[:-3]

    def calculate_checksum(self, message):
        total = sum(ord(c) for c in message) % 256
        return f"{total:03d}"

    def create_fix_message(self, msg_type, fields, sender_comp_id, target_comp_id, sender_sub_id, seq_num):
        utc_time = self.get_timestamp()
        body = (
            f"35={msg_type}|"
            f"49={sender_comp_id}|"
            f"56={target_comp_id}|"
            f"34={seq_num}|"
            f"52={utc_time}|"
            f"57={sender_sub_id}|"
            f"50={sender_sub_id}|"
            + fields
        )
        body_length = len(body.replace("|", "\x01"))
        header = f"8={FIX_VERSION}|9={body_length}|"
        message = header + body
        checksum = self.calculate_checksum(message.replace("|", "\x01"))
        return f"{message}10={checksum}|"

    def send_message(self, sock, msg, label):
        try:
            sock.send(msg.replace("|", "\x01").encode())
            print(f"[SENT] {label}: {msg}")
        except Exception as e:
            print(f"[ERROR] {label} send failed: {e}")
            raise

    def receive_message(self, sock, label):
        try:
            data = sock.recv(4096)
            if not data:
                print(f"[INFO] {label}: No data received")
                return ""
            decoded = data.decode('utf-8', errors='ignore').replace("\x01", "|")
            print(f"[RECEIVED] {label}: {decoded}")
            return decoded
        except socket.timeout:
            print(f"[INFO] {label}: Timeout - no data after 15 seconds")
            return ""
        except Exception as e:
            print(f"[ERROR] {label} receive failed: {e}")
            return ""

    def connect_quote_session(self):
        try:
            self.quote_sock = self.connect(QUOTE_HOST, QUOTE_PORT, "Quotes")
            logon_msg = self.create_fix_message(
                "A",
                f"98=0|108={HEARTBEAT_INTERVAL}|553={QUOTE_ACCOUNT_NUMBER}|554={QUOTE_PASSWORD}|",
                QUOTE_SENDER_COMP_ID, QUOTE_TARGET_COMP_ID, QUOTE_SENDER_SUB_ID, self.quote_seq_num
            )
            self.send_message(self.quote_sock, logon_msg, "Quotes")
            self.quote_seq_num += 1

            msg = self.receive_message(self.quote_sock, "Quotes")
            if "35=A" in msg:
                print("[INFO] Quotes logon successful")
            else:
                print("[ERROR] Quotes logon failed")
                return False

            for instrument, symbol_id in symbol_id_map.items():
                md_req_id = f"MD_{instrument}_{int(time.time())}"
                subscribe_msg = self.create_fix_message(
                    "V",
                    f"262={md_req_id}|263=1|264=1|265=0|267=2|269=0|269=1|146=1|55={symbol_id}|",
                    QUOTE_SENDER_COMP_ID, QUOTE_TARGET_COMP_ID, QUOTE_SENDER_SUB_ID, self.quote_seq_num
                )
                self.send_message(self.quote_sock, subscribe_msg, "Quotes")
                self.quote_seq_num += 1
            return True
        except Exception as e:
            print(f"[ERROR] Quotes connection failed: {e}")
            return False

    def connect_trade_session(self):
        try:
            self.trade_sock = self.connect(TRADE_HOST, TRADE_PORT, "Trading")
            logon_msg = self.create_fix_message(
                "A",
                f"98=0|108={HEARTBEAT_INTERVAL}|553={TRADE_ACCOUNT_NUMBER}|554={TRADE_PASSWORD}|",
                TRADE_SENDER_COMP_ID, TRADE_TARGET_COMP_ID, TRADE_SENDER_SUB_ID, self.trade_seq_num
            )
            self.send_message(self.trade_sock, logon_msg, "Trading")
            self.trade_seq_num += 1

            msg = self.receive_message(self.trade_sock, "Trading")
            if "35=A" in msg:
                print("[INFO] Trading logon successful")
                self.trade_last_heartbeat = time.time()
            else:
                print("[ERROR] Trading logon failed")
                return False
            return True
        except Exception as e:
            print(f"[ERROR] Trading connection failed: {e}")
            return False

    def send_heartbeat(self, sock, label, seq_num):
        heartbeat_msg = self.create_fix_message("0", "", 
            QUOTE_SENDER_COMP_ID if label == "Quotes" else TRADE_SENDER_COMP_ID,
            QUOTE_TARGET_COMP_ID if label == "Quotes" else TRADE_TARGET_COMP_ID,
            QUOTE_SENDER_SUB_ID if label == "Quotes" else TRADE_SENDER_SUB_ID,
            seq_num
        )
        self.send_message(sock, heartbeat_msg, label)
        return seq_num + 1

    def aggregate_h4_candle(self, instrument, current_time):
        with self.lock:
            if self.current_ticks[instrument].empty:
                print(f"[DEBUG] {instrument}: No ticks to build H4 candle")
                return False
            
            current_ts = pd.Timestamp(current_time)
            current_h4_start = current_ts.floor('4H')
            
            if self.last_processed_h4_start[instrument] is None:
                self.last_processed_h4_start[instrument] = current_h4_start
                return False
            
            if current_h4_start == self.last_processed_h4_start[instrument]:
                return False
            
            previous_start = self.last_processed_h4_start[instrument]
            previous_end = current_h4_start
            
            df = self.current_ticks[instrument].copy()
            df['time'] = pd.to_datetime(df['time'], utc=True)
            
            mask = (df['time'] >= previous_start) & (df['time'] < previous_end)
            interval_ticks = df[mask]
            
            if interval_ticks.empty:
                print(f"[DEBUG] {instrument}: No ticks for H4 interval starting at {previous_start}")
                return False
            
            candle = {
                'time': previous_start,
                'open': interval_ticks['bid'].iloc[0],
                'high': interval_ticks['bid'].max(),
                'low': interval_ticks['bid'].min(),
                'close': interval_ticks['bid'].iloc[-1],
                'bid': interval_ticks['bid'].iloc[-1],
                'ask': interval_ticks['ask'].iloc[-1]
            }
            
            print(f"[CANDLE] {instrument}: H4 candle - Open: {candle['open']:.3f}, High: {candle['high']:.3f}, "
                  f"Low: {candle['low']:.3f}, Close: {candle['close']:.3f}")
            
            self.quotes[instrument].append(candle)
            self.quotes[instrument] = sorted(self.quotes[instrument], key=lambda x: x['time'])[-3:]  # Keep last 3 candles
            
            self.current_ticks[instrument] = self.current_ticks[instrument][
                self.current_ticks[instrument]['time'] >= previous_end
            ]
            self.last_processed_h4_start[instrument] = current_h4_start
            return True

    def is_trading_allowed(self):
        now = datetime.datetime.now(datetime.UTC)
        hour = now.hour
        return not (21 <= hour < 23)

    def detect_signals(self, instrument):
        with self.lock:
            if len(self.quotes[instrument]) < 2:
                print(f"[INFO] {instrument}: Need 2+ H4 candles (have {len(self.quotes[instrument])})")
                return None
            
            if not self.is_trading_allowed():
                print(f"[INFO] {instrument}: Trading restricted between 21:00-23:00 UTC")
                return None
                
            if instrument in self.active_positions:
                return None
                
            df = pd.DataFrame(self.quotes[instrument]).tail(2)
            params = pairs_params[instrument]
            
            eq_high = abs(df['high'].iloc[-1] - df['high'].iloc[-2]) < params['tolerance'] * params['pip_value']
            eq_low = abs(df['low'].iloc[-1] - df['low'].iloc[-2]) < params['tolerance'] * params['pip_value']
            buy_signal = eq_low and df['close'].iloc[-1] > df['open'].iloc[-1]
            sell_signal = eq_high and df['close'].iloc[-1] < df['open'].iloc[-1]
            
            latest_candle = df.iloc[-1]
            if buy_signal:
                signal = {
                    'instrument': instrument,
                    'signal': 'BUY',
                    'entry_price': latest_candle['ask'],
                    'time': latest_candle['time']
                }
                self.last_signal_times[instrument] = latest_candle['time']
                print(f"[SIGNAL] {instrument}: BUY detected")
                return signal
            elif sell_signal:
                signal = {
                    'instrument': instrument,
                    'signal': 'SELL',
                    'entry_price': latest_candle['bid'],
                    'time': latest_candle['time']
                }
                self.last_signal_times[instrument] = latest_candle['time']
                print(f"[SIGNAL] {instrument}: SELL detected")
                return signal
            return None

    def check_position_exits(self, instrument):
        with self.lock:
            if instrument not in self.active_positions or self.current_ticks[instrument].empty:
                return None
            latest_tick = self.current_ticks[instrument].iloc[-1]
            position = self.active_positions[instrument]
            price_format = '.3f'
            
            if position['signal'] == 'BUY':
                if latest_tick['bid'] <= position['sl']:
                    pips = -pairs_params[instrument]['sl_pips']
                    exit_message = f"*{instrument} Closed*\nBUY\nEntry: {position['entry_price']:{price_format}}\nExit: {position['sl']:{price_format}} (SL)\nPips: {pips:.1f}"
                    del self.active_positions[instrument]
                    return exit_message
                elif latest_tick['ask'] >= position['tp']:
                    pips = pairs_params[instrument]['tp_pips']
                    exit_message = f"*{instrument} Closed*\nBUY\nEntry: {position['entry_price']:{price_format}}\nExit: {position['tp']:{price_format}} (TP)\nPips: {pips:.1f}"
                    del self.active_positions[instrument]
                    return exit_message
            else:  # SELL
                if latest_tick['ask'] >= position['sl']:
                    pips = -pairs_params[instrument]['sl_pips']
                    exit_message = f"*{instrument} Closed*\nSELL\nEntry: {position['entry_price']:{price_format}}\nExit: {position['sl']:{price_format}} (SL)\nPips: {pips:.1f}"
                    del self.active_positions[instrument]
                    return exit_message
                elif latest_tick['bid'] <= position['tp']:
                    pips = pairs_params[instrument]['tp_pips']
                    exit_message = f"*{instrument} Closed*\nSELL\nEntry: {position['entry_price']:{price_format}}\nExit: {position['tp']:{price_format}} (TP)\nPips: {pips:.1f}"
                    del self.active_positions[instrument]
                    return exit_message
            return None

    def send_order(self, signal):
        instrument = signal['instrument']
        params = pairs_params[instrument]
        executor = RealisticExecution(params)
        entry_price = executor.adjust_price(signal['entry_price'], signal['signal'])
        symbol_id = symbol_id_map[instrument]
        order_type = "1" if signal['signal'] == "BUY" else "2"
        
        sl_price = (entry_price - params['sl_pips'] * params['pip_value']) if order_type == "1" else (entry_price + params['sl_pips'] * params['pip_value'])
        tp_price = (entry_price + params['tp_pips'] * params['pip_value']) if order_type == "1" else (entry_price - params['tp_pips'] * params['pip_value'])

        order_msg = self.create_fix_message(
            "D",
            f"11={int(time.time())}|55={symbol_id}|54={order_type}|38=0.01|40=1|99={sl_price:.3f}|44={tp_price:.3f}|59=1|167=FX|",
            TRADE_SENDER_COMP_ID, TRADE_TARGET_COMP_ID, TRADE_SENDER_SUB_ID, self.trade_seq_num
        )
        self.send_message(self.trade_sock, order_msg, "Trading")
        self.trade_seq_num += 1

        with self.lock:
            self.active_positions[instrument] = {
                'signal': signal['signal'],
                'entry_price': entry_price,
                'sl': sl_price,
                'tp': tp_price,
                'time': signal['time']
            }
        message = f"*{instrument} Signal*\n{signal['signal']}\nEntry: {entry_price:.3f}\nSL: {sl_price:.3f}\nTP: {tp_price:.3f}"
        self.send_telegram_message(message)

    def send_telegram_message(self, message):
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
        try:
            requests.post(url, json=payload)
        except Exception as e:
            print(f"[ERROR] Telegram send error: {e}")

    def run_quotes(self):
        while True:
            if not self.connect_quote_session():
                time.sleep(5)
                continue
            
            last_process_time = time.time()
            while True:
                try:
                    now = time.time()
                    current_time = datetime.datetime.now(datetime.UTC)
                    if now - self.quote_last_heartbeat > HEARTBEAT_INTERVAL:
                        self.quote_seq_num = self.send_heartbeat(self.quote_sock, "Quotes", self.quote_seq_num)
                        self.quote_last_heartbeat = now

                    if now - last_process_time >= 60:  # Check every minute
                        for instrument in symbol_id_map.keys():
                            if self.aggregate_h4_candle(instrument, current_time):
                                signal = self.detect_signals(instrument)
                                if signal:
                                    self.signal_queue.put(signal)
                            exit_message = self.check_position_exits(instrument)
                            if exit_message:
                                self.signal_queue.put({'exit_message': exit_message})
                        last_process_time = now

                    msg = self.receive_message(self.quote_sock, "Quotes")
                    if "35=W" in msg:
                        fields = dict(f.split("=") for f in msg.split("|") if "=" in f)
                        symbol_id = int(fields.get('55', fields.get('146', 0)))
                        instrument = next((k for k, v in symbol_id_map.items() if v == symbol_id), None)
                        if not instrument:
                            continue
                        bid = ask = None
                        for i, field in enumerate(msg.split("|")):
                            if field == "269=0":
                                bid = float(msg.split("|")[i + 1].split("=")[1])
                            elif field == "269=1":
                                ask = float(msg.split("|")[i + 1].split("=")[1])
                        tick_time = pd.to_datetime(fields.get('52', self.get_timestamp()), utc=True)
                        tick = {'time': tick_time, 'bid': bid or 0, 'ask': ask or bid or 0}
                        with self.lock:
                            self.current_ticks[instrument] = pd.concat(
                                [self.current_ticks[instrument], pd.DataFrame([tick])], 
                                ignore_index=True
                            )
                    elif "35=5" in msg or not msg:
                        break
                except Exception as e:
                    print(f"[ERROR] Quotes processing failed: {e}")
                    break
            self.quote_sock.close()
            time.sleep(5)

    def run_signals(self):
        while True:
            try:
                for instrument in symbol_id_map.keys():
                    exit_message = self.check_position_exits(instrument)
                    if exit_message:
                        self.signal_queue.put({'exit_message': exit_message})
                time.sleep(1)
            except Exception as e:
                print(f"[ERROR] Signal detection failed: {e}")
                time.sleep(1)

    def run_trading(self):
        while True:
            if not self.connect_trade_session():
                time.sleep(5)
                continue
            
            last_heartbeat_check = time.time()
            while True:
                try:
                    now = time.time()
                    if now - last_heartbeat_check >= 5:
                        if now - self.trade_last_heartbeat > HEARTBEAT_INTERVAL:
                            self.trade_seq_num = self.send_heartbeat(self.trade_sock, "Trading", self.trade_seq_num)
                            self.trade_last_heartbeat = now
                        last_heartbeat_check = now

                    while not self.signal_queue.empty():
                        item = self.signal_queue.get()
                        if 'exit_message' in item:
                            self.send_telegram_message(item['exit_message'])
                        else:
                            self.send_order(item)
                        self.signal_queue.task_done()

                    msg = self.receive_message(self.trade_sock, "Trading")
                    if "35=5" in msg or not msg:
                        break
                except Exception as e:
                    print(f"[ERROR] Trading failed: {e}")
                    break
            self.trade_sock.close()
            time.sleep(5)

    def run(self):
        quote_thread = Thread(target=self.run_quotes)
        signal_thread = Thread(target=self.run_signals)
        trade_thread = Thread(target=self.run_trading)
        quote_thread.daemon = True
        signal_thread.daemon = True
        trade_thread.daemon = True
        quote_thread.start()
        signal_thread.start()
        trade_thread.start()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("[INFO] Shutting down...")
            if self.quote_sock:
                self.quote_sock.close()
            if self.trade_sock:
                self.trade_sock.close()

if __name__ == "__main__":
    trader = LiveTrader()
    trader.run()

# README
# Overview:
# This script implements a live forex trading system for GBP/JPY using the FIX 4.4 protocol to connect to a broker’s quote and trade servers. It aggregates H4 candlestick data, detects buy/sell signals based on equal highs and lows, executes trades with realistic slippage and commission, and sends trade notifications via Telegram. The system uses multithreading for quotes, signals, and trading, designed to run continuously in Google Colab.
#
# Usage Instructions:
# 1. Open Google Colab (colab.research.google.com) and create a new notebook.
# 2. Install the requests library if needed: `!pip install requests` (pandas, numpy are pre-installed).
# 3. Set up FIX and Telegram credentials securely:
#    - Replace placeholders (e.g., `QUOTE_HOST = "YOUR_QUOTE_HOST_HERE"`) with your broker’s details.
#    - For sensitive fields (e.g., passwords, account numbers, Telegram token), use Colab’s input prompt, such as:
#      ```python
#      QUOTE_PASSWORD = input("Enter Quote Password: ")
#      TELEGRAM_BOT_TOKEN = input("Enter Telegram Bot Token: ")
#      ```
#    - Do not hardcode credentials to ensure security.
# 4. Copy and paste this code into a cell and run it using Shift + Enter.
# 5. Monitor outputs: connection status, H4 candle formation, signal detection, trade executions, and Telegram notifications.
#
# Dependencies:
# - Python 3.x
# - Libraries: socket, ssl, datetime, time, pandas, numpy, requests
# - Note: requests may require installation in Colab; others are pre-installed.
#
# Adapting to Your Needs:
# - Update `symbol_id_map` to trade other pairs (e.g., `"EUR_USD": 1`), ensuring correct broker symbol IDs.
# - Modify `pairs_params` to adjust strategy parameters (tolerance, stop-loss, take-profit, etc.) for each pair.
# - Change the trading restriction in `is_trading_allowed` (e.g., remove 21:00–23:00 UTC limit).
# - Customize `detect_signals` for alternative strategies (e.g., different candlestick patterns).
# - Adjust `RealisticExecution` parameters (slippage, spread, commission) to match your broker’s conditions.
# - Update FIX connection details (host, port) if using a different broker or environment.
#
# Notes:
# - Ensure a stable internet connection, as the script relies on continuous FIX server communication.
# - This is configured for a demo environment. For live trading, verify credentials and risk parameters carefully.
# - Multithreading may consume significant resources; monitor Colab’s CPU/memory usage.
# - Telegram notifications require a valid bot token and chat ID; test the bot setup beforehand.
# - The script runs indefinitely until interrupted (Ctrl+C); ensure proper shutdown to close sockets.
# - Contact for support or enhancements (e.g., adding new pairs, strategies, or logging).