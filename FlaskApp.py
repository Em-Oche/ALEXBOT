import telebot
from flask import Flask, request, jsonify
import hmac
import hashlib
import json
import sqlite3
import logging
import threading
import os

# Hardcoded configurations for testing
TOKEN = "8113977650:AAHaM7k7Rt3OHOmgJf1KwnFOSZ5y-wJXjqk"
NOWPAYMENTS_IPN_SECRET = os.getenv('NOWPAYMENTS_IPN_SECRET', "MjL3K8sb2uOMR3kP6bUgmWB0L3t06D6n")
ADMIN_CHAT_ID = "8191309122"
SECOND_ADMIN_CHAT_ID = "983306530"

# Initialize Flask app
app = Flask(__name__)

# Initialize Telegram bot
bot = telebot.TeleBot(TOKEN, parse_mode=None, threaded=False)

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Database setup
db_conn = sqlite3.connect('bot_data.db', check_same_thread=False)
db_lock = threading.Lock()

# Safe message sending
def safe_send_message(chat_id, text, parse_mode='HTML'):
    try:
        bot.send_message(chat_id, text, parse_mode=parse_mode)
    except telebot.apihelper.ApiTelegramException as e:
        logging.error(f"Telegram API error: {str(e)} - Chat ID: {chat_id}")
        if parse_mode == 'HTML':
            try:
                bot.send_message(chat_id, text, parse_mode=None)
            except Exception as fallback_e:
                logging.error(f"Fallback failed: {str(fallback_e)}")

# Root route for health checks
@app.route('/', methods=['GET', 'HEAD'])
def root():
    return jsonify({"message": "IPN Webhook Service for NOWPayments. Use /ipn endpoint for payment notifications."}), 200

# Verify IPN signature
def verify_ipn_signature(data, received_signature):
    sorted_data = dict(sorted(data.items()))
    data_str = json.dumps(sorted_data, separators=(',', ':'))
    computed_signature = hmac.new(
        NOWPAYMENTS_IPN_SECRET.encode('utf-8'),
        data_str.encode('utf-8'),
        hashlib.sha512
    ).hexdigest()
    logging.info(f"Computed signature: {computed_signature}, Received signature: {received_signature}")
    return hmac.compare_digest(computed_signature, received_signature)

# IPN handler
@app.route('/ipn', methods=['POST'])
def ipn_handler():
    logging.info(f"Received IPN callback: {request.get_json()}")
    logging.info(f"Headers: {request.headers}")
    received_signature = request.headers.get('x-nowpayments-sig')
    if not received_signature:
        logging.error("No signature provided in IPN callback")
        return jsonify({"error": "No signature provided"}), 400

    data = request.get_json()
    if not data:
        logging.error("No data provided in IPN callback")
        return jsonify({"error": "No data provided"}), 400

    if not verify_ipn_signature(data, received_signature):
        logging.error("Invalid IPN signature")
        return jsonify({"error": "Invalid signature"}), 401

    payment_status = data.get('payment_status')
    payment_id = data.get('payment_id')
    actually_paid = float(data.get('actually_paid', 0))
    expected_amount = float(data.get('pay_amount', 0))
    currency = data.get('pay_currency')

    logging.info(f"IPN: Payment ID {payment_id}, Status: {payment_status}, Paid: {actually_paid}, Expected: {expected_amount}")

    with db_lock:
        c = db_conn.cursor()
        c.execute("SELECT chat_id, currency, expected_amount FROM pending_deposits WHERE payment_id = ?", (payment_id,))
        deposit = c.fetchone()
        logging.info(f"Looking for payment_id {payment_id}, Found: {deposit}")

        if not deposit:
            logging.error(f"Payment ID {payment_id} not found in pending deposits")
            return jsonify({"error": "Payment not found"}), 404

        chat_id, expected_currency, db_expected_amount = deposit

        # Map NOWPayments currency codes to your format
        REVERSE_CURRENCY_CODES = {
            'btc': 'BTC',
            'usdttrc20': 'USDT_TRC20',
            'usdc': 'USDC_ETH',
            'usdt': 'USDT_ETH',
            'usdtsol': 'USDT_SOL'
        }
        currency = REVERSE_CURRENCY_CODES.get(currency.lower(), currency)

        if currency != expected_currency:
            logging.error(f"Currency mismatch for Payment ID {payment_id}: expected {expected_currency}, got {currency}")
            return jsonify({"error": "Currency mismatch"}), 400

        if payment_status in ['finished', 'confirmed']:
            c.execute("SELECT balance FROM wallets WHERE chat_id = ? AND currency = ?", (chat_id, expected_currency))
            wallet = c.fetchone()
            if wallet:
                new_balance = wallet[0] + actually_paid
                c.execute("UPDATE wallets SET balance = ? WHERE chat_id = ? AND currency = ?", (new_balance, chat_id, expected_currency))
                logging.info(f"Updated balance for {chat_id}: {expected_currency} = {new_balance}")
            else:
                c.execute("INSERT INTO wallets (chat_id, currency, balance) VALUES (?, ?, ?)", (chat_id, expected_currency, actually_paid))
                logging.info(f"Inserted new balance for {chat_id}: {expected_currency} = {actually_paid}")

            bot_message = f"✅ Deposit of ${actually_paid:.2f} in {expected_currency} successful!\nPayment ID: {payment_id}"
            if actually_paid < expected_amount:
                bot_message += f"\n⚠️ Note: You sent less than the expected amount (${expected_amount:.2f} {expected_currency})."
            elif actually_paid > expected_amount:
                bot_message += f"\nℹ️ Note: You sent more than the expected amount (${expected_amount:.2f} {expected_currency})."
            safe_send_message(chat_id, bot_message)

            for admin_id in [ADMIN_CHAT_ID, SECOND_ADMIN_CHAT_ID]:
                bot_message_admin = f"🔔 New Deposit\nUser: {chat_id}\nAmount: ${actually_paid:.2f} {expected_currency}\nPayment ID: {payment_id}"
                if actually_paid != expected_amount:
                    bot_message_admin += f"\n⚠️ Expected: ${expected_amount:.2f} {expected_currency}"
                safe_send_message(admin_id, bot_message_admin)

            c.execute("DELETE FROM pending_deposits WHERE payment_id = ?", (payment_id,))
            db_conn.commit()

        elif payment_status in ['failed', 'expired']:
            c.execute("DELETE FROM pending_deposits WHERE payment_id = ?", (payment_id,))
            db_conn.commit()
            safe_send_message(chat_id, f"❌ Deposit failed or expired.\nPayment ID: {payment_id}")
            for admin_id in [ADMIN_CHAT_ID, SECOND_ADMIN_CHAT_ID]:
                safe_send_message(admin_id, f"🔔 Deposit Failed/Expired\nUser: {chat_id}\nPayment ID: {payment_id}")

    return jsonify({"status": "success"}), 200

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=10000)