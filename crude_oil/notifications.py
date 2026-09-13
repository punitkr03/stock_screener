"""
crude_oil/notifications.py

Multi-channel notification dispatcher for Crude Oil Mini (CRUDEOILM) signals.
Supports:
  1. Telegram Bot (HTML-formatted messages via Telegram Bot API)
     - Stage 1: Unconfirmed UT Bot Buy/Sell signal trigger (waiting for confirmation)
     - Stage 2: Confirmed breakout 4-state PCR classification with last 4 PCR readings in IST
  2. Firebase Cloud Messaging (FCM) push notifications
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import os
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo
import requests

from config import (
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    FIREBASE_SERVICE_ACCOUNT_JSON,
    FIREBASE_APP_NAME,
)

log = logging.getLogger(__name__)

# Module-level Firebase app handle (lazy-initialized on first use)
_fcm_app = None
IST = ZoneInfo("Asia/Kolkata")

# Emoji prefix per signal type used in notification title
SIGNAL_EMOJI: dict[str, str] = {
    "STRONG_BUY":  "🚀🟢",
    "RISKY_BUY":   "⚠️🟡",
    "STRONG_SELL": "💥🔴",
    "RISKY_SELL":  "⚠️🟠",
    "NONE":        "⚪",
}


# ============================================================================
# Telegram Bot Dispatcher
# ============================================================================

def send_telegram_message(
    text: str,
    bot_token: Optional[str] = None,
    chat_id: Optional[str] = None,
    parse_mode: str = "HTML",
) -> dict:
    """
    Send a message via Telegram Bot HTTP API using x-www-form-urlencoded format.

    Parameters
    ----------
    text       : Message content (supports HTML formatting)
    bot_token  : Telegram Bot Token (defaults to TELEGRAM_BOT_TOKEN from config/env)
    chat_id    : Target Chat ID (defaults to TELEGRAM_CHAT_ID from config/env)
    parse_mode : Parse mode ('HTML' or 'Markdown')

    Returns
    -------
    dict with 'sent' (bool) and response details or error string
    """
    token = bot_token or TELEGRAM_BOT_TOKEN or os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_API", "")
    chat = chat_id or TELEGRAM_CHAT_ID or os.getenv("TELEGRAM_CHAT_ID", "")

    if not token or not chat:
        log.warning(
            "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not configured. Skipping Telegram message."
        )
        return {"sent": False, "error": "Telegram credentials not configured"}

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": str(chat),
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": "true",
    }
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
    }

    try:
        r = requests.post(url, data=payload, headers=headers, timeout=10)
        if r.status_code == 200:
            log.info("Telegram notification delivered successfully to chat_id=%s.", chat)
            return {"sent": True, "response": r.json()}
        else:
            log.error("Telegram API error (%s): %s", r.status_code, r.text[:300])
            return {"sent": False, "status_code": r.status_code, "error": r.text}
    except Exception as exc:
        log.error("Failed to deliver Telegram message: %s", exc)
        return {"sent": False, "error": str(exc)}


def build_unconfirmed_signal_message(
    signal: str,
    candle: Dict[str, Any],
) -> str:
    """
    Build formatted HTML message for an unconfirmed UT Bot BUY/SELL trigger.
    """
    is_buy = signal.upper() == "BUY"
    emoji = "🟢" if is_buy else "🔴"
    action = "BUY" if is_buy else "SELL"

    # Format candle timestamp to IST
    ts = candle.get("timestamp") or candle.get("candle_start_time")
    ist_str = "N/A"
    if ts:
        if isinstance(ts, str):
            dt = datetime.fromisoformat(ts)
        else:
            dt = ts
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        ist_dt = dt.astimezone(IST)
        ist_str = ist_dt.strftime("%Y-%m-%d %H:%M:%S IST")

    close = candle.get("close", "N/A")
    ha_close = candle.get("ha_close", "N/A")
    ha_high = candle.get("ha_high", "N/A")
    ha_low = candle.get("ha_low", "N/A")
    trailing_stop = candle.get("trailing_stop")

    close_str = f"₹{close:,.2f}" if isinstance(close, (int, float)) else str(close)
    ha_close_str = f"₹{ha_close:,.2f}" if isinstance(ha_close, (int, float)) else str(ha_close)
    ha_high_str = f"₹{ha_high:,.2f}" if isinstance(ha_high, (int, float)) else str(ha_high)
    ha_low_str = f"₹{ha_low:,.2f}" if isinstance(ha_low, (int, float)) else str(ha_low)
    ts_str = f"₹{trailing_stop:,.2f}" if isinstance(trailing_stop, (int, float)) else str(trailing_stop or "N/A")

    conf_rule = (
        f"Next candle HA Close &gt; {ha_high_str} (HA High)"
        if is_buy
        else f"Next candle HA Close &lt; {ha_low_str} (HA Low)"
    )

    msg = (
        f"🚨{emoji} <b>{action} SIGNAL (Waiting Confirmation)</b> - CRUDE OIL 🛢️\n\n"
        f"<b>Signal:</b> {emoji} <b>{action}</b> (UT Bot Trigger)\n"
        f"<b>Candle Time:</b> <code>{ist_str}</code>\n\n"
        f"⏳ <i>Waiting for breakout confirmation...</i>\n"
    )
    return msg


def build_confirmed_pcr_signal_message(
    signal: str,  # STRONG_BUY | RISKY_BUY | STRONG_SELL | RISKY_SELL
    candle: Dict[str, Any],
    pcr_records: List[Dict[str, Any]],
    avg_pcr_3: Optional[float] = None,
    delta_pct: Optional[float] = None,
) -> str:
    """
    Build formatted HTML message for a confirmed signal with 4-state PCR classification
    and the last 4 PCR readings formatted with IST timestamps.
    """
    emoji = SIGNAL_EMOJI.get(signal, "📢")
    label = signal.replace("_", " ")

    # Format candle timestamp to IST
    ts = candle.get("timestamp") or candle.get("candle_start_time")
    ist_str = "N/A"
    if ts:
        if isinstance(ts, str):
            dt = datetime.fromisoformat(ts)
        else:
            dt = ts
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        ist_dt = dt.astimezone(IST)
        ist_str = ist_dt.strftime("%Y-%m-%d %H:%M:%S IST")

    close = candle.get("close", "N/A")
    ha_close = candle.get("ha_close", "N/A")
    close_str = f"₹{close:,.2f}" if isinstance(close, (int, float)) else str(close)
    ha_close_str = f"₹{ha_close:,.2f}" if isinstance(ha_close, (int, float)) else str(ha_close)

    current_pcr = pcr_records[0].get("pcr") if pcr_records else None
    current_pcr_str = f"{current_pcr:.4f}" if isinstance(current_pcr, (int, float)) else "N/A"

    # Build Last 4 PCR data list in IST
    pcr_lines = []
    for i, r in enumerate(pcr_records[:4]):
        r_ts = r.get("timestamp")
        r_ist_str = "N/A"
        if r_ts:
            if isinstance(r_ts, str):
                r_dt = datetime.fromisoformat(r_ts)
            else:
                r_dt = r_ts
            if r_dt.tzinfo is None:
                r_dt = r_dt.replace(tzinfo=timezone.utc)
            r_ist_dt = r_dt.astimezone(IST)
            r_ist_str = r_ist_dt.strftime("%H:%M:%S IST")

        pcr_v = r.get("pcr", 0.0)
        pcr_v_str = f"{pcr_v:.4f}" if isinstance(pcr_v, (int, float)) else str(pcr_v)
        pe_oi = r.get("pe_oi", 0) or 0
        ce_oi = r.get("ce_oi", 0) or 0

        tag = " <i>(Latest)</i>" if i == 0 else ""
        pcr_lines.append(f"• <b>{r_ist_str}</b>{tag}: PCR = <b>{pcr_v_str}</b>")

    pcr_section = "\n".join(pcr_lines) if pcr_lines else "• No recent PCR records found"

    delta_str = ""
    avg_3_str = f"{avg_pcr_3:.4f}" if avg_pcr_3 is not None else "N/A"
    if delta_pct is not None and avg_pcr_3 is not None:
        sign = "+" if delta_pct >= 0 else ""
        delta_str = f" (Δ {sign}{delta_pct:.2f}% vs Avg {avg_3_str})"

    msg = (
        f"{emoji} <b>{label}</b> - CRUDE OIL 🛢️\n\n"
        f"<b>Action:</b> {emoji} <b>{label}</b>\n"
        f"<b>Candle Time:</b> <code>{ist_str}</code>\n"
        f"<b>Trigger Price:</b> <b>{close_str}</b> (HA Close: {ha_close_str})\n\n"
        f"<b>Current PCR:</b> <b>{current_pcr_str}</b>{delta_str}\n"
        f"<b>3-Period Avg PCR:</b> {avg_3_str}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>LAST 4 PCR READINGS (IST)</b>\n"
        f"{pcr_section}\n"
    )
    return msg


def send_unconfirmed_signal_notification(
    signal: str,
    candle: Dict[str, Any],
    bot_token: Optional[str] = None,
    chat_id: Optional[str] = None,
) -> dict:
    """
    Send an unconfirmed UT Bot BUY/SELL trigger alert to Telegram.
    """
    msg = build_unconfirmed_signal_message(signal, candle)
    return send_telegram_message(msg, bot_token=bot_token, chat_id=chat_id)


def send_confirmed_pcr_notification(
    signal: str,
    candle: Dict[str, Any],
    pcr_records: List[Dict[str, Any]],
    avg_pcr_3: Optional[float] = None,
    delta_pct: Optional[float] = None,
    tokens: Optional[List[str]] = None,
    bot_token: Optional[str] = None,
    chat_id: Optional[str] = None,
) -> dict:
    """
    Dispatch confirmed signal notifications to both Telegram and FCM (if configured).
    """
    results: dict = {}

    # 1. Telegram dispatch
    tg_text = build_confirmed_pcr_signal_message(
        signal=signal,
        candle=candle,
        pcr_records=pcr_records,
        avg_pcr_3=avg_pcr_3,
        delta_pct=delta_pct,
    )
    tg_res = send_telegram_message(tg_text, bot_token=bot_token, chat_id=chat_id)
    results["telegram"] = tg_res

    # 2. FCM dispatch (if tokens supplied)
    if tokens:
        current_pcr = pcr_records[0].get("pcr") if pcr_records else None
        fcm_res = send_pcr_signal_notification(
            signal=signal,
            tokens=tokens,
            current_pcr=current_pcr,
            avg_pcr_3=avg_pcr_3,
            delta_pct=delta_pct,
        )
        results["fcm"] = fcm_res

    return results


# ============================================================================
# Firebase Cloud Messaging (FCM) Dispatcher
# ============================================================================

def _get_fcm_app():
    """
    Return a cached Firebase Admin app instance, or None if Firebase is not configured.
    Initializes the app on the first invocation.
    """
    global _fcm_app
    if _fcm_app is not None:
        return _fcm_app

    try:
        import firebase_admin
        from firebase_admin import credentials

        path = FIREBASE_SERVICE_ACCOUNT_JSON
        if not path or not os.path.exists(path):
            log.debug(
                "FIREBASE_SERVICE_ACCOUNT_JSON not set or file not found ('%s'). "
                "FCM push notifications are disabled.",
                path,
            )
            return None

        cred = credentials.Certificate(path)
        try:
            _fcm_app = firebase_admin.get_app(FIREBASE_APP_NAME)
            log.debug("Reusing existing Firebase app '%s'.", FIREBASE_APP_NAME)
        except ValueError:
            _fcm_app = firebase_admin.initialize_app(cred, name=FIREBASE_APP_NAME)
            log.info(
                "Firebase app '%s' initialized from %s",
                FIREBASE_APP_NAME,
                path,
            )
    except Exception as exc:
        log.error("Failed to initialize Firebase app: %s", exc)
        _fcm_app = None

    return _fcm_app


def _build_fcm_message_text(
    signal: str,
    current_pcr: Optional[float],
    avg_pcr_3: Optional[float],
    delta_pct: Optional[float],
) -> tuple[str, str]:
    """
    Build human-readable (title, body) strings for FCM push notification.
    """
    emoji = SIGNAL_EMOJI.get(signal, "")
    label = signal.replace("_", " ").title()
    title = f"{emoji} CRUDEOILM - {label}"

    parts: list[str] = []
    if current_pcr is not None:
        parts.append(f"PCR: {current_pcr:.3f}")
    if delta_pct is not None and avg_pcr_3 is not None:
        sign = "+" if delta_pct >= 0 else ""
        parts.append(f"Δ {sign}{delta_pct:.2f}% vs avg {avg_pcr_3:.3f}")

    body = " | ".join(parts) if parts else label
    return title, body


def send_pcr_signal_notification(
    signal: str,
    tokens: list[str],
    current_pcr: Optional[float] = None,
    avg_pcr_3: Optional[float] = None,
    delta_pct: Optional[float] = None,
) -> dict:
    """
    Send an FCM multicast push notification to all provided device tokens.
    """
    if not tokens:
        log.debug("send_pcr_signal_notification called with empty token list - skipping.")
        return {"sent": 0, "failed": 0, "errors": []}

    app = _get_fcm_app()
    if app is None:
        return {"sent": 0, "failed": 0, "errors": ["Firebase not configured"]}

    try:
        from firebase_admin import messaging
    except ImportError:
        log.error("firebase-admin is not installed. Run: pip install 'firebase-admin>=6.2.0'")
        return {"sent": 0, "failed": 0, "errors": ["firebase-admin not installed"]}

    title, body = _build_fcm_message_text(signal, current_pcr, avg_pcr_3, delta_pct)
    log.info(
        "Sending FCM notification '%s' to %d token(s).",
        title,
        len(tokens),
    )

    message = messaging.MulticastMessage(
        notification=messaging.Notification(title=title, body=body),
        data={
            "signal":    signal,
            "pcr":       str(current_pcr if current_pcr is not None else ""),
            "avg_pcr_3": str(avg_pcr_3 if avg_pcr_3 is not None else ""),
            "delta_pct": str(round(delta_pct, 4) if delta_pct is not None else ""),
        },
        tokens=tokens,
    )

    try:
        response = messaging.send_each_for_multicast(message, app=app)
        errors = [
            {"token": tokens[i], "error": str(r.exception)}
            for i, r in enumerate(response.responses)
            if not r.success
        ]
        log.info(
            "FCM result: %d sent, %d failed. Signal: %s",
            response.success_count,
            response.failure_count,
            signal,
        )
        return {
            "sent":   response.success_count,
            "failed": response.failure_count,
            "errors": errors,
        }
    except Exception as exc:
        log.error("FCM multicast error: %s", exc)
        return {"sent": 0, "failed": len(tokens), "errors": [str(exc)]}


__all__ = [
    "send_telegram_message",
    "build_unconfirmed_signal_message",
    "build_confirmed_pcr_signal_message",
    "send_unconfirmed_signal_notification",
    "send_confirmed_pcr_notification",
    "send_pcr_signal_notification",
    "SIGNAL_EMOJI",
]
