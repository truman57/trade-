#!/usr/bin/env python3
"""
Advanced Webhook Trading Bot with Subprocess Architecture
- Checks webhook API every 3 seconds for trade signals
- Processes signals in separate subprocess
- Executes trades with stoploss and take profit on Bybit
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

# Global configuration
CONFIG_FILE = "bybit_config.json"

def load_config(config_file: str = CONFIG_FILE) -> Dict:
    """Load configuration from JSON file"""
    if not os.path.exists(config_file):
        logging.error(f"Configuration file '{config_file}' not found!")
        sys.exit(1)
    
    with open(config_file, 'r') as f:
        config = json.load(f)
    
    return config

# Bybit API Helper Functions
def generate_bybit_signature(api_secret: str, payload: str) -> str:
    """Generate HMAC signature for Bybit API"""
    return hmac.new(
        api_secret.encode('utf-8'), 
        payload.encode('utf-8'), 
        hashlib.sha256
    ).hexdigest()

def bybit_headers(api_key: str, timestamp_ms: str, signature: str) -> Dict:
    """Create headers for Bybit API requests"""
    return {
        'X-BAPI-SIGN': signature,
        'X-BAPI-API-KEY': api_key,
        'X-BAPI-TIMESTAMP': timestamp_ms,
        'X-BAPI-RECV-WINDOW': '5000',
        'Content-Type': 'application/json'
    }

def bybit_private_request(method: str, endpoint: str, api_key: str, api_secret: str, 
                          base_url: str, params: Dict = None) -> Optional[Dict]:
    """Make authenticated request to Bybit API"""
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
    """Get current market price for a symbol"""
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
        
        # Fallback to CoinGecko if Bybit API fails
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

def place_order_with_sl_tp(symbol: str, side: str, size: float, 
                          stop_loss: float, take_profit: float,
                          api_key: str, api_secret: str, base_url: str) -> Optional[Dict]:
    """Place a market order with stop loss and take profit"""
    try:
        # 1. Place the main market order
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
            
        order_id = market_resp.get('result', {}).get('orderId')
        
        # 2. Place stop loss order
        sl_params = {
            'category': 'spot',
            'symbol': symbol,
            'side': 'Sell' if side.lower() == 'buy' else 'Buy',  # Opposite of entry
            'orderType': 'Limit',
            'qty': str(size),
            'price': str(stop_loss),
            'timeInForce': 'GoodTillCancel',
            'stopLoss': str(stop_loss)
        }
        
        sl_resp = bybit_private_request('POST', '/v5/order/create', 
                                       api_key, api_secret, base_url, sl_params)
        
        # 3. Place take profit order
        tp_params = {
            'category': 'spot',
            'symbol': symbol,
            'side': 'Sell' if side.lower() == 'buy' else 'Buy',  # Opposite of entry
            'orderType': 'Limit',
            'qty': str(size),
            'price': str(take_profit),
            'timeInForce': 'GoodTillCancel',
            'takeProfit': str(take_profit)
        }
        
        tp_resp = bybit_private_request('POST', '/v5/order/create', 
                                       api_key, api_secret, base_url, tp_params)
        
        return {
            'market_order': market_resp,
            'stop_loss_order': sl_resp,
            'take_profit_order': tp_resp,
            'order_id': order_id
        }
    except Exception as e:
        logging.error(f"Error placing order with SL/TP: {e}")
        return None

def check_webhook_api(webhook_token: str) -> Optional[Dict]:
    """Check webhook.site API for new signals"""
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
    """Parse webhook content into trading signal"""
    try:
        # Try parsing as JSON
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
            
        # Try parsing as text format
        patterns = [
            r'(\w+)\s+(BUY|SELL|LONG|SHORT)',  # Simple format: "BTC BUY"
            r'(\w+):\s+(BUY|SELL|LONG|SHORT)',  # Colon format: "BTC: BUY"
            r'Symbol:\s*(\w+).*Action:\s*(BUY|SELL|LONG|SHORT)'  # Labeled format
        ]
        
        import re
        for pattern in patterns:
            match = re.search(pattern, content, re.IGNORECASE)
            if match:
                symbol, action = match.groups()
                
                # Map LONG/SHORT to BUY/SELL
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
    """Calculate stop loss and take profit levels"""
    if action.upper() in ['BUY', 'LONG']:
        stop_loss = current_price * (1 - sl_pct/100)
        take_profit = current_price * (1 + tp_pct/100)
    else:  # SELL or SHORT
        stop_loss = current_price * (1 + sl_pct/100)
        take_profit = current_price * (1 - tp_pct/100)
        
    return round(stop_loss, 6), round(take_profit, 6)

def cross_check_trade(order_id: str, symbol: str, api_key: str, 
                     api_secret: str, base_url: str) -> bool:
    """Cross-check that a trade was executed correctly"""
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
    """Process to monitor price after trade execution
    
    This process:
    1. Waits for 2 minutes after trade begins
    2. Checks price every 10 seconds
    3. Closes trade if price falls below entry price
    """
    logging.info("Price monitor process started")
    
    # Extract configuration
    api_key = config['bybit_api']['api_key']
    api_secret = config['bybit_api']['api_secret']
    base_url = config['bybit_api']['base_url']
    
    # Dictionary to track active trades
    active_trades = {}
    
    while not stop_event.is_set():
        try:
            # Check for new trades
            if not trade_queue.empty():
                trade_info = trade_queue.get()
                trade_id = trade_info['trade_id']
                symbol = trade_info['symbol']
                entry_price = trade_info['entry_price']
                side = trade_info['side']
                order_id = trade_info['order_id']
                
                # Add to active trades with start time
                active_trades[trade_id] = {
                    'symbol': symbol,
                    'entry_price': entry_price,
                    'side': side,
                    'order_id': order_id,
                    'start_time': time.time(),
                    'monitoring': False
                }
                logging.info(f"Added trade {trade_id} to price monitoring queue")
            
            # Process active trades
            trades_to_remove = []
            current_time = time.time()
            
            for trade_id, trade in active_trades.items():
                # Check if 2 minutes have passed since trade start
                if not trade['monitoring'] and (current_time - trade['start_time']) >= 120:  # 2 minutes
                    trade['monitoring'] = True
                    logging.info(f"Starting price monitoring for trade {trade_id}")
                
                # If monitoring is active, check price every 10 seconds
                if trade['monitoring'] and (current_time - trade.get('last_check', 0)) >= 10:
                    trade['last_check'] = current_time
                    
                    # Get current price
                    current_price = get_market_price(trade['symbol'], base_url)
                    if current_price:
                        logging.info(f"Trade {trade_id}: Current price ${current_price:.6f}, Entry price ${trade['entry_price']:.6f}")
                        
                        # Check if price is below entry (for BUY) or above entry (for SELL)
                        if (trade['side'].upper() == 'BUY' and current_price < trade['entry_price']) or \
                           (trade['side'].upper() == 'SELL' and current_price > trade['entry_price']):
                            
                            # Close the trade
                            logging.info(f"Price crossed entry after 2 minutes for trade {trade_id}. Closing position.")
                            
                            # Cancel existing orders
                            cancel_params = {
                                'category': 'spot',
                                'symbol': trade['symbol'],
                                'orderId': trade['order_id']
                            }
                            
                            cancel_resp = bybit_private_request('POST', '/v5/order/cancel', 
                                                              api_key, api_secret, base_url, cancel_params)
                            
                            # Place market order to close position
                            close_side = 'Sell' if trade['side'].upper() == 'BUY' else 'Buy'
                            
                            # Get position size
                            position_params = {
                                'category': 'spot',
                                'symbol': trade['symbol']
                            }
                            
                            position_resp = bybit_private_request('GET', '/v5/position/list', 
                                                               api_key, api_secret, base_url, position_params)
                            
                            if position_resp and position_resp.get('retCode') == 0:
                                positions = position_resp.get('result', {}).get('list', [])
                                if positions:
                                    size = abs(float(positions[0].get('size', 0)))
                                    
                                    # Place market order to close
                                    close_params = {
                                        'category': 'spot',
                                        'symbol': trade['symbol'],
                                        'side': close_side,
                                        'orderType': 'Market',
                                        'qty': str(size)
                                    }
                                    
                                    close_resp = bybit_private_request('POST', '/v5/order/create', 
                                                                     api_key, api_secret, base_url, close_params)
                                    
                                    if close_resp and close_resp.get('retCode') == 0:
                                        logging.info(f"Successfully closed trade {trade_id}")
                                    else:
                                        logging.error(f"Failed to close trade {trade_id}: {close_resp}")
                            
                            # Mark for removal
                            trades_to_remove.append(trade_id)
                    else:
                        logging.warning(f"Could not get current price for {trade['symbol']}")
                
                # Check if order status is closed or canceled
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
            
            # Remove completed trades
            for trade_id in trades_to_remove:
                if trade_id in active_trades:
                    logging.info(f"Removing trade {trade_id} from monitoring")
                    del active_trades[trade_id]
                    
        except Exception as e:
            logging.error(f"Error in price monitor process: {e}")
        
        time.sleep(1)  # Small delay to prevent CPU overuse
    
    logging.info("Price monitor process stopped")

def webhook_monitor_process(signal_queue: Queue, config: Dict, stop_event: Event):
    """Process to monitor webhook API for signals"""
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
                            # Put signal in queue for trade processor
                            signal_queue.put(signal)
                        else:
                            logging.warning("Could not parse webhook signal")
        except Exception as e:
            logging.error(f"Error in webhook monitor: {e}")
            
        time.sleep(check_interval)
    
    logging.info("Webhook monitor process stopped")

def trade_processor_process(signal_queue: Queue, trade_queue: Queue, config: Dict, stop_event: Event):
    """Process to handle trade signals and execute trades"""
    logging.info("Starting trade processor process")
    
    api_config = config.get('bybit_api', {})
    api_key = api_config.get('api_key', '')
    api_secret = api_config.get('api_secret', '')
    base_url = api_config.get('base_url', 'https://api.bybit.com')
    
    trading_config = config.get('trading', {})
    trade_amount = trading_config.get('trade_amount_usdt', 10)
    sl_pct = trading_config.get('stop_loss_pct', 5.0)
    tp_pct = trading_config.get('take_profit_pct', 5.0)
    
    # Symbol mapping for standardization
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
                
                # Standardize symbol format
                symbol = signal.get('symbol', '').upper()
                if symbol in symbol_map:
                    symbol = symbol_map[symbol]
                elif not symbol.endswith('USDT'):
                    symbol = f"{symbol}USDT"
                
                action = signal.get('action', '').upper()
                
                # Validate symbol and action
                if not symbol or action not in ['BUY', 'SELL', 'LONG', 'SHORT']:
                    logging.warning(f"Invalid signal: {signal}")
                    continue
                
                # Standardize action
                if action == 'LONG':
                    action = 'BUY'
                elif action == 'SHORT':
                    action = 'SELL'
                
                # Get current market price
                current_price = get_market_price(symbol, base_url)
                if not current_price:
                    logging.error(f"Could not get market price for {symbol}")
                    continue
                
                # Calculate position size
                position_size = trade_amount / current_price
                
                # Use provided SL/TP or calculate based on percentage
                stop_loss = signal.get('stop_loss')
                take_profit = signal.get('take_profit')
                
                if not stop_loss or not take_profit:
                    stop_loss, take_profit = calculate_sl_tp(
                        symbol, action, current_price, sl_pct, tp_pct
                    )
                
                # Execute the trade
                logging.info(f"Executing {action} trade for {symbol} at {current_price}")
                logging.info(f"Stop Loss: {stop_loss}, Take Profit: {take_profit}")
                
                trade_result = place_order_with_sl_tp(
                    symbol, action, position_size, 
                    stop_loss, take_profit,
                    api_key, api_secret, base_url
                )
                
                if trade_result:
                    order_id = trade_result.get('order_id')
                    
                    # Cross-check the trade execution
                    if cross_check_trade(order_id, symbol, api_key, api_secret, base_url):
                        logging.info(f"Trade successfully executed and verified: {order_id}")
                        
                        # Send trade info to price monitor
                        trade_info = {
                            'trade_id': f"{symbol}_{action}_{int(time.time())}",
                            'symbol': symbol,
                            'entry_price': current_price,
                            'side': action,
                            'order_id': order_id
                        }
                        trade_queue.put(trade_info)
                        logging.info(f"Sent trade info to price monitor: {trade_info['trade_id']}")
                    else:
                        logging.warning(f"Trade execution could not be verified: {order_id}")
                else:
                    logging.error("Failed to execute trade")
        except Exception as e:
            logging.error(f"Error in trade processor: {e}")
            
        time.sleep(1)  # Small delay to prevent CPU overuse
    
    logging.info("Trade processor process stopped")

def main():
    """Main function to start the bot"""
    logging.info("Starting Advanced Webhook Trading Bot")
    
    # Load configuration
    config = load_config()
    
    # Create communication queues
    signal_queue = Queue()
    trade_queue = Queue()  # New queue for trade monitoring
    
    # Create stop event for clean shutdown
    stop_event = Event()
    
    # Create and start processes
    webhook_process = Process(
        target=webhook_monitor_process, 
        args=(signal_queue, config, stop_event)
    )
    
    trade_process = Process(
        target=trade_processor_process,
        args=(signal_queue, trade_queue, config, stop_event)
    )
    
    # Create price monitoring process
    price_monitor = Process(
        target=price_monitor_process,
        args=(trade_queue, stop_event, config)
    )
    
    webhook_process.start()
    trade_process.start()
    price_monitor.start()
    logging.info("Price monitoring subprocess started")
    
    # Handle graceful shutdown
    def signal_handler(sig, frame):
        logging.info("Shutdown signal received, stopping processes...")
        stop_event.set()
        
        # Wait for processes to terminate
        webhook_process.join(timeout=5)
        trade_process.join(timeout=5)
        
        # Force terminate if needed
        if webhook_process.is_alive():
            webhook_process.terminate()
        if trade_process.is_alive():
            trade_process.terminate()
            
        logging.info("Bot shutdown complete")
        sys.exit(0)
    
    # Register signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    try:
        # Keep main process alive
        while True:
            time.sleep(1)
            
            # Check if child processes are still alive
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