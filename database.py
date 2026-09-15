"""
طبقة قاعدة البيانات (SQLite) - بتدعم برنامجين تحت نفس البوت:
- ksa: تفعيل اشتراكات أمازون برايم (تفعيل مرة واحدة)
- egypt: نظام نقط شرا متراكم شهري (Amazon Points)

- users: كل عميل ليه آيدي تليجرام ثابت وبرنامج واحد (ksa/egypt)
- tags: تاجات التتبع، كل تاج مربوط ببرنامج معيّن
- admins: قائمة الأدمنز
"""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    joined_at TEXT NOT NULL,
    is_active INTEGER DEFAULT 1,
    program TEXT,
    tag_id INTEGER,
    tag_assigned_at TEXT,
    last_activity_at TEXT,
    last_ip TEXT,
    ip_capture_needed INTEGER DEFAULT 1,
    verified INTEGER DEFAULT 0,
    verified_at TEXT,
    queued_at TEXT,
    reminder_sent INTEGER DEFAULT 0,
    wheel_prize TEXT,
    wheel_won_at TEXT,
    points_balance INTEGER DEFAULT 0,
    spins_balance INTEGER DEFAULT 0,
    gift_balance REAL DEFAULT 0,
    golden_opened_count INTEGER DEFAULT 0,
    golden_answered_count INTEGER DEFAULT 0,
    golden_round_earnings REAL DEFAULT 0,
    golden_target INTEGER DEFAULT 5,
    first_round_bonus_used INTEGER DEFAULT 0,
    pending_offer_from_id INTEGER,
    pending_offer_to_id INTEGER,
    FOREIGN KEY (tag_id) REFERENCES tags(id)
);

CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword TEXT NOT NULL UNIQUE,
    category TEXT,
    program TEXT DEFAULT 'egypt',
    in_pool INTEGER DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS admins (
    user_id INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS wheel_spins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    prize TEXT NOT NULL,
    collected INTEGER DEFAULT 1,
    spun_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deals_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER,
    photo_file_id TEXT,
    caption TEXT,
    base_link TEXT NOT NULL,
    posted_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sent_offer_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS notify_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_notified_deal_id INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS golden_deals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER,
    photo_file_id TEXT,
    caption TEXT,
    base_link TEXT NOT NULL,
    posted_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS golden_quiz_log (
    user_id INTEGER NOT NULL,
    asin TEXT NOT NULL,
    quiz_date TEXT NOT NULL,
    PRIMARY KEY (user_id, asin, quiz_date)
);

CREATE TABLE IF NOT EXISTS golden_questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    asin TEXT NOT NULL,
    question_type TEXT NOT NULL,
    correct_index INTEGER NOT NULL,
    epc REAL NOT NULL,
    reward_value REAL NOT NULL,
    prompt TEXT,
    options_json TEXT,
    product_link TEXT,
    answered INTEGER DEFAULT 0,
    was_correct INTEGER,
    created_at TEXT NOT NULL,
    answered_at TEXT
);

CREATE TABLE IF NOT EXISTS lucky_spins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    prize REAL NOT NULL,
    prize_index INTEGER DEFAULT 0,
    status TEXT DEFAULT 'pending',
    created_at TEXT NOT NULL,
    claimed_at TEXT
);

CREATE TABLE IF NOT EXISTS lucky_wheel_pool (
    prize_index INTEGER PRIMARY KEY,
    prize REAL NOT NULL,
    remaining INTEGER NOT NULL,
    total INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS redemption_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    amount REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    requested_at TEXT NOT NULL,
    paid_at TEXT,
    paid_by INTEGER,
    gift_code TEXT,
    code_sent_at TEXT,
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);

CREATE TABLE IF NOT EXISTS admin_reward_resets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    old_gift_balance REAL DEFAULT 0,
    old_points_balance INTEGER DEFAULT 0,
    old_spins_balance INTEGER DEFAULT 0,
    cancelled_lucky_spins INTEGER DEFAULT 0,
    cancelled_redemptions INTEGER DEFAULT 0,
    admin_id INTEGER,
    customer_message TEXT,
    created_at TEXT NOT NULL
);

"""

_MIGRATIONS = [
    "ALTER TABLE users ADD COLUMN reminder_sent INTEGER DEFAULT 0",
    "ALTER TABLE users ADD COLUMN wheel_prize TEXT",
    "ALTER TABLE users ADD COLUMN wheel_won_at TEXT",
    "ALTER TABLE users ADD COLUMN program TEXT",
    "ALTER TABLE users ADD COLUMN points_balance INTEGER DEFAULT 0",
    "ALTER TABLE users ADD COLUMN spins_balance INTEGER DEFAULT 0",
    "ALTER TABLE tags ADD COLUMN program TEXT DEFAULT 'egypt'",
    "ALTER TABLE wheel_spins ADD COLUMN collected INTEGER DEFAULT 1",
    "ALTER TABLE users ADD COLUMN gift_balance INTEGER DEFAULT 0",
    "ALTER TABLE users ADD COLUMN last_seen_deal_id INTEGER DEFAULT 0",
    "ALTER TABLE users ADD COLUMN buyer_type TEXT",
    "ALTER TABLE users ADD COLUMN last_points_added_at TEXT",
    "ALTER TABLE users ADD COLUMN has_purchased_before INTEGER DEFAULT 0",
    "ALTER TABLE users ADD COLUMN golden_opened_count INTEGER DEFAULT 0",
    "ALTER TABLE users ADD COLUMN golden_last_deal_id INTEGER DEFAULT 0",
    "ALTER TABLE users ADD COLUMN golden_target INTEGER DEFAULT 5",
    "ALTER TABLE users ADD COLUMN golden_answered_count INTEGER DEFAULT 0",
    "ALTER TABLE users ADD COLUMN golden_round_earnings REAL DEFAULT 0",
    "ALTER TABLE users ADD COLUMN pending_offer_from_id INTEGER",
    "ALTER TABLE users ADD COLUMN pending_offer_to_id INTEGER",
    "ALTER TABLE users ADD COLUMN last_ip TEXT",
    "ALTER TABLE users ADD COLUMN ip_capture_needed INTEGER DEFAULT 1",
    "ALTER TABLE lucky_spins ADD COLUMN prize_index INTEGER DEFAULT 0",
    "ALTER TABLE redemption_requests ADD COLUMN gift_code TEXT",
    "ALTER TABLE redemption_requests ADD COLUMN code_sent_at TEXT",
    "ALTER TABLE golden_questions ADD COLUMN prompt TEXT",
    "ALTER TABLE golden_questions ADD COLUMN options_json TEXT",
    "ALTER TABLE golden_questions ADD COLUMN product_link TEXT",
]
@contextmanager
def get_conn():
    """اتصال SQLite مضبوط لـ Railway وضغط أعلى مع انتظار بدل أخطاء database is locked."""
    timeout_seconds = max(config.DATABASE_BUSY_TIMEOUT_MS / 1000.0, 1.0)
    conn = sqlite3.connect(config.DATABASE_PATH, timeout=timeout_seconds)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {config.DATABASE_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute(f"PRAGMA cache_size = {-max(config.DATABASE_CACHE_MB, 1) * 1024}")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        # WAL مناسب لخدمة Railway واحدة مع قراءات/كتابات متزامنة أكتر.
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA wal_autocheckpoint = 1000")
        conn.execute("PRAGMA mmap_size = 268435456")
        # first_round_bonus_used is intentionally migrated outside _MIGRATIONS.
        # Existing customers must NOT receive the new-customer welcome round; only
        # accounts created after this deployment start with the default value 0.
        existing_user_columns = {r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
        conn.executescript(SCHEMA)
        if existing_user_columns and "first_round_bonus_used" not in existing_user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN first_round_bonus_used INTEGER DEFAULT 0")
            conn.execute("UPDATE users SET first_round_bonus_used = 1")
        for stmt in _MIGRATIONS:
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError:
                pass  # العمود موجود بالفعل
        # أي مستخدمين أو تاجات قديمة من قبل دعم البرامج، اعتبريها ksa تلقائيًا
        conn.execute("UPDATE users SET program = 'egypt' WHERE program IS NULL")
        # تصحيح بيانات قديمة: أي حد ماسك تاج بالفعل بس معندوش تصنيف
        # (buyer_type) - لازم يبقى "بيشتري بانتظام" أصلاً عشان أخد التاج
        conn.execute(
            """UPDATE users SET buyer_type = 'regular'
               WHERE buyer_type IS NULL AND tag_id IS NOT NULL AND program = 'egypt'"""
        )
        # الهدف الافتراضي اتغيّر من 10 لـ5 - نظبّط أي عميل لسه ما بدأش
        # (عشان مايفضلش عالق على الرقم القديم)
        conn.execute(
            f"UPDATE users SET golden_target = {GOLDEN_TARGET_COUNT} WHERE golden_answered_count = 0"
        )
        conn.execute("UPDATE tags SET program = 'egypt' WHERE program IS NULL")

        for admin_id in config.ADMIN_IDS:
            conn.execute(
                "INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (admin_id,)
            )
        conn.execute(
            "INSERT OR IGNORE INTO notify_state (id, last_notified_deal_id) VALUES (1, 0)"
        )
        row = conn.execute("SELECT COUNT(*) AS c FROM lucky_wheel_pool").fetchone()
        if row["c"] == 0:
            for idx, (prize, weight) in enumerate(zip(LUCKY_WHEEL_PRIZES, LUCKY_WHEEL_WEIGHTS)):
                count = int(LUCKY_WHEEL_POOL_SIZE * weight / 100)
                conn.execute(
                    """INSERT OR IGNORE INTO lucky_wheel_pool (prize_index, prize, remaining, total)
                       VALUES (?, ?, ?, ?)""",
                    (idx, prize, count, count),
                )
        else:
            # لو حجم المخزون المتفق عليه اتغيّر (زي 5000 -> 10000)، رجّعي
            # المخزون كله للحجم الجديد من الأول (بداية دورة جديدة تمامًا)
            existing_total = conn.execute(
                "SELECT SUM(total) AS s FROM lucky_wheel_pool"
            ).fetchone()["s"] or 0
            if existing_total != LUCKY_WHEEL_POOL_SIZE:
                for idx, (prize, weight) in enumerate(zip(LUCKY_WHEEL_PRIZES, LUCKY_WHEEL_WEIGHTS)):
                    count = int(LUCKY_WHEEL_POOL_SIZE * weight / 100)
                    conn.execute(
                        """UPDATE lucky_wheel_pool SET prize = ?, remaining = ?, total = ?
                           WHERE prize_index = ?""",
                        (prize, count, count, idx),
                    )
        row = conn.execute("SELECT COUNT(*) AS c FROM tags").fetchone()
        if row["c"] == 0:
            now = datetime.utcnow().isoformat()
            for kw in config.INITIAL_TAG_POOL:
                conn.execute(
                    "INSERT OR IGNORE INTO tags (keyword, program, in_pool, created_at) VALUES (?, 'egypt', 1, ?)",
                    (kw, now),
                )

        # Indexes لتسريع أهم الاستعلامات مع عدد مستخدمين أكبر.
        conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_users_program_active
            ON users(program, is_active);
        CREATE INDEX IF NOT EXISTS idx_users_tag_id
            ON users(tag_id);
        CREATE INDEX IF NOT EXISTS idx_users_queue
            ON users(program, queued_at);
        CREATE INDEX IF NOT EXISTS idx_users_last_activity
            ON users(last_activity_at);
        CREATE INDEX IF NOT EXISTS idx_tags_pool
            ON tags(program, in_pool);
        CREATE INDEX IF NOT EXISTS idx_wheel_spins_user
            ON wheel_spins(user_id);
        CREATE INDEX IF NOT EXISTS idx_lucky_spins_user_status
            ON lucky_spins(user_id, status);
        CREATE INDEX IF NOT EXISTS idx_golden_questions_user_answered
            ON golden_questions(user_id, answered);
        CREATE INDEX IF NOT EXISTS idx_sent_offer_messages_user
            ON sent_offer_messages(user_id);
        CREATE INDEX IF NOT EXISTS idx_deals_cache_message_id
            ON deals_cache(message_id);
        CREATE INDEX IF NOT EXISTS idx_golden_deals_posted
            ON golden_deals(posted_at);
        CREATE INDEX IF NOT EXISTS idx_redemptions_status_requested
            ON redemption_requests(status, requested_at);
        CREATE INDEX IF NOT EXISTS idx_redemptions_user_status
            ON redemption_requests(user_id, status);
        """)
        conn.execute("PRAGMA optimize")

    # جداول حساب وفر كاش المستقل (Web App / TikTok / Snapchat / إلخ)
    init_web_accounts_schema()
    init_web_offers_schema()


# ---------- المستخدمين ----------

def upsert_user(user_id: int, username: str | None):
    with get_conn() as conn:
        now = datetime.utcnow().isoformat()
        existing = conn.execute(
            "SELECT user_id FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE users SET username = ?, is_active = 1 WHERE user_id = ?",
                (username, user_id),
            )
        else:
            conn.execute(
                """INSERT INTO users (user_id, username, joined_at, is_active, golden_target)
                   VALUES (?, ?, ?, 1, ?)""",
                (user_id, username, now, GOLDEN_TARGET_COUNT),
            )


def get_user(user_id: int):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()


def set_user_program(user_id: int, program: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET program = ? WHERE user_id = ?", (program, user_id)
        )


