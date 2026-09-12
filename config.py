"""
إعدادات المشروع: بيقرأ كل حاجة من ملف .env عشان محدش يحتاج يعدل في الكود
عشان يغيّر توكن أو تاج.
"""
import os
from dotenv import load_dotenv

load_dotenv()


def _split_ids(raw: str) -> list[int]:
    if not raw:
        return []
    return [int(x.strip()) for x in raw.split(",") if x.strip()]


BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# قناة برنامج السعودية (تفعيل برايم) - BASE_CHANNEL هو الاسم القديم، لسه شغال لو موجود
KSA_BASE_CHANNEL = os.getenv("KSA_BASE_CHANNEL", os.getenv("BASE_CHANNEL", ""))
# قناة برنامج مصر (نقط الشرا)
EGYPT_BASE_CHANNEL = os.getenv("EGYPT_BASE_CHANNEL", "")

ADMIN_IDS = _split_ids(os.getenv("ADMIN_IDS", ""))

DATABASE_PATH = os.getenv("DATABASE_PATH", "bot_data.db")

# لو عميل واخد لينك ولسه ما اتفعّلش (ما اشتركش في برايم) خلال المدة دي
# بالدقايق، اللينك يترجع للـ Pool تلقائيًا ويتاخد من حد تاني (برنامج السعودية بس)
TAG_LINK_TTL_MINUTES = int(os.getenv("TAG_LINK_TTL_MINUTES", "60"))
# قد إيه قبل انتهاء المدة نفكّر العميل (بالدقايق)
TAG_REMINDER_BEFORE_MINUTES = int(os.getenv("TAG_REMINDER_BEFORE_MINUTES", "10"))

# رابط تفعيل أمازون برايم - {tag} بيتستبدل بتاج التتبع بتاع كل عميل (برنامج السعودية)
# ✏️ عدّلي الرابط ده لو أمازون أسوشييتس ديتلك شكل تاني من صفحة الـ Promotions/Bounty بتاعتك
PRIME_LINK_TEMPLATE = os.getenv(
    "PRIME_LINK_TEMPLATE", "https://www.amazon.sa/amazonprime?tag={tag}"
)

# رابط الشرا بتاع أمازون مصر - {tag} بيتستبدل بتاج العميل (برنامج مصر)
EGYPT_SHOP_LINK_TEMPLATE = os.getenv(
    "EGYPT_SHOP_LINK_TEMPLATE", "https://www.amazon.eg/?tag={tag}"
)

# كل قد إيه نقطة = لفة واحدة في عجلة الحظ (برنامج مصر)
# مثال: لو القيمة 100، يبقى كل 100 نقطة (= 100 جنيه شرا) = لفة واحدة
EGYPT_POINTS_PER_SPIN = int(os.getenv("EGYPT_POINTS_PER_SPIN", "100"))

# تاجات التتبع الابتدائية (Tracking IDs الحقيقية بتاعتك في أمازون أسوشييتس)
# سيبيها فاضية وضيفي تاجاتك الحقيقية بأمر /addtag أو /addtags من البوت نفسه
INITIAL_TAG_POOL: list[str] = []

# رابط عجلة الحظ (Telegram Web App) - هيوصل للعميل تلقائيًا بعد التفعيل
# ⚠️ لازم يبقى رابط HTTPS حقيقي (زي GitHub Pages) عشان تليجرام يقبله
WHEEL_URL = os.getenv("WHEEL_URL", "")

# قناة عروض أمازون (بتاعتك انتي) اللي البوت هياخد منها آخر 10 عروض ويوزّعهم
# بلينك شخصي لكل عميل. حطي اليوزرنيم من غير @ (مثال: EgyptOffersHunter)
EGYPT_DEALS_CHANNEL = os.getenv("EGYPT_DEALS_CHANNEL", "")

# بعد قد إيه (بالساعات) نسحب تاج عميل مصري معندوش نشاط شرا (مفيش نقط
# اتضافتله) ونحوّله لمسار "بس بتابع" (لينك القناة العادي من غير تاج)
EGYPT_TAG_INACTIVITY_HOURS = int(os.getenv("EGYPT_TAG_INACTIVITY_HOURS", "30"))

# كل قد إيه عرض جديد يتجمّع قبل ما نبعت تنبيه "فيه عروض جديدة" للعملاء
# (عشان محدش يضايقه إشعار مع كل عرض عرض)
DEALS_NOTIFY_BATCH_SIZE = int(os.getenv("DEALS_NOTIFY_BATCH_SIZE", "10"))

