# Crude Oil Screener & Telegram Bot - cURL Commands Reference

This guide contains all cURL commands for testing and interacting with the Crude Oil Mini (`CRUDEOILM`) Screener, FastAPI backend, and Telegram alert dispatcher.

---

## 1. FastAPI Telegram Test Notifications

The backend exposes a test endpoint (`POST /crude-oil/notifications/test-telegram`) that reads your `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` automatically from `.env`.

### 1.1 Unconfirmed BUY Signal (Waiting Confirmation)

> Triggered when UT Bot generates a BUY signal upon candle close.

```bash
curl --location 'http://127.0.0.1:8000/crude-oil/notifications/test-telegram' \
--header 'Content-Type: application/json' \
--data '{
    "notification_type": "unconfirmed",
    "signal": "BUY"
}'
```

---

### 1.2 Unconfirmed SELL Signal (Waiting Confirmation)

> Triggered when UT Bot generates a SELL signal upon candle close.

```bash
curl --location 'http://127.0.0.1:8000/crude-oil/notifications/test-telegram' \
--header 'Content-Type: application/json' \
--data '{
    "notification_type": "unconfirmed",
    "signal": "SELL"
}'
```

---

### 1.3 Confirmed STRONG BUY (PCR > 3-Period Avg)

> Triggered when candle confirms breakout and Current PCR > Avg 3 PCR.

```bash
curl --location 'http://127.0.0.1:8000/crude-oil/notifications/test-telegram' \
--header 'Content-Type: application/json' \
--data '{
    "notification_type": "confirmed",
    "signal": "STRONG_BUY"
}'
```

---

### 1.4 Confirmed RISKY BUY (PCR ≤ 3-Period Avg)

> Triggered when candle confirms breakout but Current PCR ≤ Avg 3 PCR.

```bash
curl --location 'http://127.0.0.1:8000/crude-oil/notifications/test-telegram' \
--header 'Content-Type: application/json' \
--data '{
    "notification_type": "confirmed",
    "signal": "RISKY_BUY"
}'
```

---

### 1.5 Confirmed STRONG SELL (PCR < 3-Period Avg)

> Triggered when candle confirms breakout and Current PCR < Avg 3 PCR.

```bash
curl --location 'http://127.0.0.1:8000/crude-oil/notifications/test-telegram' \
--header 'Content-Type: application/json' \
--data '{
    "notification_type": "confirmed",
    "signal": "STRONG_SELL"
}'
```

---

### 1.6 Confirmed RISKY SELL (PCR ≥ 3-Period Avg)

> Triggered when candle confirms breakout but Current PCR ≥ Avg 3 PCR.

```bash
curl --location 'http://127.0.0.1:8000/crude-oil/notifications/test-telegram' \
--header 'Content-Type: application/json' \
--data '{
    "notification_type": "confirmed",
    "signal": "RISKY_SELL"
}'
```

---

### 1.7 Custom Telegram Text Message

```bash
curl --location 'http://127.0.0.1:8000/crude-oil/notifications/test-telegram' \
--header 'Content-Type: application/json' \
--data '{
    "notification_type": "custom",
    "custom_text": "🔔 Test notification from Crude Oil Screener Backend"
}'
```

---

## 2. Direct Telegram Bot API cURLs

You can also send messages directly to the Telegram Bot API without going through FastAPI:

### 2.1 Send Plain Text Message

```bash
curl --location 'https://api.telegram.org/bot8653935387:AAG9KiaJhHpZ6ZlbOEwfUMjGRt2O-e9jgqI/sendMessage' \
--header 'Content-Type: application/x-www-form-urlencoded' \
--data-urlencode 'chat_id=-5328191316' \
--data-urlencode 'text=Hello from Crude Oil Screener!'
```

### 2.2 Send HTML-Formatted Message

```bash
curl --location 'https://api.telegram.org/bot8653935387:AAG9KiaJhHpZ6ZlbOEwfUMjGRt2O-e9jgqI/sendMessage' \
--header 'Content-Type: application/x-www-form-urlencoded' \
--data-urlencode 'chat_id=-5328191316' \
--data-urlencode 'parse_mode=HTML' \
--data-urlencode 'text=🚀🟢 <b>STRONG BUY - CRUDE OIL</b> 🛢️%0A%0A<b>Current PCR:</b> 1.8500%0A<b>Status:</b> Confirmed'
```

---

## 3. Crude Oil Screener Status & Strategy API

### 3.1 Get Live Status, Strategy Signals & Last PCR Readings

```bash
curl --location 'http://127.0.0.1:8000/crude-oil/status?limit=20&pcr_limit=10'
```

### 3.2 Root Health Check

```bash
curl --location 'http://127.0.0.1:8000/'
```

---

## 4. Firebase Cloud Messaging (FCM) Push Alerts (Optional)

### 4.1 Register Mobile FCM Token

```bash
curl --location 'http://127.0.0.1:8000/crude-oil/fcm/register' \
--header 'Content-Type: application/json' \
--data '{
    "token": "<YOUR_FCM_DEVICE_REGISTRATION_TOKEN>",
    "device_name": "Pixel 8 Pro"
}'
```

### 4.2 Test FCM Multicast Push

```bash
curl --location 'http://127.0.0.1:8000/crude-oil/notifications/test' \
--header 'Content-Type: application/json' \
--data '{
    "signal": "STRONG_BUY"
}'
```
