#!/usr/bin/env python3
"""
Advanced Webhook Trading Bot with Subprocess Architecture
- Checks webhook API every 3 seconds for trade signals
- Processes signals in separate subprocess
- Executes trades with stoploss and take profit on Bybit
- Uses 50% of available balance per trade
- SL/TP are handled by market orders via monitoring, not limit orders
- Includes cross-checking for trade verification
"""

import subprocess
import time
import json
import os
import logging
import sys
import requests
import hmac
import hashlib
from datetime import datetime
from urllib.parse import urlencode
import multiprocessing
from multiprocessing import Queue, Process, Event
from typing import Dict, Optional, List, Tuple
import signal

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(f"advanced_webhook_bot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"),
        logging.StreamHandler()
    ]
)

CONFIG_FILE = "bybit_config.json"

def load_config(config_file: str = CONFIG_FILE) -> Dict:
    if not os.path.exists(config_file):
        logging.error(f"Configuration file '{config_file}' not found!")
        sys.exit(1)
    with open(config_file, 'r') as f:
        config = json.load(f)
    return config

def generate_bybit_signature(api_secret: str, payload: str) -> str:
    return hmac.new(
        api_secret.encode('utf-8'), 
        payload.encode('utf-8'), 
        hashlib.sha256
    ).hexdigest()

def bybit_headers(api_key: str, timestamp_ms: str, signature: str) -> Dict:
    return {
        'X-BAPI-SIGN': signature,
        'X-BAPI-API-KEY': api_key,
        'X-BAPI-TIMESTAMP': timestamp_ms,
        'X-BAPI-RECV-WINDOW': '5000',
        'Content-Type': 'application/json'
    }

