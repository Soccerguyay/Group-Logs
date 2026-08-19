import os
import logging
from threading import Thread
from flask import Flask, jsonify

log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

app = Flask(__name__)
_bot_ref = None

@app.route('/')
@app.route('/ping')
@app.route('/health')
@app.route('/cron')
def health_check():
    return jsonify({"status": "ok", "service": "Roblox Group Logger"}), 200

def run():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive(bot=None):
    global _bot_ref
    _bot_ref = bot
    t = Thread(target=run)
    t.daemon = True
    t.start()


