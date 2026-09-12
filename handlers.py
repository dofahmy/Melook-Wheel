"""
أوامر البوت - بيدعم برنامجين:
- ksa: تفعيل اشتراكات أمازون برايم (/start /mytag /stop)
- egypt: نظام نقط الشرا (Amazon Points) (/start /mytag /stop /wheel)

أدمن السعودية: /addtag /addtags /removetag /listtags /verify /verified /wheelwinners /notifywheel
أدمن مصر: /addpoints /closemonth
مشترك: /stats /listusers /removeuser /broadcast /msg
"""
import logging
import json

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
    WebAppInfo,
)
from telegram.constants import ChatMemberStatus
from telegram.ext import ContextTypes

import config
import database
import egypt_amazon_client
import product_catalog

logger = logging.getLogger(__name__)

RESTART_KEYBOARD = ReplyKeyboardMarkup(
    [["/start"]], resize_keyboard=True, one_time_keyboard=False
)

_BOTH_PROGRAMS = bool(config.KSA_BASE_CHANNEL) and bool(config.EGYPT_BASE_CHANNEL)


def _program_choice_keyboard():
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("🇸🇦 السعودية", callback_data="program:ksa"),
            InlineKeyboardButton("🇪🇬 مصر", callback_data="program:egypt"),
        ]]
    )


def _wheel_keyboard(spins_balance: int | None = None):
    """
    ⚠️ مهم: لازم زرار العجلة يكون جوه لوحة المفاتيح تحت (Reply Keyboard)
    مش زرار جوه الرسالة (Inline) - عشان تليجرام مايسمحش بإرسال نتيجة الـ
    Web App للبوت (sendData) غير من زرار لوحة المفاتيح بس.

    لو spins_balance اتحدد (برنامج مصر)، بيتبعت رقم رصيد اللفّات في رابط
    الصفحة عشان زرار اللف يتعطّل تلقائي هناك لو الرصيد صفر.

    بنضيف كمان "كسر كاش" (timestamp) في الرابط كل مرة، عشان تطبيق تليجرام
    مايفتحش نسخة قديمة محفوظة عنده من الصفحة (ده كان بيخلي الرصيد القديم
    يفضل ظاهر حتى بعد ما يتغيّر فعليًا).
    """
    if not config.WHEEL_URL:
        return None
    import time
    url = config.WHEEL_URL
    sep = "&" if "?" in url else "?"
    # الوضع القديم مخصص لعجلة السعودية فقط. فتح wheel1.html من غير mode
    # يعرض عجلة مصر الجديدة افتراضيًا.
    params = f"t={int(time.time() * 1000)}&mode=legacy"
    if spins_balance is not None:
        params += f"&balance={spins_balance}"
    url = f"{url}{sep}{params}"

    label = "🎡 لفي عجلة الحظ"
    if spins_balance is not None:
        label += f" ({spins_balance})"

    return ReplyKeyboardMarkup(
        [[KeyboardButton(label, web_app=WebAppInfo(url=url))]],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


ACCOUNT_BUTTON_TEXT = "📊 حسابي"
GOLDEN_BUTTON_TEXT = "🎁 عجلة بطاقات الهدايا"


def _format_egp(amount: float) -> str:
    """يعرض المبالغ الصغيرة من غير تقريبها ظلمًا إلى صفر."""
    value = float(amount or 0)
    if value >= 1:
        return f"{value:.2f}".rstrip("0").rstrip(".")
    return f"{value:.6f}".rstrip("0").rstrip(".") or "0"


def _egypt_customer_keyboard(user_id: int | None = None):
    """
    لوحة مفاتيح عميل مصر اللي معاه تاج شخصي: زرار "عجلة بطاقات الهدايا"
    (بيفتح العجلة مباشرة بدوسة واحدة - أو يجيب لفة مستنية لو موجودة)،
    وزرار "حسابي" ثابت.
    """
    rows = []
    if config.EGYPT_GOLDEN_PRODUCTS_FILE:
        rows.append(_golden_button_row(user_id))
    rows.append([KeyboardButton(ACCOUNT_BUTTON_TEXT)])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=False)


def _prime_keyboard(keyword: str):
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("✅ اضغطي هنا وسجّلي في برايم", url=_prime_link(keyword))]]
    )


def _shop_keyboard(keyword: str):
    link = config.EGYPT_SHOP_LINK_TEMPLATE.format(tag=keyword)
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🛍️ روح عروض أمازون من لينكك", url=link)]]
    )


def _prime_link(keyword: str) -> str:
    return config.PRIME_LINK_TEMPLATE.format(tag=keyword)


def _base_channel_for(program: str) -> str:
    return config.KSA_BASE_CHANNEL if program == "ksa" else config.EGYPT_BASE_CHANNEL


async def _is_subscribed(context: ContextTypes.DEFAULT_TYPE, user_id: int, channel: str) -> bool:
    if not channel:
        return True
    try:
        member = await context.bot.get_chat_member(channel, user_id)
        return member.status in (
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
        )
    except Exception as exc:
        logger.warning("تعذر التحقق من اشتراك %s في %s: %s", user_id, channel, exc)
        return False


# ---------------- /start وتفرّع البرنامج ----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """بداية البوت الحالية: مصر فقط — ترحيب ثم عجلة العروض الذهبية مباشرة."""
    user = update.effective_user
    database.upsert_user(user.id, user.username)
    database.set_user_program(user.id, "egypt")

    # لو قناة مصر متضبطة، نتأكد من الاشتراك الأول.
    if config.EGYPT_BASE_CHANNEL:
        subscribed = await _is_subscribed(context, user.id, config.EGYPT_BASE_CHANNEL)
        if not subscribed:
            await update.message.reply_text(
                f"لازم الأول تكون مشترك في قناة {config.EGYPT_BASE_CHANNEL} عشان تفعّل البوت.\n"
                "بعد الاشتراك ابعت /start تاني."
            )
            return

    # شاشة الانترو/الترحيب الحالية — بدون أي اختيار مصر/السعودية.
    await update.message.reply_text(
        "🎉 أهلاً بيك في وفر كاش!\n\n"
        "هنا هتدخل مباشرة على عجلة العروض الذهبية، تختار عرض وتبدأ أسئلة الجولة. 🏆",
        reply_markup=_egypt_customer_keyboard(user.id),
    )

    # افتح مسار العجلة الذهبية فورًا بعد الترحيب.
    await _offer_golden_round(context, update.effective_chat.id, user.id)


async def program_choice_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    program = query.data.split(":")[1]
    user = update.effective_user
    database.set_user_program(user.id, program)
    await query.message.delete()
    await _continue_start_from_query(update, context, user.id, program)


async def _continue_start_from_query(update, context, user_id, program):
    channel = _base_channel_for(program)
    subscribed = await _is_subscribed(context, user_id, channel)
    chat_id = update.effective_chat.id
    if not subscribed:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"لازم الأول تكوني مشتركة في قناة {channel} عشان تفعّلي البوت.\nبعد الاشتراك ابعتي /start تاني.",
        )
        return
    await _proceed_program_flow(context, user_id, chat_id, program)


async def _continue_start(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, program: str):
    channel = _base_channel_for(program)
    subscribed = await _is_subscribed(context, user_id, channel)
    if not subscribed:
        await update.message.reply_text(
            f"لازم الأول تكوني مشتركة في قناة {channel} عشان تفعّلي البوت.\nبعد الاشتراك ابعتي /start تاني."
        )
        return
    await _proceed_program_flow(context, user_id, update.effective_chat.id, program)


