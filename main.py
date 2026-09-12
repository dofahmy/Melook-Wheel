"""
نقطة التشغيل الرئيسية للبوت.
شغّليه بالأمر: python main.py
"""
import asyncio
import os
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import BotCommand, BotCommandScopeChat, BotCommandScopeDefault
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

import config
import database
import handlers
import product_catalog
import scheduler

os.makedirs("logs", exist_ok=True)

logging.basicConfig(
    filename="logs/bot.log",
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
# اطبع في الشاشة كمان
logging.getLogger().addHandler(logging.StreamHandler())

logger = logging.getLogger(__name__)


def _build_customer_commands() -> list[BotCommand]:
    return [
        BotCommand("offers", "🛍️ آخر عروض أمازون"),
        BotCommand("goldenwheel", "🏆 عجلة العروض الذهبية"),
        BotCommand("myaccount", "📊 حسابي"),
        BotCommand("redeem", "استبدل رصيد الهدايا"),
        BotCommand("start", "إعادة تفعيل الحساب"),
        BotCommand("stop", "إلغاء الاشتراك"),
    ]


def _build_admin_commands() -> list[BotCommand]:
    cmds = list(_build_customer_commands())
    if config.EGYPT_BASE_CHANNEL:
        cmds += [
            BotCommand("reclaimstale", "🇪🇬 اسحبي تاجات العملاء الساكنين"),
            BotCommand("markpaid", "🇪🇬 صفّري رصيد عميل بعد الدفع"),
            BotCommand("pendingredeems", "🇪🇬 عملاء مستنيين تسليم هداياهم"),
        ]
    if config.EGYPT_GOLDEN_PRODUCTS_FILE:
        cmds.append(BotCommand("goldenstats", "🏆 تشخيص كاش العروض الذهبية"))
    cmds += [
        BotCommand("wheelwinners", "قايمة الفايزين بالعجلة"),
        BotCommand("listusers", "كل العملاء وحالتهم"),
        BotCommand("removeuser", "احذفي عميل خالص"),
        BotCommand("addtag", "ضيفي تاج تتبع واحد"),
        BotCommand("addtags", "ضيفي كذا تاج مرة واحدة"),
        BotCommand("removetag", "احذفي تاج"),
        BotCommand("listtags", "كل التاجات وحالتها"),
        BotCommand("stats", "إحصائيات سريعة"),
        BotCommand("msg", "ابعتي رسالة لعميل واحد"),
        BotCommand("broadcast", "ابعتي رسالة لكل العملاء"),
    ]
    return cmds


async def _set_bot_commands(app: Application):
    await app.bot.set_my_commands(_build_customer_commands(), scope=BotCommandScopeDefault())
    admin_commands = _build_admin_commands()
    for admin_id in config.ADMIN_IDS:
        try:
            await app.bot.set_my_commands(
                admin_commands, scope=BotCommandScopeChat(chat_id=admin_id)
            )
        except Exception as exc:
            logger.warning("مقدرتش أظبط قائمة أوامر الأدمن %s: %s", admin_id, exc)


async def _start_scheduler(app: Application):
    """
    بتشغّل الجدولة الدورية بعد ما تليجرام تجهز الـ event loop بتاعها.
    شغّالة كل دقيقة عشان توقيت الـ 60 دقيقة/10 دقايق يبقى دقيق.
    """
    aps = AsyncIOScheduler()
    aps.add_job(
        scheduler.send_reminders,
        "interval",
        minutes=1,
        args=[app],
        id="send_reminders",
    )
    aps.add_job(
        scheduler.recycle_expired_tags,
        "interval",
        minutes=1,
        args=[app],
        id="recycle_expired_tags",
    )
    aps.add_job(
        scheduler.reclaim_stale_egypt_tags,
        "interval",
        hours=1,
        args=[app],
        id="reclaim_stale_egypt_tags",
    )
    aps.start()
    logger.info("الجدولة الدورية بدأت الشغل")
    await _set_bot_commands(app)
    logger.info("قوائم أوامر الأدمن والعملاء اتظبطت")


def main():
    if not config.BOT_TOKEN:
        raise RuntimeError("لازم تحطي BOT_TOKEN في ملف .env الأول")

    # ⚠️ بايثون 3.13+ (وخصوصًا 3.14) مبقاش بيجهز event loop افتراضي
    # تلقائي زي زمان. مكتبة تليجرام لسه بتفترض وجوده، فبنجهزه إحنا يدويًا
    # قبل ما نبدأ، عشان نتفادى RuntimeError: There is no current event loop.
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    database.init_db()
    products = product_catalog.load_products()
    logger.info("تم تحميل %s منتج مقبول للعجلة الذهبية", len(products))

    app = (
        Application.builder()
        .token(config.BOT_TOKEN)
        .connection_pool_size(config.TELEGRAM_CONNECTION_POOL_SIZE)
        .pool_timeout(config.TELEGRAM_POOL_TIMEOUT)
        .post_init(_start_scheduler)
        .build()
    )

    # أوامر المستخدم العادي
    app.add_handler(CommandHandler("start", handlers.start))
    app.add_handler(CommandHandler("mytag", handlers.mytag))
    app.add_handler(CommandHandler("stop", handlers.stop))
    app.add_handler(CallbackQueryHandler(handlers.buyer_type_callback, pattern="^buyertype:"))
    app.add_handler(CallbackQueryHandler(handlers.show_offers_callback, pattern="^show_offers$"))
    app.add_handler(CallbackQueryHandler(handlers.start_shopping_callback, pattern="^start_shopping$"))
    app.add_handler(CallbackQueryHandler(handlers.golden_wheel_entry, pattern="^golden_start$"))
    app.add_handler(CallbackQueryHandler(handlers.golden_answer_callback, pattern="^goldenans:"))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handlers.handle_webapp_data))
    app.add_handler(MessageHandler(filters.Regex("^📊 حسابي$"), handlers.account_button))
    app.add_handler(CommandHandler("myaccount", handlers.account_button))
    app.add_handler(MessageHandler(filters.Regex("^🏆 عجلة العروض الذهبية$"), handlers.golden_wheel_entry))
    if config.EGYPT_DEALS_CHANNEL:
        app.add_handler(
            MessageHandler(
                filters.Chat(username=config.EGYPT_DEALS_CHANNEL) & filters.UpdateType.CHANNEL_POST,
                handlers.handle_new_deal_post,
            )
        )
    # العجلة الذهبية بتقرأ منتجاتها من الملف، مش من قناة أو جروب.

    # أوامر الأدمن
    app.add_handler(CommandHandler("addtag", handlers.addtag))
    app.add_handler(CommandHandler("addtags", handlers.addtags))
    app.add_handler(CommandHandler("removetag", handlers.removetag))
    app.add_handler(CommandHandler("listtags", handlers.listtags))
    app.add_handler(CommandHandler("wheelwinners", handlers.wheelwinners))
    app.add_handler(CommandHandler("reclaimstale", handlers.reclaimstale))
    app.add_handler(CommandHandler("redeem", handlers.redeem))
    app.add_handler(CommandHandler("goldenwheel", handlers.golden_wheel_entry))
    app.add_handler(CommandHandler("goldenstats", handlers.goldenstats))
    app.add_handler(CommandHandler("offers", handlers.offers))
    app.add_handler(CommandHandler("markpaid", handlers.markpaid))
    app.add_handler(CommandHandler("pendingredeems", handlers.pendingredeems))
    app.add_handler(CommandHandler("removeuser", handlers.removeuser))
    app.add_handler(CommandHandler("stats", handlers.stats))
    app.add_handler(CommandHandler("listusers", handlers.listusers))
    app.add_handler(CommandHandler("broadcast", handlers.broadcast))
    app.add_handler(CommandHandler("msg", handlers.msg))

    logger.info("البوت بدأ الشغل...")
    app.run_polling(allowed_updates=["message", "callback_query", "chat_member", "channel_post"])


if __name__ == "__main__":
    main()