def set_buyer_type(user_id: int, buyer_type: str):
    """buyer_type: 'regular' (بيشتري بانتظام) أو 'browser' (لسه بس بيتابع)."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET buyer_type = ? WHERE user_id = ?", (buyer_type, user_id)
        )


def get_user_tag_keyword(user_id: int):
    with get_conn() as conn:
        row = conn.execute(
            """SELECT t.keyword FROM users u JOIN tags t ON u.tag_id = t.id
               WHERE u.user_id = ?""",
            (user_id,),
        ).fetchone()
        return row["keyword"] if row else None


def set_user_activity_now(user_id: int):
    """Mark activity. A return after >5 minutes offline starts a new IP-capture session."""
    with get_conn() as conn:
        now = datetime.utcnow().isoformat()
        row = conn.execute(
            "SELECT last_activity_at FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        was_offline = True
        if row and row["last_activity_at"]:
            try:
                previous = datetime.fromisoformat(str(row["last_activity_at"]).replace("Z", "+00:00")).replace(tzinfo=None)
                was_offline = (datetime.utcnow() - previous).total_seconds() > 300
            except Exception:
                was_offline = True
        if was_offline:
            conn.execute(
                "UPDATE users SET last_activity_at=?, ip_capture_needed=1 WHERE user_id=?",
                (now, user_id),
            )
        else:
            conn.execute(
                "UPDATE users SET last_activity_at=? WHERE user_id=?", (now, user_id)
            )


def user_needs_ip_capture(user_id: int) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT ip_capture_needed FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        return bool(row and int(row["ip_capture_needed"] or 0))


def capture_telegram_user_ip(user_id: int, ip_address: str | None):
    """Save IP for a Telegram user and close the current session's capture requirement."""
    ip_address = (ip_address or "").strip()[:64]
    if not ip_address:
        return
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET last_ip=?, ip_capture_needed=0 WHERE user_id=?",
            (ip_address, int(user_id)),
        )
        # If this Telegram user is linked to a web account, keep both views in sync.
        conn.execute(
            "UPDATE web_accounts SET last_ip=? WHERE user_id=? OR telegram_user_id=?",
            (ip_address, int(user_id), int(user_id)),
        )


def get_golden_question_for_redirect(question_id: int, user_id: int):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id,user_id,asin FROM golden_questions WHERE id=? AND user_id=? LIMIT 1",
            (int(question_id), int(user_id)),
        ).fetchone()
        return dict(row) if row else None


def deactivate_user(user_id: int):
    """بتلغي تفعيل العميل، وترجّع buyer_type للصفر عشان لو رجع تاني يتسأل
    من الأول (عميل بيشتري بانتظام ولا لسه بيتابع بس)."""
    with get_conn() as conn:
        conn.execute(
            """UPDATE users SET is_active = 0, tag_id = NULL, tag_assigned_at = NULL,
               queued_at = NULL, buyer_type = NULL WHERE user_id = ?""",
            (user_id,),
        )


def list_all_users():
    with get_conn() as conn:
        return conn.execute("SELECT * FROM users ORDER BY joined_at").fetchall()


def list_users_by_program(program: str):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE program = ? ORDER BY joined_at", (program,)
        ).fetchall()


def delete_user(user_id: int) -> bool:
    """يمسح المستخدم خالص من القاعدة (بعد ما يرجّع تاجه للـ Pool لو ماسك واحد)."""
    release_tag_by_user(user_id)
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
        return cur.rowcount > 0


def list_verified_users():
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE verified = 1 ORDER BY verified_at"
        ).fetchall()


def list_egypt_users_with_tag():
    """كل عملاء مصر النشطين اللي معاهم لينك شخصي دلوقتي (عشان نبعتلهم تنبيه عرض جديد)."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT user_id FROM users WHERE program = 'egypt' AND is_active = 1 AND tag_id IS NOT NULL"
        ).fetchall()


def list_active_egypt_users():
    """كل عملاء مصر النشطين (بتاج شخصي أو من غيره) - بيشوفوا العروض عادي جوه البوت."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT user_id FROM users WHERE program = 'egypt' AND is_active = 1"
        ).fetchall()


def find_stale_tag_holders(inactivity_hours: int):
    """
    بترجع عملاء مصر اللي ماسكين تاج بس معندهمش أي نشاط مع البوت (مفتحوش
    عروض ذهبية، ما اتفاعلوش خالص) من قد المدة دي بالساعات - سواء من وقت
    ما اخدوا التاج، أو من آخر نشاط مسجّل ليهم.
    """
    cutoff = (datetime.utcnow() - timedelta(hours=inactivity_hours)).isoformat()
    with get_conn() as conn:
        return conn.execute(
            """SELECT user_id, tag_id FROM users
               WHERE program = 'egypt' AND tag_id IS NOT NULL
               AND (
                   (last_activity_at IS NULL AND tag_assigned_at < ?)
                   OR (last_activity_at IS NOT NULL AND last_activity_at < ?)
               )""",
            (cutoff, cutoff),
        ).fetchall()


def downgrade_to_browser(user_id: int):
    """بتسحب تاج العميل (بسبب عدم الشرا) وتحوّله لمسار المتابعة العادي
    (لينك القناة بس، من غير تاج شخصي)."""
    release_tag_by_user(user_id)
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET buyer_type = 'browser', last_points_added_at = NULL WHERE user_id = ?",
            (user_id,),
        )


# ---------- التاجات (Tag Pool) ----------

def get_available_tag(program: str = "ksa"):
    """يرجع أول تاج متاح في الـ Pool لبرنامج معيّن، أو None لو مفيش."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM tags WHERE in_pool = 1 AND program = ? ORDER BY id LIMIT 1",
            (program,),
        ).fetchone()


def assign_tag_to_user(user_id: int, tag_id: int):
    with get_conn() as conn:
        now = datetime.utcnow().isoformat()
        conn.execute(
            """UPDATE users SET tag_id = ?, tag_assigned_at = ?, last_activity_at = ?,
               queued_at = NULL, reminder_sent = 0 WHERE user_id = ?""",
            (tag_id, now, now, user_id),
        )
        conn.execute("UPDATE tags SET in_pool = 0 WHERE id = ?", (tag_id,))


def release_tag_by_user(user_id: int):
    """يرجع تاج المستخدم للـ Pool (لو اتلغى اشتراكه أو خمل أو خلصت مدته)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT tag_id FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        if not row or not row["tag_id"]:
            return
        conn.execute("UPDATE tags SET in_pool = 1 WHERE id = ?", (row["tag_id"],))
        conn.execute(
            """UPDATE users SET tag_id = NULL, tag_assigned_at = NULL, reminder_sent = 0
               WHERE user_id = ?""",
            (user_id,),
        )


def add_tag(keyword: str, program: str = "ksa", category: str | None = None) -> bool:
    with get_conn() as conn:
        try:
            conn.execute(
                "INSERT INTO tags (keyword, category, program, in_pool, created_at) VALUES (?, ?, ?, 1, ?)",
                (keyword, category, program, datetime.utcnow().isoformat()),
            )
            return True
        except sqlite3.IntegrityError:
            return False


def remove_tag(keyword: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM tags WHERE keyword = ?", (keyword,))


def list_tags(program: str | None = None):
    with get_conn() as conn:
        if program:
            return conn.execute(
                "SELECT * FROM tags WHERE program = ? ORDER BY id", (program,)
            ).fetchall()
        return conn.execute("SELECT * FROM tags ORDER BY id").fetchall()


def find_expired_assignments(ttl_minutes: int):
    """يرجع مستخدمين برنامج السعودية اللي عدّت مدة اللينك بتاعهم من غير تفعيل."""
    cutoff = (datetime.utcnow() - timedelta(minutes=ttl_minutes)).isoformat()
    with get_conn() as conn:
        return conn.execute(
            """SELECT user_id, tag_id FROM users
               WHERE tag_id IS NOT NULL AND verified = 0 AND program = 'ksa'
               AND tag_assigned_at IS NOT NULL AND tag_assigned_at < ?""",
            (cutoff,),
        ).fetchall()


def find_users_needing_reminder(ttl_minutes: int, remind_before_minutes: int):
    """يرجع مستخدمين برنامج السعودية اللي قربت مدتهم تخلص (وماوصلهمش تذكير قبل كده)."""
    remind_cutoff = (
        datetime.utcnow() - timedelta(minutes=ttl_minutes - remind_before_minutes)
    ).isoformat()
    with get_conn() as conn:
        return conn.execute(
            """SELECT user_id, tag_id FROM users
               WHERE tag_id IS NOT NULL AND verified = 0 AND reminder_sent = 0 AND program = 'ksa'
               AND tag_assigned_at IS NOT NULL AND tag_assigned_at < ?""",
            (remind_cutoff,),
        ).fetchall()


def mark_reminder_sent(user_id: int):
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET reminder_sent = 1 WHERE user_id = ?", (user_id,)
        )


# ---------- التفعيل (Verify) - برنامج السعودية ----------

def find_user_holding_tag(keyword: str):
    with get_conn() as conn:
        return conn.execute(
            """SELECT u.* FROM users u JOIN tags t ON u.tag_id = t.id
               WHERE t.keyword = ?""",
            (keyword,),
        ).fetchone()


def mark_verified(user_id: int):
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET verified = 1, verified_at = ? WHERE user_id = ?",
            (datetime.utcnow().isoformat(), user_id),
        )


# ---------- نقط الشرا - برنامج مصر ----------

def add_points(user_id: int, amount: int, is_purchase: bool = True) -> int:
    """
    يضيف نقط لعميل ويرجّع رصيده الجديد.
    is_purchase=True (الافتراضي): نقط شرا حقيقية - بتسجّل وقت آخر إضافة نقط
    وتعلّم العميل كـ"مشترى قبل كده" (عشان يبقى ليه أولوية في الطابور بعدين).
    is_purchase=False: نقط هدية بس - بتتضاف للرصيد بس من غير ما تأثر على
    "مشترى قبل كده" ولا وقت آخر نشاط شرا.
    """
    with get_conn() as conn:
        if is_purchase:
            conn.execute(
                """UPDATE users SET points_balance = points_balance + ?,
                   last_points_added_at = ?,
                   has_purchased_before = CASE WHEN ? > 0 THEN 1 ELSE has_purchased_before END
                   WHERE user_id = ?""",
                (amount, datetime.utcnow().isoformat(), amount, user_id),
            )
        else:
            conn.execute(
                "UPDATE users SET points_balance = points_balance + ? WHERE user_id = ?",
                (amount, user_id),
            )
        row = conn.execute(
            "SELECT points_balance FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return row["points_balance"] if row else 0


def close_month(points_per_spin: int):
    """
    بتحوّل نقط الشهر لكل عملاء مصر لعدد لفات، وتصفّر النقط لبداية شهر جديد.
    بترجع قائمة فيها كل عميل وعدد اللفات اللي كسبها هذا الشهر.
    """
    with get_conn() as conn:
        users = conn.execute(
            "SELECT user_id, points_balance FROM users WHERE program = 'egypt' AND is_active = 1"
        ).fetchall()
        results = []
        for u in users:
            earned_spins = u["points_balance"] // points_per_spin
            conn.execute(
                """UPDATE users SET spins_balance = spins_balance + ?, points_balance = 0
                   WHERE user_id = ?""",
                (earned_spins, u["user_id"]),
            )
            results.append({"user_id": u["user_id"], "spins_earned": earned_spins})
        return results


def convert_user_points(user_id: int, points_per_spin: int) -> int:
    """
    بتحوّل نقط عميل واحد بس لعدد لفّات فورًا (من غير ما تأثر على باقي العملاء
    ومن غير ما تستنى قفل الشهر). بتسيب الباقي (Remainder) زي ما هو - مش
    بتصفّره زي /closemonth. بترجع عدد اللفّات اللي اتكسبت.
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT points_balance FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        if not row:
            return 0
        earned_spins = row["points_balance"] // points_per_spin
        if earned_spins <= 0:
            return 0
        conn.execute(
            """UPDATE users SET spins_balance = spins_balance + ?,
               points_balance = points_balance - ? WHERE user_id = ?""",
            (earned_spins, earned_spins * points_per_spin, user_id),
        )
        return earned_spins


def get_spins_balance(user_id: int) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT spins_balance FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return row["spins_balance"] if row else 0


def use_one_spin(user_id: int) -> bool:
    """بتستهلك لفة واحدة من رصيد العميل (بترجع False لو مفيش رصيد)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT spins_balance FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        if not row or row["spins_balance"] <= 0:
            return False
        conn.execute(
            "UPDATE users SET spins_balance = spins_balance - 1 WHERE user_id = ?",
            (user_id,),
        )
        return True


def record_spin_result(user_id: int, prize: str, collected: bool = True):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO wheel_spins (user_id, prize, collected, spun_at) VALUES (?, ?, ?, ?)",
            (user_id, prize, 1 if collected else 0, datetime.utcnow().isoformat()),
        )


def add_gift_balance(user_id: int, amount: float) -> float:
    """بتضيف قيمة الجايزة (بالجنيه) لرصيد العميل، وترجّع الرصيد الجديد."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET gift_balance = gift_balance + ? WHERE user_id = ?",
            (amount, user_id),
        )
        row = conn.execute(
            "SELECT gift_balance FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return row["gift_balance"] if row else 0


def get_gift_balance(user_id: int) -> float:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT gift_balance FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return row["gift_balance"] if row else 0


def reset_gift_balance(user_id: int) -> float:
    """بتصفّر رصيد العميل بعد ما تدفعيله، وترجّع القيمة اللي كانت متجمّعة قبل التصفير."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT gift_balance FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        old = row["gift_balance"] if row else 0
        conn.execute(
            "UPDATE users SET gift_balance = 0 WHERE user_id = ?", (user_id,)
        )
        return old


