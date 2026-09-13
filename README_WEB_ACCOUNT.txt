WAFR CASH - Multi-platform account system
========================================

Public Web App:
  https://YOUR-RAILWAY-DOMAIN/app

Traffic source examples:
  /app?src=tiktok
  /app?src=snapchat
  /app?src=instagram
  /app?src=facebook

Production OTP variables on Railway:
  WEB_AUTH_SECRET=<long random secret>
  OTP_DELIVERY_MODE=twilio_verify
  TWILIO_ACCOUNT_SID=<Twilio Account SID>
  TWILIO_AUTH_TOKEN=<Twilio Auth Token>
  TWILIO_VERIFY_SERVICE_SID=<Twilio Verify Service SID>

Local/dev-only OTP mode:
  OTP_DELIVERY_MODE=dev
The OTP is printed to Railway/server logs. Do NOT use dev mode in production.

Telegram linking:
  1. Customer logs into Web App with phone + OTP.
  2. Customer presses "اعمل كود ربط Telegram".
  3. Web App shows: /linkweb XXXXXX
  4. Customer sends that command to the Telegram bot.
  5. Web account and Telegram account become one account; balances are preserved.

New files:
  web_auth.py
  web_app.html

Modified files:
  database.py
  config.py
  admin_web.py
  handlers.py
  main.py

Existing admin dashboard remains at /admin.
Existing bot/redemption/report logic is preserved.
