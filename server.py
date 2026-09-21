"""
ANX Mini App - backend (Flask + SQLite)

- Xác thực người dùng bằng initData của Telegram (chống giả mạo).
- Kiểm tra thành viên thật bằng Bot API (getChatMember) trước khi trả thưởng.
- Số dư, nhiệm vụ, thưởng, đào coin đều nằm trên server nên client không sửa được.
- Rút USDT (BEP20): tạo yêu cầu, admin duyệt và chuyển thủ công.

Chạy:  python server.py
"""
import hashlib
import hmac
import json
import logging
import math
import os
import re
import sqlite3
import threading
import time
from decimal import Decimal
from contextlib import contextmanager
from functools import wraps
from urllib.parse import parse_qsl

import requests
from flask import Flask, jsonify, request, send_from_directory

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("anx")


def load_dotenv(path=os.path.join(BASE_DIR, ".env")):
    """Đọc file .env đơn giản (KEY=VALUE), không ghi đè biến môi trường có sẵn."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_dotenv()

# ------------------------------------------------------------------ cấu hình
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
BOT_USERNAME = os.environ.get("BOT_USERNAME", "").lstrip("@")
APP_SHORT_NAME = os.environ.get("APP_SHORT_NAME", "")   # tuỳ chọn, dùng cho link mời
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "anx.db"))

PRICE = int(os.environ.get("PRICE_PER_MEMBER", 50))     # người tạo trả cho mỗi thành viên
REWARD = int(os.environ.get("REWARD_PER_MEMBER", 30))   # thành viên nhận
FEE = PRICE - REWARD                                    # phí nền tảng, thu ngay khi tạo nhiệm vụ
MINE_SECONDS = int(os.environ.get("MINE_SECONDS", 7200))   # mỗi lượt đào kéo dài 2 giờ
MINE_REWARD = int(os.environ.get("MINE_REWARD", 80))       # nhận sau mỗi lượt
# Cấp đào: (tổng thưởng mỗi lượt tính theo %, giá). Cấp 0 = 100%.
# Phải mua lần lượt từ thấp lên cao; cấp cuối là cấp 4.
MINE_LEVELS = [(110, 500), (120, 1000), (130, 2000), (160, 5000)]
MIN_WITHDRAW = int(os.environ.get("MIN_WITHDRAW", 1000))   # số xu tối thiểu để rút
MICRO_PER_1000 = int(Decimal(os.environ.get("USDT_PER_1000", "0.04")) * 1_000_000)  # 1000 xu = 0.04 USDT
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")            # bắt buộc nếu muốn dùng trang admin
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "")        # nhận thông báo khi có yêu cầu rút
HOLD_HOURS = int(os.environ.get("HOLD_HOURS", 24))      # sau thời gian này bot kiểm tra lại người đã vào
REFERRAL_REWARD = int(os.environ.get("REFERRAL_REWARD", 0))
MAX_TARGET = 100000
DEV_MODE = os.environ.get("DEV_MODE") == "1"            # chỉ dùng khi test ở máy local

if not 0 < REWARD < PRICE:
    raise SystemExit("REWARD_PER_MEMBER phải lớn hơn 0 và nhỏ hơn PRICE_PER_MEMBER")
if not BOT_TOKEN:
    log.warning("Chưa có BOT_TOKEN, hãy điền trong file .env")

# ------------------------------------------------------------------ database
DB = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
DB.row_factory = sqlite3.Row
LOCK = threading.RLock()

with LOCK:
    DB.execute("PRAGMA journal_mode=WAL")
    DB.executescript(
        """
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            balance INTEGER NOT NULL DEFAULT 0,
            total INTEGER NOT NULL DEFAULT 0,
            collects INTEGER NOT NULL DEFAULT 0,
            last_collect REAL NOT NULL,
            mine_start REAL,                          -- thời điểm bắt đầu lượt đào hiện tại (NULL = đang rảnh)
            mine_level INTEGER NOT NULL DEFAULT 0,    -- cấp đào đã mua
            session_reward INTEGER,                   -- thưởng chốt lúc bắt đầu lượt (nâng cấp giữa chừng không ảnh hưởng)
            referred_by INTEGER,
            created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS campaigns(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            username TEXT NOT NULL,
            title TEXT NOT NULL,
            kind TEXT NOT NULL,
            target INTEGER NOT NULL,
            joined INTEGER NOT NULL DEFAULT 0,
            reward_pool INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',   -- active | paused | done | cancelled
            created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS task_starts(
            campaign_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            ts REAL NOT NULL,
            PRIMARY KEY(campaign_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS completions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'ok',       -- ok | left
            created_at REAL NOT NULL,
            rechecked INTEGER NOT NULL DEFAULT 0,
            attempts INTEGER NOT NULL DEFAULT 0,
            UNIQUE(campaign_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS ledger(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,                -- 0 = nền tảng
            delta INTEGER NOT NULL,
            reason TEXT NOT NULL,
            ref INTEGER,
            ts REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS withdrawals(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            units INTEGER NOT NULL,
            usdt_micro INTEGER NOT NULL,             -- USDT * 1e6
            address TEXT NOT NULL,
            network TEXT NOT NULL DEFAULT 'BEP20',
            status TEXT NOT NULL DEFAULT 'pending',  -- pending | paid | rejected
            txhash TEXT,
            note TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_wd_user ON withdrawals(user_id, status);
        CREATE INDEX IF NOT EXISTS idx_camp_status ON campaigns(status);
        CREATE INDEX IF NOT EXISTS idx_users_ref ON users(referred_by);
        """
    )


with LOCK:
    _cols = [r["name"] for r in DB.execute("PRAGMA table_info(users)").fetchall()]
    for _name, _ddl in (
        ("mine_start", "REAL"),
        ("mine_level", "INTEGER NOT NULL DEFAULT 0"),
        ("session_reward", "INTEGER"),
    ):
        if _name not in _cols:                    # nâng cấp từ bản cũ
            DB.execute("ALTER TABLE users ADD COLUMN %s %s" % (_name, _ddl))


@contextmanager
def tx():
    """Giao dịch ghi. Lỗi bất kỳ sẽ rollback."""
    with LOCK:
        DB.execute("BEGIN IMMEDIATE")
        try:
            yield DB
            DB.execute("COMMIT")
        except BaseException:
            DB.execute("ROLLBACK")
            raise


def q(sql, args=()):
    with LOCK:
        return DB.execute(sql, args).fetchall()


def q1(sql, args=()):
    rows = q(sql, args)
    return rows[0] if rows else None


def add_balance(uid, delta, reason, ref=None, earned=False):
    """Chỉ gọi trong tx()."""
    DB.execute(
        "UPDATE users SET balance = balance + ?, total = total + ? WHERE id = ?",
        (delta, delta if earned else 0, uid),
    )
    DB.execute(
        "INSERT INTO ledger(user_id, delta, reason, ref, ts) VALUES(?,?,?,?,?)",
        (uid, delta, reason, ref, time.time()),
    )


# ------------------------------------------------------------------ Telegram
_bot_id = None


def tg(method, **params):
    """Gọi Bot API. Luôn trả về dict (ok=False nếu lỗi mạng)."""
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", json=params, timeout=10
        )
        return r.json()
    except Exception as e:  # noqa: BLE001
        log.warning("Telegram %s lỗi: %s", method, e)
        return {"ok": False, "error_code": 0, "description": str(e)}


def get_bot_id():
    global _bot_id
    if _bot_id is None:
        d = tg("getMe")
        if not d.get("ok"):
            raise ApiError("tg_error", "Telegram đang bận, thử lại sau.", 503)
        _bot_id = d["result"]["id"]
    return _bot_id


def member_status(chat_id, user_id):
    """(True/False/None, raw). None = không xác định được (lỗi)."""
    d = tg("getChatMember", chat_id=chat_id, user_id=user_id)
    if not d.get("ok"):
        return None, d
    r = d["result"]
    s = r.get("status")
    if s in ("creator", "administrator", "member"):
        return True, d
    if s == "restricted":
        return bool(r.get("is_member")), d
    return False, d          # left, kicked


GONE_HINTS = (
    "chat not found",
    "not enough rights",
    "bot was kicked",
    "bot is not a member",
    "member list is inaccessible",
    "chat_admin_required",
)


def chat_gone(d):
    desc = (d.get("description") or "").lower()
    return d.get("error_code") in (400, 403) and any(h in desc for h in GONE_HINTS)


# ------------------------------------------------------------------ địa chỉ ví BEP20
_M64 = (1 << 64) - 1
_RC = [
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
]
_ROT = [[0, 36, 3, 41, 18], [1, 44, 10, 45, 2], [62, 6, 43, 15, 61], [28, 55, 25, 21, 56], [27, 20, 39, 8, 14]]


def _rol(x, n):
    n %= 64
    return ((x << n) | (x >> (64 - n))) & _M64 if n else x


def _permute(A):
    for rc in _RC:
        C = [A[x][0] ^ A[x][1] ^ A[x][2] ^ A[x][3] ^ A[x][4] for x in range(5)]
        D = [C[(x - 1) % 5] ^ _rol(C[(x + 1) % 5], 1) for x in range(5)]
        A = [[A[x][y] ^ D[x] for y in range(5)] for x in range(5)]
        B = [[0] * 5 for _ in range(5)]
        for x in range(5):
            for y in range(5):
                B[y][(2 * x + 3 * y) % 5] = _rol(A[x][y], _ROT[x][y])
        A = [[B[x][y] ^ ((~B[(x + 1) % 5][y]) & B[(x + 2) % 5][y]) for y in range(5)] for x in range(5)]
        A[0][0] ^= rc
    return A


def keccak256(data: bytes) -> bytes:
    rate = 136
    p = bytearray(data) + b"\x01"
    p += b"\x00" * ((-len(p)) % rate)
    p[-1] |= 0x80
    A = [[0] * 5 for _ in range(5)]
    for off in range(0, len(p), rate):
        block = p[off:off + rate]
        for i in range(rate // 8):
            A[i % 5][i // 5] ^= int.from_bytes(block[8 * i:8 * i + 8], "little")
        A = _permute(A)
    return b"".join(A[i % 5][i // 5].to_bytes(8, "little") for i in range(4))


def to_checksum(addr_lower_hex40: str) -> str:
    h = keccak256(addr_lower_hex40.encode()).hex()
    return "0x" + "".join(c.upper() if int(h[i], 16) >= 8 else c for i, c in enumerate(addr_lower_hex40))


def valid_bep20_address(a: str) -> bool:
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", a or ""):
        return False
    body = a[2:]
    if body == "0" * 40:
        return False
    if body == body.lower() or body == body.upper():
        return True                      # không có checksum
    return to_checksum(body.lower()) == a   # có checksum thì phải khớp



def reward_for(level):
    """Thưởng mỗi lượt đào ở cấp `level` (số nguyên)."""
    pct = 100 if level <= 0 else MINE_LEVELS[level - 1][0]
    return MINE_REWARD * pct // 100


def usdt_micro(units):
    return units * MICRO_PER_1000 // 1000


# ------------------------------------------------------------------ xác thực
class ApiError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def parse_init_data(init_data):
    """Kiểm tra chữ ký initData theo tài liệu Telegram Web Apps."""
    if not init_data or not BOT_TOKEN:
        return None
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    given = pairs.pop("hash", None)
    if not given:
        return None
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, given):
        return None
    try:
        if time.time() - int(pairs.get("auth_date", "0")) > 86400:
            return None
        user = json.loads(pairs["user"])
        int(user["id"])
    except Exception:  # noqa: BLE001
        return None
    return {"user": user, "start_param": pairs.get("start_param", "")}


def upsert_user(tg_user, start_param):
    uid = int(tg_user["id"])
    name = (
        (tg_user.get("first_name", "") + " " + tg_user.get("last_name", "")).strip()
        or tg_user.get("username")
        or str(uid)
    )[:64]
    with tx():
        row = DB.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if row is None:
            ref = None
            m = re.fullmatch(r"ref_(\d+)", start_param or "")
            if m and int(m.group(1)) != uid:
                if DB.execute("SELECT 1 FROM users WHERE id=?", (int(m.group(1)),)).fetchone():
                    ref = int(m.group(1))
            now = time.time()
            DB.execute(
                "INSERT INTO users(id, name, last_collect, referred_by, created_at) VALUES(?,?,?,?,?)",
                (uid, name, now, ref, now),
            )
            if ref and REFERRAL_REWARD > 0:
                add_balance(ref, REFERRAL_REWARD, "referral", uid, earned=True)
        elif row["name"] != name:
            DB.execute("UPDATE users SET name=? WHERE id=?", (name, uid))
    return q1("SELECT * FROM users WHERE id=?", (uid,))


def auth():
    if DEV_MODE and request.headers.get("X-Dev-User"):
        uid, _, name = request.headers["X-Dev-User"].partition(":")
        try:
            tg_user = {"id": int(uid), "first_name": name or "Dev"}
        except ValueError:
            raise ApiError("unauthorized", "Dev user không hợp lệ.", 401)
        return upsert_user(tg_user, request.headers.get("X-Dev-Start", ""))
    parsed = parse_init_data(request.headers.get("X-Init-Data", ""))
    if not parsed:
        raise ApiError("unauthorized", "Hãy mở app từ Telegram.", 401)
    return upsert_user(parsed["user"], parsed["start_param"])


_last_call = {}


def cooldown(uid, key, sec):
    now = time.time()
    if len(_last_call) > 20000:
        _last_call.clear()
    if now - _last_call.get((uid, key), 0) < sec:
        raise ApiError("slow_down", "Thao tác quá nhanh, thử lại sau giây lát.", 429)
    _last_call[(uid, key)] = now


app = Flask(__name__)


@app.errorhandler(ApiError)
def on_api_error(e):
    return jsonify(ok=False, error=e.code, message=e.message), e.status


def api(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        user = auth()
        return jsonify(ok=True, **fn(user, *a, **kw))

    return wrapper


# ------------------------------------------------------------------ dữ liệu trả về
def me_payload(uid):
    u = q1("SELECT * FROM users WHERE id=?", (uid,))
    friends = [
        r["name"]
        for r in q("SELECT name FROM users WHERE referred_by=? ORDER BY created_at DESC LIMIT 100", (uid,))
    ]
    done = q1("SELECT COUNT(*) AS c FROM completions WHERE user_id=? AND status='ok'", (uid,))["c"]
    return {
        "id": u["id"],
        "name": u["name"],
        "balance": u["balance"],
        "total": u["total"],
        "collects": u["collects"],
        "mine_start": u["mine_start"],
        "mine_seconds": MINE_SECONDS,
        "mine_level": u["mine_level"],
        "mine_reward": reward_for(u["mine_level"]),        # thưởng của lượt đào kế tiếp
        "session_reward": u["session_reward"] if u["mine_start"] is not None else None,
        "now": time.time(),
        "tasks_done": done,
        "friends": friends,
        "withdrawals": [
            dict(r)
            for r in q(
                "SELECT id, units, usdt_micro, address, status, txhash, note, created_at"
                " FROM withdrawals WHERE user_id=? ORDER BY id DESC LIMIT 10",
                (uid,),
            )
        ],
        "config": {
            "price": PRICE,
            "reward": REWARD,
            "fee": FEE,
            "levels": [
                {"level": i + 1, "percent": pct, "price": price, "reward": reward_for(i + 1)}
                for i, (pct, price) in enumerate(MINE_LEVELS)
            ],
            "base_reward": MINE_REWARD,
            "withdraw": {"min_units": MIN_WITHDRAW, "micro_per_1000": MICRO_PER_1000, "network": "BEP20"},
            "bot_username": BOT_USERNAME,
            "app_short_name": APP_SHORT_NAME,
        },
    }


def campaign_payload(c):
    return {
        "id": c["id"],
        "username": c["username"],
        "title": c["title"],
        "kind": c["kind"],
        "target": c["target"],
        "joined": c["joined"],
        "status": c["status"],
        "refundable": c["reward_pool"] if c["status"] in ("active", "paused") else 0,
    }


# ------------------------------------------------------------------ routes
@app.route("/")
def index():
    resp = send_from_directory(BASE_DIR, "index.html")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/health")
def health():
    return jsonify(ok=True)


@app.route("/api/me")
@api
def me(user):
    return {"me": me_payload(user["id"])}


@app.route("/api/mine/start", methods=["POST"])
@api
def mine_start(user):
    uid = user["id"]
    cooldown(uid, "mine", 0.5)
    with tx():
        u = DB.execute("SELECT mine_start, mine_level FROM users WHERE id=?", (uid,)).fetchone()
        if u["mine_start"] is not None:
            raise ApiError("already_mining", "Bạn đang trong một lượt đào.", 409)
        DB.execute(
            "UPDATE users SET mine_start=?, session_reward=? WHERE id=?",
            (time.time(), reward_for(u["mine_level"]), uid),      # chốt thưởng theo cấp lúc bắt đầu
        )
    return {"me": me_payload(uid)}


@app.route("/api/mine/claim", methods=["POST"])
@api
def mine_claim(user):
    uid = user["id"]
    cooldown(uid, "mine", 0.5)
    with tx():
        u = DB.execute("SELECT mine_start, session_reward FROM users WHERE id=?", (uid,)).fetchone()
        if u["mine_start"] is None:
            raise ApiError("not_mining", "Bạn chưa bắt đầu lượt đào.", 409)
        left = u["mine_start"] + MINE_SECONDS - time.time()
        if left > 0:
            raise ApiError("not_ready", "Chưa đủ thời gian, còn khoảng %d phút." % math.ceil(left / 60), 409)
        reward = u["session_reward"] or MINE_REWARD
        DB.execute("UPDATE users SET mine_start=NULL, session_reward=NULL, collects=collects+1 WHERE id=?", (uid,))
        add_balance(uid, reward, "mine", None, earned=True)
    return {"whole": reward, "me": me_payload(uid)}


@app.route("/api/shop/upgrade", methods=["POST"])
@api
def shop_upgrade(user):
    """Mua cấp đào kế tiếp. Chỉ có hiệu lực từ lượt đào sau."""
    uid = user["id"]
    cooldown(uid, "upgrade", 1)
    try:
        want = int((request.get_json(silent=True) or {}).get("level"))
    except (TypeError, ValueError):
        raise ApiError("invalid_level", "Cấp không hợp lệ.")
    if not 1 <= want <= len(MINE_LEVELS):
        raise ApiError("invalid_level", "Cấp không hợp lệ.")
    with tx():
        u = DB.execute("SELECT balance, mine_level FROM users WHERE id=?", (uid,)).fetchone()
        if want <= u["mine_level"]:
            raise ApiError("already_owned", "Bạn đã sở hữu cấp này.", 409)
        if want > u["mine_level"] + 1:
            raise ApiError("need_previous", "Hãy mở cấp thấp hơn trước.", 409)
        price = MINE_LEVELS[want - 1][1]
        if u["balance"] < price:
            raise ApiError("insufficient", "Không đủ xu.", 409)
        add_balance(uid, -price, "upgrade", want)
        DB.execute("UPDATE users SET mine_level=? WHERE id=?", (want, uid))
    return {"me": me_payload(uid)}


# ---- người tạo nhiệm vụ
USERNAME_RE = re.compile(r"^(?:https?://)?(?:t|telegram)\.me/([A-Za-z][A-Za-z0-9_]{4,31})/?$", re.I)


def parse_username(v):
    v = (v or "").strip()
    m = re.fullmatch(r"@([A-Za-z][A-Za-z0-9_]{4,31})", v) or USERNAME_RE.match(v)
    return m.group(1) if m else None


@app.route("/api/campaigns", methods=["POST"])
@api
def create_campaign(user):
    uid = user["id"]
    cooldown(uid, "create", 3)
    data = request.get_json(silent=True) or {}
    username = parse_username(data.get("link"))
    if not username:
        raise ApiError(
            "invalid_link",
            "Link không hợp lệ. Chỉ hỗ trợ kênh/nhóm công khai, dạng https://t.me/tenkenh hoặc @tenkenh.",
        )
    try:
        target = int(data.get("target"))
    except (TypeError, ValueError):
        raise ApiError("invalid_target", "Số lượng thành viên không hợp lệ.")
    if not 1 <= target <= MAX_TARGET:
        raise ApiError("invalid_target", f"Số lượng phải từ 1 đến {MAX_TARGET}.")
    cost = target * PRICE

    if (q1("SELECT balance FROM users WHERE id=?", (uid,)))["balance"] < cost:
        raise ApiError("insufficient", "Không đủ xu.", 409)

    d = tg("getChat", chat_id="@" + username)
    if not d.get("ok"):
        if d.get("error_code") == 400:
            raise ApiError("chat_not_found", "Không tìm thấy kênh hoặc nhóm này.", 404)
        raise ApiError("tg_error", "Telegram đang bận, thử lại sau.", 503)
    chat = d["result"]
    if chat.get("type") not in ("channel", "supergroup", "group"):
        raise ApiError("not_a_chat", "Link này không phải kênh hoặc nhóm.")
    chat_id = chat["id"]

    bot_info = tg("getChatMember", chat_id=chat_id, user_id=get_bot_id())
    if not bot_info.get("ok") or bot_info["result"].get("status") not in ("administrator", "creator"):
        raise ApiError(
            "bot_not_admin",
            "Bot chưa là quản trị viên của kênh/nhóm này. Hãy thêm bot rồi thử lại.",
            409,
        )
    om = tg("getChatMember", chat_id=chat_id, user_id=uid)
    if not om.get("ok") or om["result"].get("status") not in ("administrator", "creator"):
        raise ApiError("not_owner", "Bạn cần là quản trị viên của kênh/nhóm này.", 403)

    with tx():
        row = DB.execute("SELECT balance FROM users WHERE id=?", (uid,)).fetchone()
        if row["balance"] < cost:
            raise ApiError("insufficient", "Không đủ xu.", 409)
        if DB.execute(
            "SELECT 1 FROM campaigns WHERE chat_id=? AND status IN ('active','paused')", (chat_id,)
        ).fetchone():
            raise ApiError("already_active", "Kênh/nhóm này đang có nhiệm vụ chạy.", 409)
        add_balance(uid, -cost, "campaign", None)
        cur = DB.execute(
            "INSERT INTO campaigns(owner_id, chat_id, username, title, kind, target, reward_pool, created_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (
                uid,
                chat_id,
                username,
                (chat.get("title") or username)[:80],
                "channel" if chat.get("type") == "channel" else "group",
                target,
                target * REWARD,
                time.time(),
            ),
        )
        cid = cur.lastrowid
        DB.execute(
            "INSERT INTO ledger(user_id, delta, reason, ref, ts) VALUES(0,?,?,?,?)",
            (target * FEE, "fee", cid, time.time()),
        )
    c = q1("SELECT * FROM campaigns WHERE id=?", (cid,))
    return {"campaign": campaign_payload(c), "me": me_payload(uid)}


@app.route("/api/campaigns/mine")
@api
def my_campaigns(user):
    rows = q("SELECT * FROM campaigns WHERE owner_id=? ORDER BY id DESC LIMIT 50", (user["id"],))
    return {"campaigns": [campaign_payload(c) for c in rows]}


@app.route("/api/campaigns/<int:cid>/cancel", methods=["POST"])
@api
def cancel_campaign(user, cid):
    uid = user["id"]
    with tx():
        c = DB.execute("SELECT * FROM campaigns WHERE id=? AND owner_id=?", (cid, uid)).fetchone()
        if not c:
            raise ApiError("not_found", "Không tìm thấy nhiệm vụ.", 404)
        if c["status"] not in ("active", "paused"):
            raise ApiError("not_cancellable", "Nhiệm vụ này không thể huỷ.", 409)
        refund = c["reward_pool"]
        DB.execute("UPDATE campaigns SET status='cancelled', reward_pool=0 WHERE id=?", (cid,))
        if refund:
            add_balance(uid, refund, "refund", cid)
    return {"refund": refund, "me": me_payload(uid)}


# ---- người làm nhiệm vụ
def load_available(cid, uid):
    c = q1("SELECT * FROM campaigns WHERE id=?", (cid,))
    if not c:
        raise ApiError("not_found", "Không tìm thấy nhiệm vụ.", 404)
    if c["owner_id"] == uid:
        raise ApiError("own_campaign", "Bạn không thể làm nhiệm vụ của chính mình.", 403)
    if q1("SELECT 1 FROM completions WHERE campaign_id=? AND user_id=?", (cid, uid)):
        raise ApiError("already_done", "Bạn đã làm nhiệm vụ này rồi.", 409)
    if c["status"] == "paused":
        raise ApiError("unavailable", "Nhiệm vụ đang tạm dừng.", 409)
    if c["status"] != "active" or c["joined"] >= c["target"]:
        raise ApiError("full", "Nhiệm vụ đã đủ người.", 409)
    return c


def pause_campaign(cid):
    with tx():
        DB.execute("UPDATE campaigns SET status='paused' WHERE id=? AND status='active'", (cid,))


@app.route("/api/tasks")
@api
def list_tasks(user):
    rows = q(
        """SELECT id, username, title, kind FROM campaigns c
           WHERE status='active' AND owner_id != ? AND joined < target
             AND NOT EXISTS (SELECT 1 FROM completions x WHERE x.campaign_id=c.id AND x.user_id=?)
           ORDER BY id DESC LIMIT 50""",
        (user["id"], user["id"]),
    )
    return {"tasks": [dict(r) for r in rows]}


@app.route("/api/tasks/<int:cid>/start", methods=["POST"])
@api
def task_start(user, cid):
    uid = user["id"]
    cooldown(uid, "start", 1.5)
    c = load_available(cid, uid)
    m, d = member_status(c["chat_id"], uid)
    if m is None:
        if chat_gone(d):
            pause_campaign(cid)
            raise ApiError("unavailable", "Nhiệm vụ đang tạm dừng.", 409)
        raise ApiError("tg_error", "Telegram đang bận, thử lại sau.", 503)
    if m:
        # đã ở trong kênh/nhóm từ trước thì không phải thành viên mới
        raise ApiError("already_member", "Bạn đã là thành viên rồi nên không nhận thưởng được.", 409)
    with tx():
        DB.execute(
            "INSERT OR REPLACE INTO task_starts(campaign_id, user_id, ts) VALUES(?,?,?)",
            (cid, uid, time.time()),
        )
    return {}


@app.route("/api/tasks/<int:cid>/verify", methods=["POST"])
@api
def task_verify(user, cid):
    uid = user["id"]
    cooldown(uid, "verify", 2)
    c = load_available(cid, uid)
    if not q1("SELECT 1 FROM task_starts WHERE campaign_id=? AND user_id=?", (cid, uid)):
        raise ApiError("not_started", "Hãy bấm Tham gia trước.", 409)

    m, d = member_status(c["chat_id"], uid)       # gọi Telegram ngoài giao dịch
    if m is None:
        if chat_gone(d):
            pause_campaign(cid)
            raise ApiError("unavailable", "Nhiệm vụ đang tạm dừng.", 409)
        raise ApiError("tg_error", "Telegram đang bận, thử lại sau.", 503)
    if not m:
        raise ApiError("not_member", "Bạn chưa tham gia. Hãy tham gia rồi bấm Kiểm tra.", 409)

    with tx():
        c = DB.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
        if c["status"] != "active" or c["joined"] >= c["target"]:
            raise ApiError("full", "Nhiệm vụ đã đủ người.", 409)
        try:
            DB.execute(
                "INSERT INTO completions(campaign_id, user_id, status, created_at) VALUES(?,?, 'ok', ?)",
                (cid, uid, time.time()),
            )
        except sqlite3.IntegrityError:
            raise ApiError("already_done", "Bạn đã làm nhiệm vụ này rồi.", 409)
        joined = c["joined"] + 1
        DB.execute(
            "UPDATE campaigns SET joined=?, reward_pool=reward_pool-?, status=? WHERE id=?",
            (joined, REWARD, "done" if joined >= c["target"] else "active", cid),
        )
        add_balance(uid, REWARD, "task", cid, earned=True)
    return {"reward": REWARD, "me": me_payload(uid)}


# ------------------------------------------------------------------ rút USDT (BEP20)
def notify(chat_id, text):
    """Gửi tin nhắn Telegram, không chặn request và bỏ qua lỗi."""
    if not chat_id:
        return

    def run():
        try:
            tg("sendMessage", chat_id=chat_id, text=text)
        except Exception:  # noqa: BLE001
            log.exception("notify lỗi")

    threading.Thread(target=run, daemon=True).start()


def fmt_usdt(micro):
    s = ("%.6f" % (micro / 1_000_000)).rstrip("0").rstrip(".")
    return s or "0"


@app.route("/api/withdraw", methods=["POST"])
@api
def withdraw(user):
    uid = user["id"]
    cooldown(uid, "withdraw", 3)
    data = request.get_json(silent=True) or {}
    try:
        units = int(data.get("amount"))
    except (TypeError, ValueError):
        raise ApiError("invalid_amount", "Số lượng không hợp lệ.")
    address = (data.get("address") or "").strip()
    if not valid_bep20_address(address):
        raise ApiError(
            "invalid_address",
            "Địa chỉ ví không hợp lệ. Cần địa chỉ BEP20 dạng 0x… (42 ký tự), đúng chữ hoa/thường nếu có.",
        )
    if units < MIN_WITHDRAW:
        raise ApiError("below_min", "Tối thiểu %d xu mỗi lần rút." % MIN_WITHDRAW)
    with tx():
        bal = DB.execute("SELECT balance FROM users WHERE id=?", (uid,)).fetchone()["balance"]
        if bal < units:
            raise ApiError("insufficient", "Không đủ xu.", 409)
        if DB.execute("SELECT 1 FROM withdrawals WHERE user_id=? AND status='pending'", (uid,)).fetchone():
            raise ApiError("pending_exists", "Bạn đang có yêu cầu rút chờ duyệt.", 409)
        now = time.time()
        micro = usdt_micro(units)
        cur = DB.execute(
            "INSERT INTO withdrawals(user_id, units, usdt_micro, address, network, status, created_at, updated_at)"
            " VALUES(?,?,?,?, 'BEP20', 'pending', ?, ?)",
            (uid, units, micro, address, now, now),
        )
        wid = cur.lastrowid
        add_balance(uid, -units, "withdraw", wid)          # giữ xu ngay khi tạo yêu cầu
        shared = DB.execute(
            "SELECT COUNT(DISTINCT user_id) AS c FROM withdrawals WHERE lower(address)=lower(?) AND user_id!=?",
            (address, uid),
        ).fetchone()["c"]
    notify(
        ADMIN_CHAT_ID,
        "Yêu cầu rút #%d\nNgười dùng: %s (%d)\nSố tiền: %s USDT (%d xu)\nMạng: BEP20\nVí: %s%s"
        % (wid, user["name"], uid, fmt_usdt(micro), units, address,
           ("\nCảnh báo: ví này cũng được %d tài khoản khác dùng" % shared) if shared else ""),
    )
    return {"me": me_payload(uid)}


def admin_required():
    tok = request.headers.get("X-Admin-Token", "")
    if not ADMIN_TOKEN or not hmac.compare_digest(tok, ADMIN_TOKEN):
        raise ApiError("forbidden", "Forbidden", 403)


@app.route("/admin/withdrawals")
def admin_list():
    admin_required()
    status = request.args.get("status", "pending")
    rows = q(
        """SELECT w.*, u.name AS user_name,
                  (SELECT COUNT(DISTINCT user_id) FROM withdrawals x
                    WHERE lower(x.address)=lower(w.address) AND x.user_id != w.user_id) AS shared
           FROM withdrawals w JOIN users u ON u.id = w.user_id
           WHERE w.status = ? ORDER BY w.id LIMIT 200""",
        (status,),
    )
    return jsonify(
        ok=True,
        withdrawals=[
            {
                "id": r["id"], "user_id": r["user_id"], "user": r["user_name"], "units": r["units"],
                "usdt": fmt_usdt(r["usdt_micro"]), "address": r["address"], "network": r["network"],
                "status": r["status"], "shared_with_other_accounts": r["shared"],
            }
            for r in rows
        ],
    )


@app.route("/admin/withdrawals/<int:wid>/paid", methods=["POST"])
def admin_paid(wid):
    admin_required()
    txhash = ((request.get_json(silent=True) or {}).get("txhash") or "").strip()
    if not re.fullmatch(r"0x[0-9a-fA-F]{64}", txhash):
        raise ApiError("invalid_txhash", "txhash không hợp lệ (0x + 64 ký tự hex).")
    with tx():
        w = DB.execute("SELECT * FROM withdrawals WHERE id=?", (wid,)).fetchone()
        if not w or w["status"] != "pending":
            raise ApiError("not_pending", "Yêu cầu không tồn tại hoặc đã xử lý.", 409)
        DB.execute(
            "UPDATE withdrawals SET status='paid', txhash=?, updated_at=? WHERE id=?", (txhash, time.time(), wid)
        )
    notify(w["user_id"], "Đã chuyển %s USDT (BEP20) tới ví %s.\nTx: %s" % (fmt_usdt(w["usdt_micro"]), w["address"], txhash))
    return jsonify(ok=True)


@app.route("/admin/withdrawals/<int:wid>/reject", methods=["POST"])
def admin_reject(wid):
    admin_required()
    note = ((request.get_json(silent=True) or {}).get("note") or "").strip()[:200]
    with tx():
        w = DB.execute("SELECT * FROM withdrawals WHERE id=?", (wid,)).fetchone()
        if not w or w["status"] != "pending":
            raise ApiError("not_pending", "Yêu cầu không tồn tại hoặc đã xử lý.", 409)
        DB.execute(
            "UPDATE withdrawals SET status='rejected', note=?, updated_at=? WHERE id=?", (note, time.time(), wid)
        )
        add_balance(w["user_id"], w["units"], "withdraw_refund", wid)   # hoàn xu
    notify(w["user_id"], "Yêu cầu rút #%d bị từ chối, xu đã được hoàn lại.%s" % (wid, ("\nLý do: " + note) if note else ""))
    return jsonify(ok=True)


# ------------------------------------------------------------------ kiểm tra lại sau khi giữ chân
def recheck_once():
    """Sau HOLD_HOURS, người đã rời kênh/nhóm sẽ bị thu hồi thưởng và mở lại 1 suất."""
    cutoff = time.time() - HOLD_HOURS * 3600
    rows = q(
        """SELECT x.id, x.campaign_id, x.user_id, x.attempts, c.chat_id
           FROM completions x JOIN campaigns c ON c.id = x.campaign_id
           WHERE x.status='ok' AND x.rechecked=0 AND x.created_at <= ?
           ORDER BY x.id LIMIT 100""",
        (cutoff,),
    )
    done = 0
    for r in rows:
        m, _ = member_status(r["chat_id"], r["user_id"])
        with tx():
            if m is None:
                # lỗi tạm thời: thử lại lần sau, quá 5 lần thì bỏ qua
                DB.execute(
                    "UPDATE completions SET attempts=attempts+1, rechecked=CASE WHEN attempts+1>=5 THEN 1 ELSE 0 END WHERE id=?",
                    (r["id"],),
                )
            elif m:
                DB.execute("UPDATE completions SET rechecked=1 WHERE id=?", (r["id"],))
            else:
                bal = DB.execute("SELECT balance FROM users WHERE id=?", (r["user_id"],)).fetchone()["balance"]
                claw = min(REWARD, bal)
                DB.execute("UPDATE completions SET status='left', rechecked=1 WHERE id=?", (r["id"],))
                if claw:
                    add_balance(r["user_id"], -claw, "clawback", r["campaign_id"])
                DB.execute(
                    """UPDATE campaigns SET joined = joined - 1, reward_pool = reward_pool + ?,
                       status = CASE WHEN status='done' THEN 'active' ELSE status END WHERE id=?""",
                    (claw, r["campaign_id"]),
                )
                done += 1
        time.sleep(0.05)
    return done


def recheck_loop():
    while True:
        time.sleep(600)
        try:
            n = recheck_once()
            if n:
                log.info("Đã thu hồi thưởng của %s người rời kênh/nhóm", n)
        except Exception:  # noqa: BLE001
            log.exception("recheck lỗi")


if __name__ == "__main__":
    from waitress import serve

    threading.Thread(target=recheck_loop, daemon=True).start()
    port = int(os.environ.get("PORT", 8000))
    log.info("Chạy tại http://0.0.0.0:%s", port)
    serve(app, host=os.environ.get("HOST", "0.0.0.0"), port=port, threads=8)