def admin_zero_customer_balance_and_spins(user_id: int, admin_id: int | None = None, customer_message: str = "") -> dict | None:
    """Hard reset of all *current/unpaid* customer rewards without deleting history.

    Paid redemption history and answered-question history remain untouched. Pending
    lucky spins and pending/processing redemption requests are cancelled so no
    pre-reset reward can be collected after the reset.
    """
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """SELECT user_id, gift_balance, points_balance, spins_balance
               FROM users WHERE user_id=?""", (int(user_id),)
        ).fetchone()
        if not row:
            return None

        old_gift = float(row["gift_balance"] or 0)
        old_points = int(row["points_balance"] or 0)
        old_spins = int(row["spins_balance"] or 0)

        pending_spin_count = conn.execute(
            "SELECT COUNT(*) AS c FROM lucky_spins WHERE user_id=? AND status='pending'",
            (int(user_id),),
        ).fetchone()["c"] or 0
        pending_redemption_count = conn.execute(
            """SELECT COUNT(*) AS c FROM redemption_requests
               WHERE user_id=? AND status IN ('pending','processing')""",
            (int(user_id),),
        ).fetchone()["c"] or 0

        conn.execute(
            """UPDATE users SET gift_balance=0, points_balance=0, spins_balance=0,
               golden_opened_count=0, golden_answered_count=0,
               golden_round_earnings=0, golden_target=?
               WHERE user_id=?""",
            (GOLDEN_TARGET_COUNT, int(user_id)),
        )
        conn.execute(
            """UPDATE lucky_spins SET status='cancelled'
               WHERE user_id=? AND status='pending'""",
            (int(user_id),),
        )
        conn.execute(
            """UPDATE redemption_requests SET status='cancelled'
               WHERE user_id=? AND status IN ('pending','processing')""",
            (int(user_id),),
        )

        # Keep old questions/spins/redemptions as evidence/history; only current
        # entitlements are zeroed/cancelled.
        conn.execute(
            """INSERT INTO admin_reward_resets
               (user_id, old_gift_balance, old_points_balance, old_spins_balance,
                cancelled_lucky_spins, cancelled_redemptions, admin_id, customer_message, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (int(user_id), old_gift, old_points, old_spins,
             int(pending_spin_count), int(pending_redemption_count),
             int(admin_id) if admin_id is not None else None,
             str(customer_message or "")[:2000], now),
        )

        wa = conn.execute(
            "SELECT id, phone_e164, telegram_user_id FROM web_accounts WHERE user_id=? LIMIT 1",
            (int(user_id),),
        ).fetchone()
        return {
            "user_id": int(user_id),
            "old_gift_balance": old_gift,
            "old_points_balance": old_points,
            "old_spins_balance": old_spins,
            "cancelled_lucky_spins": int(pending_spin_count),
            "cancelled_redemptions": int(pending_redemption_count),
            "telegram_user_id": int(wa["telegram_user_id"]) if wa and wa["telegram_user_id"] else None,
            "phone_e164": wa["phone_e164"] if wa else None,
            "created_at": now,
        }


def list_spin_history(user_id: int | None = None):
    with get_conn() as conn:
        if user_id:
            return conn.execute(
                "SELECT * FROM wheel_spins WHERE user_id = ? ORDER BY spun_at", (user_id,)
            ).fetchall()
        return conn.execute("SELECT * FROM wheel_spins ORDER BY spun_at").fetchall()


# ---------- العجلة (برنامج السعودية - لفة واحدة بس) ----------

def set_wheel_prize(user_id: int, prize: str) -> bool:
    """يسجّل نتيجة العجلة لأول مرة بس (بيرجع False لو العميل لف قبل كده)."""
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT wheel_prize FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        if existing and existing["wheel_prize"]:
            return False
        conn.execute(
            "UPDATE users SET wheel_prize = ?, wheel_won_at = ? WHERE user_id = ?",
            (prize, datetime.utcnow().isoformat(), user_id),
        )
        return True


def get_wheel_prize(user_id: int):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT wheel_prize FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return row["wheel_prize"] if row else None


def list_wheel_winners():
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE wheel_prize IS NOT NULL ORDER BY wheel_won_at"
        ).fetchall()


def list_pending_gift_balances():
    """كل عملاء مصر اللي عندهم رصيد جوايز لسه محتاج يتسلّم (رصيد > صفر)."""
    with get_conn() as conn:
        return conn.execute(
            """SELECT * FROM users WHERE program = 'egypt' AND gift_balance > 0
               ORDER BY gift_balance DESC"""
        ).fetchall()



# ---------- طلبات استبدال الهدايا / المستحقات ----------

def get_open_redemption_request(user_id: int):
    """آخر طلب استبدال غير مدفوع للعميل، إن وجد."""
    with get_conn() as conn:
        row = conn.execute(
            """SELECT r.*, u.username
               FROM redemption_requests r
               JOIN users u ON u.user_id = r.user_id
               WHERE r.user_id = ? AND r.status IN ('pending', 'processing')
               ORDER BY r.id DESC LIMIT 1""",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None


def create_redemption_request(user_id: int):
    """ينشئ طلب استبدال جديد من كل الجنيهات الصحيحة في الرصيد الحالي.

    أي طلبات قديمة Pending لا تمنع إنشاء طلب جديد من رصيد جديد اتجمع بعدها.
    مثال: طلب قديم 1 جنيه + رصيد حالي 2.04 جنيه
    => يظل الطلب القديم 1 جنيه، ويتعمل طلب جديد 2 جنيه، ويتبقى 0.04 جنيه.
    """
    with get_conn() as conn:
        # BEGIN IMMEDIATE يمنع طلبين متزامنين من حجز نفس الرصيد.
        conn.execute("BEGIN IMMEDIATE")

        user = conn.execute(
            "SELECT gift_balance FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        balance = float(user["gift_balance"] or 0) if user else 0.0

        # نستبدل الجنيهات الصحيحة فقط، ونسيب الكسور في حساب العميل.
        # مثال: 12.75 جنيه -> طلب الاستبدال 12 جنيه، والمتبقي 0.75 جنيه.
        amount = int(balance + 1e-9)
        remainder = round(balance - amount, 6)
        if amount <= 0:
            return None

        now = datetime.utcnow().isoformat()
        cur = conn.execute(
            """INSERT INTO redemption_requests
               (user_id, amount, status, requested_at)
               VALUES (?, ?, 'pending', ?)""",
            (user_id, amount, now),
        )
        # نخصم فقط المبلغ الصحيح المحجوز للطلب، ونحتفظ بالباقي في رصيد العميل.
        conn.execute(
            "UPDATE users SET gift_balance = ? WHERE user_id = ?",
            (remainder, user_id),
        )
        return {
            "id": cur.lastrowid,
            "user_id": user_id,
            "amount": amount,
            "remainder": remainder,
            "status": "pending",
            "requested_at": now,
            "paid_at": None,
            "paid_by": None,
            "created": True,
        }


def list_redemption_requests(status: str = "pending", limit: int = 10, offset: int = 0):
    with get_conn() as conn:
        return conn.execute(
            """SELECT r.*, u.username
               FROM redemption_requests r
               JOIN users u ON u.user_id = r.user_id
               WHERE r.status = ?
               ORDER BY r.requested_at ASC, r.id ASC
               LIMIT ? OFFSET ?""",
            (status, limit, offset),
        ).fetchall()


def count_redemption_requests(status: str = "pending") -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM redemption_requests WHERE status = ?", (status,)
        ).fetchone()
        return int(row["c"] or 0)


def get_redemption_request(request_id: int):
    with get_conn() as conn:
        row = conn.execute(
            """SELECT r.*, u.username
               FROM redemption_requests r
               JOIN users u ON u.user_id = r.user_id
               WHERE r.id = ?""",
            (request_id,),
        ).fetchone()
        return dict(row) if row else None


def get_pending_redemption_for_user(user_id: int):
    with get_conn() as conn:
        row = conn.execute(
            """SELECT r.*, u.username
               FROM redemption_requests r
               JOIN users u ON u.user_id = r.user_id
               WHERE r.user_id = ? AND r.status = 'pending'
               ORDER BY r.id DESC LIMIT 1""",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None


def reserve_redemption_for_send(request_id: int, gift_code: str):
    """يحجز الطلب لأدمن واحد قبل إرسال الكود لمنع الإرسال المكرر."""
    clean_code = (gift_code or "").strip()
    if not clean_code:
        return None
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM redemption_requests WHERE id = ?", (request_id,)
        ).fetchone()
        if not row or row["status"] != "pending":
            return None
        conn.execute(
            "UPDATE redemption_requests SET status='processing', gift_code=? WHERE id=?",
            (clean_code, request_id),
        )
        result = dict(row)
        result.update({"status": "processing", "gift_code": clean_code})
        return result


def release_redemption_send(request_id: int):
    """يرجع الطلب Pending لو إرسال Telegram فشل."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE redemption_requests SET status='pending' WHERE id=? AND status='processing'",
            (request_id,),
        )


def mark_redemption_paid(request_id: int, admin_id: int, gift_code: str | None = None):
    """
    يعلّم الطلب مدفوعًا بدون لمس الرصيد الجديد.
    لو gift_code موجود بيتحفظ مع سجل الدفع للمراجعة والمحاسبة.
    استدعاء الدالة يكون بعد نجاح إرسال الكود للعميل.
    """
    clean_code = (gift_code or "").strip() or None
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM redemption_requests WHERE id = ?", (request_id,)
        ).fetchone()
        if not row:
            return None
        if row["status"] == "paid":
            result = dict(row)
            return result
        now = datetime.utcnow().isoformat()
        conn.execute(
            """UPDATE redemption_requests
               SET status = 'paid', paid_at = ?, paid_by = ?,
                   gift_code = COALESCE(?, gift_code),
                   code_sent_at = CASE WHEN ? IS NOT NULL THEN ? ELSE code_sent_at END
               WHERE id = ?""",
            (now, admin_id, clean_code, clean_code, now, request_id),
        )
        result = dict(row)
        result.update({
            "status": "paid", "paid_at": now, "paid_by": admin_id,
            "gift_code": clean_code or result.get("gift_code"),
            "code_sent_at": now if clean_code else result.get("code_sent_at"),
        })
        return result

def get_redemption_summary():
    with get_conn() as conn:
        pending = conn.execute(
            """SELECT COUNT(*) AS c, COALESCE(SUM(amount), 0) AS total
               FROM redemption_requests WHERE status = 'pending'"""
        ).fetchone()
        paid = conn.execute(
            """SELECT COUNT(*) AS c, COALESCE(SUM(amount), 0) AS total
               FROM redemption_requests WHERE status = 'paid'"""
        ).fetchone()
        return {
            "pending_count": int(pending["c"] or 0),
            "pending_total": float(pending["total"] or 0),
            "paid_count": int(paid["c"] or 0),
            "paid_total": float(paid["total"] or 0),
        }


# ---------- عروض القناة الذهبية (Golden Deals) ----------

MAX_CACHED_GOLDEN_DEALS = 50
GOLDEN_TARGET_COUNT = config.EGYPT_GOLDEN_QUESTIONS_PER_ROUND

# ---------- عجلة الحظ (Lucky Wheel) - اختيار الجايزة من الـ backend ----------
# القيم والاحتمالات دي متفق عليها معاك: EV = 1.28 جنيه لكل لفة (RTP = 32%
# من قيمة 4 جنيه لكل لفة = 5 أسئلة × 0.80 جنيه للسؤال)
LUCKY_WHEEL_PRIZES = [0.50, 1, 2, 3, 5, 10]
LUCKY_WHEEL_WEIGHTS = [46, 27, 16, 7, 3, 1]
LUCKY_WHEEL_POOL_SIZE = 10000  # إجمالي المحاولات لكل دورة مخزون قبل ما يترجع من الأول


def create_lucky_spin(user_id: int, prize: float | None = None) -> tuple[int, float, int]:
    """
    بتختار جايزة عشوائية آمنة (Server-side) من "مخزون" محدود (5000 محاولة
    موزّعة حسب النسب المتفق عليها). كل جايزة بتتاخد بتقل من مخزونها، فكل
    ما حد ياخدها يقل احتمال حد تاني ياخدها لحد ما المخزون كله يخلص
    ويترجع من الأول تلقائي. بتسجّل اللفة كـ"معلّقة" لحد ما العميل يأكّدها.
    بترجع (spin_id, prize, prize_index).
    """
    if prize is not None:
        prize = round(max(float(prize), 0), 6)
        with get_conn() as conn:
            existing = conn.execute(
                """SELECT * FROM lucky_spins WHERE user_id = ? AND status = 'pending'
                   ORDER BY id DESC LIMIT 1""",
                (user_id,),
            ).fetchone()
            if existing:
                return existing["id"], existing["prize"], existing["prize_index"]
            cur = conn.execute(
                """INSERT INTO lucky_spins
                   (user_id, prize, prize_index, status, created_at)
                   VALUES (?, ?, 0, 'pending', ?)""",
                (user_id, prize, datetime.utcnow().isoformat()),
            )
            conn.execute(
                """UPDATE users SET golden_opened_count = 0,
                   golden_answered_count = 0, golden_round_earnings = 0,
                   golden_target = ? WHERE user_id = ?""",
                (GOLDEN_TARGET_COUNT, user_id),
            )
            return cur.lastrowid, prize, 0

    # المسار القديم محفوظ للتوافق فقط.
    import secrets
    with get_conn() as conn:
        pool = conn.execute(
            "SELECT * FROM lucky_wheel_pool ORDER BY prize_index"
        ).fetchall()
        total_remaining = sum(p["remaining"] for p in pool)

        if total_remaining <= 0:
            # المخزون خلص خالص - نرجّعه للأول تلقائي
            for p in pool:
                conn.execute(
                    "UPDATE lucky_wheel_pool SET remaining = total WHERE prize_index = ?",
                    (p["prize_index"],),
                )
            pool = conn.execute(
                "SELECT * FROM lucky_wheel_pool ORDER BY prize_index"
            ).fetchall()
            total_remaining = sum(p["remaining"] for p in pool)

        r = secrets.randbelow(total_remaining)
        cumulative = 0
        chosen = pool[-1]
        for p in pool:
            cumulative += p["remaining"]
            if r < cumulative:
                chosen = p
                break

        conn.execute(
            "UPDATE lucky_wheel_pool SET remaining = remaining - 1 WHERE prize_index = ?",
            (chosen["prize_index"],),
        )
        prize = chosen["prize"]
        prize_index = chosen["prize_index"]

        cur = conn.execute(
            "INSERT INTO lucky_spins (user_id, prize, prize_index, status, created_at) VALUES (?, ?, ?, 'pending', ?)",
            (user_id, prize, prize_index, datetime.utcnow().isoformat()),
        )
        spin_id = cur.lastrowid
    return spin_id, prize, prize_index


