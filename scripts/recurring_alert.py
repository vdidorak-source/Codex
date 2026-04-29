#!/usr/bin/env python3
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


SLACK_API_BASE = "https://slack.com/api"
TELEGRAM_API_BASE = "https://api.telegram.org"


def env(name: str, required: bool = True, default: str | None = None) -> str:
    value = os.getenv(name, default)
    if required and not value:
        raise RuntimeError(f"Missing environment variable: {name}")
    return value or ""


def slack_api(method: str, token: str, params: dict[str, str]) -> dict:
    query = urllib.parse.urlencode(params)
    url = f"{SLACK_API_BASE}/{method}?{query}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if not data.get("ok"):
        raise RuntimeError(f"Slack API error in {method}: {data.get('error')}")
    return data


def telegram_send(bot_token: str, chat_id: str, text: str) -> None:
    url = f"{TELEGRAM_API_BASE}/bot{bot_token}/sendMessage"
    payload = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error: {data}")


def contains_completion_marker(text: str, markers: list[str]) -> bool:
    low = (text or "").lower()
    return any(marker in low for marker in markers)


def fetch_overdue(
    slack_token: str,
    channel_id: str,
    responsible_user_id: str,
    completion_markers: list[str],
    now_utc: datetime,
) -> list[dict]:
    overdue: list[dict] = []
    cursor = ""
    stop = False

    while True:
        params = {"channel": channel_id, "limit": "200"}
        if cursor:
            params["cursor"] = cursor
        history = slack_api("conversations.history", slack_token, params)

        for parent in history.get("messages", []):
            ts = parent.get("ts")
            if not ts:
                continue

            replies_data = slack_api(
                "conversations.replies",
                slack_token,
                {"channel": channel_id, "ts": ts, "limit": "200"},
            )
            messages = replies_data.get("messages", [])
            replies = messages[1:] if len(messages) > 1 else []

            # Stop scan at the first thread that already has a completion marker.
            if any(contains_completion_marker(r.get("text", ""), completion_markers) for r in replies):
                stop = True
                break

            answered = any(r.get("user") == responsible_user_id for r in replies)
            if answered:
                continue

            age_days = int((now_utc.timestamp() - float(ts)) // 86400)
            if age_days > 5:
                overdue.append({"ts": ts, "age_days": age_days})

        if stop:
            break

        meta = history.get("response_metadata", {})
        cursor = meta.get("next_cursor", "") if history.get("has_more") else ""
        if not cursor:
            break

    return overdue


def main() -> int:
    slack_token = env("SLACK_BOT_TOKEN")
    channel_id = env("SLACK_CHANNEL_ID", required=False, default="C0690KA5QM6")
    responsible_user_id = env("RESPONSIBLE_USER_ID", required=False, default="U067VFSCBGT")
    telegram_bot_token = env("TELEGRAM_BOT_TOKEN")
    telegram_chat_id = env("TELEGRAM_CHAT_ID")
    force_run = env("FORCE_RUN", required=False, default="0") == "1"

    markers_raw = env("COMPLETION_MARKERS", required=False, default="done,отменил,отменена")
    completion_markers = [m.strip().lower() for m in markers_raw.split(",") if m.strip()]

    now_utc = datetime.now(timezone.utc)
    now_kyiv = now_utc.astimezone(ZoneInfo("Europe/Kyiv"))
    if not force_run and now_kyiv.hour != 16:
        print("Skip run: not 16:00 Kyiv hour.")
        return 0

    try:
        overdue = fetch_overdue(
            slack_token=slack_token,
            channel_id=channel_id,
            responsible_user_id=responsible_user_id,
            completion_markers=completion_markers,
            now_utc=now_utc,
        )
    except Exception:
        telegram_send(
            telegram_bot_token,
            telegram_chat_id,
            "Технический алерт: автопроверка recurring не выполнилась, проверь GitHub Actions run.",
        )
        raise

    if not overdue:
        print("No overdue recurring requests.")
        return 0

    oldest_ts = min(float(item["ts"]) for item in overdue)
    oldest_date = datetime.fromtimestamp(oldest_ts, tz=ZoneInfo("Europe/Kyiv")).strftime("%d.%m.%Y")
    min_days = min(item["age_days"] for item in overdue)
    max_days = max(item["age_days"] for item in overdue)

    lines = [
        f"У тебя висит {len(overdue)} неотмененных реккаринга, которые висят с {oldest_date} числа.",
        f"Просрочка: от {min_days} до {max_days} дней.",
    ]
    for item in sorted(overdue, key=lambda x: float(x["ts"]))[:5]:
        d = datetime.fromtimestamp(float(item["ts"]), tz=ZoneInfo("Europe/Kyiv")).strftime("%d.%m.%Y")
        lines.append(f"{d} — {item['age_days']} дней")

    telegram_send(telegram_bot_token, telegram_chat_id, "\n".join(lines))
    print("Telegram alert sent.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Fatal: {exc}", file=sys.stderr)
        raise
