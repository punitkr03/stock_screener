"""
crude_oil/notifications.py

Firebase Cloud Messaging (FCM) push notification dispatcher for Crude Oil Mini signals.

Uses the firebase-admin SDK (v6+) with the FCM v1 HTTP API under the hood.

Lazy-initializes the Firebase app on the first call to send_pcr_signal_notification().
If FIREBASE_SERVICE_ACCOUNT_JSON is not set or the file is missing, a warning is logged
and every send call returns {"sent": 0, "failed": 0} — the rest of the system is unaffected.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

log = logging.getLogger(__name__)

# Module-level Firebase app handle (lazy-initialized on first use)
_fcm_app = None

# Emoji prefix per signal type used in notification title
SIGNAL_EMOJI: dict[str, str] = {
    "STRONG_BUY":  "🟢",
    "RISKY_BUY":   "🟡",
    "STRONG_SELL": "🔴",
    "RISKY_SELL":  "🟠",
    "NONE":        "⚪",
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

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
        from config import FIREBASE_SERVICE_ACCOUNT_JSON, FIREBASE_APP_NAME

        path = FIREBASE_SERVICE_ACCOUNT_JSON
        if not path or not os.path.exists(path):
            log.warning(
                "FIREBASE_SERVICE_ACCOUNT_JSON not set or file not found ('%s'). "
                "FCM push notifications are disabled.",
                path,
            )
            return None

        cred = credentials.Certificate(path)
        # Use a named app so multiple imports don't re-initialize
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


def _build_message_text(
    signal: str,
    current_pcr: Optional[float],
    avg_pcr_3: Optional[float],
    delta_pct: Optional[float],
) -> tuple[str, str]:
    """
    Build human-readable (title, body) strings for the push notification.

    Example output:
        title: "🟢 CRUDEOILM — Strong Buy"
        body:  "PCR: 1.450 | Δ +3.21% vs avg 1.405"
    """
    emoji = SIGNAL_EMOJI.get(signal, "")
    label = signal.replace("_", " ").title()
    title = f"{emoji} CRUDEOILM — {label}"

    parts: list[str] = []
    if current_pcr is not None:
        parts.append(f"PCR: {current_pcr:.3f}")
    if delta_pct is not None and avg_pcr_3 is not None:
        sign = "+" if delta_pct >= 0 else ""
        parts.append(f"Δ {sign}{delta_pct:.2f}% vs avg {avg_pcr_3:.3f}")

    body = " | ".join(parts) if parts else label
    return title, body


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def send_pcr_signal_notification(
    signal: str,
    tokens: list[str],
    current_pcr: Optional[float] = None,
    avg_pcr_3: Optional[float] = None,
    delta_pct: Optional[float] = None,
) -> dict:
    """
    Send an FCM multicast push notification to all provided device tokens.

    Parameters
    ----------
    signal      : One of STRONG_BUY | RISKY_BUY | STRONG_SELL | RISKY_SELL | NONE
    tokens      : List of FCM registration token strings
    current_pcr : Latest PCR value (shown in notification body)
    avg_pcr_3   : Average of the last 3 stored PCR values (shown in body)
    delta_pct   : Percentage delta of current_pcr vs avg_pcr_3

    Returns
    -------
    dict with keys:
        sent   : number of tokens that received the message successfully
        failed : number of tokens where delivery failed
        errors : list of {"token": str, "error": str} for each failure
    """
    if not tokens:
        log.debug("send_pcr_signal_notification called with empty token list — skipping.")
        return {"sent": 0, "failed": 0, "errors": []}

    app = _get_fcm_app()
    if app is None:
        return {"sent": 0, "failed": 0, "errors": ["Firebase not configured"]}

    try:
        from firebase_admin import messaging
    except ImportError:
        log.error("firebase-admin is not installed. Run: pip install 'firebase-admin>=6.2.0'")
        return {"sent": 0, "failed": 0, "errors": ["firebase-admin not installed"]}

    title, body = _build_message_text(signal, current_pcr, avg_pcr_3, delta_pct)
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
    "send_pcr_signal_notification",
    "SIGNAL_EMOJI",
]