async def _proceed_program_flow(context, user_id: int, chat_id: int, program: str):
    existing = database.get_user(user_id)

    if program == "ksa":
        if existing["verified"]:
            await context.bot.send_message(
                chat_id=chat_id,
                text="حسابك مفعّل بالفعل ✅ اشتراكك في برايم اتأكد، وهتدخلي السحب على الهدايا.",
            )
            return
        if existing["tag_id"]:
            keyword = database.get_user_tag_keyword(user_id)
            await context.bot.send_message(
                chat_id=chat_id,
                text="معاك بالفعل لينك تفعيل. دوس على الزرار تحت وسجّل اشتراكك في برايم:",
                reply_markup=_prime_keyboard(keyword),
            )
            return
        await _assign_new_tag(context, chat_id, user_id, program)

    else:  # egypt
        if existing["tag_id"]:
            await _send_offers_welcome_message(context, chat_id)
            await context.bot.send_message(
                chat_id=chat_id,
                text="لوحة أزرارك جاهزة تحت 👇",
                reply_markup=_egypt_customer_keyboard(user_id),
            )
            return

        if existing["buyer_type"] == "browser":
            if existing["has_purchased_before"]:
                # عميل اشترى قبل كده ورجع تاني - أولوية ليه
                tag = database.get_available_tag(program)
                if tag:
                    database.set_buyer_type(user_id, "regular")
                    database.assign_tag_to_user(user_id, tag["id"])
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=(
                            "أهلاً بيك تاني! 🎉 بما إنك اشتريت قبل كده، ده لينكك الخاص جاهز على طول.\n"
                            "استخدمه في كل مرة تشتري فيها من أمازون مصر."
                        ),
                        reply_markup=_egypt_customer_keyboard(user_id),
                    )
                else:
                    database.set_buyer_type(user_id, "regular")
                    database.add_to_queue(user_id)
                    await context.bot.send_message(chat_id=chat_id, text="أهلاً بيك تاني! 🎉", reply_markup=ReplyKeyboardRemove())
                    await _send_offers_welcome_message(context, chat_id)
                return
            await context.bot.send_message(chat_id=chat_id, text="أهلاً بيك تاني! 👋", reply_markup=ReplyKeyboardRemove())
            await _send_offers_welcome_message(context, chat_id)
            return

        if existing["buyer_type"] == "regular" and existing["queued_at"]:
            await _send_offers_welcome_message(context, chat_id)
            return

        if existing["buyer_type"] == "regular":
            await _assign_new_tag(context, chat_id, user_id, program)
            return

        # لسه ما اخترش نوعه - اسأليه الأول
        await context.bot.send_message(
            chat_id=chat_id,
            text="أهلاً بيك! 👋 عشان نظبطلك التجربة صح، انت إيه فيهم؟",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🛍️ بشتري من أمازون بانتظام", callback_data="buyertype:regular")],
                [InlineKeyboardButton("👀 لسه بس عايز أتابع العروض", callback_data="buyertype:browser")],
            ]),
        )


async def _send_offers_welcome_message(context, chat_id: int):
    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "🎉 أهلاً بيك في بوتنا!\n\n"
            "هدف البوت إنك تستفيد بأفضل عروض أمازون مصر بس، ومع كل عملية "
            "شرا بتكسب هدايا حقيقية من خلال عجلة بطاقات الهدايا 🎁\n\n"
            "دوس تحت وشوف آخر العروض دلوقتي:"
        ),
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🛍️ اعرض آخر عروض أمازون", callback_data="show_offers")]]
        ),
    )


async def buyer_type_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    buyer_type = query.data.split(":")[1]
    user = update.effective_user
    database.set_buyer_type(user.id, buyer_type)
    await query.message.delete()
    chat_id = update.effective_chat.id

    if buyer_type == "browser":
        await context.bot.send_message(chat_id=chat_id, text="تمام 👍", reply_markup=ReplyKeyboardRemove())
        await _send_offers_welcome_message(context, chat_id)
    else:
        await _assign_new_tag(context, chat_id, user.id, "egypt")


async def _assign_new_tag(context, chat_id: int, user_id: int, program: str):
    tag = database.get_available_tag(program)
    if not tag:
        database.add_to_queue(user_id)
        if program == "egypt":
            await _send_offers_welcome_message(context, chat_id)
        else:
            position = database.queue_length(program)
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    "كل التاجات مشغولة حاليًا 🙏\n"
                    f"انت رقم {position} في قايمة الانتظار، وهيوصلك تاجك أول ما واحد يتوفر."
                ),
            )
        return

    database.assign_tag_to_user(user_id, tag["id"])

    if program == "ksa":
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "أهلاً بيك! 🎉\n"
                "دوس على الزرار تحت وسجّل اشتراكك في أمازون برايم.\n"
                "بعد ما تخلّصي، هنراجع اشتراكك ونفعّل حسابك، وتبقي داخلة في سحوبات الهدايا 🎁\n\n"
                "⚠️ الزرار ده مخصص لك وحدك — لو اتفوّت لحد تاني واستخدمه هو، انت اللي هتخسر الفرصة."
            ),
            reply_markup=_prime_keyboard(tag["keyword"]),
        )
    else:
        await _send_offers_welcome_message(context, chat_id)
        await context.bot.send_message(
            chat_id=chat_id,
            text="لوحة أزرارك جاهزة تحت 👇",
            reply_markup=_egypt_customer_keyboard(user_id),
        )


async def account_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """شاشة حساب عميل مصر: حالة الجولة، الهدية المعلقة، ورصيد الجوائز."""
    user = update.effective_user
    row = database.get_user(user.id)

    if not row:
        await update.message.reply_text("لسه حسابك مش متفعّل. ابعت /start الأول.")
        return

    # النسخة الحالية مصر فقط. ما نربطش فتح الحساب بوجود tag_id؛
    # العميل الجديد بيدخل العجلة الذهبية مباشرة وممكن مايبقاش عنده تاج شخصي.
    if row["program"] != "egypt":
        database.set_user_program(user.id, "egypt")
        row = database.get_user(user.id)

    answered = int(row["golden_answered_count"] or 0)
    target = int(row["golden_target"] or config.EGYPT_GOLDEN_QUESTIONS_PER_ROUND)
    correct = int(row["golden_opened_count"] or 0)
    remaining = max(target - answered, 0)
    round_earnings = float(row["golden_round_earnings"] or 0)
    gift_balance = float(row["gift_balance"] or 0)
    pending = database.get_pending_lucky_spin(user.id)

    if pending:
        round_status = "🎁 عندك لفة هدية مستنية الاستلام"
    elif answered >= target:
        round_status = "✅ خلّصت أسئلة الجولة — عجلة الهدية جاهزة"
    elif answered == 0:
        round_status = "🎡 جاهز تبدأ جولة جديدة"
    else:
        round_status = f"🎯 مكمل الجولة — فاضلك {remaining} سؤال"

    pending_line = (
        f"\n🎁 هدية مستنية: {_format_egp(pending['prize'])} جنيه"
        if pending else ""
    )

    await update.message.reply_text(
        "📊 حسابك\n\n"
        f"الحالة: {round_status}\n"
        f"📝 الأسئلة: {answered}/{target}\n"
        f"✅ الإجابات الصح: {correct}\n"
        f"⏳ المتبقي في الجولة: {remaining}\n"
        f"💰 قيمة الجولة الحالية: {_format_egp(round_earnings)} جنيه"
        f"{pending_line}\n"
        f"🎁 رصيد جوايزك الجاهز للاستبدال: {_format_egp(gift_balance)} جنيه\n\n"
        f"الحد الأدنى للاستبدال: {_format_egp(config.EGYPT_REDEEM_MIN_BALANCE)} جنيه\n"
        "للاستبدال ابعت /redeem",
        reply_markup=_egypt_customer_keyboard(user.id),
    )