def bybit_private_request(method: str, endpoint: str, api_key: str, api_secret: str, 
                          base_url: str, params: Dict = None) -> Optional[Dict]:
    try:
        url = base_url + endpoint
        ts = str(int(time.time() * 1000))
        query = ''
        body_str = ''
        if method.upper() == 'GET':
            if params:
                query = urlencode(params)
                url += '?' + query
        else:
            if params:
                body_str = json.dumps(params)
        payload = ts + api_key + '5000' + (query or '') + (body_str or '')
        sig = generate_bybit_signature(api_secret, payload)
        headers = bybit_headers(api_key, ts, sig)
        if method.upper() == 'GET':
            resp = requests.get(url, headers=headers, timeout=10)
        else:
            resp = requests.post(url, headers=headers, data=body_str, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logging.error(f"Bybit API error: {e}")
        return None

def get_market_price(symbol: str, base_url: str) -> Optional[float]:
    try:
        resp = requests.get(f"{base_url}/v5/market/tickers", 
                           params={'category': 'spot', 'symbol': symbol},
                           timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data and data.get('retCode') == 0:
            result = data.get('result', {}).get('list', [])
            if result:
                return float(result[0].get('lastPrice', '0'))
        coingecko_map = {
            "BNBUSDT": "binancecoin",
            "ETHUSDT": "ethereum", 
            "AVAXUSDT": "avalanche-2",
            "SOLUSDT": "solana",
            "XRPUSDT": "ripple",
            "AAVEUSDT": "aave"
        }
        coin_id = coingecko_map.get(symbol)
        if coin_id:
            cg_resp = requests.get("https://api.coingecko.com/api/v3/simple/price",
                                  params={"ids": coin_id, "vs_currencies": "usd"},
                                  timeout=10)
            cg_resp.raise_for_status()
            cg_data = cg_resp.json()
            if coin_id in cg_data and 'usd' in cg_data[coin_id]:
                return float(cg_data[coin_id]['usd'])
        return None
    except Exception as e:
        logging.error(f"Error getting market price for {symbol}: {e}")
        return None

def get_available_balance(asset: str, api_key: str, api_secret: str, base_url: str) -> float:
    """
    Fetch available balance for given asset.
    """
    try:
        params = {'accountType': 'SPOT'}
        data = bybit_private_request('GET', '/v5/account/wallet-balance', api_key, api_secret, base_url, params)
        if not data or data.get('retCode') != 0:
            return 0.0
        balance_data = data.get('result', {}).get('balance', [])
        for entry in balance_data:
            if entry.get('coin', '').upper() == asset.upper():
                return float(entry.get('availableToWithdraw', 0))
        return 0.0
    except Exception as e:
        logging.error(f"Failed to fetch balance for {asset}: {e}")
        return 0.0

def place_market_order(symbol: str, side: str, size: float,
                      api_key: str, api_secret: str, base_url: str) -> Optional[Dict]:
    """
    Place a market order without SL/TP, returns order response.
    """
    try:
        market_params = {
            'category': 'spot',
            'symbol': symbol,
            'side': 'Buy' if side.lower() == 'buy' else 'Sell',
            'orderType': 'Market',
            'qty': str(size)
        }
        market_resp = bybit_private_request('POST', '/v5/order/create', 
                                           api_key, api_secret, base_url, market_params)
        if not market_resp or market_resp.get('retCode') != 0:
            logging.error(f"Failed to place market order: {market_resp}")
            return None
        return market_resp
    except Exception as e:
        logging.error(f"Error placing market order: {e}")
        return None

def check_webhook_api(webhook_token: str) -> Optional[Dict]:
    try:
        r = requests.get(f"https://webhook.site/token/{webhook_token}/requests", timeout=10)
        r.raise_for_status()
        if r.status_code == 200:
            return r.json()
        return None
    except Exception as e:
        logging.error(f"Error checking webhook API: {e}")
        return None

def parse_webhook_signal(content: str) -> Optional[Dict]:
    try:
        try:
            data = json.loads(content)
            if 'symbol' in data and 'action' in data:
                return {
                    'symbol': data['symbol'],
                    'action': data['action'],
                    'stop_loss': data.get('stopLoss'),
                    'take_profit': data.get('takeProfit')
                }
        except json.JSONDecodeError:
            pass
        patterns = [
            r'(\w+)\s+(BUY|SELL|LONG|SHORT)',
            r'(\w+):\s+(BUY|SELL|LONG|SHORT)',
            r'Symbol:\s*(\w+).*Action:\s*(BUY|SELL|LONG|SHORT)'
        ]
        import re
        for pattern in patterns:
            match = re.search(pattern, content, re.IGNORECASE)
            if match:
                symbol, action = match.groups()
                if action.upper() == 'LONG':
                    action = 'BUY'
                elif action.upper() == 'SHORT':
                    action = 'SELL'
                return {
                    'symbol': symbol,
                    'action': action
                }
        return None
    except Exception as e:
        logging.error(f"Error parsing webhook signal: {e}")
        return None

def calculate_sl_tp(symbol: str, action: str, current_price: float, 
                   sl_pct: float, tp_pct: float) -> Tuple[float, float]:
    if action.upper() in ['BUY', 'LONG']:
        stop_loss = current_price * (1 - sl_pct/100)
        take_profit = current_price * (1 + tp_pct/100)
    else:
        stop_loss = current_price * (1 + sl_pct/100)
        take_profit = current_price * (1 - tp_pct/100)
    return round(stop_loss, 6), round(take_profit, 6)

def cross_check_trade(order_id: str, symbol: str, api_key: str, 
                     api_secret: str, base_url: str) -> bool:
    try:
        params = {'category': 'spot', 'orderId': order_id, 'symbol': symbol}
        resp = bybit_private_request('GET', '/v5/order/realtime', 
                                    api_key, api_secret, base_url, params)
        if resp and resp.get('retCode') == 0:
            order_list = resp.get('result', {}).get('list', [])
            if order_list:
                order = order_list[0]
                status = order.get('orderStatus')
                return status in ['Filled', 'PartiallyFilled']
        return False
    except Exception as e:
        logging.error(f"Error cross-checking trade: {e}")
        return False

def price_monitor_process(trade_queue: Queue, stop_event: Event, config: Dict):
    logging.info("Price monitor process started")
    api_key = config['bybit_api']['api_key']
    api_secret = config['bybit_api']['api_secret']
    base_url = config['bybit_api']['base_url']
    trading_config = config.get('trading', {})
    sl_pct = trading_config.get('stop_loss_pct', 5.0)
    tp_pct = trading_config.get('take_profit_pct', 5.0)
    active_trades = {}
    while not stop_event.is_set():
        try:
            if not trade_queue.empty():
                trade_info = trade_queue.get()
                trade_id = trade_info['trade_id']
                symbol = trade_info['symbol']
                entry_price = trade_info['entry_price']
                side = trade_info['side']
                order_id = trade_info['order_id']
                size = trade_info.get('size')
                active_trades[trade_id] = {
                    'symbol': symbol,
                    'entry_price': entry_price,
                    'side': side,
                    'order_id': order_id,
                    'size': size,
                    'start_time': time.time(),
                    'monitoring': False,
                    'sl_pct': trade_info.get('sl_pct', sl_pct),
                    'tp_pct': trade_info.get('tp_pct', tp_pct)
                }
                logging.info(f"Added trade {trade_id} to price monitoring queue")
            trades_to_remove = []
            current_time = time.time()
            for trade_id, trade in active_trades.items():
                if not trade['monitoring'] and (current_time - trade['start_time']) >= 120:
                    trade['monitoring'] = True
                    logging.info(f"Starting price monitoring for trade {trade_id}")
                if trade['monitoring'] and (current_time - trade.get('last_check', 0)) >= 10:
                    trade['last_check'] = current_time
                    current_price = get_market_price(trade['symbol'], base_url)
                    if current_price:
                        logging.info(f"Trade {trade_id}: Current price ${current_price:.6f}, Entry price ${trade['entry_price']:.6f}")
                        # Calculate SL/TP levels on-the-fly
                        stop_loss, take_profit = calculate_sl_tp(
                            trade['symbol'], trade['side'], trade['entry_price'],
                            trade.get('sl_pct', sl_pct), trade.get('tp_pct', tp_pct)
                        )
                        # Close position at market if SL/TP criteria met
                        close_reason = None
                        if (trade['side'].upper() == 'BUY'):
                            if current_price <= stop_loss:
                                close_reason = 'Stop Loss'
                            elif current_price >= take_profit:
                                close_reason = 'Take Profit'
                        elif (trade['side'].upper() == 'SELL'):
                            if current_price >= stop_loss:
                                close_reason = 'Stop Loss'
                            elif current_price <= take_profit:
                                close_reason = 'Take Profit'
                        if close_reason:
                            logging.info(f"{close_reason} triggered for trade {trade_id}. Closing at market.")
                            close_side = 'Sell' if trade['side'].upper() == 'BUY' else 'Buy'
                            close_params = {
                                'category': 'spot',
                                'symbol': trade['symbol'],
                                'side': close_side,
                                'orderType': 'Market',
                                'qty': str(trade['size'])
                            }
                            close_resp = bybit_private_request('POST', '/v5/order/create', 
                                                              api_key, api_secret, base_url, close_params)
                            if close_resp and close_resp.get('retCode') == 0:
                                logging.info(f"Successfully closed trade {trade_id} by {close_reason}")
                            else:
                                logging.error(f"Failed to close trade {trade_id}: {close_resp}")
                            trades_to_remove.append(trade_id)
                    else:
                        logging.warning(f"Could not get current price for {trade['symbol']}")
                order_params = {
                    'category': 'spot',
                    'symbol': trade['symbol'],
                    'orderId': trade['order_id']
                }
                order_resp = bybit_private_request('GET', '/v5/order/history', 
                                                 api_key, api_secret, base_url, order_params)
                if order_resp and order_resp.get('retCode') == 0:
                    orders = order_resp.get('result', {}).get('list', [])
                    if orders and orders[0].get('orderStatus') in ['Filled', 'Cancelled', 'Rejected']:
                        trades_to_remove.append(trade_id)
            for trade_id in trades_to_remove:
                if trade_id in active_trades:
                    logging.info(f"Removing trade {trade_id} from monitoring")
                    del active_trades[trade_id]
        except Exception as e:
            logging.error(f"Error in price monitor process: {e}")
        time.sleep(1)
    logging.info("Price monitor process stopped")

def webhook_monitor_process(signal_queue: Queue, config: Dict, stop_event: Event):
    logging.info("Starting webhook monitor process")
    webhook_token = config.get('webhook', {}).get('token', '')
    check_interval = config.get('webhook', {}).get('check_interval', 3)
    last_webhook_id = None
    while not stop_event.is_set():
        try:
            webhook_data = check_webhook_api(webhook_token)
            if webhook_data:
                data_list = webhook_data.get('data', [])
                if data_list:
                    latest = data_list[0]
                    wid = latest.get('uuid')
                    if wid != last_webhook_id:
                        last_webhook_id = wid
                        content = latest.get('content', '')
                        when = latest.get('created_at', 'Unknown')
                        logging.info(f"New webhook received at {when}")
                        logging.info(f"Content: {content}")
                        signal = parse_webhook_signal(content)
                        if signal:
                            logging.info(f"Parsed signal: {signal}")
                            signal_queue.put(signal)
                        else:
                            logging.warning("Could not parse webhook signal")
        except Exception as e:
            logging.error(f"Error in webhook monitor: {e}")
        time.sleep(check_interval)
    logging.info("Webhook monitor process stopped")

def trade_processor_process(signal_queue: Queue, trade_queue: Queue, config: Dict, stop_event: Event):
    logging.info("Starting trade processor process")
    api_config = config.get('bybit_api', {})
    api_key = api_config.get('api_key', '')
    api_secret = api_config.get('api_secret', '')
    base_url = api_config.get('base_url', 'https://api.bybit.com')
    trading_config = config.get('trading', {})
    sl_pct = trading_config.get('stop_loss_pct', 5.0)
    tp_pct = trading_config.get('take_profit_pct', 5.0)
    use_balance_pct = 0.5  # 50% of available balance
    symbol_map = {
        "BNB": "BNBUSDT",
        "ETH": "ETHUSDT",
        "AVAX": "AVAXUSDT",
        "SOL": "SOLUSDT",
        "XRP": "XRPUSDT",
        "AAVE": "AAVEUSDT"
    }
    while not stop_event.is_set():
        try:
            if not signal_queue.empty():
                signal = signal_queue.get()
                symbol = signal.get('symbol', '').upper()
                if symbol in symbol_map:
                    symbol = symbol_map[symbol]
                elif not symbol.endswith('USDT'):
                    symbol = f"{symbol}USDT"
                action = signal.get('action', '').upper()
                if not symbol or action not in ['BUY', 'SELL', 'LONG', 'SHORT']:
                    logging.warning(f"Invalid signal: {signal}")
                    continue
                if action == 'LONG':
                    action = 'BUY'
                elif action == 'SHORT':
                    action = 'SELL'
                current_price = get_market_price(symbol, base_url)
                if not current_price:
                    logging.error(f"Could not get market price for {symbol}")
                    continue
                # Use available balance for position sizing
                base_asset, quote_asset = symbol.replace('USDT',''), 'USDT'
                if action == 'BUY':
                    balance = get_available_balance(quote_asset, api_key, api_secret, base_url)
                    trade_amount = balance * use_balance_pct
                    position_size = trade_amount / current_price
                else:
                    balance = get_available_balance(base_asset, api_key, api_secret, base_url)
                    position_size = balance * use_balance_pct
                if position_size <= 0:
                    logging.error(f"Insufficient balance for {symbol} ({action})")
                    continue
                stop_loss = signal.get('stop_loss')
                take_profit = signal.get('take_profit')
                if not stop_loss or not take_profit:
                    stop_loss, take_profit = calculate_sl_tp(
                        symbol, action, current_price, sl_pct, tp_pct
                    )
                logging.info(f"Executing {action} trade for {symbol} at {current_price} using {position_size} units")
                market_order = place_market_order(
                    symbol, action, position_size,
                    api_key, api_secret, base_url
                )
                if market_order:
                    order_id = market_order.get('result', {}).get('orderId')
                    if cross_check_trade(order_id, symbol, api_key, api_secret, base_url):
                        logging.info(f"Trade successfully executed and verified: {order_id}")
                        trade_info = {
                            'trade_id': f"{symbol}_{action}_{int(time.time())}",
                            'symbol': symbol,
                            'entry_price': current_price,
                            'side': action,
                            'order_id': order_id,
                            'size': position_size,
                            'sl_pct': sl_pct,
                            'tp_pct': tp_pct
                        }
                        trade_queue.put(trade_info)
                        logging.info(f"Sent trade info to price monitor: {trade_info['trade_id']}")
                    else:
                        logging.warning(f"Trade execution could not be verified: {order_id}")
                else:
                    logging.error("Failed to execute trade")
        except Exception as e:
            logging.error(f"Error in trade processor: {e}")
        time.sleep(1)
    logging.info("Trade processor process stopped")

def main():
    logging.info("Starting Advanced Webhook Trading Bot")
    config = load_config()
    signal_queue = Queue()
    trade_queue = Queue()
    stop_event = Event()
    webhook_process = Process(
        target=webhook_monitor_process, 
        args=(signal_queue, config, stop_event)
    )
    trade_process = Process(
        target=trade_processor_process,
        args=(signal_queue, trade_queue, config, stop_event)
    )
    price_monitor = Process(
        target=price_monitor_process,
        args=(trade_queue, stop_event, config)
    )
    webhook_process.start()
    trade_process.start()
    price_monitor.start()
    logging.info("Price monitoring subprocess started")
    def signal_handler(sig, frame):
        logging.info("Shutdown signal received, stopping processes...")
        stop_event.set()
        webhook_process.join(timeout=5)
        trade_process.join(timeout=5)
        if webhook_process.is_alive():
            webhook_process.terminate()
        if trade_process.is_alive():
            trade_process.terminate()
        logging.info("Bot shutdown complete")
        sys.exit(0)
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    try:
        while True:
            time.sleep(1)
            if not webhook_process.is_alive() or not trade_process.is_alive():
                logging.error("A subprocess has terminated unexpectedly, shutting down...")
                stop_event.set()
                if webhook_process.is_alive():
                    webhook_process.join(timeout=5)
                    if webhook_process.is_alive():
                        webhook_process.terminate()
                if trade_process.is_alive():
                    trade_process.join(timeout=5)
                    if trade_process.is_alive():
                        trade_process.terminate()
                break
    except KeyboardInterrupt:
        logging.info("Keyboard interrupt received, shutting down...")
        stop_event.set()
        webhook_process.join(timeout=5)
        trade_process.join(timeout=5)
        if webhook_process.is_alive():
            webhook_process.terminate()
        if trade_process.is_alive():
            trade_process.terminate()
    logging.info("Bot has been shut down")

if __name__ == "__main__":
    main()
