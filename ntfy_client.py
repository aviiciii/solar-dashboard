"""Tiny shared helper for pushing a notification via ntfy.sh. Used by both
collector/collect.py (auth/schema failure alerts) and alerts/daily_alert.py."""

import requests


def send_ntfy(topic: str, message: str, title: str = "Solar dashboard") -> None:
    resp = requests.post(
        f"https://ntfy.sh/{topic}",
        data=message.encode("utf-8"),
        headers={"Title": title},
        timeout=15,
    )
    resp.raise_for_status()