async def mytag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    database.set_user_activity_now(user.id)
    row = database.get_user(user.id)
    if not row or not row["program"]:
        await update.message.reply_text("لسه ما فعّلتيش حسابك. ابعتي /start الأول.")
        return

    if row["program"] == "ksa":
        if row["verified"]:
            await update.message.reply_text("حسابك مفعّل ✅ وداخلة في سحوبات الهدايا.")
            return
        if row["tag_id"]:
            keyword = database.get_user_tag_keyword(user.id)
            await update.message.reply_text(
                "لسه بنستنى نتأكد إن الاشتراك اتم. لينك التفعيل بتاعك تحت:",
                reply_markup=_prime_keyboard(keyword),
            )
            return
        await update.message.reply_text("انت في قايمة الانتظار حاليًا، هيوصلك رسالة أول ما يتوفر لينك.")
    else:
        if row["tag_id"]:
            await update.message.reply_text(
                f"إجابات الجولة: {row['golden_answered_count']}/{row['golden_target']} "
                f"(الصح: {row['golden_opened_count']})\n"
                f"رصيد جوايزك (جاهز للاستبدال): {_format_egp(row['gift_balance'])} جنيه\n\n"
                "عايز تستبدل رصيدك؟ ابعت /redeem",
                reply_markup=_egypt_customer_keyboard(user.id),
            )
            return
        if row["queued_at"]:
            await update.message.reply_text(
                "حسابك شغال 👍 شوف آخر العروض من هنا:",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🛍️ اعرض آخر عروض أمازون", callback_data="show_offers")]]
                ),
            )
        else:
            await update.message.reply_text(
                "كفاية متابعة 😍 ابدأ الشرا علشان تدخل تلف عجلة بطاقات الهدايا وتكسب هدايا وفلوس كتيرررررر\n"
                "شوف آخر العروض من هنا 👇🏻",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🛒 ابدأ الشرا دلوقتي", callback_data="start_shopping")]]
                ),
            )


async def wheel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """عجلة الحظ - برنامج السعودية بس (لفة واحدة بعد التفعيل). عملاء مصر
    بقى عندهم عجلة بطاقات الهدايا بدل النظام القديم ده."""
    user = update.effective_user
    row = database.get_user(user.id)
    if not row or not row["program"]:
        await update.message.reply_text("لسه ما فعّلتيش حسابك. ابعتي /start الأول.")
        return

    if row["program"] == "ksa":
        keyboard = _wheel_keyboard()
        if not keyboard:
            await update.message.reply_text("عجلة الحظ لسه مش متاحة دلوقتي.")
            return
        if not row["verified"]:
            await update.message.reply_text("عجلة الحظ متاحة بس للعملاء المفعّلين. لسه حسابك مايتفعّلش.")
            return
        prize = database.get_wheel_prize(user.id)
        if prize:
            await update.message.reply_text(f"أنت لفيت العجلة بالفعل وكسبت: {prize} 🎁")
            return
        await update.message.reply_text("دوس تحت ولف عجلة الحظ 🎡", reply_markup=keyboard)
    else:
        await update.message.reply_text(
            "دلوقتي هدايانا بتيجي من عجلة بطاقات الهدايا 🏆\nدوس على الزرار تحت ولف على طول:",
            reply_markup=_egypt_customer_keyboard(user.id),
        )


async def handle_webapp_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """بيستقبل بيانات من أي صفحة Web App (نتيجة العجلة، أو تأكيد فتح عرض ذهبي)."""
    user = update.effective_user
    raw = update.effective_message.web_app_data.data
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return

    if payload.get("type") == "golden_select":
        await _send_golden_question(context, update.effective_chat.id, user.id)
        return

    if payload.get("type") == "golden_prize_result":
        try:
            spin_id = int(payload.get("spin_id"))
        except (TypeError, ValueError):
            return
        prize = database.claim_lucky_spin(spin_id, user.id)
        chat_id = update.effective_chat.id
        if prize is None:
            # اللفة دي مش موجودة، أو بتاعة عميل تاني، أو مُستلمة قبل كده -
            # برضه نرجّعله الزرار الطبيعي عشان مايفضلش عالق على زرار قديم مايعملش حاجة
            logger.warning("محاولة استلام لفة غير صالحة: spin_id=%s user=%s", spin_id, user.id)
            await _offer_golden_round(context, chat_id, user.id)
            return
        new_balance = database.add_gift_balance(user.id, prize)
        prize_text = f"{_format_egp(prize)} جنيه"
        await update.effective_message.reply_text(
            f"🎉 مبروك! كسبت من عجلة بطاقات الهدايا: {prize_text}\n"
            f"رصيدك الحالي: {_format_egp(new_balance)} جنيه.\nعايز تستبدله؟ ابعت /redeem"
        )
        _notify_admins(context, f"🏆 عميل {user.id} (@{user.username or '—'}) كسب من العجلة الذهبية: {prize_text}")
        # نرجّعله الزرار الطبيعي (عجلة الاختيار للدورة الجاية) بدل ما يفضل
        # زرار "استلم" القديم ظاهر وهو خلاص استلم
        await _offer_golden_round(context, chat_id, user.id)
        return

    if payload.get("type") != "wheel_result":
        return
    prize = payload.get("prize", "هدية")
    row = database.get_user(user.id)
    if not row:
        return

    if row["program"] == "ksa":
        saved = database.set_wheel_prize(user.id, prize)
        if saved:
            await update.effective_message.reply_text(
                f"🎉 اتسجّلت هديتك: {prize}\nهتتأكد وتوصلك من فريق الدعم خلال يومين."
            )
            _notify_admins(context, f"🎡 عميل السعودية {user.id} (@{user.username or '—'}) كسب: {prize}")
        else:
            await update.effective_message.reply_text(
                f"انت لفيت العجلة قبل كده وكسبت: {database.get_wheel_prize(user.id)}"
            )
    else:
        # نظام النقط/اللفّات القديم اتلغى خالص - العجلة الذهبية هي المصدر
        # الوحيد للجوايز دلوقتي. الفرع ده باقي بس كاحتياط لو حد فتح نسخة
        # قديمة متخزّنة من صفحة العجلة.
        await update.effective_message.reply_text(
            "عجلة الحظ القديمة اتلغت. استخدم عجلة بطاقات الهدايا 🏆",
            reply_markup=_egypt_customer_keyboard(user.id),
        )


def _extract_egp_amount(prize_text: str) -> int:
    """بتستخرج الرقم من نص الهدية (زي '5 جنيه' -> 5). لو مفيش رقم، بترجع 0."""
    import re
    match = re.search(r"\d+", prize_text)
    return int(match.group()) if match else 0


def _notify_admins(context: ContextTypes.DEFAULT_TYPE, text: str):
    for admin_id in config.ADMIN_IDS:
        try:
            context.application.create_task(context.bot.send_message(chat_id=admin_id, text=text))
        except Exception:
            pass


async def _switch_program(update: Update, context: ContextTypes.DEFAULT_TYPE, target_program: str):
    """يسمح لأي عميل (جديد أو قديم) إنه ينضم لبرنامج تاني، حتى لو كان مسجّل في برنامج مختلف بالفعل."""
    user = update.effective_user
    database.upsert_user(user.id, user.username)
    current = database.get_user(user.id)

    if current["program"] != target_program:
        # سيبي أي لينك/تاج ماسكاه في البرنامج القديم يرجع للـ Pool بتاعه
        database.release_tag_by_user(user.id)
        database.set_user_program(user.id, target_program)

    await _continue_start(update, context, user.id, target_program)


async def join_ksa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _switch_program(update, context, "ksa")


async def join_egypt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _switch_program(update, context, "egypt")