def get_lucky_wheel_pool_status():
    """بترجع حالة المخزون الحالية (كام فاضل من كل جايزة)."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM lucky_wheel_pool ORDER BY prize_index"
        ).fetchall()


def get_pending_lucky_spin(user_id: int):
    """بترجع آخر لفة معلّقة (لسه ما اتستلمتش) للعميل ده، أو None لو مفيش."""
    with get_conn() as conn:
        return conn.execute(
            """SELECT * FROM lucky_spins WHERE user_id = ? AND status = 'pending'
               ORDER BY id DESC LIMIT 1""",
            (user_id,),
        ).fetchone()


def claim_lucky_spin(spin_id: int, user_id: int) -> float | None:
    """
    بتأكّد اللفة وتضيفها كمُستلمة - بترجع قيمة الجايزة لو نجحت، أو None لو
    اللفة دي مش موجودة، أو بتاعة عميل تاني، أو مُستلمة بالفعل من قبل
    (بيمنع استلام نفس اللفة مرتين).
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM lucky_spins WHERE id = ? AND user_id = ? AND status = 'pending'",
            (spin_id, user_id),
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE lucky_spins SET status = 'claimed', claimed_at = ? WHERE id = ?",
            (datetime.utcnow().isoformat(), spin_id),
        )
        return row["prize"]


def add_golden_deal(message_id: int, photo_file_id: str | None, caption: str, link: str):
    """بتضيف عرض ذهبي جديد، وتحذف الأقدم لو عدّى عدد الـ50."""
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO golden_deals (message_id, photo_file_id, caption, base_link, posted_at)
               VALUES (?, ?, ?, ?, ?)""",
            (message_id, photo_file_id, caption, link, datetime.utcnow().isoformat()),
        )
        conn.execute(
            f"""DELETE FROM golden_deals WHERE id NOT IN (
                SELECT id FROM golden_deals ORDER BY id DESC LIMIT {MAX_CACHED_GOLDEN_DEALS}
            )"""
        )


def list_golden_deals_posted_today():
    """كل عروض القناة الذهبية اللي اتنزلت النهاردة (بتوقيت UTC)."""
    today = datetime.utcnow().date().isoformat()
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM golden_deals WHERE date(posted_at) = ? ORDER BY id DESC",
            (today,),
        ).fetchall()


def list_all_quizzed_asins(user_id: int) -> set[str]:
    """كل المنتجات التي ظهرت لهذا العميل سابقًا في أسئلة العجلة."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT asin FROM golden_questions WHERE user_id=? AND asin IS NOT NULL AND TRIM(asin)<>''",
            (int(user_id),),
        ).fetchall()
    return {str(row["asin"]).strip().upper() for row in rows if row["asin"]}


def list_todays_quizzed_asins(user_id: int) -> set[str]:
    """كل الـ ASINs اللي العميل ده اتسأل عنها النهاردة بالفعل."""
    today = datetime.utcnow().date().isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT asin FROM golden_quiz_log WHERE user_id = ? AND quiz_date = ?",
            (user_id, today),
        ).fetchall()
        return {r["asin"] for r in rows}


def log_quiz_asked(user_id: int, asin: str):
    today = datetime.utcnow().date().isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO golden_quiz_log (user_id, asin, quiz_date) VALUES (?, ?, ?)",
            (user_id, asin, today),
        )


def mark_golden_correct(user_id: int) -> int:
    """بتسجّل إجابة صح، وترجّع عدد الإجابات الصح المتتالية لحد دلوقتي."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET golden_opened_count = golden_opened_count + 1 WHERE user_id = ?",
            (user_id,),
        )
        row = conn.execute(
            "SELECT golden_opened_count FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return row["golden_opened_count"] if row else 0


def mark_golden_wrong(user_id: int) -> int:
    """بتسجّل إجابة غلط - بتزوّد إجمالي عدد أسئلة الجولة بسؤالين، وترجّع العدد الجديد."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET golden_target = golden_target + 2 WHERE user_id = ?",
            (user_id,),
        )
        row = conn.execute(
            "SELECT golden_target FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return row["golden_target"] if row else GOLDEN_TARGET_COUNT


def reset_golden_progress(user_id: int):
    with get_conn() as conn:
        conn.execute(
            """UPDATE users SET golden_opened_count = 0, golden_answered_count = 0,
               golden_round_earnings = 0, golden_target = ? WHERE user_id = ?""",
            (GOLDEN_TARGET_COUNT, user_id),
        )


def get_current_golden_round_epc(user_id: int, answered_count: int | None = None) -> float:
    """مجموع EPC للأسئلة الحالية في الجولة."""
    if answered_count is None:
        row = get_user(user_id)
        answered_count = int(row["golden_answered_count"] or 0) if row else 0
    n = max(0, int(answered_count or 0))
    if n <= 0:
        return 0.0
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT epc FROM golden_questions WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (int(user_id), n),
        ).fetchall()
    return float(sum(float(row["epc"] or 0) for row in rows))


def create_golden_question(
    user_id: int,
    asin: str,
    question_type: str,
    correct_index: int,
    epc: float,
    reward_value: float,
    prompt: str | None = None,
    options: list[str] | None = None,
    product_link: str | None = None,
) -> int:
    """يسجّل السؤال وإجابته في السيرفر قبل إرساله للعميل.

    الحقول الإضافية اختيارية عشان نسخة الويب تقدر تسترجع نفس السؤال بعد
    Refresh، وفي نفس الوقت تفضل استدعاءات Telegram القديمة شغالة كما هي.
    """
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO golden_questions
               (user_id, asin, question_type, correct_index, epc, reward_value,
                prompt, options_json, product_link, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                user_id,
                asin,
                question_type,
                correct_index,
                float(epc),
                float(reward_value),
                prompt,
                json.dumps(options, ensure_ascii=False) if options is not None else None,
                product_link,
                datetime.utcnow().isoformat(),
            ),
        )
        return cur.lastrowid


def get_pending_web_golden_question(user_id: int):
    """يرجع آخر سؤال ويب غير مُجاب عليه مع نصه واختياراته، إن وجد."""
    with get_conn() as conn:
        row = conn.execute(
            """SELECT * FROM golden_questions
               WHERE user_id = ? AND answered = 0
                 AND prompt IS NOT NULL AND options_json IS NOT NULL
               ORDER BY id DESC LIMIT 1""",
            (user_id,),
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        try:
            result["options"] = json.loads(result.get("options_json") or "[]")
        except Exception:
            result["options"] = []
        return result

def answer_golden_question(user_id: int, question_id: int, chosen_index: int):
    """يسجّل الإجابة مرة واحدة ويرجع تقدم الجولة والاستحقاق الشخصي."""
    with get_conn() as conn:
        question = conn.execute(
            """SELECT * FROM golden_questions
               WHERE id = ? AND user_id = ? AND answered = 0""",
            (question_id, user_id),
        ).fetchone()
        if not question:
            return None

        is_correct = int(chosen_index == question["correct_index"])
        contribution = question["reward_value"] if is_correct else 0.0
        conn.execute(
            """UPDATE golden_questions SET answered = 1, was_correct = ?, answered_at = ?
               WHERE id = ?""",
            (is_correct, datetime.utcnow().isoformat(), question_id),
        )
        # golden_target = إجمالي عدد الأسئلة المطلوب إكمالها في الجولة.
        # يبدأ بـ 5، وكل إجابة غلط تضيف سؤالين كعقوبة.
        # عدد الإجابات الصح المطلوب لا يتغير؛ يظل الهدف الأساسي 5.
        conn.execute(
            """UPDATE users SET
               golden_answered_count = golden_answered_count + 1,
               golden_opened_count = golden_opened_count + ?,
               golden_round_earnings = golden_round_earnings + ?,
               golden_target = golden_target + CASE WHEN ? = 0 THEN 2 ELSE 0 END
               WHERE user_id = ?""",
            (is_correct, contribution, is_correct, user_id),
        )
        progress = conn.execute(
            """SELECT golden_answered_count, golden_opened_count,
               golden_round_earnings, golden_target, first_round_bonus_used FROM users WHERE user_id = ?""",
            (user_id,),
        ).fetchone()

        # One-time welcome round for NEW accounts only. The first completed round
        # is worth exactly 2.00 EGP, regardless of the per-question EPC reward.
        # Mark it used at completion; resets/reactivation never clear this flag.
        if (
            int(progress["golden_answered_count"] or 0) >= int(progress["golden_target"] or 0)
            and int(progress["first_round_bonus_used"] or 0) == 0
        ):
            conn.execute(
                "UPDATE users SET golden_round_earnings = 2.0, first_round_bonus_used = 1 WHERE user_id = ?",
                (user_id,),
            )
            progress = conn.execute(
                """SELECT golden_answered_count, golden_opened_count,
                   golden_round_earnings, golden_target, first_round_bonus_used
                   FROM users WHERE user_id = ?""",
                (user_id,),
            ).fetchone()

        # No minimum customer reward is enforced for normal rounds.
        # The earned amount stays exactly as calculated from EPC and Railway reward-rate variables.

        return {
            "correct": bool(is_correct),
            "answered_count": progress["golden_answered_count"],
            "correct_count": progress["golden_opened_count"],
            "round_earnings": progress["golden_round_earnings"],
            "target": progress["golden_target"],
            "contribution": contribution,
        }


# ---------- طابور الانتظار (Queue) ----------

def add_to_queue(user_id: int):
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET queued_at = ? WHERE user_id = ? AND tag_id IS NULL",
            (datetime.utcnow().isoformat(), user_id),
        )


def pop_next_in_queue(program: str = "ksa"):
    """
    يرجع أول واحد في الطابور لنفس البرنامج (وبيشيله من الطابور)، أو None لو فاضي.
    العملاء اللي اشتروا قبل كده (has_purchased_before) ليهم أولوية دايمًا،
    وبين اللي ليهم نفس الأولوية بيتاخد الأقدم في الطابور الأول (FIFO).
    """
    with get_conn() as conn:
        row = conn.execute(
            """SELECT user_id FROM users
               WHERE queued_at IS NOT NULL AND tag_id IS NULL AND is_active = 1 AND program = ?
               ORDER BY has_purchased_before DESC, queued_at ASC LIMIT 1""",
            (program,),
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE users SET queued_at = NULL WHERE user_id = ?", (row["user_id"],)
        )
        return row["user_id"]


def queue_length(program: str = "ksa") -> int:
    with get_conn() as conn:
        row = conn.execute(
            """SELECT COUNT(*) AS c FROM users
               WHERE queued_at IS NOT NULL AND tag_id IS NULL AND program = ?""",
            (program,),
        ).fetchone()
        return row["c"]


# ---------- الأدمنز ----------

def is_admin(user_id: int) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM admins WHERE user_id = ?", (user_id,)
        ).fetchone()
        return row is not None


def add_admin(user_id: int):
    with get_conn() as conn:
        conn.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (user_id,))


# ---------- عروض أمازون (Deals Cache) ----------

MAX_CACHED_DEALS = 10


def _amazon_product_key(link: str) -> str:
    """مفتاح ثابت للمنتج لمنع تكراره حتى لو التاج/الباراميترز أو الرسالة اختلفت."""
    import re
    if not link:
        return ""
    match = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})(?:[/?]|$)", link, re.IGNORECASE)
    if match:
        return match.group(1).upper()
    # احتياطي: تجاهل query string لو الرابط مش بصيغة ASIN المعتادة
    return link.split("?", 1)[0].rstrip("/").lower()


def add_deal(message_id: int, photo_file_id: str | None, caption: str, links: list[str]):
    """تضيف العرض مرة واحدة فقط حسب message_id أو المنتج نفسه (ASIN)."""
    with get_conn() as conn:
        # نفس رسالة تيليجرام وصلت مرتين
        existing = conn.execute(
            "SELECT 1 FROM deals_cache WHERE message_id = ? LIMIT 1",
            (message_id,),
        ).fetchone()
        if existing:
            return

        # نفس منتج أمازون وصل في بوست مختلف أو بلينك مختلف/تاج مختلف
        new_keys = {_amazon_product_key(link) for link in links if link}
        for row in conn.execute("SELECT base_link FROM deals_cache").fetchall():
            for old_link in get_deal_links(row["base_link"]):
                if _amazon_product_key(old_link) in new_keys:
                    return

        conn.execute(
            """INSERT INTO deals_cache (message_id, photo_file_id, caption, base_link, posted_at)
               VALUES (?, ?, ?, ?, ?)""",
            (message_id, photo_file_id, caption, json.dumps(links), datetime.utcnow().isoformat()),
        )
        conn.execute(
            f"""DELETE FROM deals_cache WHERE id NOT IN (
                SELECT id FROM deals_cache ORDER BY id DESC LIMIT {MAX_CACHED_DEALS}
            )"""
        )


def get_deal_links(base_link_field: str) -> list[str]:
    """بتقرأ عمود base_link سواء كان بصيغة JSON (تخزين جديد، أكتر من لينك)
    أو نص لينك واحد بس (تخزين قديم قبل دعم أكتر من لينك)."""
    try:
        parsed = json.loads(base_link_field)
        if isinstance(parsed, list):
            return parsed
    except (ValueError, TypeError):
        pass
    return [base_link_field] if base_link_field else []


def list_recent_deals():
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM deals_cache ORDER BY id DESC LIMIT ?", (MAX_CACHED_DEALS,)
        ).fetchall()


def get_pending_notify_count() -> int:
    """كام عرض فعلي جديد اتضاف من آخر دفعة تنبيه للعملاء."""
    with get_conn() as conn:
        state_row = conn.execute(
            "SELECT last_notified_deal_id FROM notify_state WHERE id = 1"
        ).fetchone()
        last_notified = state_row["last_notified_deal_id"] if state_row else 0
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM deals_cache WHERE id > ?",
            (last_notified,),
        ).fetchone()
        return row["c"] if row else 0


