import os
import time
import logging
from threading import Thread
from flask import Flask, jsonify

logging.getLogger('werkzeug').setLevel(logging.ERROR)
log = logging.getLogger("keep_alive")

app = Flask(__name__)
_bot_ref = None
_started = False


def set_bot(bot):
    """Called each time a new bot object is created."""
    global _bot_ref
    _bot_ref = bot


def _status() -> dict:
    bot = _bot_ref
    ready = bool(bot and bot.is_ready())
    cog = bot.get_cog("RobloxAuditLogger") if bot else None
    poller_running = bool(cog and cog.poll_logs_task.is_running())
    last_cycle_age = round(time.time() - cog.last_cycle_at) if cog else None
    healthy = ready and poller_running and last_cycle_age is not None and last_cycle_age < 180
    return {
        "status": "ok",
        "service": "Roblox Group Logger",
        "healthy": healthy,
        "bot_ready": ready,
        "poller_running": poller_running,
        "seconds_since_last_cycle": last_cycle_age,
    }


# Always answers 200 so uptime pingers keep the host awake.
# Read the "healthy" field to see if the bot is really working.
@app.route('/')
@app.route('/ping')
@app.route('/health')
@app.route('/cron')
def health_check():
    try:
        return jsonify(_status()), 200
    except Exception:
        log.exception("Status check failed")
        return jsonify({"status": "ok", "service": "Roblox Group Logger"}), 200


def run():
    port = int(os.environ.get("PORT", 8080))
    while True:
        try:
            app.run(host='0.0.0.0', port=port, threaded=True, use_reloader=False)
        except Exception:
            log.exception("Web server stopped. Starting it again in 5s.")
        time.sleep(5)


def keep_alive(bot=None):
    global _started
    if bot is not None:
        set_bot(bot)
    if _started:
        return
    _started = True
    t = Thread(target=run, daemon=True)
    t.start()