async def redeem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """برنامج مصر: العميل بيطلب استبدال رصيد جوايزه المتجمّع."""
    user = update.effective_user
    row = database.get_user(user.id)
    if not row or row["program"] != "egypt":
        await update.message.reply_text("الأمر ده متاح بس لبرنامج مصر.")
        return
    balance = row["gift_balance"]
    if balance < config.EGYPT_REDEEM_MIN_BALANCE:
        await update.message.reply_text(
            f"محتاج توصل لرصيد {config.EGYPT_REDEEM_MIN_BALANCE} جنيه على الأقل عشان تقدر تستبدله.\n"
            f"رصيدك الحالي: {_format_egp(balance)} جنيه. كمّل تلف واكسب هدايا! 🎁"
        )
        return
    await update.message.reply_text(
        f"✅ طلب الاستبدال اتبعت (رصيدك: {_format_egp(balance)} جنيه).\nفريق الدعم هيتواصل معاك خلال يومين لتسليم هديتك."
    )
    username_line = f"@{user.username}" if user.username else "(مفيش يوزرنيم)"
    _notify_admins(
        context,
        f"💰 طلب استبدال جديد!\nالعميل: {user.id} {username_line}\nالرصيد: {balance} جنيه\n"
        f"استخدمي /markpaid {user.id} بعد ما تدفعيله.",
    )


async def luckypool(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """أدمن: حالة مخزون عجلة بطاقات الهدايا دلوقتي."""
    if not _require_admin(update):
        return
    pool = database.get_lucky_wheel_pool_status()
    total_remaining = sum(p["remaining"] for p in pool)
    total_size = sum(p["total"] for p in pool)
    lines = [f"📦 المخزون الحالي ({total_remaining}/{total_size}):"]
    for p in pool:
        pct = (p["remaining"] / p["total"] * 100) if p["total"] else 0
        lines.append(f"  {p['prize']:g} جنيه: {p['remaining']}/{p['total']} ({pct:.0f}%)")
    await update.message.reply_text("\n".join(lines))


async def pendingredeems(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """أدمن: كل عملاء مصر اللي عندهم رصيد جوايز لسه محتاج تسليم."""
    if not _require_admin(update):
        return
    users = database.list_pending_gift_balances()
    if not users:
        await update.message.reply_text("مفيش حد معاه رصيد مستني تسليم دلوقتي 👍")
        return
    lines = []
    total = 0
    for u in users:
        name = f"@{u['username']}" if u["username"] else str(u["user_id"])
        lines.append(f"• {u['user_id']} ({name}) — {u['gift_balance']} جنيه")
        total += u["gift_balance"]
    await update.message.reply_text(
        f"💰 عدد العملاء المستنيين: {len(users)} | الإجمالي: {total} جنيه\n\n"
        + "\n".join(lines[:60])
        + "\n\nبعد ما تسلّمي أي حد، استخدمي /markpaid <آيدي العميل>"
    )


async def markpaid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """أدمن: صفّري رصيد عميل بعد ما تدفعيله هديته."""
    if not _require_admin(update):
        return
    if not context.args:
        await update.message.reply_text("الاستخدام: /markpaid <آيدي العميل>")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("آيدي العميل لازم يكون رقم.")
        return
    old_balance = database.reset_gift_balance(target_id)
    await update.message.reply_text(f"✅ اتصفّر رصيد العميل {target_id} (كان {old_balance} جنيه).")
    try:
        await context.bot.send_message(
            chat_id=target_id,
            text=f"🎁 تم تسليم هديتك ({old_balance} جنيه)! تقدر تكمّل تجمع رصيد جديد من دلوقتي.",
        )
    except Exception:
        pass


def _inject_tag(url: str, tag: str) -> str:
    """بتحط تاج العميل في لينك أمازون (بتستبدل أي تاج موجود، أو تضيفه لو مفيش)."""
    from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query))
    query["tag"] = tag
    new_query = urlencode(query)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))


_AMAZON_LINK_RE = None


def _add_random_search_param(url: str) -> str:
    """
    بتحوّل لينك أمازون من الشكل:
    https://www.amazon.eg/dp/ASIN?ref=...&tag=...
    للشكل:
    https://www.amazon.eg/dp/ASIN/s?k=<رقم عشوائي>&ref=...&tag=...
    من غير ما تلمس التاج أو أي جزء تاني من اللينك الأصلي.
    """
    import random
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(url)
    new_path = parts.path.rstrip("/") + "/s"
    random_num = random.randint(1000, 9999)
    new_query = f"k={random_num}"
    if parts.query:
        new_query += f"&{parts.query}"
    return urlunsplit((parts.scheme, parts.netloc, new_path, new_query, parts.fragment))


async def handle_new_golden_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """بتستقبل أي بوست/رسالة جديدة من قناة أو جروب العروض الذهبية وتحفظها
    (تاج زي ما هو، من غير تخصيص). شغّالة مع الاتنين: قناة (channel_post)
    أو جروب عادي (message)."""
    post = update.channel_post or update.message
    if not post:
        return
    text = post.caption or post.text or ""
    links = _extract_amazon_links(text)
    if not links:
        return
    if len(links) > 1:
        logger.info("بوست ذهبي فيه أكتر من لينك (%s) - اتجاهل.", len(links))
        return
    photo_file_id = post.photo[-1].file_id if post.photo else None
    caption = text[:400] if text else "عرض ذهبي 🏆"
    database.add_golden_deal(post.message_id, photo_file_id, caption, links[0])
    logger.info("اتضاف عرض ذهبي جديد للكاش: %s", links[0])


async def goldenstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """أدمن: تشخيص سريع لملف منتجات العجلة الذهبية."""
    if not _require_admin(update):
        return
    try:
        products = product_catalog.load_products()
    except Exception as exc:
        await update.message.reply_text(f"❌ تعذر قراءة ملف المنتجات: {exc}")
        return
    positive_epc = [p for p in products if p.expected_revenue_per_click > 0]
    average_epc = (
        sum(p.expected_revenue_per_click for p in positive_epc) / len(positive_epc)
        if positive_epc else 0
    )
    await update.message.reply_text(
        f"🏆 منتجات العجلة المقبولة: {len(products)}\n"
        f"منتجات لها EPC: {len(positive_epc)}\n"
        f"متوسط EPC: {average_epc:.4f} جنيه\n"
        f"عدد الأسئلة في الجولة: {config.EGYPT_GOLDEN_QUESTIONS_PER_ROUND}\n"
        f"نسبة جائزة العميل النهائية من EPC: "
        f"{config.EGYPT_APPROVED_CLICK_RATE * config.EGYPT_REAL_CLICK_VALUE_RATE * config.EGYPT_CUSTOMER_REWARD_RATE:.3%}"
    )


async def golden_wheel_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """نقطة البداية: العميل بيدوس زرار/أمر عجلة بطاقات الهدايا - بتفتحله
    عجلة بصرية تلف وتقف الأول، وبعد ما تقف بيوصله سؤال العرض."""
    user = update.effective_user
    row = database.get_user(user.id)
    if not row or row["program"] != "egypt":
        if update.callback_query:
            await update.callback_query.answer()
        else:
            await update.message.reply_text("الميزة دي متاحة بس لعملاء برنامج مصر.")
        return
    if update.callback_query:
        await update.callback_query.answer()
    database.set_user_activity_now(user.id)
    chat_id = update.effective_chat.id
    await _offer_golden_round(context, chat_id, user.id)


def _build_lucky_wheel_url(spin_id: int, prize: float) -> str | None:
    """رابط الروليت وبداخله الجائزة الشخصية المحسومة من السيرفر."""
    if not config.WHEEL_URL:
        return None
    import time
    sep = "&" if "?" in config.WHEEL_URL else "?"
    return (
        f"{config.WHEEL_URL}{sep}t={int(time.time() * 1000)}"
        f"&mode=golden&spin_id={spin_id}&prize={_format_egp(prize)}&reveal=box"
    )