def get_pending_notify_batch(batch_size: int):
    """بترجع أول دفعة عروض لم يتم إرسال تنبيه عنها بعد، وبحد أقصى batch_size."""
    with get_conn() as conn:
        state_row = conn.execute(
            "SELECT last_notified_deal_id FROM notify_state WHERE id = 1"
        ).fetchone()
        last_notified = state_row["last_notified_deal_id"] if state_row else 0
        return conn.execute(
            "SELECT * FROM deals_cache WHERE id > ? ORDER BY id ASC LIMIT ?",
            (last_notified, int(batch_size)),
        ).fetchall()


def mark_notified_up_to(deal_id: int):
    """تقدّم عداد التنبيهات لحد آخر عرض في الدفعة فقط، فلا نضيع أي عرض أحدث."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE notify_state SET last_notified_deal_id = ? WHERE id = 1",
            (int(deal_id),),
        )


def mark_notified_up_to_latest():
    """توافق مع أي كود قديم: يسجل آخر عرض موجود حاليًا كآخر عرض تم التنبيه عنه."""
    with get_conn() as conn:
        row = conn.execute("SELECT MAX(id) AS max_id FROM deals_cache").fetchone()
        max_id = row["max_id"] or 0
        conn.execute(
            "UPDATE notify_state SET last_notified_deal_id = ? WHERE id = 1", (max_id,)
        )


def set_pending_offer_batch_for_active_egypt_users(from_id: int, to_id: int):
    """يثبت نفس دفعة العروض لكل العملاء النشطين وقت إرسال التنبيه."""
    with get_conn() as conn:
        conn.execute(
            """UPDATE users
               SET pending_offer_from_id = ?, pending_offer_to_id = ?
               WHERE is_active = 1 AND program = 'egypt'""",
            (int(from_id), int(to_id)),
        )


def get_pending_offer_batch_for_user(user_id: int):
    """ترجع حدود الدفعة التي وصل تنبيهها للعميل ولم يفتحها بعد."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT pending_offer_from_id, pending_offer_to_id FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()


def clear_pending_offer_batch(user_id: int):
    with get_conn() as conn:
        conn.execute(
            """UPDATE users SET pending_offer_from_id = NULL, pending_offer_to_id = NULL
               WHERE user_id = ?""",
            (user_id,),
        )


def list_deals_between(from_id: int, to_id: int):
    """ترجع نفس العروض التي كوّنت دفعة التنبيه، بالترتيب."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM deals_cache WHERE id BETWEEN ? AND ? ORDER BY id ASC",
            (int(from_id), int(to_id)),
        ).fetchall()


def list_new_deals_for_user(user_id: int):
    """للاستخدام اليدوي خارج دفعة التنبيه: العروض التي لم يرها العميل بعد."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT last_seen_deal_id FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        last_seen = row["last_seen_deal_id"] if row else 0
        return conn.execute(
            "SELECT * FROM deals_cache WHERE id > ? ORDER BY id ASC", (last_seen,)
        ).fetchall()


def mark_deals_seen_up_to(user_id: int, deal_id: int):
    """يسجل مشاهدة العميل حتى نهاية دفعة بعينها فقط."""
    with get_conn() as conn:
        conn.execute(
            """UPDATE users
               SET last_seen_deal_id = CASE
                   WHEN COALESCE(last_seen_deal_id, 0) < ? THEN ?
                   ELSE last_seen_deal_id
               END
               WHERE user_id = ?""",
            (int(deal_id), int(deal_id), user_id),
        )


def mark_deals_seen(user_id: int):
    """بتحدّث آخر عرض شافه العميل لأحدث عرض موجود في الكاش دلوقتي."""
    with get_conn() as conn:
        row = conn.execute("SELECT MAX(id) AS max_id FROM deals_cache").fetchone()
        max_id = row["max_id"] or 0
        conn.execute(
            "UPDATE users SET last_seen_deal_id = ? WHERE user_id = ?", (max_id, user_id)
        )


def record_sent_offer_message(user_id: int, message_id: int):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO sent_offer_messages (user_id, message_id) VALUES (?, ?)",
            (user_id, message_id),
        )


