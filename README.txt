Wafr Security Suspend v1

Implemented:
- After 3 completed login+logout cycles in the same Cairo calendar day, the Web account is suspended automatically.
- All active web sessions for that account are deleted immediately.
- Suspended users cannot request OTP, login, or use an old browser session.
- Customer message:
  لاحظنا دخول وخروج متكرر على حسابك. لحماية بياناتك وحسابك تم إيقاف الحساب مؤقتًا، وسيقوم أحد ممثلي خدمة العملاء بالتواصل معك خلال 24 ساعة.
- Admin has a new "⛔ الموقوفين" page with phone, suspend time, today's login/logout counters, and a Reactivate button.
- Reactivation clears the suspension, resets today's counter, and requires a fresh login.
- Reactivation sends a fresh OTP using the existing Authevo OTP flow.

No extra WhatsApp Business setup is needed. Reactivation uses the same existing OTP provider.

Upload these 5 files:
database.py
admin_web.py
web_auth.py
admin_dashboard.html
web_app.html

Telegram linking UI now includes: https://t.me/WafrCashBot and tells the customer to send /linkweb CODE.