def _golden_button_row(user_id: int | None):
    """
    زرار "عجلة بطاقات الهدايا" بيفتح مباشرة:
    - لو عند العميل لفة مستنية استلام (لأي سبب اتقفلت أو اتفوّتت قبل كده)،
      الزرار يفتحله نفس اللفة دي تاني (نفس الهدية بالظبط، من غير ما
      يخسرها أو ياخد لفة زيادة).
    - لو مفيش لفة مستنية، الزرار يفتح عجلة اختيار عرض جديد على طول.
    """
    if user_id is not None:
        pending = database.get_pending_lucky_spin(user_id)
        if pending:
            url = _build_lucky_wheel_url(pending["id"], pending["prize"])
            if url:
                return [KeyboardButton("🎁 استلم هديتك المستنية", web_app=WebAppInfo(url=url))]

    if not config.WHEEL_URL:
        return [KeyboardButton(GOLDEN_BUTTON_TEXT)]
    import time
    sep = "&" if "?" in config.WHEEL_URL else "?"
    select_url = f"{config.WHEEL_URL}{sep}t={int(time.time() * 1000)}&mode=select"
    return [KeyboardButton(GOLDEN_BUTTON_TEXT, web_app=WebAppInfo(url=select_url))]


def _select_wheel_keyboard():
    """لوحة مفاتيح زرار عجلة الاختيار البصرية - بنعيد إرسالها في أي رسالة
    ممكن تسيب العميل من غيرها من غير طريقة يكمّل بيها."""
    if not config.WHEEL_URL:
        return None
    import time
    sep = "&" if "?" in config.WHEEL_URL else "?"
    select_url = f"{config.WHEEL_URL}{sep}t={int(time.time() * 1000)}&mode=select"
    return ReplyKeyboardMarkup(
        [[KeyboardButton("🎡 لف واختار من عروض النهاردة", web_app=WebAppInfo(url=select_url))]],
        resize_keyboard=True, one_time_keyboard=False,
    )


async def _offer_golden_round(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int):
    """بتعرض على العميل يلف عجلة الاختيار (البصرية) عشان تختارله عرض جديد،
    أو تفتحله عجلة بطاقات الهدايا على طول لو خلاص وصل لهدفه، أو تجيبله لفة
    مستنية استلام لو موجودة (زي لو كانت اتقفلت أو اتفوّتت قبل كده)."""
    pending = database.get_pending_lucky_spin(user_id)
    if pending:
        url = _build_lucky_wheel_url(pending["id"], pending["prize"])
        keyboard = None
        if url:
            keyboard = ReplyKeyboardMarkup(
                [[KeyboardButton("🎁 استلم هديتك المستنية", web_app=WebAppInfo(url=url))]],
                resize_keyboard=True, one_time_keyboard=False,
            )
        await context.bot.send_message(
            chat_id=chat_id,
            text="🎁 عندك هدية مستنية الاستلام من المرة اللي فاتت! دوس تحت واستلمها:",
            reply_markup=keyboard,
        )
        return

    row = database.get_user(user_id)
    if row["golden_answered_count"] >= row["golden_target"]:
        await _send_golden_question(context, chat_id, user_id)
        return

    select_keyboard = _select_wheel_keyboard()
    if not select_keyboard:
        await _send_golden_question(context, chat_id, user_id)
        return

    await context.bot.send_message(
        chat_id=chat_id,
        text="🏆 دوس تحت ولف العجلة تختارلك عرض النهاردة:",
        reply_markup=select_keyboard,
    )


def _extract_asin(url: str) -> str | None:
    import re
    match = re.search(r"/dp/([A-Z0-9]{10})", url, re.IGNORECASE)
    return match.group(1).upper() if match else None


async def _send_golden_question(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int):
    row = database.get_user(user_id)
    answered_count = row["golden_answered_count"] if row else 0
    correct_count = row["golden_opened_count"] if row else 0
    target = row["golden_target"] if row else config.EGYPT_GOLDEN_QUESTIONS_PER_ROUND

    if answered_count >= target:
        pending = database.get_pending_lucky_spin(user_id)
        if pending:
            spin_id, prize = pending["id"], pending["prize"]
        else:
            spin_id, prize, _ = database.create_lucky_spin(
                user_id, row["golden_round_earnings"]
            )
        url = _build_lucky_wheel_url(spin_id, prize)
        keyboard = None
        if url:
            keyboard = ReplyKeyboardMarkup(
                [[KeyboardButton("🎁 لف عجلة الهدايا وافتح صندوقك", web_app=WebAppInfo(url=url))]],
                resize_keyboard=True, one_time_keyboard=False,
            )
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"🎉 خلّصت {target} أسئلة، منهم {correct_count} صح.\n"
                "دوس عجلة بطاقات الهدايا، وبعد ما تقف افتح صندوق جائزتك:"
            ),
            reply_markup=keyboard,
        )
        return

    try:
        asked_asins = database.list_todays_quizzed_asins(user_id)
        product = product_catalog.choose_product(
            asked_asins, question_index=answered_count, user_id=user_id
        )
        question = product_catalog.question_for(product)
    except Exception as exc:
        logger.exception("تعذر تجهيز سؤال من ملف المنتجات: %s", exc)
        await context.bot.send_message(
            chat_id=chat_id,
            text="حصلت مشكلة مؤقتة في قراءة منتجات العجلة. جرّب تاني بعد شوية.",
        )
        return

    reward_value = product_catalog.customer_reward_for_epc(
        product.expected_revenue_per_click
    )
    question_id = database.create_golden_question(
        user_id=user_id,
        asin=product.asin,
        question_type=question["type"],
        correct_index=question["correct_index"],
        epc=product.expected_revenue_per_click,
        reward_value=reward_value,
    )
    database.log_quiz_asked(user_id, product.asin)
    product_link = product_catalog.build_affiliate_link(product.asin)

    caption = (
        f"🏆 سؤال {answered_count + 1} من {target} — الصح حتى الآن: {correct_count}\n"
        f"دوس على لينك المنتج تحت وشوف تفاصيله كويس قبل ما تجاوب 👇\n\n"
        f"{question['prompt']}"
    )
    buttons = [
        [InlineKeyboardButton("🔗 شوف المنتج على أمازون", url=product_link)]
    ]
    buttons += [
        [InlineKeyboardButton(opt, callback_data=f"goldenans:{question_id}:{index}")]
        for index, opt in enumerate(question["options"])
    ]
    keyboard = InlineKeyboardMarkup(buttons)

    try:
        await context.bot.send_message(chat_id=chat_id, text=caption, reply_markup=keyboard)
    except Exception as exc:
        logger.warning("فشل إرسال سؤال العرض الذهبي: %s", exc)


async def golden_answer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """العميل اختار إجابة على سؤال العرض الذهبي."""
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    try:
        _, question_id, chosen_index = query.data.split(":", 2)
        question_id = int(question_id)
        chosen_index = int(chosen_index)
    except (TypeError, ValueError):
        return
    database.set_user_activity_now(user_id)

    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    result = database.answer_golden_question(user_id, question_id, chosen_index)
    if not result:
        await context.bot.send_message(
            chat_id=chat_id,
            text="الإجابة دي اتسجلت قبل كده. هنكمّل من تقدمك الحالي.",
        )
        await _offer_golden_round(context, chat_id, user_id)
        return

    if result["correct"]:
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"✅ إجابة صح! ({result['answered_count']}/{result['target']})\n"
                f"الصح في الجولة: {result['correct_count']}"
            ),
        )
    else:
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "للأسف غلط! 😅\n"
                "🎡 العجلة زعلت شوية وضافتلك سؤالين كمان 😂\n\n"
                f"بقى عندك **{result['target']} أسئلة** بدل **{result['target'] - 2}** 🎯\n"
                f"وهدفك الأساسي لسه **{config.EGYPT_GOLDEN_QUESTIONS_PER_ROUND} إجابات صح**."
            ),
        )

    await _offer_golden_round(context, chat_id, user_id)


