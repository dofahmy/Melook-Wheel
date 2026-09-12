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
from datetime import datetime, timedelta

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
    "ALTER TABLE lucky_spins ADD COLUMN prize_index INTEGER DEFAULT 0",
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
        conn.executescript(SCHEMA)
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
        """)
        conn.execute("PRAGMA optimize")


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
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET last_activity_at = ? WHERE user_id = ?",
            (datetime.utcnow().isoformat(), user_id),
        )


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


# ---------- عروض القناة الذهبية (Golden Deals) ----------

MAX_CACHED_GOLDEN_DEALS = 50
GOLDEN_TARGET_COUNT = config.EGYPT_GOLDEN_QUESTIONS_PER_ROUND

# ---------- عجلة الحظ (Lucky Wheel) - اختيار الجايزة من الـ backend ----------
# القيم والاحتمالات دي متفق عليها معاكِ: EV = 1.28 جنيه لكل لفة (RTP = 32%
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


def create_golden_question(
    user_id: int,
    asin: str,
    question_type: str,
    correct_index: int,
    epc: float,
    reward_value: float,
) -> int:
    """يسجّل السؤال وإجابته في السيرفر قبل إرساله للعميل."""
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO golden_questions
               (user_id, asin, question_type, correct_index, epc, reward_value, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                user_id,
                asin,
                question_type,
                correct_index,
                float(epc),
                float(reward_value),
                datetime.utcnow().isoformat(),
            ),
        )
        return cur.lastrowid


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
               golden_round_earnings, golden_target FROM users WHERE user_id = ?""",
            (user_id,),
        ).fetchone()
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
    """كام عرض فعلي جديد اتضاف من آخر مرة بعتنا فيها تنبيه للعملاء.

    بنعد السجلات نفسها بدل طرح أرقام الـ IDs، لأن SQLite ممكن يكمّل ترقيم
    الـ IDs بعد مسح الكاش، وساعتها طرح الأرقام كان بيدي عددًا وهميًا كبيرًا.
    """
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


def mark_notified_up_to_latest():
    """بتسجّل إننا بعتنا تنبيه لحد آخر عرض موجود دلوقتي، عشان العداد يبدأ من جديد."""
    with get_conn() as conn:
        row = conn.execute("SELECT MAX(id) AS max_id FROM deals_cache").fetchone()
        max_id = row["max_id"] or 0
        conn.execute(
            "UPDATE notify_state SET last_notified_deal_id = ? WHERE id = 1", (max_id,)
        )


def list_new_deals_for_user(user_id: int):
    """بترجع بس العروض اللي العميل ده لسه ما شافهاش، من الأقدم للأحدث."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT last_seen_deal_id FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        last_seen = row["last_seen_deal_id"] if row else 0
        return conn.execute(
            "SELECT * FROM deals_cache WHERE id > ? ORDER BY id ASC", (last_seen,)
        ).fetchall()


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
