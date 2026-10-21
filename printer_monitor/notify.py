"""Értesítések küldése. Most csak Telegram; ide jöhet később más csatorna is."""
import os
import urllib.parse
import urllib.request


class Notifier:
    def __init__(self, token=None, chat_id=None):
        # A környezeti változó felülírja a config.ini értékét.
        self.token = os.environ.get("TELEGRAM_TOKEN") or token
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID") or chat_id

    @property
    def telegram_enabled(self):
        return bool(self.token and self.chat_id)

    def send(self, text):
        if not self.telegram_enabled:
            print("[értesítés]", text)
            return
        data = urllib.parse.urlencode({"chat_id": self.chat_id, "text": text}).encode()
        try:
            urllib.request.urlopen(
                "https://api.telegram.org/bot{}/sendMessage".format(self.token),
                data=data, timeout=10)
        except Exception as e:
            print("Telegram hiba:", e)