def _extract_amazon_links(text: str) -> list[str]:
    """بتدوّر على كل لينكات أمازون في نص البوست (amazon.eg / amzn.to / amazon.com)،
    وترجّعهم كلهم من غير تكرار وبنفس ترتيب ظهورهم."""
    global _AMAZON_LINK_RE
    if _AMAZON_LINK_RE is None:
        import re
        _AMAZON_LINK_RE = re.compile(
            r"https?://(?:www\.)?(?:amazon\.[a-z.]+|amzn\.to)/\S+", re.IGNORECASE
        )
    if not text:
        return []
    seen = []
    for match in _AMAZON_LINK_RE.finditer(text):
        link = match.group(0).rstrip(").,،")
        if link not in seen:
            seen.append(link)
    return seen


async def handle_new_deal_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """بتستقبل أي بوست جديد من قناة عروض أمازون بتاعتك وتحفظه في الكاش، وتنبّه العملاء."""
    post = update.channel_post
    if not post:
        return
    text = post.caption or post.text or ""
    links = _extract_amazon_links(text)
    if not links:
        return  # مفيش لينك أمازون في البوست ده، تجاهليه
    if len(links) > 1:
        logger.info("البوست فيه أكتر من لينك أمازون (%s) - اتجاهل.", len(links))
        return  # بوست فيه أكتر من منتج - نتجاهله بدل ما نبعته ناقص أو غلط
    link = links[0]

    photo_file_id = post.photo[-1].file_id if post.photo else None
    caption = (text[:400]) if text else "عرض أمازون 🔥"
    database.add_deal(post.message_id, photo_file_id, caption, [link])
    logger.info("اتضاف عرض جديد للكاش من القناة: %s", link)

    # منبعتش تنبيه مع كل عرض عرض - بنستنى لحد ما يتجمّع دفعة كاملة (10 افتراضيًا)
    pending = database.get_pending_notify_count()
    if pending < config.DEALS_NOTIFY_BATCH_SIZE:
        return

    users = database.list_active_egypt_users()
    for u in users:
        try:
            await context.bot.send_message(
                chat_id=u["user_id"],
                text="🔥 عروض جديدة النهاردة! دوس /offers عشان تشوفها.",
            )
        except Exception:
            pass
    database.mark_notified_up_to_latest()


async def offers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """بيوري للعميل العروض اللي لسه ما شافهاش."""
    user = update.effective_user
    await _send_offers_flow(context, user.id, user.username)


async def show_offers_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """لما العميل يدوس زرار "اعرض آخر عروض أمازون" جوه رسالة الترحيب."""
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    await _send_offers_flow(context, user.id, user.username)


