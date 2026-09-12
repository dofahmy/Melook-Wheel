"""
المهام الدورية بتاعة إدارة اللينكات:
1. send_reminders - تفكّر أي عميل واخد لينك وقربت مدته تخلص (10 دقايق قبل النهاية).
2. recycle_expired_tags - ترجع لينك أي عميل عدّت مدته (60 دقيقة) من غير تفعيل،
   وتديه لأول واحد في طابور الانتظار تلقائيًا.
كل المهمتين بيشتغلوا كل دقيقة عشان التوقيت يبقى دقيق.
"""
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import Application

import config
import database

logger = logging.getLogger(__name__)

RESTART_KEYBOARD = ReplyKeyboardMarkup(
    [["/start"]], resize_keyboard=True, one_time_keyboard=False
)


def _prime_keyboard(keyword: str):
    link = config.PRIME_LINK_TEMPLATE.format(tag=keyword)
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("✅ اضغطي هنا وسجّلي في برايم", url=link)]]
    )


def _shop_keyboard(keyword: str):
    link = config.EGYPT_SHOP_LINK_TEMPLATE.format(tag=keyword)
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🛍️ روح عروض أمازون من لينكك", url=link)]]
    )


async def send_reminders(app: Application):
    users = database.find_users_needing_reminder(
        config.TAG_LINK_TTL_MINUTES, config.TAG_REMINDER_BEFORE_MINUTES
    )
    for row in users:
        database.mark_reminder_sent(row["user_id"])
        try:
            await app.bot.send_message(
                chat_id=row["user_id"],
                text=(
                    f"⏰ فاضلك {config.TAG_REMINDER_BEFORE_MINUTES} دقايق بس عشان "
                    "تخلّصي التسجيل في أمازون برايم، وبعدها اللينك هيترجع ويتاخد "
                    "من حد تاني. لو خلّصتي بالفعل استني هنراجعها ونفعّل حسابك."
                ),
            )
        except Exception:
            pass


async def recycle_expired_tags(app: Application):
    expired = database.find_expired_assignments(config.TAG_LINK_TTL_MINUTES)
    for row in expired:
        user_id = row["user_id"]
        database.release_tag_by_user(user_id)
        logger.info("خلصت مدة المستخدم %s - اترجع اللينك للـ Pool", user_id)
        try:
            await app.bot.send_message(
                chat_id=user_id,
                text=(
                    "⚠️ خلصت مدة اللينك بتاعك من غير ما نشوف تفعيل.\n"
                    "دوسي على الزرار تحت لو لسه عايزة تشتركي."
                ),
                reply_markup=RESTART_KEYBOARD,
            )
        except Exception:
            pass

        next_user_id = database.pop_next_in_queue()
        if next_user_id:
            tag = database.get_available_tag()
            if tag:
                database.assign_tag_to_user(next_user_id, tag["id"])
                try:
                    await app.bot.send_message(
                        chat_id=next_user_id,
                        text=(
                            "دورك جه! 🎉 دوسي على الزرار تحت وسجّلي اشتراكك في أمازون برايم.\n"
                            "⚠️ الزرار ده مخصص ليكي وحدك — متبعتيهوش لحد تاني."
                        ),
                        reply_markup=_prime_keyboard(tag["keyword"]),
                    )
                except Exception:
                    pass


async def reclaim_stale_egypt_tags(app: Application):
    """
    شغّالة أوتوماتيك كل كام ساعة: بتسحب تاج أي عميل مصري معندوش نشاط
    من قد EGYPT_TAG_INACTIVITY_HOURS، وتحوّله لمسار المتابعة العادي،
    والتاج يروح لحد تاني في الطابور.
    """
    stale = database.find_stale_tag_holders(config.EGYPT_TAG_INACTIVITY_HOURS)
    for row in stale:
        user_id = row["user_id"]
        database.downgrade_to_browser(user_id)
        logger.info("اترد تاج المستخدم %s بسبب عدم وجود نشاط", user_id)
        try:
            await app.bot.send_message(
                chat_id=user_id,
                text=(
                    "⚠️ سحبنا لينكك الخاص بسبب عدم وجود مشتريات مسجّلة.\n"
                    "هتفضل تشوف كل العروض عادي جوه البوت، وهنديك نقط هدية من وقت للتاني."
                ),
                reply_markup=ReplyKeyboardRemove(),
            )
        except Exception:
            pass

        next_user_id = database.pop_next_in_queue("egypt")
        if next_user_id:
            tag = database.get_available_tag("egypt")
            if tag:
                database.assign_tag_to_user(next_user_id, tag["id"])
                try:
                    await app.bot.send_message(
                        chat_id=next_user_id,
                        text="دورك جه! 🎉 ده لينكك الخاص. استخدمه في كل مرة تشتري من أمازون مصر.",
                        reply_markup=_shop_keyboard(tag["keyword"]),
                    )
                except Exception:
                    pass