# تاج عام (احتياطي) يتحط في لينك صفحة عروض أمازون للعميل اللي لسه مستني
# دوره في الطابور ومعندوش تاج شخصي بعد (مؤقتًا لحد ما ياخد تاجه هو)
EGYPT_GENERAL_TAG = os.getenv("EGYPT_GENERAL_TAG", "")

# قناة "العروض الذهبية" (قناتك الخاصة، البوت أدمن فيها). بما إنها قناة
# خاصة (رابط دعوة مش يوزرنيم عام)، لازم تحطي هنا آيدي القناة الرقمي مش
# اسمها - شوفي ملف README.md لطريقة معرفة الآيدي ده.
EGYPT_GOLDEN_CHANNEL_ID = os.getenv("EGYPT_GOLDEN_CHANNEL_ID", "")

# بيانات Amazon Creators API لمصر - مستخدمة في عجلة العروض الذهبية بس
# (لجلب اسم/سعر/خصم المنتج الحقيقي وقت السؤال)
EGYPT_AMAZON_CLIENT_ID = os.getenv("EGYPT_AMAZON_CLIENT_ID", "")
EGYPT_AMAZON_CLIENT_SECRET = os.getenv("EGYPT_AMAZON_CLIENT_SECRET", "")
EGYPT_AMAZON_PARTNER_TAG = os.getenv("EGYPT_AMAZON_PARTNER_TAG", "")

# ملف المنتجات المقبولة في برنامج SPCC. البوت يقرأ الأسئلة والروابط منه
# بدل استقبال المنتجات من قناة/جروب.
EGYPT_GOLDEN_PRODUCTS_FILE = os.getenv(
    "EGYPT_GOLDEN_PRODUCTS_FILE", "amazon_all_accepted_products.json"
)

# تاج روابط منتجات العجلة الذهبية.
EGYPT_GOLDEN_PARTNER_TAG = os.getenv("EGYPT_GOLDEN_PARTNER_TAG", "leeno-21")

# معادلة جائزة العميل الشخصية:
# EPC المكتوب × نسبة الكليكات المعتمدة × نسبة قيمة الكليك الفعلية × نصيب العميل.
EGYPT_APPROVED_CLICK_RATE = float(os.getenv("EGYPT_APPROVED_CLICK_RATE", "0.30"))
EGYPT_REAL_CLICK_VALUE_RATE = float(os.getenv("EGYPT_REAL_CLICK_VALUE_RATE", "0.30"))
EGYPT_CUSTOMER_REWARD_RATE = float(os.getenv("EGYPT_CUSTOMER_REWARD_RATE", "0.40"))
# تقسيم منتجات الجولة حسب ERP. كل 5 أسئلة = 2 منخفض + 2 متوسط + 1 عالي.
# الحدود قابلة للتعديل من ENV من غير تعديل الكود.
EGYPT_GOLDEN_LOW_MIN_EPC = float(os.getenv("EGYPT_GOLDEN_LOW_MIN_EPC", "0.25"))
EGYPT_GOLDEN_LOW_MAX_EPC = float(os.getenv("EGYPT_GOLDEN_LOW_MAX_EPC", "0.75"))
EGYPT_GOLDEN_MEDIUM_MAX_EPC = float(os.getenv("EGYPT_GOLDEN_MEDIUM_MAX_EPC", "2.00"))
EGYPT_GOLDEN_QUESTIONS_PER_ROUND = int(
    os.getenv("EGYPT_GOLDEN_QUESTIONS_PER_ROUND", "5")
)

# أقل رصيد جوايز (بالجنيه) لازم يوصله العميل قبل ما يقدر يطلب استبدال
EGYPT_REDEEM_MIN_BALANCE = float(os.getenv("EGYPT_REDEEM_MIN_BALANCE", "5"))

# Railway / SQLite tuning (safe defaults for a single worker)
DATABASE_BUSY_TIMEOUT_MS = int(os.getenv("DATABASE_BUSY_TIMEOUT_MS", "30000"))
DATABASE_CACHE_MB = int(os.getenv("DATABASE_CACHE_MB", "64"))
TELEGRAM_CONNECTION_POOL_SIZE = int(os.getenv("TELEGRAM_CONNECTION_POOL_SIZE", "64"))
TELEGRAM_POOL_TIMEOUT = float(os.getenv("TELEGRAM_POOL_TIMEOUT", "30"))