async def start_shopping_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    لما عميل "بيتابع بس" يدوس زرار "ابدأ الشرا دلوقتي":
    1) بيتحول لعميل "بيشتري بانتظام" (وياخد تاج شخصي لو فيه متاح، أو يتحط
       في الطابور بأولوية لو التاجات خلصت)
    2) بيتفتحله صفحة عروض أمازون على طول بتاجه (أو بتاج عام احتياطي لو
       لسه في الطابور)
    """
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    database.set_buyer_type(user_id, "regular")
    tag = database.get_available_tag("egypt")

    if tag:
        database.assign_tag_to_user(user_id, tag["id"])
        tag_str = tag["keyword"]
        await context.bot.send_message(
            chat_id=chat_id,
            text="🎉 تم تفعيل حسابك! ده لينكك الخاص هيتسجّل بيه أي شرا تعمليه من دلوقتي.",
            reply_markup=_egypt_customer_keyboard(user_id),
        )
    else:
        database.add_to_queue(user_id)
        tag_str = config.EGYPT_GENERAL_TAG
        await context.bot.send_message(
            chat_id=chat_id,
            text="لسه التاجات مشغولة، هتاخد لينكك الخاص أول ما واحد يتوفر (وليك أولوية بمجرد ما تشتري).",
        )

    deals_url = "https://www.amazon.eg/deals?ref_=nav_cs_gb"
    if tag_str:
        deals_url = _inject_tag(deals_url, tag_str)

    await context.bot.send_message(
        chat_id=chat_id,
        text="دوس هنا وشوف كل عروض أمازون دلوقتي 🛍️",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🛒 روح صفحة العروض", url=deals_url)]]
        ),
    )


async def _send_offers_flow(context: ContextTypes.DEFAULT_TYPE, user_id: int, username: str | None):
    """المنطق الأساسي لإرسال العروض الجديدة للعميل. بيتنادى من /offers أو من الزرار.
    لو العميل معاه تاج شخصي، اللينك بتاجه هو. لو لأ (لسه بيتابع بس)، اللينك
    بيتبعت زي ما هو من غير أي تخصيص (بتاج القناة العام)."""
    row = database.get_user(user_id)
    if not row or row["program"] != "egypt":
        await context.bot.send_message(
            chat_id=user_id,
            text="الأمر ده متاح بس لعملاء برنامج مصر.",
        )
        return
    database.set_user_activity_now(user_id)

    tag = database.get_user_tag_keyword(user_id)  # None لو معندوش تاج شخصي
    deals = database.list_new_deals_for_user(user_id)
    if not deals:
        await context.bot.send_message(
            chat_id=user_id,
            text="مفيش عروض جديدة من آخر مرة شفت فيها 🙏 تابعينا وهتوصلك أول ما تنزل 🛍️",
        )
        return

    # فيه عروض جديدة فعلاً - دلوقتي نمسح رسايل العروض القديمة قبل ما نبعت الجديدة
    old_message_ids = database.pop_sent_offer_messages(user_id)
    for msg_id in old_message_ids:
        try:
            await context.bot.delete_message(chat_id=user_id, message_id=msg_id)
        except Exception:
            pass

    intro = await context.bot.send_message(chat_id=user_id, text=f"🔥 {len(deals)} عرض جديد من آخر مرة:")
    database.record_sent_offer_message(user_id, intro.message_id)

    for deal in deals:
        original_links = database.get_deal_links(deal["base_link"])
        if not original_links:
            continue

        # لو معاه تاج شخصي، خصّصي كل لينك بيه. لو لأ، سيبي اللينكات زي ما هي
        # (بتاج القناة العام اللي كان مكتوب في البوست الأصلي)
        display_caption = deal["caption"]
        buttons = []
        for i, original_link in enumerate(original_links, start=1):
            personal_link = _inject_tag(original_link, tag) if tag else original_link
            display_caption = display_caption.replace(original_link, personal_link)
            label = "🛒 اشتري من هنا" if len(original_links) == 1 else f"🛒 المنتج {i}"
            buttons.append([InlineKeyboardButton(label, url=personal_link)])
        keyboard = InlineKeyboardMarkup(buttons)

        try:
            if deal["photo_file_id"]:
                sent = await context.bot.send_photo(
                    chat_id=user_id,
                    photo=deal["photo_file_id"],
                    caption=display_caption,
                    reply_markup=keyboard,
                )
            else:
                sent = await context.bot.send_message(
                    chat_id=user_id,
                    text=display_caption,
                    reply_markup=keyboard,
                )
            database.record_sent_offer_message(user_id, sent.message_id)
        except Exception as exc:
            logger.warning("فشل إرسال عرض للعميل %s: %s", user_id, exc)

    database.mark_deals_seen(user_id)


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    database.release_tag_by_user(user.id)
    database.deactivate_user(user.id)
    await update.message.reply_text(
        "تم إلغاء تفعيل حسابك. دوس على الزرار تحت لما تحب ترجع.",
        reply_markup=RESTART_KEYBOARD,
    )


# ---------------- أوامر الأدمن ----------------

def _require_admin(update: Update) -> bool:
    return database.is_admin(update.effective_user.id)


def _parse_program_arg(args: list[str]) -> tuple[str, list[str]]:
    """البوت الحالي مصر فقط؛ أي تاج جديد يتسجل على برنامج egypt."""
    if args and args[0].lower() == "egypt":
        return "egypt", args[1:]
    return "egypt", args

    if args and args[0].lower() in ("ksa", "egypt"):
        return args[0].lower(), args[1:]
    return "ksa", args


async def _drain_queue(context: ContextTypes.DEFAULT_TYPE, program: str) -> int:
    """
    بعد ما تضيفي تاج/تاجات جديدة، الدالة دي بتوزّعهم على طول على أول
    ناس في الطابور (لو فيه)، بنفس ترتيب الأولوية المعتاد. بترجع عدد
    العملاء اللي اتخصص لهم تاج.
    """
    assigned = 0
    while True:
        tag = database.get_available_tag(program)
        if not tag:
            break
        next_user_id = database.pop_next_in_queue(program)
        if not next_user_id:
            break
        database.assign_tag_to_user(next_user_id, tag["id"])
        assigned += 1
        try:
            if program == "ksa":
                await context.bot.send_message(
                    chat_id=next_user_id,
                    text=(
                        "دورك جه! 🎉 دوس على الزرار تحت وسجّل اشتراكك في أمازون برايم.\n"
                        "⚠️ الزرار ده مخصص لك وحدك — متبعتوش لحد تاني."
                    ),
                    reply_markup=_prime_keyboard(tag["keyword"]),
                )
            else:
                await context.bot.send_message(
                    chat_id=next_user_id,
                    text="🎉 دورك جه! ده لينكك الخاص جاهز. استخدمه في كل مرة تشتري فيها من أمازون مصر.",
                    reply_markup=_egypt_customer_keyboard(next_user_id),
                )
        except Exception:
            pass
    return assigned


async def addtag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _require_admin(update):
        return
    if not context.args:
        await update.message.reply_text("الاستخدام: /addtag [ksa|egypt] nonzksa-21-t01\n(لو ماكتبتيش البرنامج، هيتحسب ksa تلقائيًا)")
        return
    program, rest = _parse_program_arg(context.args)
    if not rest:
        await update.message.reply_text("لازم تكتبي التاج بعد اسم البرنامج.")
        return
    keyword = rest[0].strip()
    ok = database.add_tag(keyword, program)
    if not ok:
        await update.message.reply_text(f"التاج {keyword} موجود بالفعل.")
        return
    assigned = await _drain_queue(context, program)
    msg = f"تمت إضافة التاج ({program}): {keyword}"
    if assigned:
        msg += f"\n✅ اتوزّع تلقائي على {assigned} عميل كانوا في الطابور."
    await update.message.reply_text(msg)


async def addtags(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """إضافة كذا تاج مرة واحدة - أول سطر يحدد البرنامج (ksa/egypt) اختياري، باقي السطور تاجات."""
    if not _require_admin(update):
        return
    text = update.message.text or ""
    lines = [ln.strip() for ln in text.split("\n")[1:] if ln.strip()]
    if not lines:
        await update.message.reply_text(
            "الاستخدام: اكتبي /addtags وتحتها (اختياري) ksa أو egypt في أول سطر، وبعدها كل تاج في سطر لوحده"
        )
        return
    program = "egypt"
    if lines and lines[0].lower() == "egypt":
        lines = lines[1:]
    added = sum(1 for kw in lines if database.add_tag(kw, program))
    assigned = await _drain_queue(context, program)
    msg = f"اتضاف {added} من أصل {len(lines)} تاج للبرنامج ({program})."
    if assigned:
        msg += f"\n✅ اتوزّع تلقائي على {assigned} عميل كانوا في الطابور."
    await update.message.reply_text(msg)


async def removetag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _require_admin(update):
        return
    if not context.args:
        await update.message.reply_text("الاستخدام: /removetag nonzksa-21-t01")
        return
    keyword = context.args[-1].strip()
    database.remove_tag(keyword)
    await update.message.reply_text(f"تم حذف التاج: {keyword}")


async def listtags(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _require_admin(update):
        return
    program = context.args[0].lower() if context.args and context.args[0].lower() in ("ksa", "egypt") else None
    tags = database.list_tags(program)
    if not tags:
        await update.message.reply_text("مفيش تاجات مسجلة. ضيفي بـ /addtag أو /addtags")
        return
    lines = [
        f"{'🟢 متاح' if t['in_pool'] else '🔴 مشغول'} — [{t['program']}] {t['keyword']}"
        for t in tags
    ]
    await update.message.reply_text("\n".join(lines[:80]))


async def verify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    برنامج السعودية بس: لما تشوفي عمولة/تحويل نزلتلك على تاج معين في تقرير أمازون،
    ابعتي /verify <keyword> - هيفعّل صاحب التاج ده، ويرجع التاج لأول واحد
    مستني في الطابور تلقائيًا.
    """
    if not _require_admin(update):
        return
    if not context.args:
        await update.message.reply_text("الاستخدام: /verify nonzksa-21-t01")
        return
    keyword = context.args[0].strip()

    holder = database.find_user_holding_tag(keyword)
    if not holder:
        await update.message.reply_text(f"مفيش حد ماسك التاج {keyword} دلوقتي.")
        return

    database.mark_verified(holder["user_id"])
    database.release_tag_by_user(holder["user_id"])
    username_line = f"@{holder['username']}" if holder["username"] else "(مفيش يوزرنيم عام)"
    await update.message.reply_text(
        f"✅ اتفعّل صاحب التاج {keyword}\n"
        f"آيدي تليجرام: {holder['user_id']}\n"
        f"اليوزرنيم: {username_line}"
    )

    try:
        await context.bot.send_message(
            chat_id=holder["user_id"],
            text="🎉 مبروك! اتأكد اشتراكك في أمازون برايم وحسابك دلوقتي مفعّل بالكامل.\nهتدخلي سحوبات الهدايا القادمة 🎁",
        )
        keyboard = _wheel_keyboard()
        if keyboard:
            await context.bot.send_message(
                chat_id=holder["user_id"],
                text="وكمان، هدية فورية! دوس تحت ولف عجلة الحظ 🎡",
                reply_markup=keyboard,
            )
    except Exception:
        pass

    next_user_id = database.pop_next_in_queue("ksa")
    if next_user_id:
        tag = database.get_available_tag("ksa")
        if tag:
            database.assign_tag_to_user(next_user_id, tag["id"])
            try:
                await context.bot.send_message(
                    chat_id=next_user_id,
                    text=(
                        "دورك جه! 🎉 دوس على الزرار تحت وسجّل اشتراكك في أمازون برايم.\n"
                        "⚠️ الزرار ده مخصص ليكي وحدك — متبعتيهوش لحد تاني."
                    ),
                    reply_markup=_prime_keyboard(tag["keyword"]),
                )
            except Exception:
                pass