def pop_sent_offer_messages(user_id: int) -> list[int]:
    """بترجع آيديهات آخر رسايل عروض اتبعتت للعميل ده، وتمسحهم من الجدول."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT message_id FROM sent_offer_messages WHERE user_id = ?", (user_id,)
        ).fetchall()
        conn.execute("DELETE FROM sent_offer_messages WHERE user_id = ?", (user_id,))
        return [r["message_id"] for r in rows]


# ---------- تقارير الـ Back Office ----------

CAIRO_TZ = ZoneInfo("Africa/Cairo")


def _cairo_date_range_utc(date_from: str, date_to: str) -> tuple[str, str]:
    """حوّل تاريخين بالتوقيت المصري إلى حدود UTC: البداية شاملة والنهاية غير شاملة."""
    start_day = datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=CAIRO_TZ)
    end_day = (datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)).replace(tzinfo=CAIRO_TZ)
    return (
        start_day.astimezone(timezone.utc).replace(tzinfo=None).isoformat(),
        end_day.astimezone(timezone.utc).replace(tzinfo=None).isoformat(),
    )


def _period_where(column: str, period: str, date_from: str | None = None, date_to: str | None = None) -> tuple[str, list[str]]:
    """SQL fragment + params لفلاتر اليوم / أمس / 7 أيام / 30 يوم / فترة مخصصة / كل الوقت، بتوقيت القاهرة."""
    p = (period or "all").lower()
    cairo_now = datetime.now(CAIRO_TZ)

    if p == "today":
        d = cairo_now.strftime("%Y-%m-%d")
        start, end = _cairo_date_range_utc(d, d)
        return f"datetime({column}) >= datetime(?) AND datetime({column}) < datetime(?)", [start, end]
    if p == "yesterday":
        d = (cairo_now - timedelta(days=1)).strftime("%Y-%m-%d")
        start, end = _cairo_date_range_utc(d, d)
        return f"datetime({column}) >= datetime(?) AND datetime({column}) < datetime(?)", [start, end]
    if p == "custom" and date_from and date_to:
        try:
            if date_from > date_to:
                date_from, date_to = date_to, date_from
            start, end = _cairo_date_range_utc(date_from, date_to)
            return f"datetime({column}) >= datetime(?) AND datetime({column}) < datetime(?)", [start, end]
        except (TypeError, ValueError):
            return "1=0", []
    if p == "7d":
        d1 = (cairo_now - timedelta(days=6)).strftime("%Y-%m-%d")
        d2 = cairo_now.strftime("%Y-%m-%d")
        start, end = _cairo_date_range_utc(d1, d2)
        return f"datetime({column}) >= datetime(?) AND datetime({column}) < datetime(?)", [start, end]
    if p == "30d":
        d1 = (cairo_now - timedelta(days=29)).strftime("%Y-%m-%d")
        d2 = cairo_now.strftime("%Y-%m-%d")
        start, end = _cairo_date_range_utc(d1, d2)
        return f"datetime({column}) >= datetime(?) AND datetime({column}) < datetime(?)", [start, end]
    return "1=1", []


def get_admin_report_summary(period: str = "all", date_from: str | None = None, date_to: str | None = None) -> dict:
    """ملخص مالي وتشغيلي للنظام كله، مع دعم فترة تاريخ مخصصة."""
    q_where, q_params = _period_where("created_at", period, date_from, date_to)
    s_where, s_params = _period_where("created_at", period, date_from, date_to)
    r_req_where, req_params = _period_where("requested_at", period, date_from, date_to)
    r_paid_where, paid_params = _period_where("paid_at", period, date_from, date_to)
    with get_conn() as conn:
        users = conn.execute(
            "SELECT COUNT(*) AS c FROM users WHERE program='egypt'"
        ).fetchone()
        active = conn.execute(
            "SELECT COUNT(*) AS c FROM users WHERE program='egypt' AND is_active=1"
        ).fetchone()
        q = conn.execute(f"""
            SELECT COUNT(*) AS products_shown,
                   COALESCE(SUM(epc),0) AS expected_revenue,
                   COALESCE(SUM(CASE WHEN answered=1 AND was_correct=1 THEN reward_value ELSE 0 END),0) AS product_rewards,
                   COALESCE(SUM(CASE WHEN answered=1 AND was_correct=1 THEN 1 ELSE 0 END),0) AS correct_answers
            FROM golden_questions WHERE {q_where}
        """, q_params).fetchone()
        spins = conn.execute(f"""
            SELECT COUNT(*) AS spin_count,
                   COALESCE(SUM(CASE WHEN status='claimed' THEN prize ELSE 0 END),0) AS claimed_prizes
            FROM lucky_spins WHERE {s_where}
        """, s_params).fetchone()
        requested = conn.execute(f"""
            SELECT COUNT(*) AS c, COALESCE(SUM(amount),0) AS total
            FROM redemption_requests WHERE {r_req_where}
        """, req_params).fetchone()
        paid = conn.execute(f"""
            SELECT COUNT(*) AS c, COALESCE(SUM(amount),0) AS total
            FROM redemption_requests WHERE status='paid' AND {r_paid_where}
        """, paid_params).fetchone()
        pending = conn.execute("""
            SELECT COUNT(*) AS c, COALESCE(SUM(amount),0) AS total
            FROM redemption_requests WHERE status IN ('pending','processing')
        """).fetchone()
        balances = conn.execute("""
            SELECT COALESCE(SUM(gift_balance),0) AS total
            FROM users WHERE program='egypt'
        """).fetchone()

        expected_revenue = float(q["expected_revenue"] or 0)
        product_rewards = float(q["product_rewards"] or 0)
        return {
            "users": int(users["c"] or 0),
            "active_users": int(active["c"] or 0),
            "products_shown": int(q["products_shown"] or 0),
            "correct_answers": int(q["correct_answers"] or 0),
            "spin_count": int(spins["spin_count"] or 0),
            "expected_revenue": expected_revenue,
            "product_rewards": product_rewards,
            "claimed_prizes": float(spins["claimed_prizes"] or 0),
            "requested_count": int(requested["c"] or 0),
            "requested_total": float(requested["total"] or 0),
            "paid_count": int(paid["c"] or 0),
            "paid_total": float(paid["total"] or 0),
            "pending_count": int(pending["c"] or 0),
            "pending_total": float(pending["total"] or 0),
            "unrequested_balance": float(balances["total"] or 0),
            "expected_net": expected_revenue - product_rewards,
        }


def list_customer_reports(period: str = "all", search: str = "", limit: int = 200, offset: int = 0, date_from: str | None = None, date_to: str | None = None):
    """قائمة العملاء. فلتر الفترة هنا معناه: العملاء الجدد الذين انضموا في الفترة،
    بينما أرقام النشاط/اللفات/المنتجات المعروضة في الصف تظل إجماليات العميل حتى الآن.
    """
    joined_where, joined_params = _period_where("u.joined_at", period, date_from, date_to)
    search = (search or "").strip()
    like = f"%{search.lstrip('@')}%"
    with get_conn() as conn:
        return conn.execute(f"""
            SELECT u.user_id, u.username, u.is_active, u.gift_balance, u.last_activity_at, u.joined_at,
                   wa.id AS web_account_id, wa.phone_e164, wa.source_first, wa.source_last, wa.telegram_user_id, COALESCE(wa.last_ip, u.last_ip) AS last_ip,
                   COALESCE(wa.is_suspended,0) AS is_suspended, wa.suspended_at, wa.suspended_reason,
                   CASE WHEN wa.id IS NOT NULL THEN 1 ELSE 0 END AS has_web_account,
                   COALESCE(q.products_shown,0) AS products_shown,
                   COALESCE(q.expected_revenue,0) AS expected_revenue,
                   COALESCE(q.product_rewards,0) AS product_rewards,
                   COALESCE(s.spin_count,0) AS spin_count,
                   COALESCE(s.claimed_prizes,0) AS claimed_prizes,
                   COALESCE(r.paid_total,0) AS paid_total,
                   COALESCE(r.pending_total,0) AS pending_total,
                   COALESCE(r.redemption_count,0) AS redemption_count
            FROM users u
            LEFT JOIN web_accounts wa ON wa.user_id=u.user_id
            LEFT JOIN (
                SELECT user_id, COUNT(*) AS products_shown,
                       SUM(epc) AS expected_revenue,
                       SUM(CASE WHEN answered=1 AND was_correct=1 THEN reward_value ELSE 0 END) AS product_rewards
                FROM golden_questions GROUP BY user_id
            ) q ON q.user_id=u.user_id
            LEFT JOIN (
                SELECT user_id, COUNT(*) AS spin_count,
                       SUM(CASE WHEN status='claimed' THEN prize ELSE 0 END) AS claimed_prizes
                FROM lucky_spins GROUP BY user_id
            ) s ON s.user_id=u.user_id
            LEFT JOIN (
                SELECT user_id,
                       COUNT(*) AS redemption_count,
                       SUM(CASE WHEN status='paid' THEN amount ELSE 0 END) AS paid_total,
                       SUM(CASE WHEN status IN ('pending','processing') THEN amount ELSE 0 END) AS pending_total
                FROM redemption_requests GROUP BY user_id
            ) r ON r.user_id=u.user_id
            WHERE u.program='egypt'
              AND {joined_where}
              AND (?='' OR CAST(u.user_id AS TEXT) LIKE ? OR COALESCE(u.username,'') LIKE ? OR COALESCE(wa.phone_e164,'') LIKE ? OR COALESCE(wa.last_ip, u.last_ip, '') LIKE ?)
            ORDER BY CASE WHEN u.last_activity_at IS NOT NULL AND datetime(u.last_activity_at) >= datetime('now','-5 minutes') THEN 0 ELSE 1 END,
                     datetime(u.last_activity_at) DESC, datetime(u.joined_at) DESC, u.user_id DESC
            LIMIT ? OFFSET ?
        """, (*joined_params, search, like, like, like, like, int(limit), int(offset))).fetchall()


def get_customer_list_stats(period: str = "all", date_from: str | None = None, date_to: str | None = None) -> dict:
    """عدد العملاء الجدد في الفترة + عدد الموجودين Online الآن (آخر نشاط خلال 5 دقائق)."""
    joined_where, joined_params = _period_where("joined_at", period, date_from, date_to)
    with get_conn() as conn:
        new_count = conn.execute(
            f"SELECT COUNT(*) AS c FROM users WHERE program='egypt' AND {joined_where}",
            joined_params,
        ).fetchone()["c"] or 0
        online_now = conn.execute("""
            SELECT COUNT(*) AS c FROM users
            WHERE program='egypt' AND last_activity_at IS NOT NULL
              AND datetime(last_activity_at) >= datetime('now','-5 minutes')
        """).fetchone()["c"] or 0
        total = conn.execute("SELECT COUNT(*) AS c FROM users WHERE program='egypt'").fetchone()["c"] or 0
        return {"new_customers": int(new_count), "online_now": int(online_now), "total_customers": int(total)}


def get_customer_report(user_id: int, period: str = "all") -> dict | None:
    q_where, q_params = _period_where("created_at", period)
    s_where, s_params = _period_where("created_at", period)
    with get_conn() as conn:
        u = conn.execute(
            "SELECT * FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        if not u:
            return None
        q = conn.execute(f"""
            SELECT COUNT(*) AS products_shown,
                   COALESCE(SUM(epc),0) AS expected_revenue,
                   COALESCE(SUM(CASE WHEN answered=1 AND was_correct=1 THEN reward_value ELSE 0 END),0) AS product_rewards,
                   COALESCE(SUM(CASE WHEN answered=1 AND was_correct=1 THEN 1 ELSE 0 END),0) AS correct_answers
            FROM golden_questions WHERE user_id=? AND {q_where}
        """, (user_id, *q_params)).fetchone()
        s = conn.execute(f"""
            SELECT COUNT(*) AS spin_count,
                   COALESCE(SUM(CASE WHEN status='claimed' THEN prize ELSE 0 END),0) AS claimed_prizes
            FROM lucky_spins WHERE user_id=? AND {s_where}
        """, (user_id, *s_params)).fetchone()
        r = conn.execute("""
            SELECT COUNT(*) AS redemption_count,
                   COALESCE(SUM(CASE WHEN status='paid' THEN amount ELSE 0 END),0) AS paid_total,
                   COALESCE(SUM(CASE WHEN status IN ('pending','processing') THEN amount ELSE 0 END),0) AS pending_total
            FROM redemption_requests WHERE user_id=?
        """, (user_id,)).fetchone()
        recent_products = conn.execute("""
            SELECT asin, epc, reward_value, answered, was_correct, created_at
            FROM golden_questions WHERE user_id=? ORDER BY id DESC LIMIT 50
        """, (user_id,)).fetchall()
        redeems = conn.execute("""
            SELECT id, amount, status, requested_at, paid_at, gift_code
            FROM redemption_requests WHERE user_id=? ORDER BY id DESC LIMIT 50
        """, (user_id,)).fetchall()
        wa = conn.execute(
            "SELECT id, phone_e164, telegram_user_id, source_first, source_last, created_at, last_login_at FROM web_accounts WHERE user_id=? LIMIT 1",
            (user_id,),
        ).fetchone()
        expected_revenue = float(q["expected_revenue"] or 0)
        product_rewards = float(q["product_rewards"] or 0)
        return {
            "user_id": int(u["user_id"]), "username": u["username"],
            "is_active": int(u["is_active"] or 0),
            "web_account": dict(wa) if wa else None,
            "gift_balance": float(u["gift_balance"] or 0),
            "products_shown": int(q["products_shown"] or 0),
            "correct_answers": int(q["correct_answers"] or 0),
            "expected_revenue": expected_revenue,
            "product_rewards": product_rewards,
            "spin_count": int(s["spin_count"] or 0),
            "claimed_prizes": float(s["claimed_prizes"] or 0),
            "redemption_count": int(r["redemption_count"] or 0),
            "paid_total": float(r["paid_total"] or 0),
            "pending_total": float(r["pending_total"] or 0),
            "expected_net": expected_revenue - product_rewards,
            "recent_products": [dict(x) for x in recent_products],
            "redemptions": [dict(x) for x in redeems],
        }


def list_pending_redemptions_for_web(limit: int = 500):
    with get_conn() as conn:
        return conn.execute("""
            SELECT r.id, r.user_id, r.amount, r.status, r.requested_at,
                   u.username, u.gift_balance
            FROM redemption_requests r
            JOIN users u ON u.user_id=r.user_id
            WHERE r.status IN ('pending','processing')
            ORDER BY r.requested_at ASC, r.id ASC
            LIMIT ?
        """, (int(limit),)).fetchall()

# ---------- حساب وفر كاش المستقل (Web App / multi-platform) ----------

WEB_ACCOUNT_SCHEMA = """
CREATE TABLE IF NOT EXISTS web_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phone_e164 TEXT NOT NULL UNIQUE,
    user_id INTEGER NOT NULL UNIQUE,
    telegram_user_id INTEGER UNIQUE,
    source_first TEXT DEFAULT 'direct',
    source_last TEXT DEFAULT 'direct',
    created_at TEXT NOT NULL,
    last_login_at TEXT,
    last_ip TEXT,
    is_suspended INTEGER NOT NULL DEFAULT 0,
    suspended_at TEXT,
    suspended_reason TEXT,
    reactivated_at TEXT,
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);
CREATE TABLE IF NOT EXISTS web_sessions (
    token_hash TEXT PRIMARY KEY,
    account_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    FOREIGN KEY (account_id) REFERENCES web_accounts(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS web_dev_otps (
    phone_e164 TEXT PRIMARY KEY,
    code_hash TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS telegram_link_codes (
    token_hash TEXT PRIMARY KEY,
    account_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    FOREIGN KEY (account_id) REFERENCES web_accounts(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS web_source_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    seen_at TEXT NOT NULL,
    FOREIGN KEY (account_id) REFERENCES web_accounts(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS web_auth_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    cairo_day TEXT NOT NULL,
    FOREIGN KEY (account_id) REFERENCES web_accounts(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_web_sessions_account ON web_sessions(account_id);
CREATE INDEX IF NOT EXISTS idx_web_sessions_expires ON web_sessions(expires_at);
CREATE INDEX IF NOT EXISTS idx_web_source_account ON web_source_events(account_id, seen_at);
CREATE INDEX IF NOT EXISTS idx_web_auth_events_account_day ON web_auth_events(account_id, cairo_day, event_type);
CREATE INDEX IF NOT EXISTS idx_link_codes_account ON telegram_link_codes(account_id, expires_at);
"""


def init_web_accounts_schema():
    with get_conn() as conn:
        conn.executescript(WEB_ACCOUNT_SCHEMA)
        # Safe migrations for existing Railway databases.
        for stmt in (
            "ALTER TABLE web_accounts ADD COLUMN is_suspended INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE web_accounts ADD COLUMN suspended_at TEXT",
            "ALTER TABLE web_accounts ADD COLUMN suspended_reason TEXT",
            "ALTER TABLE web_accounts ADD COLUMN reactivated_at TEXT",
            "ALTER TABLE web_accounts ADD COLUMN last_ip TEXT",
        ):
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError:
                pass


def _safe_source(source: str | None) -> str:
    raw = (source or "direct").strip().lower()[:40]
    allowed = "abcdefghijklmnopqrstuvwxyz0123456789_-"
    cleaned = "".join(ch for ch in raw if ch in allowed)
    return cleaned or "direct"


def get_or_create_web_account(phone_e164: str, source: str = "direct") -> dict:
    """Creates a Wafr account with a synthetic negative user_id so old bot tables keep working."""
    source = _safe_source(source)
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM web_accounts WHERE phone_e164=?", (phone_e164,)).fetchone()
        if row:
            conn.execute(
                "UPDATE web_accounts SET source_last=?, last_login_at=? WHERE id=?",
                (source, now, row["id"]),
            )
            conn.execute(
                "INSERT INTO web_source_events(account_id, source, seen_at) VALUES(?,?,?)",
                (row["id"], source, now),
            )
            fresh = conn.execute("SELECT * FROM web_accounts WHERE id=?", (row["id"],)).fetchone()
            return dict(fresh)

        # Generate a collision-safe negative ID reserved for web-only customers.
        seed = conn.execute("SELECT COALESCE(MAX(id),0)+1 AS n FROM web_accounts").fetchone()["n"]
        synthetic = -(10_000_000_000 + int(seed))
        while conn.execute("SELECT 1 FROM users WHERE user_id=?", (synthetic,)).fetchone():
            synthetic -= 1
        conn.execute(
            """INSERT INTO users(user_id, username, joined_at, is_active, program, golden_target)
               VALUES(?, NULL, ?, 1, 'egypt', ?)""",
            (synthetic, now, GOLDEN_TARGET_COUNT),
        )
        cur = conn.execute(
            """INSERT INTO web_accounts(phone_e164,user_id,source_first,source_last,created_at,last_login_at)
               VALUES(?,?,?,?,?,?)""",
            (phone_e164, synthetic, source, source, now, now),
        )
        account_id = cur.lastrowid
        conn.execute(
            "INSERT INTO web_source_events(account_id, source, seen_at) VALUES(?,?,?)",
            (account_id, source, now),
        )
        row = conn.execute("SELECT * FROM web_accounts WHERE id=?", (account_id,)).fetchone()
        return dict(row)


def set_web_account_last_ip(account_id: int, ip_address: str | None):
    """Store the latest public IP seen for this Web account (admin visibility only)."""
    ip_address = (ip_address or "").strip()[:64]
    if not ip_address:
        return
    with get_conn() as conn:
        conn.execute(
            "UPDATE web_accounts SET last_ip=? WHERE id=?",
            (ip_address, int(account_id)),
        )


def create_web_session(token_hash: str, account_id: int, expires_at: str):
    with get_conn() as conn:
        now = datetime.utcnow().isoformat()
        conn.execute("DELETE FROM web_sessions WHERE expires_at < ?", (now,))
        conn.execute(
            "INSERT OR REPLACE INTO web_sessions(token_hash,account_id,created_at,expires_at) VALUES(?,?,?,?)",
            (token_hash, account_id, now, expires_at),
        )


def get_web_account_by_session(token_hash: str, now_iso: str):
    with get_conn() as conn:
        row = conn.execute(
            """SELECT a.*, u.gift_balance, u.points_balance, u.spins_balance
               FROM web_sessions s
               JOIN web_accounts a ON a.id=s.account_id
               JOIN users u ON u.user_id=a.user_id
               WHERE s.token_hash=? AND s.expires_at>=?""",
            (token_hash, now_iso),
        ).fetchone()
        return dict(row) if row else None


def extend_web_session(token_hash: str, expires_at: str):
    """Extend an already-valid web session (sliding login)."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE web_sessions SET expires_at=? WHERE token_hash=?",
            (expires_at, token_hash),
        )


def delete_web_session(token_hash: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM web_sessions WHERE token_hash=?", (token_hash,))


def save_dev_otp(phone_e164: str, code_hash: str, expires_at: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO web_dev_otps(phone_e164,code_hash,expires_at) VALUES(?,?,?)",
            (phone_e164, code_hash, expires_at),
        )


def get_dev_otp(phone_e164: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM web_dev_otps WHERE phone_e164=?", (phone_e164,)).fetchone()
        return dict(row) if row else None


def delete_dev_otp(phone_e164: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM web_dev_otps WHERE phone_e164=?", (phone_e164,))


def create_telegram_link_code(account_id: int, token_hash: str, expires_at: str):
    with get_conn() as conn:
        now = datetime.utcnow().isoformat()
        conn.execute("DELETE FROM telegram_link_codes WHERE account_id=? OR expires_at<?", (account_id, now))
        conn.execute(
            "INSERT INTO telegram_link_codes(token_hash,account_id,created_at,expires_at) VALUES(?,?,?,?)",
            (token_hash, account_id, now, expires_at),
        )


def consume_telegram_link_code(token_hash: str, telegram_user_id: int, now_iso: str) -> tuple[bool, str]:
    """Link a web account to Telegram and make Telegram ID the canonical user ID."""
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        code = conn.execute(
            """SELECT * FROM telegram_link_codes
               WHERE token_hash=? AND used_at IS NULL AND expires_at>=?""",
            (token_hash, now_iso),
        ).fetchone()
        if not code:
            return False, "الكود غير صحيح أو انتهت صلاحيته"
        account = conn.execute("SELECT * FROM web_accounts WHERE id=?", (code["account_id"],)).fetchone()
        if not account:
            return False, "حساب وفر كاش غير موجود"
        other = conn.execute(
            "SELECT id FROM web_accounts WHERE telegram_user_id=? AND id<>?",
            (telegram_user_id, account["id"]),
        ).fetchone()
        if other:
            return False, "حساب Telegram ده مربوط بالفعل بحساب وفر كاش تاني"

        target = conn.execute("SELECT * FROM users WHERE user_id=?", (telegram_user_id,)).fetchone()
        if not target:
            conn.execute(
                """INSERT INTO users(user_id,username,joined_at,is_active,program,golden_target)
                   VALUES(?,NULL,?,1,'egypt',?)""",
                (telegram_user_id, now_iso, GOLDEN_TARGET_COUNT),
            )
            target = conn.execute("SELECT * FROM users WHERE user_id=?", (telegram_user_id,)).fetchone()

        source_uid = int(account["user_id"])
        if source_uid != telegram_user_id:
            src = conn.execute("SELECT * FROM users WHERE user_id=?", (source_uid,)).fetchone()
            if src:
                # Preserve monetary/reward balances accumulated on the standalone web account.
                conn.execute(
                    """UPDATE users SET
                       gift_balance = COALESCE(gift_balance,0) + ?,
                       points_balance = COALESCE(points_balance,0) + ?,
                       spins_balance = COALESCE(spins_balance,0) + ?,
                       golden_opened_count = COALESCE(golden_opened_count,0) + ?,
                       golden_answered_count = COALESCE(golden_answered_count,0) + ?,
                       golden_round_earnings = COALESCE(golden_round_earnings,0) + ?,
                       is_active = 1, program='egypt'
                       WHERE user_id=?""",
                    (
                        float(src["gift_balance"] or 0), int(src["points_balance"] or 0),
                        int(src["spins_balance"] or 0), int(src["golden_opened_count"] or 0),
                        int(src["golden_answered_count"] or 0), float(src["golden_round_earnings"] or 0),
                        telegram_user_id,
                    ),
                )
                # Tables with no uniqueness conflict.
                for table in ("wheel_spins", "sent_offer_messages", "golden_questions", "lucky_spins", "redemption_requests"):
                    conn.execute(f"UPDATE {table} SET user_id=? WHERE user_id=?", (telegram_user_id, source_uid))
                # Composite primary key can conflict; copy safely then remove old rows.
                conn.execute(
                    """INSERT OR IGNORE INTO golden_quiz_log(user_id,asin,quiz_date)
                       SELECT ?,asin,quiz_date FROM golden_quiz_log WHERE user_id=?""",
                    (telegram_user_id, source_uid),
                )
                conn.execute("DELETE FROM golden_quiz_log WHERE user_id=?", (source_uid,))
                # Move the web-account foreign key before deleting the synthetic user.
                conn.execute(
                    "UPDATE web_accounts SET user_id=?, telegram_user_id=? WHERE id=?",
                    (telegram_user_id, telegram_user_id, account["id"]),
                )
                conn.execute("DELETE FROM users WHERE user_id=?", (source_uid,))

        if source_uid == telegram_user_id:
            conn.execute(
                "UPDATE web_accounts SET user_id=?, telegram_user_id=? WHERE id=?",
                (telegram_user_id, telegram_user_id, account["id"]),
            )
        conn.execute("UPDATE telegram_link_codes SET used_at=? WHERE token_hash=?", (now_iso, token_hash))
        return True, "تم ربط Telegram بحساب وفر كاش بنجاح"


# ---------- عروض الويب / تتبع الضغطات ----------

WEB_OFFERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS web_offer_clicks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    deal_id INTEGER NOT NULL,
    link_index INTEGER NOT NULL DEFAULT 0,
    clicked_at TEXT NOT NULL,
    source TEXT DEFAULT 'direct',
    FOREIGN KEY (account_id) REFERENCES web_accounts(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_web_offer_clicks_account ON web_offer_clicks(account_id, clicked_at);
CREATE INDEX IF NOT EXISTS idx_web_offer_clicks_deal ON web_offer_clicks(deal_id, clicked_at);
"""


def init_web_offers_schema():
    with get_conn() as conn:
        conn.executescript(WEB_OFFERS_SCHEMA)


def list_web_offers_for_user(user_id: int, limit: int = 20, min_display: int = 8):
    """
    ترجع عروض الويب مع نافذة ثابتة لا تقل عن 8 عروض قدر الإمكان.

    الفكرة:
    - العروض الجديدة لا تمسح القديمة من الشاشة فورًا.
    - لو فيه عرض جديد واحد فقط، نضيف له أحدث 7 عروض أقدم ليظل الإجمالي 8.
    - لو فيه 8 عروض جديدة أو أكثر، نعرض الجديدة (بحد أقصى limit).
    - لو مفيش جديد، نعرض أحدث 8 عروض موجودة.
    - دفعة Telegram الـ pending تظل محترمة، ولو أقل من 8 نكمّلها بعروض أقدم.

    النتيجة: (rows, mode, seen_up_to_id)
    """
    limit = max(8, min(int(limit or 20), 50))
    min_display = max(1, min(int(min_display or 8), limit))

    def _fill_recent(conn, rows, needed):
        if needed <= 0:
            return list(rows)
        existing_ids = {int(r["id"]) for r in rows}
        # نجيب أحدث عروض إضافية، ثم نرتب الكل تنازليًا: الأحدث يظهر أول الصفحة.
        extra = conn.execute(
            "SELECT * FROM deals_cache ORDER BY id DESC LIMIT ?",
            (max(needed + len(existing_ids) + 10, min_display * 2),),
        ).fetchall()
        out = list(rows)
        for r in extra:
            rid = int(r["id"])
            if rid in existing_ids:
                continue
            out.append(r)
            existing_ids.add(rid)
            if len(out) >= min_display:
                break
        out.sort(key=lambda r: int(r["id"]), reverse=True)
        return out[:limit]

    with get_conn() as conn:
        pending = conn.execute(
            "SELECT pending_offer_from_id, pending_offer_to_id FROM users WHERE user_id=?",
            (int(user_id),),
        ).fetchone()

        if pending and pending["pending_offer_from_id"] is not None and pending["pending_offer_to_id"] is not None:
            primary = conn.execute(
                """SELECT * FROM deals_cache
                   WHERE id BETWEEN ? AND ?
                   ORDER BY id DESC LIMIT ?""",
                (int(pending["pending_offer_from_id"]), int(pending["pending_offer_to_id"]), limit),
            ).fetchall()
            seen_up_to = max((int(r["id"]) for r in primary), default=None)
            rows = _fill_recent(conn, primary, min_display - len(primary))
            return rows, "pending", seen_up_to

        user = conn.execute(
            "SELECT last_seen_deal_id FROM users WHERE user_id=?", (int(user_id),)
        ).fetchone()
        last_seen = int((user["last_seen_deal_id"] if user else 0) or 0)
        new_rows = conn.execute(
            "SELECT * FROM deals_cache WHERE id>? ORDER BY id DESC LIMIT ?",
            (last_seen, limit),
        ).fetchall()

        if new_rows:
            seen_up_to = max(int(r["id"]) for r in new_rows)
            rows = _fill_recent(conn, new_rows, min_display - len(new_rows))
            return rows, "new", seen_up_to

        recent = conn.execute(
            "SELECT * FROM deals_cache ORDER BY id DESC LIMIT ?",
            (min_display,),
        ).fetchall()
        rows = list(recent)
        return rows, "recent", None

def mark_web_offers_seen(user_id: int, mode: str, seen_up_to_id: int | None):
    if not seen_up_to_id:
        return
    with get_conn() as conn:
        conn.execute(
            """UPDATE users
               SET last_seen_deal_id = CASE
                   WHEN COALESCE(last_seen_deal_id,0) < ? THEN ? ELSE last_seen_deal_id END
               WHERE user_id=?""",
            (int(seen_up_to_id), int(seen_up_to_id), int(user_id)),
        )
        if mode == "pending":
            row = conn.execute(
                "SELECT pending_offer_to_id FROM users WHERE user_id=?", (int(user_id),)
            ).fetchone()
            if row and row["pending_offer_to_id"] is not None and int(seen_up_to_id) >= int(row["pending_offer_to_id"]):
                conn.execute(
                    "UPDATE users SET pending_offer_from_id=NULL, pending_offer_to_id=NULL WHERE user_id=?",
                    (int(user_id),),
                )


def record_web_offer_click(account_id: int, user_id: int, deal_id: int, link_index: int, source: str = "direct"):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO web_offer_clicks(account_id,user_id,deal_id,link_index,clicked_at,source)
               VALUES(?,?,?,?,?,?)""",
            (int(account_id), int(user_id), int(deal_id), int(link_index), datetime.utcnow().isoformat(), _safe_source(source)),
        )


def get_deal_by_id(deal_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM deals_cache WHERE id=?", (int(deal_id),)).fetchone()
        return dict(row) if row else None


# ---------- واجهة العميل: الاستبدال وحسابي ----------

def get_web_account_for_user(user_id: int):
    """بيانات حساب الويب المرتبط بالـ user_id، لو موجود."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM web_accounts WHERE user_id=? LIMIT 1", (user_id,)
        ).fetchone()
        return dict(row) if row else None


def get_web_redemption_status(user_id: int) -> dict:
    """ملخص آمن للعميل: المتاح للاستبدال والطلبات المفتوحة."""
    with get_conn() as conn:
        user = conn.execute(
            "SELECT gift_balance FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        balance = float(user["gift_balance"] or 0) if user else 0.0
        redeemable = int(balance + 1e-9)
        remainder = round(balance - redeemable, 6)
        open_rows = conn.execute(
            """SELECT id, amount, status, requested_at
               FROM redemption_requests
               WHERE user_id=? AND status IN ('pending','processing')
               ORDER BY id DESC LIMIT 20""",
            (user_id,),
        ).fetchall()
        return {
            "balance": balance,
            "redeemable": redeemable,
            "remainder": remainder,
            "open_requests": [dict(x) for x in open_rows],
        }


def get_web_account_history(user_id: int, limit: int = 20) -> dict:
    """ملخص النشاط الذي يجوز عرضه للعميل داخل صفحة حسابي."""
    limit = max(1, min(int(limit), 50))
    with get_conn() as conn:
        u = conn.execute(
            """SELECT user_id, gift_balance, points_balance, spins_balance,
                      golden_answered_count, golden_opened_count
               FROM users WHERE user_id=?""",
            (user_id,),
        ).fetchone()
        if not u:
            return {}
        spin = conn.execute(
            """SELECT COUNT(*) AS c,
                      COALESCE(SUM(CASE WHEN status='claimed' THEN prize ELSE 0 END),0) AS total
               FROM lucky_spins WHERE user_id=?""",
            (user_id,),
        ).fetchone()
        red = conn.execute(
            """SELECT COUNT(*) AS c,
                      COALESCE(SUM(CASE WHEN status='paid' THEN amount ELSE 0 END),0) AS paid_total,
                      COALESCE(SUM(CASE WHEN status IN ('pending','processing') THEN amount ELSE 0 END),0) AS pending_total
               FROM redemption_requests WHERE user_id=?""",
            (user_id,),
        ).fetchone()
        redeems = conn.execute(
            """SELECT id, amount, status, requested_at, paid_at, gift_code
               FROM redemption_requests WHERE user_id=?
               ORDER BY id DESC LIMIT ?""",
            (user_id, limit),
        ).fetchall()
        prizes = conn.execute(
            """SELECT id, prize, status, created_at, claimed_at
               FROM lucky_spins WHERE user_id=?
               ORDER BY id DESC LIMIT ?""",
            (user_id, limit),
        ).fetchall()
        return {
            "gift_balance": float(u["gift_balance"] or 0),
            "points_balance": int(u["points_balance"] or 0),
            "spins_balance": int(u["spins_balance"] or 0),
            "questions_answered": int(u["golden_answered_count"] or 0),
            "correct_answers": int(u["golden_opened_count"] or 0),
            "prize_count": int(spin["c"] or 0),
            "claimed_prizes": float(spin["total"] or 0),
            "redemption_count": int(red["c"] or 0),
            "paid_total": float(red["paid_total"] or 0),
            "pending_total": float(red["pending_total"] or 0),
            "redemptions": [dict(x) for x in redeems],
            "prizes": [dict(x) for x in prizes],
        }


# ---------- لوحة الإدارة المركزية: Telegram + Web ----------


def _cairo_day_key() -> str:
    """Calendar day in Cairo, including DST when zoneinfo is available."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Africa/Cairo")).date().isoformat()
    except Exception:
        # Conservative fallback for the Railway runtime.
        return (datetime.utcnow() + timedelta(hours=3)).date().isoformat()


def get_web_account_by_phone(phone_e164: str):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM web_accounts WHERE phone_e164=? LIMIT 1",
            (phone_e164,),
        ).fetchone()
        return dict(row) if row else None


def get_web_account_by_id(account_id: int):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM web_accounts WHERE id=? LIMIT 1",
            (int(account_id),),
        ).fetchone()
        return dict(row) if row else None


def record_web_login(account_id: int) -> dict:
    """Record a successful OTP login. Returns today's login/logout counters."""
    now = datetime.utcnow().isoformat()
    day = _cairo_day_key()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO web_auth_events(account_id,event_type,occurred_at,cairo_day) VALUES(?,?,?,?)",
            (int(account_id), "login", now, day),
        )
        row = conn.execute(
            """SELECT
                 SUM(CASE WHEN event_type='login' THEN 1 ELSE 0 END) AS logins,
                 SUM(CASE WHEN event_type='logout' THEN 1 ELSE 0 END) AS logouts
               FROM web_auth_events
               WHERE account_id=? AND cairo_day=?""",
            (int(account_id), day),
        ).fetchone()
        logins = int(row["logins"] or 0)
        logouts = int(row["logouts"] or 0)
        return {"logins": logins, "logouts": logouts, "cycles": min(logins, logouts)}


def record_web_logout_and_maybe_suspend(account_id: int, threshold: int = 3) -> dict:
    """Suspend after 3 completed login/logout cycles during the same Cairo day."""
    now = datetime.utcnow().isoformat()
    day = _cairo_day_key()
    reason = "3_login_logout_cycles_same_day"
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        account = conn.execute(
            "SELECT * FROM web_accounts WHERE id=? LIMIT 1",
            (int(account_id),),
        ).fetchone()
        if not account:
            return {"suspended": False, "cycles": 0}

        if int(account["is_suspended"] or 0):
            return {"suspended": True, "cycles": int(threshold)}

        conn.execute(
            "INSERT INTO web_auth_events(account_id,event_type,occurred_at,cairo_day) VALUES(?,?,?,?)",
            (int(account_id), "logout", now, day),
        )
        row = conn.execute(
            """SELECT
                 SUM(CASE WHEN event_type='login' THEN 1 ELSE 0 END) AS logins,
                 SUM(CASE WHEN event_type='logout' THEN 1 ELSE 0 END) AS logouts
               FROM web_auth_events
               WHERE account_id=? AND cairo_day=?""",
            (int(account_id), day),
        ).fetchone()
        logins = int(row["logins"] or 0)
        logouts = int(row["logouts"] or 0)
        cycles = min(logins, logouts)

        suspended = cycles >= int(threshold)
        if suspended:
            conn.execute(
                """UPDATE web_accounts
                   SET is_suspended=1, suspended_at=?, suspended_reason=?
                   WHERE id=?""",
                (now, reason, int(account_id)),
            )
            # Kill every browser session immediately.
            conn.execute("DELETE FROM web_sessions WHERE account_id=?", (int(account_id),))

        return {
            "suspended": bool(suspended),
            "cycles": cycles,
            "logins": logins,
            "logouts": logouts,
        }


def list_suspended_web_accounts(limit: int = 500):
    limit = max(1, min(int(limit or 500), 1000))
    day = _cairo_day_key()
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT wa.id AS account_id, wa.user_id, wa.phone_e164,
                      wa.telegram_user_id, wa.source_first, wa.source_last,
                      wa.suspended_at, wa.suspended_reason,
                      COALESCE(SUM(CASE WHEN e.cairo_day=? AND e.event_type='login' THEN 1 ELSE 0 END),0) AS today_logins,
                      COALESCE(SUM(CASE WHEN e.cairo_day=? AND e.event_type='logout' THEN 1 ELSE 0 END),0) AS today_logouts
               FROM web_accounts wa
               LEFT JOIN web_auth_events e ON e.account_id=wa.id
               WHERE wa.is_suspended=1
               GROUP BY wa.id
               ORDER BY wa.suspended_at DESC
               LIMIT ?""",
            (day, day, limit),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["today_cycles"] = min(int(item["today_logins"] or 0), int(item["today_logouts"] or 0))
            result.append(item)
        return result



def suspend_web_account_by_user_id(user_id: int, reason: str = "manual_admin"):
    """Manually suspend a Web account from the admin Customers page.

    Existing browser sessions are deleted immediately.  The same suspended
    flag used by the automatic 3-login/logout rule is used, so the customer
    sees exactly the same protection message on the next login attempt.
    """
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM web_accounts WHERE user_id=? LIMIT 1",
            (int(user_id),),
        ).fetchone()
        if not row:
            return None

        account_id = int(row["id"])
        conn.execute(
            """UPDATE web_accounts
               SET is_suspended=1, suspended_at=?, suspended_reason=?
               WHERE id=?""",
            (now, str(reason or "manual_admin"), account_id),
        )
        # Log out the customer immediately on every browser/device.
        conn.execute("DELETE FROM web_sessions WHERE account_id=?", (account_id,))

        fresh = conn.execute(
            "SELECT * FROM web_accounts WHERE id=? LIMIT 1",
            (account_id,),
        ).fetchone()
        return dict(fresh) if fresh else None


def reactivate_web_account(account_id: int):
    """Reactivate and reset today's login/logout counter so they get a fresh start."""
    now = datetime.utcnow().isoformat()
    day = _cairo_day_key()
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM web_accounts WHERE id=? LIMIT 1",
            (int(account_id),),
        ).fetchone()
        if not row:
            return None
        conn.execute(
            """UPDATE web_accounts
               SET is_suspended=0, suspended_at=NULL, suspended_reason=NULL, reactivated_at=?
               WHERE id=?""",
            (now, int(account_id)),
        )
        conn.execute(
            "DELETE FROM web_auth_events WHERE account_id=? AND cairo_day=?",
            (int(account_id), day),
        )
        # Require a fresh login after admin reactivation.
        conn.execute("DELETE FROM web_sessions WHERE account_id=?", (int(account_id),))
        fresh = conn.execute(
            "SELECT * FROM web_accounts WHERE id=? LIMIT 1",
            (int(account_id),),
        ).fetchone()
        return dict(fresh) if fresh else None


def count_suspended_web_accounts() -> int:
    with get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM web_accounts WHERE is_suspended=1").fetchone()
        return int(row["c"] or 0)

def get_central_admin_summary(period: str = "all") -> dict:
    """ملخص مركزي يجمع نشاط Telegram وحسابات الويب في شاشة واحدة."""
    web_where, web_params = _period_where("created_at", period)
    login_where, login_params = _period_where("seen_at", period)
    click_where, click_params = _period_where("clicked_at", period)
    spin_where, spin_params = _period_where("created_at", period)
    redeem_where, redeem_params = _period_where("requested_at", period)
    with get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) c FROM users WHERE program='egypt'").fetchone()["c"] or 0
        web = conn.execute(f"SELECT COUNT(*) c FROM web_accounts WHERE {web_where}", web_params).fetchone()["c"] or 0
        web_all = conn.execute("SELECT COUNT(*) c FROM web_accounts").fetchone()["c"] or 0
        linked = conn.execute("SELECT COUNT(*) c FROM web_accounts WHERE telegram_user_id IS NOT NULL").fetchone()["c"] or 0
        web_only = conn.execute("SELECT COUNT(*) c FROM web_accounts WHERE telegram_user_id IS NULL").fetchone()["c"] or 0
        tg_only = conn.execute("""SELECT COUNT(*) c FROM users u
            LEFT JOIN web_accounts wa ON wa.user_id=u.user_id
            WHERE u.program='egypt' AND wa.id IS NULL AND u.user_id>0""").fetchone()["c"] or 0
        logins = conn.execute(f"SELECT COUNT(*) c FROM web_source_events WHERE {login_where}", login_params).fetchone()["c"] or 0
        clicks = conn.execute(f"SELECT COUNT(*) c FROM web_offer_clicks WHERE {click_where}", click_params).fetchone()["c"] or 0
        unique_clickers = conn.execute(f"SELECT COUNT(DISTINCT account_id) c FROM web_offer_clicks WHERE {click_where}", click_params).fetchone()["c"] or 0
        spins = conn.execute(f"SELECT COUNT(*) c FROM lucky_spins WHERE {spin_where}", spin_params).fetchone()["c"] or 0
        claims = conn.execute(f"SELECT COALESCE(SUM(CASE WHEN status='claimed' THEN prize ELSE 0 END),0) s FROM lucky_spins WHERE {spin_where}", spin_params).fetchone()["s"] or 0
        redeems = conn.execute(f"SELECT COUNT(*) c, COALESCE(SUM(amount),0) s FROM redemption_requests WHERE {redeem_where}", redeem_params).fetchone()
        pending = conn.execute("SELECT COUNT(*) c, COALESCE(SUM(amount),0) s FROM redemption_requests WHERE status IN ('pending','processing')").fetchone()
        paid = conn.execute("SELECT COUNT(*) c, COALESCE(SUM(amount),0) s FROM redemption_requests WHERE status='paid'").fetchone()
        sources = conn.execute(f"""SELECT source, COUNT(*) events, COUNT(DISTINCT account_id) accounts
            FROM web_source_events WHERE {login_where}
            GROUP BY source ORDER BY accounts DESC, events DESC LIMIT 20""", login_params).fetchall()
        return {
            "users_total": int(total), "web_accounts_period": int(web), "web_accounts_total": int(web_all),
            "telegram_linked": int(linked), "web_only": int(web_only), "telegram_only": int(tg_only),
            "web_visits": int(logins), "offer_clicks": int(clicks), "unique_clickers": int(unique_clickers),
            "spin_count": int(spins), "claimed_prizes": float(claims),
            "redemption_count": int(redeems["c"] or 0), "redemption_total": float(redeems["s"] or 0),
            "pending_count": int(pending["c"] or 0), "pending_total": float(pending["s"] or 0),
            "paid_count": int(paid["c"] or 0), "paid_total": float(paid["s"] or 0),
            "sources": [dict(x) for x in sources],
        }


def list_central_customers(period: str = "all", search: str = "", limit: int = 500, offset: int = 0, date_from: str | None = None, date_to: str | None = None):
    """قائمة العملاء المركزية؛ الفترة تخص تاريخ انضمام العميل."""
    return list_customer_reports(period, search, limit, offset, date_from, date_to)



def get_admin_funnel(period: str = "all") -> dict:
    """مسار العميل النشط على الويب خلال الفترة المختارة.

    ملاحظة: النظام الحالي لا يسجل الزائر المجهول قبل إنشاء الحساب، لذلك أول خطوة هنا
    هي الحسابات التي ظهر لها نشاط Web مسجل بالفعل خلال الفترة.
    """
    event_where, event_params = _period_where("e.seen_at", period)
    click_where, click_params = _period_where("c.clicked_at", period)
    spin_where, spin_params = _period_where("ls.created_at", period)
    redeem_where, redeem_params = _period_where("r.requested_at", period)
    paid_where, paid_params = _period_where("r.paid_at", period)
    with get_conn() as conn:
        active = conn.execute(f"""
            SELECT COUNT(DISTINCT e.account_id) AS c
            FROM web_source_events e
            WHERE {event_where}
        """, event_params).fetchone()["c"] or 0
        linked = conn.execute(f"""
            SELECT COUNT(DISTINCT e.account_id) AS c
            FROM web_source_events e
            JOIN web_accounts wa ON wa.id=e.account_id
            WHERE {event_where} AND wa.telegram_user_id IS NOT NULL
        """, event_params).fetchone()["c"] or 0
        clickers = conn.execute(f"""
            SELECT COUNT(DISTINCT c.account_id) AS c
            FROM web_offer_clicks c
            WHERE {click_where}
        """, click_params).fetchone()["c"] or 0
        players = conn.execute(f"""
            SELECT COUNT(DISTINCT wa.id) AS c
            FROM lucky_spins ls
            JOIN web_accounts wa ON wa.user_id=ls.user_id
            WHERE {spin_where}
        """, spin_params).fetchone()["c"] or 0
        winners = conn.execute(f"""
            SELECT COUNT(DISTINCT wa.id) AS c
            FROM lucky_spins ls
            JOIN web_accounts wa ON wa.user_id=ls.user_id
            WHERE {spin_where} AND ls.status='claimed'
        """, spin_params).fetchone()["c"] or 0
        requested = conn.execute(f"""
            SELECT COUNT(DISTINCT wa.id) AS c
            FROM redemption_requests r
            JOIN web_accounts wa ON wa.user_id=r.user_id
            WHERE {redeem_where}
        """, redeem_params).fetchone()["c"] or 0
        paid = conn.execute(f"""
            SELECT COUNT(DISTINCT wa.id) AS c
            FROM redemption_requests r
            JOIN web_accounts wa ON wa.user_id=r.user_id
            WHERE r.status='paid' AND {paid_where}
        """, paid_params).fetchone()["c"] or 0

        return {
            "active_web_accounts": int(active),
            "telegram_linked_active": int(linked),
            "offer_clickers": int(clickers),
            "players": int(players),
            "winners": int(winners),
            "redeem_requesters": int(requested),
            "paid_redeemers": int(paid),
        }


def get_admin_alerts(limit: int = 30) -> dict:
    """تنبيهات عملية تحتاج متابعة من الإدارة، بدون تعديل أي بيانات."""
    limit = max(5, min(int(limit or 30), 100))
    with get_conn() as conn:
        overdue = conn.execute("""
            SELECT r.id AS request_id, r.user_id, r.amount, r.status, r.requested_at,
                   u.username,
                   CAST((julianday('now') - julianday(r.requested_at)) * 24 AS INTEGER) AS age_hours
            FROM redemption_requests r
            JOIN users u ON u.user_id=r.user_id
            WHERE r.status IN ('pending','processing')
              AND datetime(r.requested_at) <= datetime('now','-24 hours')
            ORDER BY r.requested_at ASC
            LIMIT ?
        """, (limit,)).fetchall()

        paid_missing_code = conn.execute("""
            SELECT r.id AS request_id, r.user_id, r.amount, r.paid_at, u.username
            FROM redemption_requests r
            JOIN users u ON u.user_id=r.user_id
            WHERE r.status='paid' AND (r.gift_code IS NULL OR trim(r.gift_code)='')
            ORDER BY COALESCE(r.paid_at,r.requested_at) ASC
            LIMIT ?
        """, (limit,)).fetchall()

        ready = conn.execute("""
            SELECT u.user_id, u.username, u.gift_balance,
                   wa.phone_e164, wa.source_last
            FROM users u
            LEFT JOIN web_accounts wa ON wa.user_id=u.user_id
            WHERE u.program='egypt'
              AND COALESCE(u.gift_balance,0) >= 1
              AND NOT EXISTS (
                  SELECT 1 FROM redemption_requests r
                  WHERE r.user_id=u.user_id AND r.status IN ('pending','processing')
              )
            ORDER BY u.gift_balance DESC, u.user_id DESC
            LIMIT ?
        """, (limit,)).fetchall()

        clicked_no_spin = conn.execute("""
            SELECT wa.user_id, u.username, wa.phone_e164, wa.source_last,
                   COUNT(c.id) AS clicks,
                   MAX(c.clicked_at) AS last_click
            FROM web_accounts wa
            JOIN users u ON u.user_id=wa.user_id
            JOIN web_offer_clicks c ON c.account_id=wa.id
            WHERE datetime(c.clicked_at) >= datetime('now','-7 days')
              AND NOT EXISTS (
                  SELECT 1 FROM lucky_spins ls
                  WHERE ls.user_id=wa.user_id
                    AND datetime(ls.created_at) >= datetime('now','-7 days')
              )
            GROUP BY wa.id, wa.user_id, u.username, wa.phone_e164, wa.source_last
            ORDER BY clicks DESC, last_click DESC
            LIMIT ?
        """, (limit,)).fetchall()

        return {
            "counts": {
                "overdue_redemptions": len(overdue),
                "paid_missing_code": len(paid_missing_code),
                "ready_to_redeem": len(ready),
                "clicked_no_spin": len(clicked_no_spin),
            },
            "overdue_redemptions": [dict(x) for x in overdue],
            "paid_missing_code": [dict(x) for x in paid_missing_code],
            "ready_to_redeem": [dict(x) for x in ready],
            "clicked_no_spin": [dict(x) for x in clicked_no_spin],
        }
