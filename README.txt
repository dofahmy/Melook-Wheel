Wafr Cash iOS/PWA package

Upload these files to the GitHub repo root:
- web_app.html
- admin_web.py
- manifest.webmanifest
- service-worker.js
- app-icon-180.png
- app-icon-192.png
- app-icon-512.png

app-icon-1024.png is included as the master icon for future App Store / native-container work.

After Railway deploy:
1. Open https://melook-wheel-production.up.railway.app/app in Safari on iPhone.
2. Tap Share.
3. Tap Add to Home Screen.
4. The Wafr Cash icon appears on the iPhone.
5. Opening it uses standalone mode, without the normal Safari address bar.

Existing APIs, OTP, suspend logic, Telegram linking, offers, wheel, redemption, and account logic are unchanged.