async def verified(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _require_admin(update):
        return
    users = database.list_verified_users()
    if not users:
        await update.message.reply_text("مفيش حد اتفعّل لسه.")
        return
    lines = [f"• {u['user_id']} (@{u['username']})" if u['username'] else f"• {u['user_id']}" for u in users]
    await update.message.reply_text(f"العدد: {len(users)}\n\n" + "\n".join(lines[:50]))


async def wheelwinners(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _require_admin(update):
        return
    users = database.list_wheel_winners()
    spins = database.list_spin_history()
    if not users and not spins:
        await update.message.reply_text("مفيش حد لف العجلة لسه.")
        return
    lines = []
    for u in users:
        name = f"@{u['username']}" if u["username"] else str(u["user_id"])
        lines.append(f"🇸🇦 • {name} — {u['wheel_prize']}")
    for s in spins[-50:]:
        tag = "✅" if s["collected"] else "❌ (لم يُستلم - رصيد غير كافٍ)"
        lines.append(f"🇪🇬 • {s['user_id']} — {s['prize']} {tag} ({s['spun_at'][:10]})")
    await update.message.reply_text(f"العدد: {len(lines)}\n\n" + "\n".join(lines[:80]))


async def notifywheel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يبعت زرار العجلة لكل عملاء السعودية المفعّلين اللي لسه ملفّوش."""
    if not _require_admin(update):
        return
    keyboard = _wheel_keyboard()
    if not keyboard:
        await update.message.reply_text("رابط العجلة (WHEEL_URL) مش متظبط في .env لسه.")
        return
    users = database.list_verified_users()
    sent = 0
    skipped = 0
    for u in users:
        if database.get_wheel_prize(u["user_id"]):
            skipped += 1
            continue
        try:
            await context.bot.send_message(
                chat_id=u["user_id"],
                text="🎡 هدية مننا لك! دوس تحت ولف عجلة الحظ.",
                reply_markup=keyboard,
            )
            sent += 1
        except Exception:
            continue
    await update.message.reply_text(
        f"✅ اترسلت لـ {sent} عميل مفعّل.\n(اترفض/اتخطى {skipped} كانوا لفّوا العجلة بالفعل)"
    )


async def reclaimstale(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    برنامج مصر بس: تسحب فورًا أي تاج معندوش نشاط من قد
    EGYPT_TAG_INACTIVITY_HOURS، وتحوّل صاحبه لمسار المتابعة العادي
    (لينك القناة)، والتاج يروح لحد تاني في الطابور.
    """
    if not _require_admin(update):
        return
    stale = database.find_stale_tag_holders(config.EGYPT_TAG_INACTIVITY_HOURS)
    if not stale:
        await update.message.reply_text("مفيش تاجات ساكنة دلوقتي 👍")
        return

    reclaimed = 0
    for row in stale:
        user_id = row["user_id"]
        database.downgrade_to_browser(user_id)
        reclaimed += 1
        try:
            await context.bot.send_message(
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
                    await context.bot.send_message(
                        chat_id=next_user_id,
                        text="دورك جه! 🎉 ده لينكك الخاص. استخدمه في كل مرة تشتري من أمازون مصر.",
                        reply_markup=_egypt_customer_keyboard(next_user_id),
                    )
                except Exception:
                    pass

    await update.message.reply_text(f"✅ اترد {reclaimed} تاج من عملاء ساكنين.")


async def removeuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يمسح عميل بالكامل من قاعدة بيانات البوت (من القايمة والمفعّلين وأي حاجة تانية)."""
    if not _require_admin(update):
        return
    if not context.args:
        await update.message.reply_text(
            "الاستخدام: /removeuser <آيدي العميل>\nخدي الآيدي من /verified أو /stats"
        )
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("آيدي العميل لازم يكون رقم.")
        return
    removed = database.delete_user(target_id)
    if removed:
        await update.message.reply_text(f"✅ اتمسح العميل {target_id} خالص من القايمة.")
    else:
        await update.message.reply_text(f"مفيش عميل بالآيدي {target_id} أصلاً.")


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _require_admin(update):
        return
    users = database.list_all_users()
    ksa_users = [u for u in users if u["program"] == "ksa"]
    egypt_users = [u for u in users if u["program"] == "egypt"]

    ksa_verified = sum(1 for u in ksa_users if u["verified"])
    ksa_holding = sum(1 for u in ksa_users if u["tag_id"])
    egypt_holding = sum(1 for u in egypt_users if u["tag_id"])
    egypt_regular = sum(1 for u in egypt_users if u["buyer_type"] == "regular")
    egypt_browsers = sum(1 for u in egypt_users if u["buyer_type"] == "browser")
    egypt_undecided = sum(1 for u in egypt_users if not u["buyer_type"])
    egypt_points_total = sum(u["gift_balance"] for u in egypt_users)

    egypt_tags = database.list_tags("egypt")
    egypt_tags_total = len(egypt_tags)
    egypt_tags_used = sum(1 for t in egypt_tags if not t["in_pool"])
    egypt_tags_free = egypt_tags_total - egypt_tags_used

    await update.message.reply_text(
        f"👥 إجمالي كل العملاء: {len(users)}\n\n"
        f"🇸🇦 السعودية:\n"
        f"  المسجّلين: {len(ksa_users)} | مفعّلين: {ksa_verified} | ماسكين لينك: {ksa_holding} | طابور: {database.queue_length('ksa')}\n\n"
        f"🇪🇬 مصر:\n"
        f"  قالوا هيشتروا بانتظام: {egypt_regular} | قالوا هيتابعوا بس: {egypt_browsers} | لسه ما اختاروش: {egypt_undecided}\n"
        f"  ماسكين تاج شخصي دلوقتي: {egypt_holding} | طابور: {database.queue_length('egypt')}\n"
        f"  التاجات: {egypt_tags_used} مستخدم / {egypt_tags_free} متاح / {egypt_tags_total} إجمالي\n"
        f"  إجمالي رصيد الجوايز المستنية التسليم: {egypt_points_total} جنيه"
    )


async def listusers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يوريكي كل عميل اتسجّل في البوت وحالته بالظبط."""
    if not _require_admin(update):
        return
    users = database.list_all_users()
    if not users:
        await update.message.reply_text("مفيش عملاء مسجّلين خالص.")
        return
    lines = []
    for u in users:
        name = f"@{u['username']}" if u["username"] else "(بدون يوزرنيم)"
        flag = "🇸🇦" if u["program"] == "ksa" else ("🇪🇬" if u["program"] == "egypt" else "❔")
        if not u["is_active"]:
            status = "❌ ملغي اشتراكه"
        elif u["program"] == "ksa" and u["verified"]:
            status = "🎉 مفعّل"
        elif u["program"] == "egypt" and u["tag_id"]:
            status = f"🔗 عجلة ذهبية: {u['golden_opened_count']}/{u['golden_target']} | جوايز: {u['gift_balance']} ج"
        elif u["tag_id"]:
            status = "🔗 ماسك لينك"
        elif u["queued_at"]:
            status = "⏳ في الطابور"
        else:
            status = "—"
        lines.append(f"{flag} {u['user_id']} {name} — {status}")
    await update.message.reply_text(f"العدد: {len(users)}\n\n" + "\n".join(lines[:50]))


async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """الاستخدام: /broadcast [ksa|egypt] رسالتك هنا (لو ماحددتيش برنامج، تتبعت لكل العملاء)"""
    if not _require_admin(update):
        return
    if not context.args:
        await update.message.reply_text("الاستخدام: /broadcast [ksa|egypt] رسالتك هنا")
        return
    program, rest = _parse_program_arg(context.args) if context.args[0].lower() in ("ksa", "egypt") else (None, context.args)
    if not rest:
        await update.message.reply_text("لازم تكتبي نص الرسالة.")
        return
    message = " ".join(rest)
    users = database.list_all_users()
    sent = 0
    for u in users:
        if not u["is_active"]:
            continue
        if program and u["program"] != program:
            continue
        try:
            await context.bot.send_message(chat_id=u["user_id"], text=message)
            sent += 1
        except Exception:
            continue
    await update.message.reply_text(f"اترسلت الرسالة لـ {sent} مستخدم.")


async def msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """بعت رسالة لعميل واحد بعينه بآيدي حسابه على تليجرام (شوفيه من /verified أو /listusers)."""
    if not _require_admin(update):
        return
    if len(context.args) < 2:
        await update.message.reply_text(
            "الاستخدام: /msg <آيدي العميل> رسالتك هنا\n"
            "مثال: /msg 8747836095 مبروك! تواصلي معانا لتسليم هديتك 🎁"
        )
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("آيدي العميل لازم يكون رقم. خديه من /verified أو /listusers.")
        return
    text = " ".join(context.args[1:])
    try:
        await context.bot.send_message(chat_id=target_id, text=text)
        await update.message.reply_text("✅ اترسلت الرسالة.")
    except Exception as exc:
        await update.message.reply_text(f"❌ مقدرتش أبعت الرسالة: {exc}")
