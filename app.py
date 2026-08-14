import os
import io
import re
import json
import base64
import secrets
import sqlite3
import hmac
import hashlib
import requests
import importlib
import time
import threading
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta
from functools import wraps
from urllib.parse import urlencode, quote_plus
from dotenv import load_dotenv
from flask import (
    Flask, render_template_string, request, jsonify, Response,
    stream_with_context, session, redirect, url_for, flash, g, send_from_directory
)
from werkzeug.security import generate_password_hash, check_password_hash
from pypdf import PdfReader

# OAuth (đăng nhập bằng Google) — dùng Authlib, cài qua requirements.txt
try:
    oauth_module = importlib.import_module("authlib.integrations.flask_client")
    OAuth = getattr(oauth_module, "OAuth", None)
except ImportError:
    OAuth = None

# Optional dependencies — the app still runs without them, just with reduced features.
try:
    docx_lib = importlib.import_module("docx")
except ImportError:
    docx_lib = None

try:
    from PIL import Image
except ImportError:
    Image = None

# ==========================================
# 0. CẤU HÌNH ỨNG DỤNG
# ==========================================
load_dotenv()  # đọc các biến từ file .env cùng thư mục (nếu có)

app = Flask(__name__)
# Trần cứng ở tầng Flask/Werkzeug — áp dụng cho MỌI request trước khi code của ta kịp chạy.
# Đặt bằng đúng mức trần cao nhất trong số các gói (Max = 1GB/file); giới hạn thấp hơn cho
# từng gói (Free/Premium) được kiểm tra riêng, chi tiết hơn, ngay trong route /api/upload.
app.config['MAX_CONTENT_LENGTH'] = 1024 * 1024 * 1024 + 8 * 1024 * 1024  # 1GB + đệm cho overhead multipart
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'


@app.template_filter('vnd')
def _format_vnd(amount):
    """Format số tiền kiểu Việt Nam: dấu chấm ngăn cách hàng nghìn (vd: 30000 -> '30.000')."""
    try:
        return f"{int(amount):,}".replace(',', '.')
    except (TypeError, ValueError):
        return str(amount)


from werkzeug.middleware.proxy_fix import ProxyFix

# Render / Nginx: tin 1 lớp proxy (HTTPS, Host)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# Khóa bí mật để ký session cookie (đăng nhập). Nên đặt cố định qua biến môi trường
# SECRET_KEY trong .env khi deploy thật, nếu không mỗi lần restart server người dùng
# sẽ bị đăng xuất (vì khóa được sinh ngẫu nhiên lại).
_env_secret = os.environ.get("SECRET_KEY", "").strip()
if _env_secret:
    app.secret_key = _env_secret
else:
    app.secret_key = secrets.token_hex(32)
    print("⚠️  CẢNH BÁO: Chưa có SECRET_KEY trong .env — phiên đăng nhập sẽ mất khi restart server.")
    print("   Thêm dòng sau vào .env để cố định: SECRET_KEY=" + secrets.token_hex(16))

# Giới hạn số ký tự trích từ file để tránh vượt quá token limit khi gửi cho AI (mức Free —
# Premium/Max có mức cao hơn hoặc không giới hạn, xem PLAN_LIMITS bên dưới).
MAX_FILE_CHARS = 12000
MAX_IMAGE_DIMENSION = 1600  # px, ảnh lớn hơn sẽ được thu nhỏ để gửi AI nhanh hơn

ALLOWED_IMAGE_EXT = {'.png', '.jpg', '.jpeg', '.gif', '.webp'}
ALLOWED_DOC_EXT = {'.pdf', '.docx', '.txt', '.csv'}

USERNAME_RE = re.compile(r'^[A-Za-z0-9_]{3,32}$')

# Lấy cấu hình từ biến môi trường / file .env (KHÔNG hard-code API key trong code!)
CONSOLEX_API_BASE = os.environ.get("CONSOLEX_API_BASE", "https://api.x.ai/v1")
XAI_API_KEY = os.environ.get("XAI_API_KEY", "")
CONSOLEX_MODEL = os.environ.get("CONSOLEX_MODEL", "grok-4.5")

if not XAI_API_KEY:
    print("⚠️  CẢNH BÁO: Chưa thiết lập XAI_API_KEY.")
    print("   Tạo file .env cùng thư mục với app.py, nội dung:")
    print("   XAI_API_KEY=xai-xxxxxxxxxxxxxxxx")

# Session dùng chung để tái sử dụng kết nối TCP/TLS tới xAI -> giảm độ trễ mỗi request.
SESSION = requests.Session()

# ==========================================
# 0.05. ĐĂNG NHẬP BẰNG GOOGLE (OAuth 2.0)
# ==========================================
# Lấy Client ID / Client Secret từ .env. Nếu không đặt, nút tương ứng sẽ tự ẩn
# trên trang đăng nhập — app vẫn chạy bình thường với đăng nhập bằng mật khẩu.
GOOGLE_CLIENT_ID = os.environ.get('GOOGLE_CLIENT_ID', '').strip()
GOOGLE_CLIENT_SECRET = os.environ.get('GOOGLE_CLIENT_SECRET', '').strip()

GOOGLE_OAUTH_ENABLED = bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET and OAuth)

oauth = OAuth(app) if OAuth else None

if GOOGLE_OAUTH_ENABLED:
    oauth.register(
        name='google',
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
        client_kwargs={'scope': 'openid email profile'},
    )

if not GOOGLE_OAUTH_ENABLED:
    print("ℹ️  Đăng nhập Google đang TẮT (chưa đặt Client ID/Secret trong .env).")
    print("   Xem hướng dẫn lấy Client ID/Secret trong README.md để bật.")

# ==========================================
# 0.1. CƠ SỞ DỮ LIỆU (Tài khoản + Lịch sử chat) — SQLite
# ==========================================
# Mặc định: file studymate.db nằm CẠNH app.py — đúng cho chạy local hoặc VPS tự quản lý (thư
# mục code = nơi lưu database luôn, đơn giản). Trên các nền tảng PaaS có ổ đĩa tạm (Render
# Free, Railway...), thư mục này bị XOÁ mỗi lần deploy lại — muốn giữ dữ liệu qua các lần
# deploy, đặt biến môi trường DB_PATH trỏ vào ổ đĩa BỀN VỮNG (Persistent Disk) đã gắn riêng,
# vd trên Render: DB_PATH=/data/studymate.db (xem README mục 31 để biết cách gắn Disk).
_db_path_override = os.environ.get('DB_PATH', '').strip()
if _db_path_override:
    DB_PATH = _db_path_override
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)) or '.', exist_ok=True)
else:
    DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'studymate.db')

# Tài khoản DUY NHẤT được phép giữ vai trò Super Admin trong toàn hệ thống — lấy từ .env
# (SUPER_ADMIN_USERNAME) nếu có, mặc định "BlackadaNutella". Hằng số cấp module để dùng nhất
# quán ở cả nơi TỰ ĐỘNG nâng quyền (init_db) lẫn nơi CHẶN nâng quyền cho tài khoản khác
# (can_manage_role) — xem 2 chỗ dùng bên dưới.
SUPER_ADMIN_USERNAME = (os.environ.get('SUPER_ADMIN_USERNAME', '') or 'BlackadaNutella').strip()

# ==========================================
# 0.36. GIỚI HẠN TỐC ĐỘ (Rate Limiting) — chống dò mật khẩu / đăng ký hàng loạt
# ==========================================
# Bộ đếm trong bộ nhớ tiến trình — không cần Redis/hạ tầng ngoài, đơn giản, đủ để chặn bot cơ
# bản. GIỚI HẠN CẦN BIẾT: nếu chạy nhiều worker process (vd `gunicorn -w 4`), MỖI worker có bộ
# đếm RIÊNG không chia sẻ — giới hạn thực tế sẽ cao hơn số cấu hình (x số worker). Muốn chặn
# triệt để ở quy mô lớn/nhiều worker cần Redis hoặc thư viện như Flask-Limiter + backend chung.
#
# Giới hạn đăng nhập tính theo CẶP (IP, username) chứ không phải riêng IP — để 1 học sinh gõ
# sai mật khẩu nhiều lần KHÔNG làm khoá luôn cả lớp đang dùng chung WiFi trường (rất thực tế
# với app này). Có thêm 1 giới hạn tổng theo IP (ngưỡng cao hơn nhiều) chỉ để chặn kiểu bot dò
# quét nhiều tài khoản khác nhau từ 1 địa chỉ.
_rate_limit_buckets = defaultdict(deque)
_rate_limit_lock = threading.Lock()


def _rate_limit_check(key, max_attempts, window_seconds):
    """True = còn được phép (đã tự ghi nhận lượt này luôn). False = đã vượt giới hạn."""
    now = time.time()
    with _rate_limit_lock:
        bucket = _rate_limit_buckets[key]
        while bucket and now - bucket[0] > window_seconds:
            bucket.popleft()
        if len(bucket) >= max_attempts:
            return False
        bucket.append(now)
        return True


def _rate_limit_reset(key):
    with _rate_limit_lock:
        _rate_limit_buckets.pop(key, None)


def client_ip():
    return (request.headers.get('X-Forwarded-For', '') or request.remote_addr or 'unknown').split(',')[0].strip()



def ensure_columns(conn, table, columns):
    """Tự thêm các cột còn THIẾU vào 1 bảng đã tồn tại — an toàn để gọi lại nhiều lần (chỉ
    ALTER TABLE nếu cột chưa có). `columns`: dict {tên_cột: định_nghĩa_SQL}.

    Lý do cần hàm này: `CREATE TABLE IF NOT EXISTS` là no-op nếu bảng đã tồn tại — nếu sau
    này code thêm cột mới vào câu CREATE nhưng người dùng đang chạy 1 database SQLite được
    tạo từ TRƯỚC lúc thêm cột đó, bảng cũ sẽ KHÔNG tự có cột mới, gây lỗi
    "sqlite3.OperationalError: no such column: ..." ngay khi code cố đọc/ghi cột đó (đã xảy
    ra thực tế với issue_reports.resolved_by). Gọi ensure_columns() cho MỌI bảng ngay sau
    CREATE TABLE để tự "vá" schema cũ, không cần người dùng xoá database đi tạo lại."""
    existing = {r[1] for r in conn.execute(f'PRAGMA table_info({table})').fetchall()}
    changed = False
    for col_name, col_def in columns.items():
        if col_name not in existing:
            conn.execute(f'ALTER TABLE {table} ADD COLUMN {col_name} {col_def}')
            changed = True
    if changed:
        conn.commit()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user',
            created_at TEXT NOT NULL
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (conversation_id) REFERENCES conversations (id)
        )
    ''')
    # Nhật ký "lượt sử dụng" AI — mỗi lần học sinh gửi câu hỏi tới /api/chat sẽ có 1 dòng ở đây.
    # Dùng để dựng trang thống kê cho tài khoản developer (KHÔNG lưu nội dung câu hỏi/trả lời,
    # chỉ lưu số liệu tổng quát: độ dài, môn học, chế độ, có kèm file/ảnh không, trạng thái).
    conn.execute('''
        CREATE TABLE IF NOT EXISTS usage_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            endpoint TEXT NOT NULL,
            subject TEXT,
            mode TEXT,
            message_chars INTEGER DEFAULT 0,
            response_chars INTEGER DEFAULT 0,
            had_file INTEGER DEFAULT 0,
            had_image INTEGER DEFAULT 0,
            status TEXT DEFAULT 'ok',
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')
    # "Dự án" (giống Claude Projects) — nhóm các đoạn chat lại theo chủ đề của riêng từng tài khoản.
    conn.execute('''
        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')
    # Cấu hình hệ thống dạng key-value (thông báo chung, bật/tắt tính năng...) do developer chỉnh.
    conn.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')
    conn.commit()

    # ---- Di trú (migration) cho database cũ đã tồn tại trước khi có cột "role" ----
    existing_cols = [r[1] for r in conn.execute('PRAGMA table_info(users)').fetchall()]
    if 'role' not in existing_cols:
        conn.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user'")
        conn.commit()

    # ---- Di trú cho đăng nhập Google (OAuth) ----
    # email: dùng để hiển thị / tránh trùng tài khoản OAuth.
    # oauth_provider + oauth_id: định danh duy nhất của tài khoản bên Google.
    # Tài khoản tạo qua OAuth sẽ có password_hash = '' (không thể đăng nhập bằng mật khẩu).
    if 'email' not in existing_cols:
        conn.execute("ALTER TABLE users ADD COLUMN email TEXT")
        conn.commit()
    if 'oauth_provider' not in existing_cols:
        conn.execute("ALTER TABLE users ADD COLUMN oauth_provider TEXT")
        conn.commit()
    if 'oauth_id' not in existing_cols:
        conn.execute("ALTER TABLE users ADD COLUMN oauth_id TEXT")
        conn.commit()
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_oauth ON users (oauth_provider, oauth_id) "
        "WHERE oauth_provider IS NOT NULL"
    )
    conn.commit()

    # ---- Di trú: tuỳ chỉnh cá nhân theo tài khoản (giao diện, ngôn ngữ, môn/chế độ mặc định) ----
    if 'preferences' not in existing_cols:
        conn.execute("ALTER TABLE users ADD COLUMN preferences TEXT DEFAULT '{}'")
        conn.commit()

    # ---- Di trú: Ghim đoạn chat + gom vào "Dự án" (giống Claude Projects) ----
    conv_cols = [r[1] for r in conn.execute('PRAGMA table_info(conversations)').fetchall()]
    if 'pinned' not in conv_cols:
        conn.execute("ALTER TABLE conversations ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    if 'project_id' not in conv_cols:
        conn.execute("ALTER TABLE conversations ADD COLUMN project_id INTEGER")
        conn.commit()

    # ---- Di trú: hệ thống vai trò 4 cấp (user → developer → admin → super_admin) ----
    # Khoá tài khoản (is_locked/lock_reason) + "reset session" (session_version: tăng số này
    # sẽ làm mọi phiên đăng nhập cũ của tài khoản đó tự động bị đăng xuất ở lần request kế tiếp).
    existing_cols = [r[1] for r in conn.execute('PRAGMA table_info(users)').fetchall()]
    if 'is_locked' not in existing_cols:
        conn.execute("ALTER TABLE users ADD COLUMN is_locked INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    if 'lock_reason' not in existing_cols:
        conn.execute("ALTER TABLE users ADD COLUMN lock_reason TEXT DEFAULT ''")
        conn.commit()
    if 'session_version' not in existing_cols:
        conn.execute("ALTER TABLE users ADD COLUMN session_version INTEGER NOT NULL DEFAULT 0")
        conn.commit()

    # ---- Di trú: gói sử dụng (Free/Premium/Max) ----
    # Chỉ áp dụng thật sự cho tài khoản role='user' — Developer trở lên luôn có Max vô điều
    # kiện, tính động qua effective_plan() ở runtime (xem mục 0.25), không đọc từ cột này.
    if 'plan' not in existing_cols:
        conn.execute("ALTER TABLE users ADD COLUMN plan TEXT NOT NULL DEFAULT 'free'")
        conn.commit()

    # Gói Premium/Max giờ tính THEO THÁNG (không phải vĩnh viễn) — plan_expires_at là mốc
    # hết hạn. Hết hạn mà chưa gia hạn thì effective_plan() tự coi như 'free' (không cần job
    # nền dọn dẹp gì cả, tính "lazy" ngay lúc đọc — xem effective_plan() ở mục 0.25).
    if 'plan_expires_at' not in existing_cols:
        conn.execute("ALTER TABLE users ADD COLUMN plan_expires_at TEXT")
        conn.commit()

    # "AI Tutor" tuỳ chỉnh do developer trở lên tự tạo (tên + system prompt riêng).
    conn.execute('''
        CREATE TABLE IF NOT EXISTS custom_tutors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            system_prompt TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (owner_id) REFERENCES users (id)
        )
    ''')
    conn.commit()
    # API Key cho developer trở lên — chỉ lưu bản băm (hash), không bao giờ lưu key gốc.
    conn.execute('''
        CREATE TABLE IF NOT EXISTS api_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            key_prefix TEXT NOT NULL,
            key_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_used_at TEXT,
            revoked INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')
    conn.commit()
    # Nhật ký thao tác nhạy cảm (đổi vai trò, khoá tài khoản, xoá tài khoản, cấu hình hệ thống...)
    # — chỉ Super Admin xem được, phục vụ truy vết trách nhiệm (audit trail).
    conn.execute('''
        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor_id INTEGER,
            actor_username TEXT,
            action TEXT NOT NULL,
            target TEXT DEFAULT '',
            detail TEXT DEFAULT '',
            created_at TEXT NOT NULL
        )
    ''')
    conn.commit()
    # Nhật ký từng lượt tải file/ảnh lên — dùng để tính giới hạn "X file/ảnh mỗi 24h" theo
    # gói (Free/Premium/Max). Đếm theo cửa sổ trượt 24h kể từ thời điểm hỏi (rolling window),
    # KHÔNG reset cứng theo nửa đêm — đúng như yêu cầu "thời gian reset 24h".
    conn.execute('''
        CREATE TABLE IF NOT EXISTS file_uploads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            size_bytes INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')
    conn.commit()
    # Đơn nâng cấp gói (thanh toán). "method" = 'vnpay' (ATM/Visa/Mastercard/JCB qua cổng
    # VNPAY) hoặc 'bank_transfer' (chuyển khoản quét mã VietQR, xác nhận thủ công bởi Admin).
    # "order_code" vừa là mã tra cứu, vừa dùng làm vnp_TxnRef / nội dung chuyển khoản.
    conn.execute('''
        CREATE TABLE IF NOT EXISTS payment_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_code TEXT UNIQUE NOT NULL,
            user_id INTEGER NOT NULL,
            plan TEXT NOT NULL,
            amount INTEGER NOT NULL,
            base_amount INTEGER NOT NULL DEFAULT 0,
            is_discounted INTEGER NOT NULL DEFAULT 0,
            method TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            provider_txn_id TEXT,
            created_at TEXT NOT NULL,
            paid_at TEXT,
            note TEXT,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')
    conn.commit()
    payment_cols = [r[1] for r in conn.execute('PRAGMA table_info(payment_orders)').fetchall()]
    if 'base_amount' not in payment_cols:
        conn.execute("ALTER TABLE payment_orders ADD COLUMN base_amount INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    if 'is_discounted' not in payment_cols:
        conn.execute("ALTER TABLE payment_orders ADD COLUMN is_discounted INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    # "Bộ nhớ" AI — điều đáng nhớ về 1 học sinh (môn yếu, mục tiêu, cách giải thích ưa thích...)
    # để cá nhân hoá câu trả lời ở các lượt chat sau. category giúp phân loại khi hiển thị.
    conn.execute('''
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            content TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'general',
            source TEXT NOT NULL DEFAULT 'auto',
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')
    conn.commit()
    # Báo lỗi câu trả lời từ học sinh — gắn với 1 đoạn chat cụ thể (nếu có) để Admin xem lại.
    conn.execute('''
        CREATE TABLE IF NOT EXISTS issue_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            conversation_id INTEGER,
            message_excerpt TEXT,
            description TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            created_at TEXT NOT NULL,
            resolved_at TEXT,
            resolved_by TEXT,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')
    conn.commit()
    # Bảng issue_reports từng được tạo TRƯỚC KHI có cột resolved_by (đã gây lỗi thực tế:
    # "no such column: resolved_by" khi Admin bấm Đánh dấu đã xử lý) — vá lại schema cũ.
    ensure_columns(conn, 'issue_reports', {
        'resolved_at': 'TEXT',
        'resolved_by': 'TEXT',
    })
    # Gamification nhẹ: XP + streak (số ngày học liên tiếp) theo tài khoản.
    conn.execute('''
        CREATE TABLE IF NOT EXISTS user_stats (
            user_id INTEGER PRIMARY KEY,
            xp INTEGER NOT NULL DEFAULT 0,
            streak_days INTEGER NOT NULL DEFAULT 0,
            longest_streak INTEGER NOT NULL DEFAULT 0,
            last_active_date TEXT,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')
    conn.commit()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS achievements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            code TEXT NOT NULL,
            earned_at TEXT NOT NULL,
            UNIQUE(user_id, code),
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')
    conn.commit()
    # "Thẻ ghi nhớ" (flashcards) — bộ thẻ do học sinh tự tạo hoặc AI tạo giúp từ 1 chủ đề /
    # đoạn chat. box_level dùng kiểu Leitner đơn giản (1-5, đúng thì tăng, sai thì về 1) để
    # ưu tiên cho học sinh ôn lại thẻ còn yếu trước.
    conn.execute('''
        CREATE TABLE IF NOT EXISTS flashcard_decks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            subject TEXT DEFAULT '',
            source TEXT NOT NULL DEFAULT 'manual',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')
    conn.commit()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS flashcards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            deck_id INTEGER NOT NULL,
            front TEXT NOT NULL,
            back TEXT NOT NULL,
            box_level INTEGER NOT NULL DEFAULT 1,
            times_reviewed INTEGER NOT NULL DEFAULT 0,
            times_correct INTEGER NOT NULL DEFAULT 0,
            last_reviewed_at TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (deck_id) REFERENCES flashcard_decks (id)
        )
    ''')
    conn.commit()
    # "Sổ lỗi sai" (Mistake Book) — học sinh (hoặc chính học sinh tự đánh giá sau khi đọc lời
    # sửa của AI ở chế độ "Kiểm tra bài làm") lưu lại 1 dạng lỗi hay mắc. Lỗi lặp lại (cùng
    # môn + cùng mô tả, sau khi chuẩn hoá) chỉ tăng occurrence_count chứ không tạo dòng mới,
    # để ra đúng kiểu "Chuyển vế sai dấu ×3" như trong Sổ lỗi sai thật.
    conn.execute('''
        CREATE TABLE IF NOT EXISTS mistakes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            subject TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL,
            occurrence_count INTEGER NOT NULL DEFAULT 1,
            conversation_id INTEGER,
            resolved INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            last_occurred_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')
    conn.commit()

    # ---- "Vá" toàn diện: đảm bảo MỌI bảng có ĐỦ MỌI cột mà code hiện tại mong đợi, bất kể
    # database đang chạy được tạo từ phiên bản nào trong quá khứ. Đây là lớp phòng thủ cuối
    # cùng, tự sửa các trường hợp "no such column" còn sót lại ở TẤT CẢ các bảng (không chỉ
    # issue_reports) — kể cả những bảng hiện tại chưa phát hiện thiếu cột nào, khai đầy đủ
    # ở đây vẫn AN TOÀN vì ensure_columns() chỉ ALTER những cột thật sự còn thiếu.
    ensure_columns(conn, 'users', {
        'role': "TEXT NOT NULL DEFAULT 'user'", 'email': 'TEXT', 'oauth_provider': 'TEXT',
        'oauth_id': 'TEXT', 'preferences': "TEXT DEFAULT '{}'", 'is_locked': 'INTEGER NOT NULL DEFAULT 0',
        'lock_reason': "TEXT DEFAULT ''", 'session_version': 'INTEGER NOT NULL DEFAULT 0',
        'plan': "TEXT NOT NULL DEFAULT 'free'", 'plan_expires_at': 'TEXT',
        'avatar_emoji': 'TEXT', 'avatar_color': 'TEXT', 'is_guest': 'INTEGER NOT NULL DEFAULT 0',
        'recovery_code_hash': 'TEXT',
    })
    ensure_columns(conn, 'conversations', {
        'pinned': 'INTEGER NOT NULL DEFAULT 0', 'project_id': 'INTEGER',
    })
    ensure_columns(conn, 'payment_orders', {
        'base_amount': 'INTEGER NOT NULL DEFAULT 0', 'is_discounted': 'INTEGER NOT NULL DEFAULT 0',
    })
    ensure_columns(conn, 'memories', {
        'category': "TEXT NOT NULL DEFAULT 'general'", 'source': "TEXT NOT NULL DEFAULT 'auto'",
    })
    ensure_columns(conn, 'mistakes', {
        'occurrence_count': 'INTEGER NOT NULL DEFAULT 1', 'conversation_id': 'INTEGER',
        'resolved': 'INTEGER NOT NULL DEFAULT 0',
    })
    ensure_columns(conn, 'flashcard_decks', {
        'subject': "TEXT DEFAULT ''", 'source': "TEXT NOT NULL DEFAULT 'manual'",
    })
    ensure_columns(conn, 'flashcards', {
        'box_level': 'INTEGER NOT NULL DEFAULT 1', 'times_reviewed': 'INTEGER NOT NULL DEFAULT 0',
        'times_correct': 'INTEGER NOT NULL DEFAULT 0', 'last_reviewed_at': 'TEXT',
    })
    ensure_columns(conn, 'api_keys', {'last_used_at': 'TEXT', 'revoked': 'INTEGER NOT NULL DEFAULT 0'})

    # "Quiz Generator" (Phase 1) — AI tạo bộ câu hỏi từ 1 chủ đề (hoặc từ nội dung 1 đoạn
    # chat có sẵn). Chỉ hỗ trợ các dạng câu hỏi CHẤM ĐƯỢC TỰ ĐỘNG, CHÍNH XÁC, KHÔNG cần thêm
    # lượt gọi AI nào lúc chấm (trắc nghiệm, đúng/sai, điền khuyết) — dạng tự luận/ghép nối
    # cần AI chấm chủ quan nên chưa hỗ trợ ở bản này (xem README).
    conn.execute('''
        CREATE TABLE IF NOT EXISTS quizzes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            subject TEXT DEFAULT '',
            difficulty TEXT NOT NULL DEFAULT 'medium',
            source TEXT NOT NULL DEFAULT 'topic',
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')
    conn.commit()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS quiz_questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            quiz_id INTEGER NOT NULL,
            q_type TEXT NOT NULL,
            question TEXT NOT NULL,
            options TEXT,
            correct_answer TEXT NOT NULL,
            explanation TEXT DEFAULT '',
            topic TEXT DEFAULT '',
            order_index INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (quiz_id) REFERENCES quizzes (id)
        )
    ''')
    conn.commit()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS quiz_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            quiz_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            score INTEGER NOT NULL DEFAULT 0,
            total INTEGER NOT NULL DEFAULT 0,
            duration_seconds INTEGER DEFAULT 0,
            answers TEXT,
            weak_topics TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY (quiz_id) REFERENCES quizzes (id),
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')
    conn.commit()

    # "Study Plan" (Phase 1) — AI chia 1 mục tiêu ôn tập thành kế hoạch theo từng ngày.
    # "Sắp xếp lại" (reorganize) khi học sinh bị trễ tiến độ = gọi lại AI phân bổ các việc
    # CÒN LẠI (chưa Hoàn thành) vào số ngày CÒN LẠI — không phải thuật toán lịch phức tạp,
    # nhưng vẫn là "tự điều chỉnh kế hoạch theo tiến độ" đúng như yêu cầu.
    conn.execute('''
        CREATE TABLE IF NOT EXISTS study_plans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            subject TEXT DEFAULT '',
            total_days INTEGER NOT NULL,
            start_date TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')
    conn.commit()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS study_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_id INTEGER NOT NULL,
            day_number INTEGER NOT NULL,
            title TEXT NOT NULL,
            description TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            completed_at TEXT,
            FOREIGN KEY (plan_id) REFERENCES study_plans (id)
        )
    ''')
    conn.commit()

    # "StudyMate Lab" — Feature Registry + Feature Flags: hạ tầng để đăng ký, thử nghiệm, tăng
    # dần tỉ lệ rollout, và giám sát tính năng thử nghiệm mà KHÔNG cần sửa code/deploy lại.
    # status: off | internal | beta | public | archived.
    # rollout_pct chỉ có ý nghĩa khi status='beta' — phần trăm NGƯỜI DÙNG THƯỜNG được thấy
    # tính năng (Developer trở lên LUÔN thấy được ở mọi trạng thái khác off/archived, để tự
    # test được bất cứ lúc nào — xem is_feature_enabled()).
    conn.execute('''
        CREATE TABLE IF NOT EXISTS feature_flags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT UNIQUE NOT NULL,
            name TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'off',
            category TEXT DEFAULT 'other',
            description TEXT DEFAULT '',
            owner_username TEXT DEFAULT '',
            environment TEXT DEFAULT 'production',
            version TEXT DEFAULT '1.0.0',
            rollout_pct INTEGER NOT NULL DEFAULT 0,
            depends_on TEXT DEFAULT '',
            expires_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    ''')
    conn.commit()
    ensure_columns(conn, 'feature_flags', {
        'name': "TEXT DEFAULT ''", 'category': "TEXT DEFAULT 'other'", 'owner_username': "TEXT DEFAULT ''",
        'environment': "TEXT DEFAULT 'production'", 'version': "TEXT DEFAULT '1.0.0'",
        'rollout_pct': 'INTEGER NOT NULL DEFAULT 0', 'depends_on': "TEXT DEFAULT ''", 'expires_at': 'TEXT',
    })
    # Các tính năng CÓ SẴN, đang hiện cho MỌI người trước khi có hệ thống flag này — "gieo"
    # (seed) TẤT CẢ với status='public' ngay từ đầu để KHÔNG vô tình ẩn mất tính năng đang
    # chạy thật ngay khi bản cập nhật này lên (khác với tính năng HOÀN TOÀN MỚI luôn bắt đầu
    # ở 'internal' khi tạo qua giao diện Dev Lab — xem developer_lab_create_flag()). Từ giờ
    # Developer/Admin vào /developer/lab đổi trạng thái BẤT KỲ tính năng nào bên dưới để
    # ẩn/thử nghiệm/giới hạn % người dùng thấy — không cần sửa code, không cần deploy lại.
    EXISTING_FEATURES_TO_SEED = [
        ('game_snake_quiz', 'Rắn Săn Chữ (Snake Quiz)', 'games', 'Trò chơi rắn ăn mồi kết hợp câu hỏi phép tính.'),
        ('game_quick_math', 'Đố Vui Tính Nhanh (Quick Math)', 'games', 'Trò chơi trả lời phép tính trong 60 giây.'),
        ('game_memory_match', 'Lật Thẻ Ghi Nhớ (Memory Match)', 'games', 'Trò chơi lật thẻ tìm cặp khớp nhau.'),
    ]
    for f_key, f_name, f_category, f_desc in EXISTING_FEATURES_TO_SEED:
        existing_f = conn.execute("SELECT id FROM feature_flags WHERE key = ?", (f_key,)).fetchone()
        if not existing_f:
            conn.execute(
                '''INSERT INTO feature_flags (key, name, status, category, description, owner_username,
                   environment, version, rollout_pct, created_at, updated_at)
                   VALUES (?, ?, 'public', ?, ?, 'system', 'production', '1.0.0', 0, ?, ?)''',
                (f_key, f_name, f_category, f_desc, now_iso(), now_iso())
            )
    conn.commit()

    # "Đố Vui Tính Nhanh" (Quick Math) — lưu kết quả từng ván để tính XP/thành tựu + báo cáo
    # điểm yếu theo TỪNG PHÉP TÍNH (không cần gọi AI — số liệu tự thống kê từ ván chơi).
    conn.execute('''
        CREATE TABLE IF NOT EXISTS game_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            game TEXT NOT NULL,
            difficulty TEXT DEFAULT 'medium',
            score INTEGER NOT NULL DEFAULT 0,
            correct_count INTEGER NOT NULL DEFAULT 0,
            total_count INTEGER NOT NULL DEFAULT 0,
            best_combo INTEGER NOT NULL DEFAULT 0,
            weak_topics TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')
    conn.commit()

    # ========== "LỚP HỌC" (Teacher Mode) ==========
    # Biến app từ công cụ cá nhân thành nền tảng cho cả lớp: giáo viên tạo lớp -> lấy mã mời ->
    # học sinh vào lớp bằng mã -> giáo viên giao BÀI QUIZ CÓ SẴN (dùng lại nguyên hệ Quiz
    # Generator đã có) kèm hạn nộp -> hệ thống tự tổng hợp ai đã làm, điểm trung bình, và
    # ĐIỂM YẾU CHUNG CỦA CẢ LỚP (gom từ weak_topics của từng bài làm — dữ liệu vốn đã có sẵn,
    # không cần gọi thêm AI).
    # KHÔNG tạo vai trò "teacher" riêng: bất kỳ tài khoản nào cũng có thể tạo lớp (giáo viên
    # thật, hoặc 1 học sinh lập nhóm học chung) — đơn giản hơn và không phá vỡ hệ phân quyền
    # user/developer/admin/super_admin đang chạy ổn định.
    conn.execute('''
        CREATE TABLE IF NOT EXISTS classes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            subject TEXT DEFAULT '',
            join_code TEXT UNIQUE NOT NULL,
            archived INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY (owner_id) REFERENCES users (id)
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS class_members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            class_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            joined_at TEXT NOT NULL,
            UNIQUE(class_id, user_id),
            FOREIGN KEY (class_id) REFERENCES classes (id),
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS assignments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            class_id INTEGER NOT NULL,
            quiz_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            due_at TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (class_id) REFERENCES classes (id),
            FOREIGN KEY (quiz_id) REFERENCES quizzes (id)
        )
    ''')
    conn.commit()
    # Nối bài làm quiz với bài tập được giao (nếu có) — dùng lại bảng quiz_attempts sẵn có
    # thay vì tạo bảng "bài nộp" riêng, để điểm/phân tích điểm yếu dùng chung một nguồn.
    ensure_columns(conn, 'quiz_attempts', {'assignment_id': 'INTEGER'})

    # ========== CHỈ MỤC (INDEX) — HIỆU NĂNG ==========
    # Trước đây toàn bộ 29 bảng KHÔNG có index nào, trong khi có ~38 truy vấn lọc theo
    # user_id. Nghĩa là MỖI lần mở lịch sử chat / bảng tiến độ / trang developer, SQLite phải
    # QUÉT TOÀN BỘ bảng từ đầu tới cuối. Lúc ít dữ liệu thì không thấy gì, nhưng càng dùng lâu
    # càng chậm dần đều — và chậm nhất đúng với tài khoản CHĂM HỌC NHẤT (nhiều dữ liệu nhất),
    # tức là phạt oan chính người dùng tốt nhất.
    # CREATE INDEX IF NOT EXISTS chạy lại nhiều lần vẫn an toàn; SQLite tự dựng index cho dữ
    # liệu đã có sẵn ngay lần khởi động đầu tiên sau khi cập nhật.
    for idx_sql in [
        'CREATE INDEX IF NOT EXISTS idx_conv_user ON conversations(user_id, updated_at DESC)',
        'CREATE INDEX IF NOT EXISTS idx_msg_conv ON messages(conversation_id, id)',
        'CREATE INDEX IF NOT EXISTS idx_usage_user ON usage_logs(user_id, created_at)',
        'CREATE INDEX IF NOT EXISTS idx_usage_created ON usage_logs(created_at)',
        'CREATE INDEX IF NOT EXISTS idx_mistakes_user ON mistakes(user_id, resolved, occurrence_count DESC)',
        'CREATE INDEX IF NOT EXISTS idx_memories_user ON memories(user_id, created_at DESC)',
        'CREATE INDEX IF NOT EXISTS idx_quizzes_user ON quizzes(user_id, created_at DESC)',
        'CREATE INDEX IF NOT EXISTS idx_qquestions_quiz ON quiz_questions(quiz_id, order_index)',
        'CREATE INDEX IF NOT EXISTS idx_qattempts_user ON quiz_attempts(user_id, created_at DESC)',
        'CREATE INDEX IF NOT EXISTS idx_qattempts_assignment ON quiz_attempts(assignment_id)',
        'CREATE INDEX IF NOT EXISTS idx_decks_user ON flashcard_decks(user_id, updated_at DESC)',
        'CREATE INDEX IF NOT EXISTS idx_cards_deck ON flashcards(deck_id)',
        'CREATE INDEX IF NOT EXISTS idx_plans_user ON study_plans(user_id, created_at DESC)',
        'CREATE INDEX IF NOT EXISTS idx_tasks_plan ON study_tasks(plan_id, day_number)',
        'CREATE INDEX IF NOT EXISTS idx_gamesess_user ON game_sessions(user_id, game)',
        'CREATE INDEX IF NOT EXISTS idx_achieve_user ON achievements(user_id)',
        'CREATE INDEX IF NOT EXISTS idx_projects_user ON projects(user_id)',
        'CREATE INDEX IF NOT EXISTS idx_uploads_user ON file_uploads(user_id, created_at)',
        'CREATE INDEX IF NOT EXISTS idx_issues_status ON issue_reports(status, created_at DESC)',
        'CREATE INDEX IF NOT EXISTS idx_orders_user ON payment_orders(user_id, created_at DESC)',
        'CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_logs(created_at DESC)',
        'CREATE INDEX IF NOT EXISTS idx_classmem_class ON class_members(class_id)',
        'CREATE INDEX IF NOT EXISTS idx_classmem_user ON class_members(user_id)',
        'CREATE INDEX IF NOT EXISTS idx_assign_class ON assignments(class_id, created_at DESC)',
        'CREATE INDEX IF NOT EXISTS idx_assign_quiz ON assignments(quiz_id)',
        'CREATE INDEX IF NOT EXISTS idx_classes_owner ON classes(owner_id, archived)',
    ]:
        try:
            conn.execute(idx_sql)
        except sqlite3.OperationalError:
            pass   # bảng chưa tồn tại ở database rất cũ — bỏ qua an toàn
    conn.commit()


    # Có thể tuỳ chỉnh qua .env: DEVELOPER_USERNAME / DEVELOPER_PASSWORD.
    # Nếu chưa có tài khoản này, server sẽ tự tạo và in mật khẩu ra console 1 lần duy nhất.
    dev_username = (os.environ.get('DEVELOPER_USERNAME', '') or 'developer').strip()
    dev_row = conn.execute('SELECT id, role FROM users WHERE username = ?', (dev_username,)).fetchone()
    if dev_row:
        if dev_row[1] != 'developer':
            conn.execute("UPDATE users SET role = 'developer' WHERE id = ?", (dev_row[0],))
            conn.commit()
            print(f"👨‍💻 Đã nâng quyền tài khoản '{dev_username}' thành developer.")
    else:
        dev_password = os.environ.get('DEVELOPER_PASSWORD', '').strip()
        auto_generated = False
        if not dev_password:
            dev_password = secrets.token_urlsafe(9)
            auto_generated = True
        conn.execute(
            'INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?)',
            (dev_username, generate_password_hash(dev_password), 'developer', now_iso())
        )
        conn.commit()
        print("👨‍💻 Đã tạo tài khoản developer mới:")
        print(f"   Tên đăng nhập: {dev_username}")
        if auto_generated:
            print(f"   Mật khẩu (tự sinh — hãy đăng nhập và đổi ngay): {dev_password}")
            print("   Gợi ý: đặt DEVELOPER_USERNAME / DEVELOPER_PASSWORD trong .env để cố định thông tin này.")
        else:
            print("   Mật khẩu: lấy từ DEVELOPER_PASSWORD trong .env")

    # ---- Nâng tài khoản chỉ định thành Super Admin ----
    # Super Admin đứng trên cùng hệ thống phân quyền (bao hàm mọi quyền của Admin/Developer/User).
    # Tên tài khoản lấy từ SUPER_ADMIN_USERNAME (hằng số cấp module, mặc định "BlackadaNutella").
    # Chỉ áp dụng nếu tài khoản đã tồn tại sẵn — không tự tạo tài khoản mới ở đây vì không có
    # mật khẩu do người dùng đặt để gán vào.
    sa_row = conn.execute('SELECT id, role FROM users WHERE username = ?', (SUPER_ADMIN_USERNAME,)).fetchone()
    if sa_row and sa_row[1] != 'super_admin':
        conn.execute("UPDATE users SET role = 'super_admin' WHERE id = ?", (sa_row[0],))
        conn.commit()
        print(f"👑 Đã nâng tài khoản '{SUPER_ADMIN_USERNAME}' thành Super Admin (có toàn bộ quyền, kể cả Developer).")

    # ---- Dọn dẹp: KHÔNG cho phép tài khoản nào khác ngoài SUPER_ADMIN_USERNAME giữ vai trò
    # Super Admin (phòng trường hợp trước khi có ràng buộc này, đã lỡ có tài khoản khác được
    # gán Super Admin bằng tay/qua giao diện cũ) — tự động hạ về 'admin' (vẫn còn quyền quản
    # trị cao, chỉ mất đúng phần "duy nhất kiểm soát toàn hệ thống").
    stray_super_admins = conn.execute(
        "SELECT id, username FROM users WHERE role = 'super_admin' AND username != ?", (SUPER_ADMIN_USERNAME,)
    ).fetchall()
    for row in stray_super_admins:
        conn.execute("UPDATE users SET role = 'admin' WHERE id = ?", (row[0],))
        print(f"⚠️  Tài khoản '{row[1]}' trước đó có vai trò Super Admin nhưng không phải "
              f"'{SUPER_ADMIN_USERNAME}' — đã tự hạ về Admin (chỉ '{SUPER_ADMIN_USERNAME}' được giữ Super Admin).")
    if stray_super_admins:
        conn.commit()

    conn.close()


def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
    return db


@app.teardown_appcontext
def close_db(_exc):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


# ==========================================
# 0.2. HỆ THỐNG VAI TRÒ (4 cấp, mỗi cấp bao hàm quyền của cấp dưới)
# ==========================================
# user < developer < admin < super_admin.
# Super Admin có TẤT CẢ quyền của Admin, Admin có TẤT CẢ quyền của Developer, v.v.
# (không cần gán nhiều vai trò cùng lúc — 1 cột "role" duy nhất, cấp cao hơn tự động
# thừa hưởng quyền của cấp thấp hơn thông qua role_rank()).
ROLE_ORDER = ['user', 'developer', 'admin', 'super_admin']
ROLE_META = {
    'user':        {'label': 'Người dùng', 'icon': '🧑‍🎓',
                     'badge': 'bg-gray-100 text-gray-500 dark:bg-gray-800 dark:text-gray-400'},
    'developer':   {'label': 'Developer',  'icon': '🧑‍💻',
                     'badge': 'bg-indigo-100 text-indigo-600 dark:bg-indigo-900 dark:text-indigo-300'},
    'admin':       {'label': 'Admin',      'icon': '👑',
                     'badge': 'bg-amber-100 text-amber-700 dark:bg-amber-900 dark:text-amber-300'},
    'super_admin': {'label': 'Super Admin', 'icon': '🔥',
                     'badge': 'bg-red-100 text-red-600 dark:bg-red-900 dark:text-red-300'},
}


def role_rank(role):
    try:
        return ROLE_ORDER.index(role)
    except ValueError:
        return 0


def role_meta(role):
    return ROLE_META.get(role, ROLE_META['user'])


def can_manage_role(actor_role, target_role, new_role, is_self=False, target_username=''):
    """Quy tắc: chỉ Super Admin được đụng tới vai trò Admin/Super Admin (cấp hoặc thu hồi).
    Admin chỉ được đổi qua lại giữa User <-> Developer. Một Super Admin CÓ THỂ hạ quyền một
    Super Admin KHÁC (vd: tài khoản bị tự động cấp quyền do trùng SUPER_ADMIN_USERNAME, hoặc
    bị cấp nhầm) — nhưng không ai được tự hạ quyền Super Admin của chính mình qua giao diện
    này, để luôn còn ít nhất 1 người điều khiển được hệ thống. Số lượng Admin/Super Admin còn
    lại tối thiểu 1 người được kiểm tra riêng ở nơi gọi hàm này (developer_change_role).

    QUAN TRỌNG: vai trò Super Admin CHỈ được cấp cho ĐÚNG tài khoản SUPER_ADMIN_USERNAME —
    không ai, kể cả 1 Super Admin khác, được cấp Super Admin cho bất kỳ tài khoản nào khác
    qua giao diện này (chặn ở đây, trước khi động tới DB)."""
    if new_role not in ROLE_ORDER:
        return False, "Vai trò không hợp lệ."
    if new_role == 'super_admin' and target_username != SUPER_ADMIN_USERNAME:
        return False, f"Chỉ tài khoản '{SUPER_ADMIN_USERNAME}' được phép giữ vai trò Super Admin."
    if target_role == 'super_admin':
        if actor_role != 'super_admin':
            return False, "Chỉ Super Admin mới có thể thay đổi vai trò của một Super Admin khác."
        if is_self and new_role != 'super_admin':
            return False, "Bạn không thể tự hạ quyền Super Admin của chính mình qua giao diện này."
    if actor_role != 'super_admin':
        if role_rank(target_role) >= role_rank('admin'):
            return False, "Chỉ Super Admin mới có thể thay đổi vai trò của Admin/Super Admin."
        if role_rank(new_role) >= role_rank('admin'):
            return False, "Chỉ Super Admin mới có thể cấp quyền Admin trở lên."
    return True, None


def write_audit(action, target='', detail=''):
    """Ghi log các thao tác nhạy cảm (đổi vai trò, khoá tài khoản, cấu hình hệ thống...).
    Chỉ Super Admin xem được (trang /developer/audit)."""
    try:
        db = get_db()
        actor = current_user()
        db.execute(
            'INSERT INTO audit_logs (actor_id, actor_username, action, target, detail, created_at) '
            'VALUES (?, ?, ?, ?, ?, ?)',
            (actor['id'] if actor else None, actor['username'] if actor else 'system',
             action, target, detail, now_iso())
        )
        db.commit()
    except Exception:
        pass


# ==========================================
# 0.3. XÁC THỰC NGƯỜI DÙNG (session-based)
# ==========================================
def current_user():
    """Nạp thông tin tài khoản hiện tại từ DB 1 lần / request (cache trong g)."""
    if not hasattr(g, '_current_user'):
        uid = session.get('user_id')
        g._current_user = None
        if uid:
            g._current_user = get_db().execute('SELECT * FROM users WHERE id = ?', (uid,)).fetchone()
    return g._current_user


def current_user_id():
    return session.get('user_id')


def current_user_role():
    u = current_user()
    return u['role'] if u else 'user'


def _auth_gate(min_role=None):
    """Kiểm tra: đã đăng nhập? tài khoản có bị khoá? phiên có bị admin reset không?
    có đủ cấp vai trò tối thiểu không? Trả về response lỗi nếu vi phạm, None nếu hợp lệ."""
    if not session.get('user_id'):
        if request.path.startswith('/api/'):
            return jsonify({"error": "Vui lòng đăng nhập để tiếp tục."}), 401
        return redirect(url_for('login_page', next=request.path))

    user = current_user()
    if not user:
        session.clear()
        if request.path.startswith('/api/'):
            return jsonify({"error": "Phiên đăng nhập không hợp lệ."}), 401
        return redirect(url_for('login_page'))

    if user['is_locked']:
        session.clear()
        if request.path.startswith('/api/'):
            return jsonify({"error": "Tài khoản của bạn đã bị khoá."}), 403
        flash('Tài khoản của em đã bị khoá. Liên hệ quản trị viên nếu có thắc mắc.')
        return redirect(url_for('login_page'))

    if session.get('session_version') != user['session_version']:
        session.clear()
        if request.path.startswith('/api/'):
            return jsonify({"error": "Phiên đăng nhập đã được đặt lại. Vui lòng đăng nhập lại."}), 401
        flash('Phiên đăng nhập của em đã được đặt lại, vui lòng đăng nhập lại.')
        return redirect(url_for('login_page'))

    if min_role and role_rank(user['role']) < role_rank(min_role):
        if request.path.startswith('/api/'):
            return jsonify({"error": "Bạn không có quyền truy cập chức năng này."}), 403
        flash('Tài khoản của em không có quyền truy cập trang này.')
        return redirect(url_for('home'))

    return None


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        err = _auth_gate()
        return err if err is not None else view(*args, **kwargs)
    return wrapped


def developer_required(view):
    """Từ Developer trở lên (Developer / Admin / Super Admin)."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        err = _auth_gate(min_role='developer')
        return err if err is not None else view(*args, **kwargs)
    return wrapped


def admin_required(view):
    """Từ Admin trở lên (Admin / Super Admin)."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        err = _auth_gate(min_role='admin')
        return err if err is not None else view(*args, **kwargs)
    return wrapped


def super_admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        err = _auth_gate(min_role='super_admin')
        return err if err is not None else view(*args, **kwargs)
    return wrapped


# ==========================================
# 0.25. GÓI SỬ DỤNG (Free / Premium / Max) & CHẾ ĐỘ SUY NGHĨ AI
# ==========================================
# free < premium < max — mỗi gói cao hơn kế thừa toàn bộ quyền lợi của gói thấp hơn.
# Developer/Admin/Super Admin luôn được cấp Max VÔ ĐIỀU KIỆN (tính động qua effective_plan(),
# không cần ghi vào cột `plan` trong DB) — đây là quyền lợi đi kèm vai trò, không phải trả phí.
PLAN_ORDER = ['free', 'premium', 'max']
PLAN_META = {
    'free':    {'label': 'Free',    'icon': '🆓',
                'badge': 'bg-gray-100 text-gray-500 dark:bg-gray-800 dark:text-gray-400'},
    'premium': {'label': 'Premium', 'icon': '💎',
                'badge': 'bg-indigo-100 text-indigo-600 dark:bg-indigo-900 dark:text-indigo-300'},
    'max':     {'label': 'Max',     'icon': '🚀',
                'badge': 'bg-amber-100 text-amber-700 dark:bg-amber-900 dark:text-amber-300'},
}
# daily_uploads: số file/ảnh tối đa trong 24h gần nhất (rolling window, không phải theo lịch) —
# None = không giới hạn. max_file_mb: dung lượng tối đa MỖI file/ảnh. text_chars: số ký tự tối
# đa trích xuất từ file văn bản/PDF/Word trước khi bị cắt bớt — None = không cắt.
PLAN_LIMITS = {
    'free':    {'daily_uploads': 20,   'max_file_mb': 20,   'text_chars': MAX_FILE_CHARS},
    'premium': {'daily_uploads': 50,   'max_file_mb': 500,  'text_chars': MAX_FILE_CHARS * 4},
    'max':     {'daily_uploads': None, 'max_file_mb': 1024, 'text_chars': None},
}
UPLOAD_QUOTA_WINDOW_HOURS = 24

# Giá nâng cấp gói (VNĐ/THÁNG) — Premium 30.000đ/tháng, Max 50.000đ/tháng. Đây là subscription
# THEO THÁNG: mỗi đơn thanh toán thành công chỉ cấp đúng 1 THÁNG quyền lợi (xem add_one_month()
# + grant_plan_upgrade()), hết hạn tự rơi về Free nếu không thanh toán tiếp — KHÔNG tự động trừ
# tiền định kỳ (app không lưu thông tin thẻ để làm việc đó), học sinh cần tự vào lại nâng cấp
# mỗi tháng. Free không cần thanh toán nên không có trong bảng giá.
PLAN_PRICING = {
    'premium': 30000,
    'max': 50000,
}

# Ưu đãi lần đầu: 3 THÁNG ĐẦU TIÊN học sinh từng thanh toán thành công (bất kể gói Premium hay
# Max) được giảm giá; từ tháng thanh toán thứ 4 trở đi tính giá bình thường. Đếm theo TỔNG SỐ
# đơn đã thanh toán thành công trong lịch sử tài khoản (payment_orders.status='paid'), không
# phân biệt loại gói — nâng cấp/hạ cấp giữa Premium <-> Max vẫn tính chung 1 "tháng đã dùng ưu đãi".
FIRST_TIME_DISCOUNT_PCT = 50       # % giảm — có thể chỉnh lại nếu ý bạn là mức khác
FIRST_TIME_DISCOUNT_MONTHS = 3     # số THÁNG đầu được hưởng ưu đãi

# ==========================================
# 0.26. THANH TOÁN NÂNG CẤP GÓI — VNPAY (ATM/Visa/Mastercard/JCB) + Chuyển khoản VietQR
# ==========================================
# VNPAY: cổng thanh toán thẻ (ATM nội địa qua NAPAS, thẻ quốc tế Visa/Mastercard/JCB, ví
# VNPAY QR). Cần đăng ký tài khoản merchant tại https://vnpay.vn để lấy vnp_TmnCode +
# vnp_HashSecret — CHƯA đăng ký thì tính năng này tự ẩn khỏi giao diện (app vẫn chạy bình
# thường, chỉ còn phương thức Chuyển khoản VietQR bên dưới).
VNPAY_TMN_CODE = os.environ.get('VNPAY_TMN_CODE', '').strip()
VNPAY_HASH_SECRET = os.environ.get('VNPAY_HASH_SECRET', '').strip()
VNPAY_PAYMENT_URL = os.environ.get('VNPAY_PAYMENT_URL', 'https://sandbox.vnpayment.vn/paymentv2/vpcpay.html').strip()
VNPAY_ENABLED = bool(VNPAY_TMN_CODE and VNPAY_HASH_SECRET)
if not VNPAY_ENABLED:
    print("ℹ️  Thanh toán VNPAY (thẻ ATM/Visa/Mastercard) đang TẮT — chưa cấu hình "
          "VNPAY_TMN_CODE/VNPAY_HASH_SECRET trong .env. Xem README mục 14 để đăng ký & bật.")

# Chuyển khoản ngân hàng qua mã VietQR (img.vietqr.io — dịch vụ công khai, MIỄN PHÍ, không
# cần API key: chỉ cần đúng số tài khoản NGÂN HÀNG CỦA BẠN thì ảnh QR tạo ra mới chuyển tiền
# vào đúng chỗ). Học sinh quét bằng app ngân hàng bất kỳ, hoặc MoMo/ZaloPay (2 ví này đều hỗ
# trợ quét mã VietQR chuẩn NAPAS để chuyển thẳng vào tài khoản ngân hàng — không cần tích hợp
# API riêng của MoMo/ZaloPay). Việc xác nhận "đã nhận tiền" hiện làm THỦ CÔNG bởi Admin (bấm
# 1 nút ở /developer) vì app không có quyền đọc sao kê ngân hàng tự động.
VIETQR_BANK_ID = os.environ.get('VIETQR_BANK_ID', '').strip()       # vd: 'mbbank', 'vietinbank', hoặc mã BIN '970422'
VIETQR_ACCOUNT_NO = os.environ.get('VIETQR_ACCOUNT_NO', '').strip()
VIETQR_ACCOUNT_NAME = os.environ.get('VIETQR_ACCOUNT_NAME', '').strip()
BANK_TRANSFER_ENABLED = bool(VIETQR_BANK_ID and VIETQR_ACCOUNT_NO and VIETQR_ACCOUNT_NAME)
if not BANK_TRANSFER_ENABLED:
    print("ℹ️  Thanh toán Chuyển khoản VietQR đang TẮT — chưa cấu hình VIETQR_BANK_ID/"
          "VIETQR_ACCOUNT_NO/VIETQR_ACCOUNT_NAME trong .env. Xem README mục 14 để bật.")

PAYMENT_METHODS_ENABLED = VNPAY_ENABLED or BANK_TRANSFER_ENABLED


def generate_order_code():
    """Mã đơn hàng ngắn, duy nhất — dùng làm vnp_TxnRef (VNPAY) và nội dung chuyển khoản
    (VietQR) nên phải NGẮN, chỉ chữ+số (không dấu, không khoảng trắng) để tránh lỗi ký tự
    đặc biệt khi ngân hàng/VNPAY xử lý nội dung giao dịch."""
    return 'SM' + datetime.now(timezone.utc).strftime('%y%m%d') + secrets.token_hex(3).upper()


def add_one_month(dt):
    """Cộng đúng 1 THÁNG LỊCH (không phải 30 ngày) — vd 31/1 + 1 tháng = 28 hoặc 29/2 (tự
    kẹp về ngày cuối cùng của tháng đích nếu tháng đích ngắn hơn). Chỉ dùng thư viện chuẩn
    (datetime + calendar), không cần cài thêm dateutil."""
    import calendar
    year = dt.year + (dt.month // 12)
    month = dt.month % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    day = min(dt.day, last_day)
    return dt.replace(year=year, month=month, day=day)


def compute_checkout_price(user_id, plan):
    """Tính giá THÁNG NÀY cho 1 gói, áp dụng ưu đãi lần đầu nếu còn hạn mức (xem
    FIRST_TIME_DISCOUNT_PCT/FIRST_TIME_DISCOUNT_MONTHS). Trả về (amount, base_amount,
    is_discounted, paid_months_so_far)."""
    base_amount = PLAN_PRICING[plan]
    conn = open_write_db()
    try:
        paid_count = conn.execute(
            "SELECT COUNT(*) c FROM payment_orders WHERE user_id = ? AND status = 'paid'", (user_id,)
        ).fetchone()['c']
    finally:
        conn.close()
    is_discounted = paid_count < FIRST_TIME_DISCOUNT_MONTHS
    amount = round(base_amount * (100 - FIRST_TIME_DISCOUNT_PCT) / 100) if is_discounted else base_amount
    return amount, base_amount, is_discounted, paid_count


def vietqr_image_url(amount, order_code):
    """Trả về link ảnh QR chuyển khoản (dịch vụ công khai img.vietqr.io, không cần API key).
    Học sinh quét mã này bằng app ngân hàng hoặc MoMo/ZaloPay để chuyển thẳng vào tài khoản
    ngân hàng đã cấu hình — số tiền + nội dung chuyển khoản được điền sẵn trong mã QR."""
    from urllib.parse import quote
    return (
        f"https://img.vietqr.io/image/{quote(VIETQR_BANK_ID)}-{quote(VIETQR_ACCOUNT_NO)}-compact2.png"
        f"?amount={int(amount)}&addInfo={quote(order_code)}&accountName={quote(VIETQR_ACCOUNT_NAME)}"
    )


def vnpay_sign(params: dict) -> str:
    """Ký (hoặc xác thực) dữ liệu theo đúng thuật toán VNPAY yêu cầu: sắp xếp key theo
    alphabet, nối thành query string đã URL-encode giá trị, rồi HMAC-SHA512 với vnp_HashSecret.
    Dùng chung cho cả lúc TẠO link thanh toán lẫn lúc XÁC THỰC callback (Return URL / IPN)."""
    sorted_items = sorted(params.items())
    query_string = urlencode(sorted_items, quote_via=quote_plus)
    return hmac.new(
        VNPAY_HASH_SECRET.encode('utf-8'), query_string.encode('utf-8'), hashlib.sha512
    ).hexdigest()


def vnpay_build_payment_url(order_code, amount, order_info, ip_addr, return_url):
    now = datetime.now(timezone(timedelta(hours=7)))  # giờ Việt Nam (ICT, UTC+7) theo yêu cầu VNPAY
    expire = now + timedelta(minutes=15)
    params = {
        'vnp_Version': '2.1.0',
        'vnp_Command': 'pay',
        'vnp_TmnCode': VNPAY_TMN_CODE,
        'vnp_Amount': str(int(amount) * 100),  # VNPAY yêu cầu nhân 100 (không thập phân)
        'vnp_CurrCode': 'VND',
        'vnp_TxnRef': order_code,
        'vnp_OrderInfo': order_info,
        'vnp_OrderType': 'other',
        'vnp_Locale': 'vn',
        'vnp_ReturnUrl': return_url,
        'vnp_IpAddr': ip_addr or '127.0.0.1',
        'vnp_CreateDate': now.strftime('%Y%m%d%H%M%S'),
        'vnp_ExpireDate': expire.strftime('%Y%m%d%H%M%S'),
    }
    secure_hash = vnpay_sign(params)
    query_string = urlencode(sorted(params.items()), quote_via=quote_plus)
    return f"{VNPAY_PAYMENT_URL}?{query_string}&vnp_SecureHash={secure_hash}"


def vnpay_verify_return(args: dict) -> bool:
    """Xác thực chữ ký vnp_SecureHash trên dữ liệu VNPAY gửi về (Return URL hoặc IPN).
    PHẢI gọi hàm này trước khi tin bất kỳ thông tin nào (mã đơn, trạng thái...) trong `args` —
    tuyệt đối không tự ý cập nhật đơn hàng thành "đã thanh toán" nếu chữ ký sai."""
    received_hash = args.get('vnp_SecureHash', '')
    check_params = {k: v for k, v in args.items() if k not in ('vnp_SecureHash', 'vnp_SecureHashType')}
    expected_hash = vnpay_sign(check_params)
    return hmac.compare_digest(received_hash, expected_hash)


def grant_plan_upgrade(user_id, plan, order_code, actor='system', months=1):
    """Gán gói THEO THÁNG cho tài khoản — hạn dùng luôn tính lại từ THỜI ĐIỂM GÁN (không cộng
    dồn vào hạn cũ nếu gia hạn sớm, để tránh rắc rối tính toán khi đổi qua lại Premium/Max).
    Gọi khi: thanh toán được xác nhận (VNPAY IPN tự động, hoặc Admin xác nhận chuyển khoản thủ
    công), hoặc Admin "tặng" 1 tháng miễn phí cho tài khoản không phải Developer trở lên (xem
    developer_change_plan()). Dùng open_write_db() vì VNPAY IPN có thể tới bất kỳ lúc nào,
    không nhất thiết trong 1 request context bình thường có sẵn `g`."""
    expires_at = add_one_month(datetime.now(timezone.utc)) if months == 1 else \
        datetime.now(timezone.utc) + timedelta(days=30 * months)
    conn = open_write_db()
    try:
        conn.execute('UPDATE users SET plan = ?, plan_expires_at = ? WHERE id = ?',
                     (plan, expires_at.isoformat(), user_id))
        conn.commit()
    finally:
        conn.close()
    write_audit('grant_plan_upgrade', target=str(user_id),
                detail=f"{plan} tới {expires_at.strftime('%d/%m/%Y')} (đơn {order_code}, xác nhận bởi {actor})")


def plan_price(plan):
    return PLAN_PRICING.get(plan)


# ==========================================
# 0.27. "BỘ NHỚ" AI — ghi nhớ cách học của từng học sinh, cá nhân hoá câu trả lời
# ==========================================
# Không gọi thêm API AI nào để trích xuất — chỉ dùng quy tắc (regex) đơn giản, nhanh và
# miễn phí. Độ chính xác vì vậy phụ thuộc vào cách học sinh diễn đạt, không phải AI tự suy
# luận/tổng hợp như một hệ thống Memory "đầy đủ" sẽ cần (xem ghi chú trong README).
MEMORY_TRIGGER_RE = re.compile(
    r'(?:ghi\s*nhớ|hãy\s*nhớ|nhớ\s*giúp|note\s*giúp|lưu\s*ý\s*giúp)(?:\s*(?:em|mình|giúp|rằng|là))*\s*[:,-]?\s*(.+)',
    re.IGNORECASE
)
GRADE_LEVEL_RE = re.compile(r'\blớp\s*(6|7|8|9|10|11|12)\b', re.IGNORECASE)
WEAK_HINT_RE = re.compile(r'yếu|kém|khó\s*hiểu|hay\s*sai|hay\s*nhầm', re.IGNORECASE)
GOAL_HINT_RE = re.compile(r'mục\s*tiêu|muốn\s*(đạt|thi|ôn)|ôn\s*thi|thi\s*vào', re.IGNORECASE)
STYLE_HINT_RE = re.compile(r'thích.*giải\s*thích|giải\s*thích.*(ngắn|dài|kỹ|đơn giản|chi tiết)', re.IGNORECASE)

MAX_MEMORY_LEN = 300
MAX_MEMORIES_IN_PROMPT = 6

MEMORY_CATEGORY_LABELS = {
    'weak_subject':     ('📉', 'Môn/chủ đề còn yếu'),
    'goal':             ('🎯', 'Mục tiêu học tập'),
    'style_preference': ('🎨', 'Cách giải thích ưa thích'),
    'topic_covered':    ('📚', 'Chủ đề đã luyện tập'),
    'general':          ('📝', 'Khác'),
}


def _guess_memory_category(text):
    if WEAK_HINT_RE.search(text):
        return 'weak_subject'
    if GOAL_HINT_RE.search(text):
        return 'goal'
    if STYLE_HINT_RE.search(text):
        return 'style_preference'
    return 'general'


def save_memory(user_id, content, category='general', source='auto'):
    """Lưu 1 mục bộ nhớ. Dùng open_write_db() vì hàm này còn được gọi từ BÊN TRONG generator
    streaming của /api/chat (xem giải thích ở docstring open_write_db())."""
    content = (content or '').strip()
    if not content:
        return
    content = content[:MAX_MEMORY_LEN]
    try:
        conn = open_write_db()
        try:
            conn.execute(
                'INSERT INTO memories (user_id, content, category, source, created_at) VALUES (?, ?, ?, ?, ?)',
                (user_id, content, category, source, now_iso())
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def extract_and_save_memory(user_id, user_message):
    """Phát hiện + lưu 1 'bộ nhớ' mới từ tin nhắn của học sinh (nếu có). Trả về nội dung vừa
    ghi nhớ (để báo lại cho học sinh biết qua 1 toast nhỏ), hoặc None nếu không có gì."""
    text = (user_message or '').strip()
    if not text:
        return None

    # 1) Học sinh chủ động yêu cầu ghi nhớ — ưu tiên cao nhất, tự đoán category theo từ khoá.
    m = MEMORY_TRIGGER_RE.search(text)
    if m:
        content = m.group(1).strip(' .!?')
        if content:
            save_memory(user_id, content, category=_guess_memory_category(content), source='explicit')
            return content

    # 2) Tự nhận diện lớp học (chỉ lưu 1 lần, tránh lặp lại mỗi khi học sinh gõ "lớp 8").
    g = GRADE_LEVEL_RE.search(text)
    if g:
        try:
            conn = open_write_db()
            try:
                existing = conn.execute(
                    "SELECT id FROM memories WHERE user_id = ? AND content LIKE 'Học sinh đang học lớp%'",
                    (user_id,)
                ).fetchone()
                if not existing:
                    content = f"Học sinh đang học lớp {g.group(1)}."
                    conn.execute(
                        'INSERT INTO memories (user_id, content, category, source, created_at) VALUES (?, ?, ?, ?, ?)',
                        (user_id, content, 'general', 'auto', now_iso())
                    )
                    conn.commit()
                    return content
            finally:
                conn.close()
        except Exception:
            pass

    return None


def track_topic_practice(user_id, subject, mode):
    """Tín hiệu 'chủ đề đã luyện tập' đơn giản: nếu học sinh làm 'Kiểm tra bài làm' từ 3 lần
    trở lên ở cùng 1 môn, tự ghi 1 mục bộ nhớ. KHÔNG suy diễn lỗi sai cụ thể là gì (muốn làm
    được vậy cần AI phân tích riêng câu trả lời — xem mục "Mistake Book" trong README)."""
    if mode != 'Kiểm tra bài làm' or not subject:
        return
    try:
        conn = open_write_db()
        try:
            marker = f"Học sinh luyện tập nhiều bài kiểm tra môn {subject}."
            existing = conn.execute(
                'SELECT id FROM memories WHERE user_id = ? AND content = ?', (user_id, marker)
            ).fetchone()
            if existing:
                return
            count_row = conn.execute(
                "SELECT COUNT(*) c FROM usage_logs WHERE user_id = ? AND subject = ? AND mode = 'Kiểm tra bài làm'",
                (user_id, subject)
            ).fetchone()
            if count_row['c'] >= 3:
                conn.execute(
                    'INSERT INTO memories (user_id, content, category, source, created_at) VALUES (?, ?, ?, ?, ?)',
                    (user_id, marker, 'topic_covered', 'auto', now_iso())
                )
                conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def get_recent_memories(user_id, limit=MAX_MEMORIES_IN_PROMPT):
    conn = open_write_db()
    try:
        rows = conn.execute(
            'SELECT content FROM memories WHERE user_id = ? ORDER BY created_at DESC LIMIT ?',
            (user_id, limit)
        ).fetchall()
        return [r['content'] for r in reversed(rows)]  # cũ -> mới, đọc tự nhiên hơn trong prompt
    finally:
        conn.close()


# ==========================================
# 0.28. GAMIFICATION NHẸ — XP + Streak (chuỗi ngày học liên tiếp) + Thành tựu
# ==========================================
XP_PER_TURN = 10
XP_PER_LEVEL = 100
ACHIEVEMENTS_META = {
    'first_lesson':  {'icon': '🧠', 'label': 'Bài học đầu tiên', 'desc': 'Hoàn thành lượt hỏi AI đầu tiên.'},
    'streak_7':      {'icon': '🔥', 'label': 'Chuỗi 7 ngày', 'desc': 'Học liên tục 7 ngày không nghỉ.'},
    'streak_30':     {'icon': '🏆', 'label': 'Chuỗi 30 ngày', 'desc': 'Học liên tục 30 ngày không nghỉ.'},
    'questions_100': {'icon': '📚', 'label': '100 câu hỏi', 'desc': 'Đã hỏi AI 100 lượt.'},
    'first_deck':    {'icon': '🗂️', 'label': 'Bộ thẻ đầu tiên', 'desc': 'Tạo bộ thẻ ghi nhớ đầu tiên.'},
    'game_player':   {'icon': '🎮', 'label': 'Người chơi mới', 'desc': 'Hoàn thành 1 trò chơi luyện tập.'},
    'first_mistake': {'icon': '📕', 'label': 'Tự nhận ra lỗi', 'desc': 'Ghi lại lỗi sai đầu tiên vào Sổ lỗi sai.'},
    'first_quiz':    {'icon': '📝', 'label': 'Quiz đầu tiên', 'desc': 'Hoàn thành 1 bài quiz.'},
    'perfect_quiz':  {'icon': '💯', 'label': 'Điểm tuyệt đối', 'desc': 'Đạt 100% điểm 1 bài quiz.'},
    'first_plan':    {'icon': '🎯', 'label': 'Kế hoạch đầu tiên', 'desc': 'Tạo kế hoạch ôn tập đầu tiên.'},
    'plan_finisher':  {'icon': '🏆', 'label': 'Về đích', 'desc': 'Hoàn thành trọn vẹn 1 kế hoạch ôn tập.'},
    'speed_demon':   {'icon': '⚡', 'label': 'Tia chớp', 'desc': 'Đạt combo 10 câu đúng liên tiếp trong Đố Vui Tính Nhanh.'},
    'perfect_run':   {'icon': '🎯', 'label': 'Không sai một câu', 'desc': 'Trả lời đúng 100% trong 1 ván (từ 10 câu trở lên).'},
    'snake_master':  {'icon': '🐍', 'label': 'Trăn Thần', 'desc': 'Đạt độ dài 15 ô trong 1 ván Rắn Săn Chữ.'},
}


def award_xp_and_streak(user_id, xp_amount=XP_PER_TURN, extra_achievement_checks=None):
    """Cộng XP + cập nhật streak sau 1 hoạt động học tập THÀNH CÔNG (trả lời chat, hoặc chơi
    xong 1 game luyện tập — xem api_game_complete()). Gọi từ bên trong generator streaming
    của /api/chat nên dùng open_write_db() (xem docstring open_write_db()). Trả về dict mô tả
    những gì vừa xảy ra (lên cấp? thành tựu mới?) để báo ngay trên giao diện.
    `extra_achievement_checks`: dict {code: điều_kiện_bool} để kiểm tra thêm thành tựu đặc thù
    theo ngữ cảnh gọi (vd: 'first_deck' khi vừa tạo xong bộ thẻ đầu tiên)."""
    result = {'leveled_up': False, 'new_achievements': [], 'streak_days': 0, 'xp': 0, 'level': 1}
    try:
        conn = open_write_db()
        try:
            today = datetime.now(timezone(timedelta(hours=7))).strftime('%Y-%m-%d')
            row = conn.execute('SELECT * FROM user_stats WHERE user_id = ?', (user_id,)).fetchone()
            if row is None:
                conn.execute(
                    'INSERT INTO user_stats (user_id, xp, streak_days, longest_streak, last_active_date) '
                    'VALUES (?, 0, 0, 0, NULL)', (user_id,)
                )
                conn.commit()
                row = conn.execute('SELECT * FROM user_stats WHERE user_id = ?', (user_id,)).fetchone()

            old_level = row['xp'] // XP_PER_LEVEL + 1
            new_xp = row['xp'] + xp_amount

            last_active = row['last_active_date']
            streak = row['streak_days']
            if last_active == today:
                pass  # đã hoạt động hôm nay rồi — không tăng streak thêm lần nữa
            elif last_active is None:
                streak = 1
            else:
                try:
                    last_date = datetime.strptime(last_active, '%Y-%m-%d').date()
                    today_date = datetime.strptime(today, '%Y-%m-%d').date()
                    gap = (today_date - last_date).days
                    streak = streak + 1 if gap == 1 else 1
                except ValueError:
                    streak = 1
            longest = max(row['longest_streak'], streak)

            conn.execute(
                'UPDATE user_stats SET xp = ?, streak_days = ?, longest_streak = ?, last_active_date = ? WHERE user_id = ?',
                (new_xp, streak, longest, today, user_id)
            )
            conn.commit()

            new_level = new_xp // XP_PER_LEVEL + 1
            result.update({'streak_days': streak, 'xp': new_xp, 'level': new_level, 'leveled_up': new_level > old_level})

            earned_codes = {r['code'] for r in conn.execute(
                'SELECT code FROM achievements WHERE user_id = ?', (user_id,)).fetchall()}
            total_turns = conn.execute(
                "SELECT COUNT(*) c FROM usage_logs WHERE user_id = ? AND status = 'ok'", (user_id,)
            ).fetchone()['c']

            to_check = []
            if 'first_lesson' not in earned_codes and total_turns >= 1:
                to_check.append('first_lesson')
            if 'streak_7' not in earned_codes and streak >= 7:
                to_check.append('streak_7')
            if 'streak_30' not in earned_codes and streak >= 30:
                to_check.append('streak_30')
            if 'questions_100' not in earned_codes and total_turns >= 100:
                to_check.append('questions_100')
            for code, condition in (extra_achievement_checks or {}).items():
                if code not in earned_codes and condition:
                    to_check.append(code)

            for code in to_check:
                try:
                    conn.execute('INSERT INTO achievements (user_id, code, earned_at) VALUES (?, ?, ?)',
                                 (user_id, code, now_iso()))
                    conn.commit()
                    result['new_achievements'].append(code)
                except sqlite3.IntegrityError:
                    pass  # trùng UNIQUE(user_id, code) — hiếm gặp, bỏ qua an toàn
        finally:
            conn.close()
    except Exception:
        pass
    return result


def get_user_stats(user_id):
    conn = open_write_db()
    try:
        row = conn.execute('SELECT * FROM user_stats WHERE user_id = ?', (user_id,)).fetchone()
        achievements_rows = conn.execute(
            'SELECT code, earned_at FROM achievements WHERE user_id = ? ORDER BY earned_at ASC', (user_id,)
        ).fetchall()
        xp = row['xp'] if row else 0
        level = xp // XP_PER_LEVEL + 1
        return {
            'xp': xp,
            'level': level,
            'xp_into_level': xp % XP_PER_LEVEL,
            'xp_per_level': XP_PER_LEVEL,
            'streak_days': row['streak_days'] if row else 0,
            'longest_streak': row['longest_streak'] if row else 0,
            'achievements': [
                {'code': a['code'], **ACHIEVEMENTS_META.get(a['code'], {'icon': '🏅', 'label': a['code'], 'desc': ''}),
                 'earned_at': a['earned_at']}
                for a in achievements_rows
            ],
        }
    finally:
        conn.close()


def plan_rank(plan):
    try:
        return PLAN_ORDER.index(plan)
    except ValueError:
        return 0


def plan_meta(plan):
    return PLAN_META.get(plan, PLAN_META['free'])


def plan_limits(plan):
    return PLAN_LIMITS.get(plan, PLAN_LIMITS['free'])


def effective_plan(user):
    """Gói THỰC TẾ đang áp dụng cho tài khoản. Developer trở lên luôn là 'max' bất kể cột
    `plan` lưu gì trong DB. Với tài khoản user thường: Premium/Max chỉ có hiệu lực nếu
    `plan_expires_at` còn hạn (gói tính THEO THÁNG — xem grant_plan_upgrade()); hết hạn thì
    tự động coi như 'free' ngay khi đọc (tính "lazy", không cần job nền dọn dẹp DB — cột
    `plan` trong DB có thể tạm thời vẫn còn ghi 'premium'/'max' cũ, nhưng hàm này luôn trả về
    giá trị ĐÚNG THỜI ĐIỂM HIỆN TẠI)."""
    if not user:
        return 'free'
    try:
        if role_rank(user['role']) >= role_rank('developer'):
            return 'max'
    except Exception:
        pass
    try:
        plan = user['plan']
        expires_at = user['plan_expires_at']
    except Exception:
        plan, expires_at = None, None
    if plan not in PLAN_ORDER or plan == 'free':
        return 'free'
    if not expires_at:
        return 'free'  # gói trả phí PHẢI có hạn sử dụng — không có hạn coi như đã hết hạn
    try:
        exp_dt = datetime.fromisoformat(expires_at)
        if exp_dt.tzinfo is None:
            exp_dt = exp_dt.replace(tzinfo=timezone.utc)
        if exp_dt > datetime.now(timezone.utc):
            return plan
    except ValueError:
        pass
    return 'free'


def current_effective_plan():
    return effective_plan(current_user())


def app_display_name(user):
    """Tên hiển thị của app, gắn theo gói thực tế của tài khoản đang đăng nhập — tài khoản
    Free thấy tên trơn "StudyMate AI" (không còn chữ "Pro"); Premium/Max thấy tên có gắn gói."""
    plan = effective_plan(user)
    if plan == 'max':
        return 'StudyMate AI Max'
    if plan == 'premium':
        return 'StudyMate AI Premium'
    return 'StudyMate AI'


# "Chế độ suy nghĩ" — các mức độ suy luận sâu/rộng khác nhau mà AI sẽ áp dụng khi trả lời.
# Đặt tên theo hành trình một học sinh "lên trình": Trợ Lý (mặc định, mọi gói) → Học Giả /
# Giáo Sư (mở khoá từ Premium) → Thiên Tài (độc quyền Max — mạnh nhất, kết hợp cả hai).
THINKING_MODE_ORDER = ['standard', 'scholar', 'professor', 'genius']
THINKING_MODES = {
    'standard': {
        'key': 'standard', 'label': 'Trợ Lý', 'icon': '💬', 'min_plan': 'free', 'max_tokens': 800,
        'desc': 'Phản hồi nhanh, cân bằng — phù hợp phần lớn câu hỏi hằng ngày.',
        'prompt_hint': '',
    },
    'scholar': {
        'key': 'scholar', 'label': 'Học Giả', 'icon': '📖', 'min_plan': 'premium', 'max_tokens': 1400,
        'desc': 'Suy luận từng bước kỹ càng hơn trước khi trả lời — hợp bài khó, cần độ chính xác cao.',
        'prompt_hint': ('Hãy suy nghĩ cẩn thận, từng bước một trong đầu, kiểm tra lại logic trước khi '
                         'đưa ra câu trả lời cuối cùng. Trình bày ngắn gọn các bước suy luận chính, '
                         'sau đó kết luận thật rõ ràng.'),
    },
    'professor': {
        'key': 'professor', 'label': 'Giáo Sư', 'icon': '🎓', 'min_plan': 'premium', 'max_tokens': 1600,
        'desc': 'Giải thích mở rộng hơn — nhiều ví dụ, liên hệ thực tế, nhiều góc nhìn.',
        'prompt_hint': ('Hãy giải thích mở rộng và sâu hơn bình thường: thêm ví dụ minh hoạ, liên hệ '
                         'thực tế, so sánh nhiều cách tiếp cận nếu phù hợp — giúp học sinh hiểu bản '
                         'chất chứ không chỉ nhớ đáp số.'),
    },
    'genius': {
        'key': 'genius', 'label': 'Thiên Tài', 'icon': '🌟', 'min_plan': 'max', 'max_tokens': 2200,
        'desc': 'Kết hợp suy luận sâu nhất + mở rộng kiến thức tối đa — chế độ mạnh nhất, độc quyền Max.',
        'prompt_hint': ('Hãy kết hợp cả hai: suy luận từng bước thật kỹ để đảm bảo chính xác tuyệt đối, '
                         'đồng thời giải thích mở rộng với ví dụ, liên hệ thực tế và nhiều góc nhìn. Đây '
                         'là chế độ mạnh nhất — hãy đầu tư chất lượng tối đa cho câu trả lời.'),
    },
}


def thinking_mode_unlocked(mode_key, plan):
    tm = THINKING_MODES.get(mode_key)
    if not tm:
        return False
    return plan_rank(plan) >= plan_rank(tm['min_plan'])


def resolve_thinking_mode(requested_key, plan):
    """Trả về key hợp lệ: nếu chế độ yêu cầu không tồn tại hoặc vượt quá gói hiện tại
    (kể cả khi client cố tình gửi thẳng key bị khoá qua API) thì rơi về 'standard'."""
    if requested_key in THINKING_MODES and thinking_mode_unlocked(requested_key, plan):
        return requested_key
    return 'standard'


def open_write_db():
    """Kết nối SQLite RIÊNG, không qua `g`/`get_db()`.

    Lý do cần cái này: `stream_with_context` (dùng cho các response dạng SSE streaming,
    xem `chat()` bên dưới) chỉ "hoãn" được request/session/g ở mức tham chiếu Python —
    nó KHÔNG ngăn được `teardown_appcontext` (hàm `close_db`) chạy sớm hơn generator.
    Trong Flask hiện tại, `ctx.pop()` ở cuối `wsgi_app()` (đóng kết nối `g._database`
    qua `close_db`) xảy ra NGAY khi view function return Response — tức là TRƯỚC khi
    generator SSE thật sự bắt đầu chạy và stream dữ liệu. Nếu generator dùng lại
    `get_db()`/`g._database` để ghi DB, nó sẽ gặp lỗi "Cannot operate on a closed
    database" vì kết nối đó đã bị `close_db` đóng mất rồi.
    Giải pháp: bất kỳ đoạn code nào ghi DB BÊN TRONG một generator streaming (SSE) đều
    phải tự mở kết nối riêng bằng hàm này, dùng xong tự đóng — không phụ thuộc vào `g`."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def log_usage(user_id, subject, mode, message_chars, response_chars, had_file, had_image, status):
    """Ghi lại 1 lượt sử dụng AI vào bảng usage_logs, phục vụ trang thống kê developer.
    Dùng kết nối riêng (open_write_db) vì hàm này thường được gọi từ BÊN TRONG generator
    streaming của /api/chat, lúc đó `g`/`get_db()` có thể đã bị teardown (xem giải thích
    ở open_write_db)."""
    try:
        conn = open_write_db()
        try:
            conn.execute(
                '''INSERT INTO usage_logs
                   (user_id, endpoint, subject, mode, message_chars, response_chars, had_file, had_image, status, created_at)
                   VALUES (?, 'chat', ?, ?, ?, ?, ?, ?, ?, ?)''',
                (user_id, subject, mode, message_chars, response_chars, int(bool(had_file)), int(bool(had_image)),
                 status, now_iso())
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        # Không để lỗi ghi log ảnh hưởng tới trải nghiệm chat của học sinh.
        pass


# ==========================================
# 0.3. CẤU HÌNH HỆ THỐNG (settings key-value) — do developer chỉnh qua /developer
# ==========================================
def get_setting(key, default=None):
    """Đọc 1 giá trị cấu hình từ bảng settings. Cố ý dùng kết nối RIÊNG (open_write_db(),
    không phải get_db()/g) vì hàm này còn được gọi từ BÊN TRONG generator streaming của
    /api/chat (qua stream_consolex_ai() -> đọc ai_model_override/ai_temperature_override),
    lúc đó `g._database` có thể đã bị teardown — xem giải thích chi tiết ở open_write_db()."""
    conn = open_write_db()
    try:
        row = conn.execute('SELECT value FROM settings WHERE key = ?', (key,)).fetchone()
        return row['value'] if row and row['value'] is not None else default
    finally:
        conn.close()


def set_setting(key, value):
    conn = open_write_db()
    try:
        conn.execute(
            'INSERT INTO settings (key, value) VALUES (?, ?) '
            'ON CONFLICT(key) DO UPDATE SET value = excluded.value',
            (key, value)
        )
        conn.commit()
    finally:
        conn.close()


def google_login_effective_enabled():
    """Developer có thể tạm tắt nút đăng nhập Google từ trang /developer mà không cần
    sửa .env. Nếu chưa từng đặt override, hành vi mặc định vẫn theo cấu hình .env."""
    override = get_setting('google_login_override', '')
    if override == 'off':
        return False
    if override == 'on':
        return bool(GOOGLE_OAUTH_ENABLED)  # không thể "bật" nếu chưa có Client ID/Secret
    return GOOGLE_OAUTH_ENABLED


def guest_login_effective_enabled():
    """Cho phép 'Dùng thử ngay, không cần đăng ký' — MẶC ĐỊNH BẬT (khác Google, không cần
    cấu hình .env gì để dùng được). Developer có thể tắt từ /developer nếu không muốn nữa."""
    return get_setting('guest_login_override', 'on') != 'off'


# ==========================================
# 0.35. "STUDYMATE LAB" — FEATURE REGISTRY + FEATURE FLAGS
# ==========================================
# Hạ tầng đăng ký & bật/tắt tính năng thử nghiệm theo cấp độ, KHÔNG cần sửa code/deploy lại —
# sẵn sàng để các tính năng thử nghiệm sau này cắm vào qua is_feature_enabled(key). Hiện tại
# CHƯA có tính năng nào trong app thật sự bị gate bởi flag (chưa có gì đang "thử nghiệm dở
# dang" để cần che giấu người dùng) — đây là hạ tầng chuẩn bị sẵn, Developer/Admin tạo & quản
# lý ngay trong /developer/lab.
FEATURE_FLAG_STATUSES = ('off', 'internal', 'beta', 'public', 'archived')
FEATURE_CATEGORIES = ('games', 'ai', 'ui', 'learning', 'teacher', 'developer', 'other')
FEATURE_ENVIRONMENTS = ('development', 'sandbox', 'staging', 'production')
FEATURE_ROLLOUT_STEPS = (1, 5, 10, 25, 50, 75, 100)


def _feature_rollout_bucket(key, user_id):
    """Gán 1 người dùng vào nhóm 0-99 CỐ ĐỊNH cho 1 flag cụ thể — dùng hash ổn định (không
    phải random() mỗi lần gọi) để CÙNG 1 người dùng luôn nhận CÙNG 1 kết quả cho rollout %,
    không bị 'nhấp nháy' bật/tắt qua lại giữa các lần tải trang."""
    digest = hashlib.sha256(f"{key}:{user_id}".encode('utf-8')).hexdigest()
    return int(digest[:8], 16) % 100


def is_feature_enabled(key, user=None):
    """off/archived: tắt hẳn cho tất cả, kể cả Developer (đóng dứt điểm, không cần xoá flag).
    internal: chỉ Developer trở lên. beta: Developer trở lên LUÔN thấy (để test); người dùng
    thường được thấy theo đúng % rollout, gán CỐ ĐỊNH theo tài khoản (xem
    _feature_rollout_bucket). public: bật cho mọi người. Flag chưa tồn tại -> False (an toàn
    mặc định). Nếu flag có phụ thuộc (depends_on) mà phụ thuộc đó KHÔNG bật cho chính người
    dùng này thì flag này cũng không thể bật, dù trạng thái riêng của nó là gì."""
    db = get_db()
    row = db.execute('SELECT * FROM feature_flags WHERE key = ?', (key,)).fetchone()
    if not row:
        return False

    u = user if user is not None else current_user()
    is_dev_plus = bool(u) and role_rank(u['role']) >= role_rank('developer')

    status = row['status']
    if status in ('off', 'archived'):
        return False

    if status == 'internal':
        result = is_dev_plus
    elif status == 'beta':
        if is_dev_plus:
            result = True
        elif not u:
            result = False
        else:
            result = _feature_rollout_bucket(key, u['id']) < max(0, min(100, row['rollout_pct']))
    elif status == 'public':
        result = True
    else:
        result = False

    if not result:
        return False

    deps = [d.strip() for d in (row['depends_on'] or '').split(',') if d.strip()]
    for dep_key in deps:
        if not is_feature_enabled(dep_key, u):
            return False

    return True


# ==========================================
# 0.4. TUỲ CHỈNH CÁ NHÂN (preferences theo tài khoản)
# ==========================================
PREFERENCE_DEFAULTS = {
    'theme': 'system',            # 'light' | 'dark' | 'system'
    'language': 'vi',             # 'vi' | 'en'
    'default_subject': 'Toán',
    'default_mode': 'Giải thích',
    'default_thinking_mode': 'standard',  # 'standard' | 'scholar' | 'professor' | 'genius'
}
ALLOWED_PREFERENCE_KEYS = set(PREFERENCE_DEFAULTS.keys())


def get_user_preferences(user_id):
    db = get_db()
    row = db.execute('SELECT preferences FROM users WHERE id = ?', (user_id,)).fetchone()
    raw = row['preferences'] if row and row['preferences'] else '{}'
    try:
        prefs = json.loads(raw)
        if not isinstance(prefs, dict):
            prefs = {}
    except Exception:
        prefs = {}
    merged = dict(PREFERENCE_DEFAULTS)
    merged.update({k: v for k, v in prefs.items() if k in ALLOWED_PREFERENCE_KEYS})
    return merged


def save_user_preferences(user_id, updates):
    prefs = get_user_preferences(user_id)
    if isinstance(updates, dict):
        prefs.update({k: v for k, v in updates.items() if k in ALLOWED_PREFERENCE_KEYS and isinstance(v, str)})
    db = get_db()
    db.execute('UPDATE users SET preferences = ? WHERE id = ?', (json.dumps(prefs, ensure_ascii=False), user_id))
    db.commit()
    return prefs


# ==========================================
# 1. GIAO DIỆN ĐĂNG NHẬP / ĐĂNG KÝ
# ==========================================
# ==========================================
# 1.5 KẾT QUẢ THANH TOÁN VNPAY (trang trung gian sau khi quay lại từ VNPAY)
# ==========================================
VNPAY_RETURN_HTML = r'''
<!DOCTYPE html>
<html lang="vi">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Kết quả thanh toán — StudyMate AI</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script>tailwind.config = { darkMode: 'class' };</script>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.6.0/css/all.min.css">
</head>
<body class="min-h-screen flex items-center justify-center bg-gray-50 dark:bg-[#131313] p-4">
  <div class="max-w-md w-full bg-white dark:bg-[#1c1c1c] rounded-2xl shadow-lg border border-gray-100 dark:border-gray-800 p-8 text-center">
    {% if success %}
      <div class="w-16 h-16 rounded-full bg-emerald-100 dark:bg-emerald-900 text-emerald-500 flex items-center justify-center text-3xl mx-auto mb-4">
        <i class="fas fa-check"></i>
      </div>
      <h1 class="text-xl font-bold mb-2">Thanh toán thành công!</h1>
      <p class="text-sm text-gray-500 dark:text-gray-400">
        Đơn <strong>{{ order_code }}</strong> đã được ghi nhận.
        {% if status == 'paid' %}Gói <strong>{{ plan_label }}</strong> của em đã được kích hoạt — quay lại trang chat và tải lại trang để thấy thay đổi nhé! 🎉
        {% else %}Hệ thống đang xử lý, thường chỉ mất vài giây. Em quay lại trang chat và tải lại trang sau ít phút nhé.{% endif %}
      </p>
    {% else %}
      <div class="w-16 h-16 rounded-full bg-red-100 dark:bg-red-900 text-red-500 flex items-center justify-center text-3xl mx-auto mb-4">
        <i class="fas fa-xmark"></i>
      </div>
      <h1 class="text-xl font-bold mb-2">Thanh toán không thành công</h1>
      <p class="text-sm text-gray-500 dark:text-gray-400">
        {% if not valid %}Không xác thực được dữ liệu trả về từ VNPAY.{% else %}Giao dịch <strong>{{ order_code }}</strong> chưa hoàn tất hoặc đã bị huỷ.{% endif %}
        Em có thể thử lại ở hộp thoại "Nâng cấp gói".
      </p>
    {% endif %}
    <a href="{{ url_for('home') }}" class="inline-block mt-6 px-5 py-2.5 rounded-xl bg-blue-600 hover:bg-blue-700 text-white text-sm font-semibold">
      <i class="fas fa-arrow-left mr-1"></i> Về trang chat
    </a>
  </div>
</body>
</html>
'''

RECOVERY_CODE_HTML = r'''
<!DOCTYPE html>
<html lang="vi">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Lưu mã khôi phục - StudyMate AI</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.6.0/css/all.min.css">
</head>
<body class="min-h-screen flex items-center justify-center bg-gradient-to-br from-indigo-600 to-purple-700 p-4">
  <div class="bg-white rounded-3xl shadow-2xl p-8 max-w-md w-full text-center">
    <div class="w-16 h-16 rounded-full bg-amber-100 text-amber-600 flex items-center justify-center mx-auto mb-4 text-2xl">
      <i class="fas fa-key"></i>
    </div>
    <h1 class="text-xl font-bold mb-2">
      {% if context == 'register' %}Lưu lại mã khôi phục của em!{% else %}Mã khôi phục MỚI của em{% endif %}
    </h1>
    <p class="text-sm text-gray-500 mb-5">
      Dùng mã này để lấy lại mật khẩu nếu quên — <strong>chỉ hiện đúng 1 lần này thôi</strong>, StudyMate không lưu lại bản gốc nên sẽ không hiện lại được nữa.
    </p>
    <div class="bg-gray-100 rounded-2xl py-4 px-3 mb-5">
      <p class="font-mono text-2xl font-bold tracking-wider text-indigo-700 select-all">{{ code }}</p>
    </div>
    <button onclick="copyCode()" id="copyBtn" class="w-full mb-3 px-4 py-2.5 rounded-xl bg-gray-100 hover:bg-gray-200 text-sm font-semibold text-gray-700">
      <i class="fas fa-copy mr-1"></i> Chép mã
    </button>
    <p class="text-xs text-gray-400 mb-5">Chụp màn hình hoặc chép vào nơi an toàn (không phải chat với bạn bè!) trước khi tiếp tục.</p>
    <a href="{{ url_for('home') }}" class="block w-full px-4 py-3 rounded-xl bg-indigo-600 hover:bg-indigo-700 text-white text-sm font-semibold">
      Em đã lưu rồi, tiếp tục →
    </a>
  </div>
  <script>
    function copyCode() {
      navigator.clipboard.writeText({{ code|tojson }}).then(() => {
        const btn = document.getElementById('copyBtn');
        btn.innerHTML = '<i class="fas fa-check mr-1"></i> Đã chép!';
        setTimeout(() => { btn.innerHTML = '<i class="fas fa-copy mr-1"></i> Chép mã'; }, 1500);
      });
    }
  </script>
</body>
</html>
'''


FORGOT_PASSWORD_HTML = r'''
<!DOCTYPE html>
<html lang="vi">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Quên mật khẩu - StudyMate AI</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.6.0/css/all.min.css">
</head>
<body class="min-h-screen flex items-center justify-center bg-gradient-to-br from-indigo-600 to-purple-700 p-4">
  <div class="bg-white rounded-3xl shadow-2xl p-8 max-w-md w-full">
    <div class="text-center mb-5">
      <div class="w-14 h-14 rounded-full bg-indigo-100 text-indigo-600 flex items-center justify-center mx-auto mb-3 text-xl">
        <i class="fas fa-unlock-keyhole"></i>
      </div>
      <h1 class="text-lg font-bold">Quên mật khẩu?</h1>
      <p class="text-sm text-gray-500 mt-1">Nhập mã khôi phục đã lưu lúc đăng ký để đặt mật khẩu mới.</p>
    </div>

    {% with messages = get_flashed_messages() %}
      {% if messages %}
        {% for m in messages %}
        <div class="text-sm text-red-600 bg-red-50 border border-red-100 rounded-xl px-4 py-2.5 mb-4">{{ m }}</div>
        {% endfor %}
      {% endif %}
    {% endwith %}

    <form method="POST" class="space-y-3">
      <input name="username" value="{{ username }}" placeholder="Tên đăng nhập" required
        class="w-full px-4 py-3 rounded-xl bg-gray-100 border-0 focus:outline-none focus:ring-2 focus:ring-indigo-500 text-sm">
      <input name="code" placeholder="Mã khôi phục (XXXX-XXXX-XXXX)" required maxlength="14"
        class="w-full px-4 py-3 rounded-xl bg-gray-100 border-0 focus:outline-none focus:ring-2 focus:ring-indigo-500 text-sm font-mono tracking-wide uppercase">
      <input name="new_password" type="password" placeholder="Mật khẩu mới (tối thiểu 6 ký tự)" required minlength="6"
        class="w-full px-4 py-3 rounded-xl bg-gray-100 border-0 focus:outline-none focus:ring-2 focus:ring-indigo-500 text-sm">
      <input name="confirm" type="password" placeholder="Nhập lại mật khẩu mới" required minlength="6"
        class="w-full px-4 py-3 rounded-xl bg-gray-100 border-0 focus:outline-none focus:ring-2 focus:ring-indigo-500 text-sm">
      <button type="submit" class="w-full px-4 py-3 rounded-xl bg-indigo-600 hover:bg-indigo-700 text-white text-sm font-semibold">Đặt mật khẩu mới</button>
    </form>

    <p class="text-center text-xs text-gray-400 mt-5">
      Không còn mã khôi phục (tài khoản tạo trước khi có tính năng này, hoặc làm mất mã)?
      Liên hệ Admin để được hỗ trợ đặt lại mật khẩu thủ công.
    </p>
    <p class="text-center text-sm text-gray-500 mt-3">
      <a href="{{ url_for('login_page') }}" class="text-indigo-600 font-semibold hover:underline">← Về trang đăng nhập</a>
    </p>
  </div>
</body>
</html>
'''


AUTH_HTML = r'''
<!DOCTYPE html>
<html lang="vi">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{{ 'Đăng nhập' if mode == 'login' else 'Đăng ký' }} — StudyMate AI</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script>tailwind.config = { darkMode: 'class' };</script>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.6.0/css/all.min.css">
  <style>
    body { font-family: 'Segoe UI', system-ui, sans-serif; }
    .auth-bg {
      background: radial-gradient(circle at 15% 20%, #4f46e5 0%, transparent 45%),
                  radial-gradient(circle at 85% 15%, #06b6d4 0%, transparent 40%),
                  radial-gradient(circle at 50% 90%, #6366f1 0%, transparent 45%),
                  linear-gradient(135deg, #0b1023 0%, #111827 100%);
    }
    .blob { position: absolute; border-radius: 9999px; filter: blur(70px); opacity: 0.35; animation: float 9s ease-in-out infinite; }
    @keyframes float { 0%,100% { transform: translateY(0) translateX(0); } 50% { transform: translateY(-25px) translateX(15px); } }
    .auth-card { animation: cardIn 0.5s cubic-bezier(.16,1,.3,1) both; }
    @keyframes cardIn { from { opacity: 0; transform: translateY(14px) scale(.98); } to { opacity: 1; transform: translateY(0) scale(1); } }
    .field-input:focus { box-shadow: 0 0 0 4px rgba(99,102,241,0.15); }
    .social-btn { transition: transform .15s ease, box-shadow .15s ease, background-color .15s ease; }
    .social-btn:hover { transform: translateY(-1px); box-shadow: 0 6px 16px rgba(0,0,0,0.08); }
    .social-btn:active { transform: translateY(0); }
    .primary-btn { transition: transform .15s ease, box-shadow .15s ease, opacity .15s ease; }
    .primary-btn:hover { transform: translateY(-1px); box-shadow: 0 10px 25px rgba(79,70,229,0.35); }
    .primary-btn:active { transform: translateY(0); }
  </style>
</head>
<body class="auth-bg min-h-screen flex items-center justify-center p-4 relative overflow-hidden">
  <div class="blob w-72 h-72 bg-indigo-500 top-[-4rem] left-[-4rem]"></div>
  <div class="blob w-80 h-80 bg-cyan-400 bottom-[-5rem] right-[-3rem]" style="animation-delay:-3s"></div>
  <div class="blob w-56 h-56 bg-violet-500 top-1/2 left-1/2" style="animation-delay:-6s"></div>

  <div class="w-full max-w-md relative z-10">
    <div class="text-center mb-7">
      <div class="inline-flex w-16 h-16 rounded-2xl bg-gradient-to-br from-indigo-500 to-cyan-400 items-center justify-center text-white text-3xl font-bold shadow-lg shadow-indigo-500/40">S</div>
      <h1 class="text-2xl font-extrabold mt-4 text-white tracking-tight">StudyMate AI</h1>
      <p class="text-indigo-200/80 text-sm mt-1">Gia sư AI thông minh cho học sinh THCS 🎓</p>
    </div>

    <div class="auth-card bg-white/95 backdrop-blur-xl rounded-3xl shadow-2xl border border-white/20 p-7 sm:p-8">
      <h2 class="text-xl font-bold text-gray-800 mb-1">{{ 'Chào mừng trở lại 👋' if mode == 'login' else 'Tạo tài khoản mới' }}</h2>
      <p class="text-sm text-gray-500 mb-6">{{ 'Đăng nhập để tiếp tục học cùng gia sư AI' if mode == 'login' else 'Chỉ mất chưa đến 1 phút để bắt đầu' }}</p>

      {% with messages = get_flashed_messages() %}
        {% if messages %}
          <div class="mb-5 space-y-2">
            {% for m in messages %}
              <div class="text-sm text-red-700 bg-red-50 border border-red-200 rounded-xl px-4 py-2.5 flex items-center gap-2">
                <i class="fas fa-circle-exclamation"></i> {{ m }}
              </div>
            {% endfor %}
          </div>
        {% endif %}
      {% endwith %}

      {% if google_enabled %}
      <div class="space-y-2.5 mb-5">
        <a href="{{ url_for('oauth_start', provider='google') }}"
           class="social-btn w-full flex items-center justify-center gap-3 px-4 py-3 rounded-xl border border-gray-200 bg-white hover:bg-gray-50 font-medium text-sm text-gray-700">
          <svg width="18" height="18" viewBox="0 0 48 48"><path fill="#FFC107" d="M43.6 20.5H42V20H24v8h11.3c-1.6 4.6-6 8-11.3 8-6.6 0-12-5.4-12-12s5.4-12 12-12c3.1 0 5.9 1.2 8 3.1l5.7-5.7C34.6 6 29.6 4 24 4 12.9 4 4 12.9 4 24s8.9 20 20 20 20-8.9 20-20c0-1.3-.1-2.7-.4-3.5z"/><path fill="#FF3D00" d="M6.3 14.7l6.6 4.8C14.6 15.9 18.9 13 24 13c3.1 0 5.9 1.2 8 3.1l5.7-5.7C34.6 6 29.6 4 24 4 16.3 4 9.7 8.3 6.3 14.7z"/><path fill="#4CAF50" d="M24 44c5.5 0 10.4-1.9 14.3-5.1l-6.6-5.6C29.6 35.5 26.9 36.5 24 36.5c-5.3 0-9.7-3.4-11.3-8.1l-6.5 5C9.6 39.6 16.2 44 24 44z"/><path fill="#1976D2" d="M43.6 20.5H42V20H24v8h11.3c-.8 2.3-2.2 4.3-4.1 5.7l6.6 5.6C41.9 36.6 44 30.8 44 24c0-1.3-.1-2.7-.4-3.5z"/></svg>
          Đăng nhập với Google
        </a>
      </div>
      <div class="flex items-center gap-3 mb-5">
        <div class="flex-1 h-px bg-gray-200"></div>
        <span class="text-xs text-gray-400 font-medium">hoặc dùng tên đăng nhập</span>
        <div class="flex-1 h-px bg-gray-200"></div>
      </div>
      {% endif %}

      <form method="POST" class="space-y-4">
        <div>
          <label class="block text-sm font-medium text-gray-600 mb-1.5">Tên đăng nhập</label>
          <div class="relative">
            <i class="fas fa-user absolute left-4 top-1/2 -translate-y-1/2 text-gray-400 text-sm"></i>
            <input type="text" name="username" required maxlength="32" value="{{ username or '' }}"
                   class="field-input w-full pl-10 pr-4 py-3 bg-gray-50 border border-gray-200 rounded-xl focus:outline-none focus:border-indigo-400 text-gray-800 transition-shadow"
                   placeholder="vd: hocsinh2026" autocomplete="username">
          </div>
        </div>
        <div>
          <label class="block text-sm font-medium text-gray-600 mb-1.5">Mật khẩu</label>
          <div class="relative">
            <i class="fas fa-lock absolute left-4 top-1/2 -translate-y-1/2 text-gray-400 text-sm"></i>
            <input id="pwInput" type="password" name="password" required minlength="6"
                   class="field-input w-full pl-10 pr-11 py-3 bg-gray-50 border border-gray-200 rounded-xl focus:outline-none focus:border-indigo-400 text-gray-800 transition-shadow"
                   placeholder="Ít nhất 6 ký tự" autocomplete="{{ 'current-password' if mode == 'login' else 'new-password' }}">
            <button type="button" onclick="const i=document.getElementById('pwInput'); i.type = i.type==='password'?'text':'password'; this.querySelector('i').classList.toggle('fa-eye'); this.querySelector('i').classList.toggle('fa-eye-slash');"
                    class="absolute right-3.5 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600">
              <i class="fas fa-eye text-sm"></i>
            </button>
          </div>
        </div>
        {% if mode == 'register' %}
        <div>
          <label class="block text-sm font-medium text-gray-600 mb-1.5">Nhập lại mật khẩu</label>
          <div class="relative">
            <i class="fas fa-lock absolute left-4 top-1/2 -translate-y-1/2 text-gray-400 text-sm"></i>
            <input type="password" name="confirm" required minlength="6"
                   class="field-input w-full pl-10 pr-4 py-3 bg-gray-50 border border-gray-200 rounded-xl focus:outline-none focus:border-indigo-400 text-gray-800 transition-shadow"
                   placeholder="Nhập lại mật khẩu" autocomplete="new-password">
          </div>
        </div>
        {% endif %}
        <button type="submit"
                class="primary-btn w-full bg-gradient-to-r from-indigo-600 to-cyan-500 hover:opacity-95 text-white font-semibold py-3.5 rounded-xl shadow-lg shadow-indigo-500/30">
          {{ 'Đăng nhập' if mode == 'login' else 'Tạo tài khoản' }}
        </button>
      </form>

      <p class="text-center text-sm text-gray-500 mt-6">
        {% if mode == 'login' %}
          Chưa có tài khoản? <a href="{{ url_for('register_page') }}" class="text-indigo-600 font-semibold hover:underline">Đăng ký ngay</a>
          · <a href="{{ url_for('forgot_password_page') }}" class="text-indigo-600 font-semibold hover:underline">Quên mật khẩu?</a>
        {% else %}
          Đã có tài khoản? <a href="{{ url_for('login_page') }}" class="text-indigo-600 font-semibold hover:underline">Đăng nhập</a>
        {% endif %}
      </p>

      {% if guest_enabled %}
      <div class="flex items-center gap-3 my-5">
        <div class="flex-1 h-px bg-gray-200"></div>
        <span class="text-xs text-gray-400 font-medium">hoặc</span>
        <div class="flex-1 h-px bg-gray-200"></div>
      </div>
      <form method="POST" action="{{ url_for('guest_login') }}">
        <button type="submit"
                class="w-full flex items-center justify-center gap-2 px-4 py-3 rounded-xl border border-dashed border-gray-300 hover:bg-gray-50 font-medium text-sm text-gray-600">
          <i class="fas fa-bolt text-amber-500"></i> Dùng thử ngay, không cần đăng ký
        </button>
      </form>
      <p class="text-center text-[11px] text-gray-400 mt-2">Dữ liệu dùng thử có thể mất nếu xoá cookie trình duyệt — tạo tài khoản bất cứ lúc nào để lưu lại.</p>
      {% endif %}
    </div>

    <p class="text-center text-xs text-indigo-200/60 mt-6">© {{ 2026 }} StudyMate AI — Dữ liệu đăng nhập được mã hoá, không chia sẻ cho bên thứ ba.</p>
  </div>
</body>
</html>
'''

# ==========================================
# 2. BIẾN HTML CHÍNH (GIAO DIỆN CHAT — kiểu ChatGPT/Claude)
# ==========================================
HTML = r'''
<!DOCTYPE html>
<html lang="vi" class="light">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{{ app_name }}</title>
  <link rel="manifest" href="/manifest.json">
  <link rel="icon" type="image/png" sizes="192x192" href="/static/icons/icon-192.png">
  <link rel="apple-touch-icon" href="/static/icons/icon-192.png">
  <meta name="theme-color" content="#4f46e5">
  <meta name="mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-title" content="{{ app_name }}">
  <script src="https://cdn.tailwindcss.com"></script>
  <script>tailwind.config = { darkMode: 'class' };</script>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.6.0/css/all.min.css">
  <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.css">
  <script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.js"></script>
  <script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/contrib/auto-render.min.js"></script>
  <style>
    html, body { height: 100%; }
    body { font-family: 'Segoe UI', system-ui, sans-serif; transition: background-color 0.2s, color 0.2s; }

    .ai-content { overflow-wrap: anywhere; word-break: break-word; }
    .ai-content p { margin-bottom: 0.6rem; }
    .ai-content p:last-child { margin-bottom: 0; }
    .ai-content .katex-display { overflow-x: auto; overflow-y: hidden; padding: 0.15rem 0; }
    .ai-content strong { color: #1e40af; }
    .dark .ai-content strong { color: #93c5fd; }
    .ai-content ul, .ai-content ol { padding-left: 1.4rem; margin-bottom: 0.6rem; }
    .ai-content li { list-style-type: disc; margin-bottom: 0.25rem; }
    .ai-content code { background: rgba(0,0,0,0.06); padding: 0.1rem 0.35rem; border-radius: 0.35rem; font-size: 0.9em; }
    .dark .ai-content code { background: rgba(255,255,255,0.1); }
    .ai-content pre { background: #1e293b; color: #e2e8f0; padding: 0.9rem; border-radius: 0.75rem; overflow-x: auto; margin-bottom: 0.6rem; }
    .ai-content pre code { background: transparent; padding: 0; }

    .typing-indicator span { display: inline-block; width: 6px; height: 6px; background-color: #9ca3af; border-radius: 50%; margin: 0 2px; animation: bounce 1.4s infinite ease-in-out both; }
    .typing-indicator span:nth-child(1) { animation-delay: -0.32s; }
    .typing-indicator span:nth-child(2) { animation-delay: -0.16s; }
    @keyframes bounce { 0%, 80%, 100% { transform: scale(0); } 40% { transform: scale(1); } }

    .stream-cursor { display: inline-block; width: 2px; height: 1em; background: currentColor; margin-left: 2px; vertical-align: text-bottom; animation: blink 0.9s steps(1) infinite; }
    @keyframes blink { 50% { opacity: 0; } }

    ::-webkit-scrollbar { width: 8px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb { background: #cbd5e1; border-radius: 10px; }
    .dark ::-webkit-scrollbar-thumb { background: #4b5563; }

    #chatPanel { position: relative; }
    .drag-overlay {
      display: none;
      position: absolute; inset: 0; z-index: 40;
      background: rgba(37, 99, 235, 0.10);
      backdrop-filter: blur(2px);
      border: 3px dashed #3b82f6;
      border-radius: 1rem;
      align-items: center; justify-content: center;
      flex-direction: column; gap: 0.5rem;
      pointer-events: none;
      color: #1d4ed8;
      font-weight: 700; font-size: 1.2rem;
      margin: 0.75rem;
    }
    .dark .drag-overlay { color: #93c5fd; background: rgba(37, 99, 235, 0.18); }
    #chatPanel.drag-active .drag-overlay { display: flex; }

    .attachment-chip img { border: 1px solid rgba(0,0,0,0.08); }

    .msg-actions { opacity: 0; transition: opacity 0.15s; }
    /* addMessageActions() inserts the actions bar as a SIBLING right after the
       .ai-msg-group wrapper (wrapper.after(bar)), not as a child of it — so a
       descendant selector like ".ai-msg-group:hover .msg-actions" never matches
       and the "Báo lỗi" button stayed invisible (opacity: 0) forever, even though
       it was in the DOM and technically clickable. Use the general sibling
       combinator (~) instead, and also show it on its own hover/focus so touch
       devices (which have no :hover on the message) and keyboard users can reach it. */
    .msg-actions.force-visible,
    .ai-msg-group:hover ~ .msg-actions,
    .msg-actions:hover,
    .msg-actions:focus-within { opacity: 1; }

    /* ---------- Avatar "suy nghĩ" (shimmer chạy từ dưới lên trên) ----------
       Dải sáng quét dọc từ DƯỚI lên TRÊN, lặp lại, trên avatar robot — dùng làm
       avatar chung của cả website (sidebar, khung chat, chỉ báo đang gõ). Khi AI
       đang trả lời (.thinking) chạy nhanh & rõ hơn; ở logo sidebar (.brand-avatar)
       chạy chậm, mờ hơn như một nhịp "thở". */
    .ai-avatar { position: relative; overflow: hidden; isolation: isolate; }
    .ai-avatar::after {
      content: '';
      position: absolute; inset: -60% -20%;
      background: linear-gradient(0deg,
        transparent 0%, rgba(255,255,255,0) 38%, rgba(255,255,255,0.95) 50%,
        rgba(255,255,255,0) 62%, transparent 100%);
      background-size: 100% 260%;
      background-position: 0% 160%;
      mix-blend-mode: overlay;
      opacity: 0;
      pointer-events: none;
      will-change: background-position, opacity;
    }
    .ai-avatar.thinking::after { opacity: 1; animation: avatarShimmerUp 1.3s ease-in-out infinite; }
    .ai-avatar.brand-avatar::after { opacity: 0.55; animation: avatarShimmerUp 3.4s ease-in-out infinite; }
    @keyframes avatarShimmerUp {
      0%   { background-position: 0% 160%; }
      100% { background-position: 0% -160%; }
    }
    @media (prefers-reduced-motion: reduce) {
      .ai-avatar.thinking::after, .ai-avatar.brand-avatar::after { animation: none; opacity: 0.35; }
    }

    @keyframes memoryToastFade {
      0% { opacity: 0; transform: translate(-50%, 6px); }
      10%, 85% { opacity: 1; transform: translate(-50%, 0); }
      100% { opacity: 0; transform: translate(-50%, 6px); }
    }
    .memory-toast { left: 50%; animation: memoryToastFade 3.6s ease forwards; }

    #gamifyWidget { }
    .gamify-xp-track { background: #e5e7eb; border-radius: 999px; height: 6px; overflow: hidden; }
    .dark .gamify-xp-track { background: #374151; }
    .gamify-xp-fill { background: linear-gradient(90deg, #f59e0b, #f97316); height: 100%; border-radius: 999px; transition: width 0.4s; }

    /* Hiệu ứng ngọn lửa giữa màn hình khi đạt mốc streak (3, 10, 30, 100, 200, 300, 500, 1000
       ngày...) — ngọn lửa càng đậm/nhiều lớp hơn ở mốc càng cao, xem STREAK_TIER_STYLE trong JS. */
    #streakFireOverlay { transition: none; }
    #streakFireOverlay.showing #streakFireGlow { animation: streakGlowPulse 2.7s ease forwards; }
    #streakFireOverlay.showing #streakFireContent { animation: streakPop 2.7s cubic-bezier(.34,1.56,.64,1) forwards; }
    @keyframes streakGlowPulse {
      0% { opacity: 0; }
      15% { opacity: 1; }
      75% { opacity: 1; }
      100% { opacity: 0; }
    }
    @keyframes streakPop {
      0% { transform: scale(0.3) translateY(24px); opacity: 0; }
      15% { transform: scale(1.18) translateY(0); opacity: 1; }
      25% { transform: scale(1); }
      82% { transform: scale(1); opacity: 1; }
      100% { transform: scale(0.92) translateY(-14px); opacity: 0; }
    }
    @keyframes streakEmojiFlicker {
      0%, 100% { transform: scale(1) rotate(-2deg); }
      50% { transform: scale(1.06) rotate(2deg); }
    }
    #streakFireEmoji { animation: streakEmojiFlicker 0.5s ease-in-out infinite; filter: drop-shadow(0 0 18px currentColor); }

    #sidebar { transition: transform 0.2s ease; }
    @media (max-width: 1023px) { #sidebar { transform: translateX(-100%); } #sidebar.open { transform: translateX(0); } }

    .conv-item .conv-actions { opacity: 0; }
    .conv-item:hover .conv-actions { opacity: 1; }

    textarea#messageInput { max-height: 160px; }

    .modal-panel { animation: modalIn 0.15s ease; }
    @keyframes modalIn { from { opacity: 0; transform: translateY(8px) scale(0.98); } to { opacity: 1; transform: translateY(0) scale(1); } }
    .theme-opt { border-color: #e5e7eb; color: inherit; }
    .dark .theme-opt { border-color: #374151; }
    .theme-opt.active { border-color: #3b82f6; background: rgba(59,130,246,0.08); color: #2563eb; }
    .dark .theme-opt.active { color: #93c5fd; }

    /* Menu 3 chấm của đoạn chat (Ghim / Đổi tên / Chuyển dự án / Xoá).
       LỖI ĐÃ SỬA: trước đây dùng z-index 45, THẤP HƠN sidebar (z-50) — mà menu lại mở ra
       ngay bên trong vùng ngang của sidebar. Trên ĐIỆN THOẠI sidebar là position:fixed nên
       z-index có hiệu lực -> sidebar (nền đục) VẼ ĐÈ LÊN menu, khiến menu vừa không nhìn
       thấy vừa không bấm được. Trên máy tính sidebar là position:static (z-index không có
       tác dụng) nên vẫn chạy — vì vậy lỗi chỉ xuất hiện khi dùng điện thoại.
       Sửa: z-index 55 (trên sidebar 50, dưới hộp thoại 60).
       Đổi absolute -> fixed cho khớp với toạ độ lấy từ getBoundingClientRect() (vốn tính
       theo màn hình), đồng thời tránh bị cắt bởi overflow:hidden của phần tử cha. */
    .conv-menu { position: fixed; z-index: 55; min-width: 180px; }

    /* Chiều cao khung app: trên trình duyệt ĐIỆN THOẠI, 100vh tính cả phần bị thanh địa chỉ
       và thanh công cụ dưới che mất -> layout cao hơn vùng nhìn thấy thật, khiến khung chat
       và ô nhập câu hỏi bị đẩy ra ngoài màn hình mà KHÔNG cuộn tới được (vì body đang
       overflow:hidden). 100dvh (dynamic viewport height) tính đúng vùng đang nhìn thấy và tự
       co giãn khi thanh địa chỉ ẩn/hiện. Giữ 100vh ở dòng trên làm phương án dự phòng cho
       trình duyệt cũ chưa hỗ trợ dvh. */
    .app-shell { height: 100vh; height: 100dvh; }
  </style>
</head>
<body class="app-shell overflow-hidden bg-white dark:bg-[#212121] text-gray-800 dark:text-gray-100">

<div class="flex app-shell">

  <!-- ===================== SIDEBAR ===================== -->
  <aside id="sidebar" class="fixed lg:static inset-y-0 left-0 z-50 w-72 flex-shrink-0 bg-gray-50 dark:bg-[#171717] border-r border-gray-200 dark:border-gray-800 flex flex-col">
    <div class="p-3 flex items-center justify-between">
      <div class="flex items-center gap-2 px-1">
        <div class="ai-avatar brand-avatar w-8 h-8 rounded-lg bg-gradient-to-br from-blue-600 to-indigo-600 flex items-center justify-center text-white text-sm"><i class="fas fa-robot"></i></div>
        <span class="font-bold text-base truncate">{{ app_name }}</span>
      </div>
      <button id="closeSidebarBtn" class="lg:hidden w-8 h-8 flex items-center justify-center rounded-lg hover:bg-gray-200 dark:hover:bg-gray-800 text-gray-500">
        <i class="fas fa-xmark"></i>
      </button>
    </div>

    <div class="px-3 mt-1 space-y-2">
      <button id="newChatBtn" class="w-full flex items-center gap-2 px-3 py-2.5 rounded-xl border border-gray-300 dark:border-gray-700 hover:bg-gray-100 dark:hover:bg-gray-800 font-medium text-sm transition-colors">
        <i class="fas fa-plus"></i> <span data-i18n="new_chat">Đoạn chat mới</span>
      </button>
      <button id="openFlashcardsBtn" class="w-full flex items-center gap-2 px-3 py-2.5 rounded-xl border border-gray-300 dark:border-gray-700 hover:bg-gray-100 dark:hover:bg-gray-800 font-medium text-sm transition-colors">
        <i class="fas fa-layer-group text-purple-500"></i> <span data-i18n="flashcards_games_btn">Thẻ ghi nhớ &amp; Trò chơi</span>
      </button>
      <div class="relative">
        <i class="fas fa-magnifying-glass absolute left-3 top-1/2 -translate-y-1/2 text-gray-400 text-xs"></i>
        <input id="searchInput" type="text" placeholder="Tìm đoạn chat..." data-i18n-placeholder="search_placeholder"
          class="w-full pl-8 pr-3 py-2 text-sm bg-gray-100 dark:bg-gray-800 border-0 rounded-xl focus:outline-none focus:ring-2 focus:ring-blue-500 dark:text-white">
      </div>
    </div>

    <div class="flex-1 overflow-y-auto px-3 pb-3 mt-3 space-y-4 text-sm">
      <!-- Dự án (giống Claude Projects) -->
      <div>
        <div class="flex items-center justify-between mb-1 px-1">
          <span class="text-xs font-semibold text-gray-400 uppercase tracking-wide" data-i18n="projects">Dự án</span>
          <button id="newProjectBtn" class="w-5 h-5 flex items-center justify-center rounded hover:bg-gray-200 dark:hover:bg-gray-800 text-gray-400" title="Tạo dự án mới">
            <i class="fas fa-plus text-xs"></i>
          </button>
        </div>
        <div id="projectList" class="space-y-1"></div>
      </div>

      <!-- Đã ghim -->
      <div id="pinnedSection" class="hidden">
        <div class="text-xs font-semibold text-gray-400 uppercase tracking-wide mb-1 px-1" data-i18n="pinned">Đã ghim</div>
        <div id="pinnedList" class="space-y-1"></div>
      </div>

      <!-- Gần đây -->
      <div>
        <div class="text-xs font-semibold text-gray-400 uppercase tracking-wide mb-1 px-1" data-i18n="recents">Gần đây</div>
        <div id="convList" class="space-y-1"></div>
      </div>
    </div>

    <div id="gamifyWidget" class="hidden px-3 pb-2">
      <button onclick="openProgressDashboard()" class="w-full text-left rounded-xl bg-gray-50 dark:bg-gray-900 border border-gray-100 dark:border-gray-800 px-3 py-2.5 hover:border-indigo-300 dark:hover:border-indigo-700 transition-colors">
        <div class="flex items-center justify-between text-xs mb-1.5">
          <span class="flex items-center gap-1 font-semibold text-orange-500"><i class="fas fa-fire"></i> <span id="gamifyStreak">0</span> <span data-i18n="streak_days_suffix">ngày</span></span>
          <span class="text-gray-400"><span data-i18n="level_prefix">Cấp</span> <span id="gamifyLevel" class="font-semibold text-gray-600 dark:text-gray-300">1</span></span>
        </div>
        <div class="gamify-xp-track"><div id="gamifyXpBar" class="gamify-xp-fill" style="width: 0%;"></div></div>
        <p id="gamifyXpText" class="text-[10px] text-gray-400 mt-1 text-right">0/100 XP</p>
        <p class="text-[10px] text-indigo-500 dark:text-indigo-400 font-medium mt-1.5 flex items-center gap-1"><i class="fas fa-chart-line"></i> Xem tiến độ học tập →</p>
      </button>
    </div>

    <div class="border-t border-gray-200 dark:border-gray-800 p-3 relative">
      <button id="userMenuBtn" type="button" class="w-full flex items-center gap-3 px-2 py-2 rounded-xl hover:bg-gray-100 dark:hover:bg-gray-800 transition-colors">
        <div id="userAvatar" class="w-8 h-8 rounded-full bg-indigo-600 text-white flex items-center justify-center font-bold text-sm flex-shrink-0"></div>
        <span id="userNameLabel" class="flex-1 text-left truncate font-medium text-sm"></span>
        <i class="fas fa-chevron-up text-xs text-gray-400"></i>
      </button>
      <div id="userMenu" class="hidden absolute bottom-[64px] left-3 right-3 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-xl shadow-lg overflow-hidden z-10">
        <div class="px-4 py-2.5 border-b border-gray-100 dark:border-gray-700 flex items-center gap-1.5">
          <span class="text-[11px] font-semibold px-2 py-0.5 rounded-full {{ plan_meta[current_plan].badge }}">
            {{ plan_meta[current_plan].icon }} {{ plan_meta[current_plan].label }}
          </span>
          {% if is_plan_role_based %}
          <span class="text-[10px] text-gray-400">(theo vai trò {{ role_label }})</span>
          {% endif %}
        </div>
        {% if is_developer %}
        <a href="/developer" class="flex items-center gap-2 px-4 py-3 text-sm hover:bg-gray-100 dark:hover:bg-gray-700 text-indigo-600 dark:text-indigo-400 border-b border-gray-100 dark:border-gray-700">
          <i class="fas fa-chart-line"></i> Thống kê (Developer)
        </a>
        {% endif %}
        <button type="button" onclick="openModal('settingsModal')" class="w-full flex items-center gap-2 px-4 py-3 text-sm hover:bg-gray-100 dark:hover:bg-gray-700 text-left">
          <i class="fas fa-gear w-4 text-gray-400"></i> <span data-i18n="settings">Cài đặt</span>
        </button>
        <button type="button" onclick="openModal('helpModal')" class="w-full flex items-center gap-2 px-4 py-3 text-sm hover:bg-gray-100 dark:hover:bg-gray-700 text-left border-t border-gray-100 dark:border-gray-700">
          <i class="fas fa-circle-question w-4 text-gray-400"></i> <span data-i18n="help">Trợ giúp &amp; phím tắt</span>
        </button>
        <button type="button" onclick="openModal('upgradeModal')" class="w-full flex items-center gap-2 px-4 py-3 text-sm hover:bg-gray-100 dark:hover:bg-gray-700 text-left border-t border-gray-100 dark:border-gray-700">
          <i class="fas fa-bolt w-4 text-amber-500"></i> <span data-i18n="upgrade">Nâng cấp gói</span>
        </button>
        <a href="/logout" class="flex items-center gap-2 px-4 py-3 text-sm hover:bg-gray-100 dark:hover:bg-gray-700 text-red-600 dark:text-red-400 border-t border-gray-100 dark:border-gray-700">
          <i class="fas fa-right-from-bracket w-4"></i> <span data-i18n="logout">Đăng xuất</span>
        </a>
      </div>
    </div>
  </aside>
  <div id="sidebarOverlay" class="hidden fixed inset-0 bg-black/40 z-40 lg:hidden"></div>

  <!-- ===================== MAIN ===================== -->
  <div class="flex-1 flex flex-col min-w-0 min-h-0">

    <header class="flex items-center gap-2 px-3 lg:px-5 py-3 border-b border-gray-200 dark:border-gray-800 flex-wrap flex-shrink-0">
      <button id="openSidebarBtn" class="lg:hidden w-9 h-9 flex items-center justify-center rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800 text-gray-600 dark:text-gray-300">
        <i class="fas fa-bars"></i>
      </button>

      <select id="subject" class="text-sm font-medium bg-gray-100 dark:bg-gray-800 border-0 rounded-full pl-4 pr-8 py-2 focus:outline-none focus:ring-2 focus:ring-blue-500 cursor-pointer dark:text-white">
        <option value="Toán" data-i18n="subj_toan">📐 Toán Học</option>
        <option value="Ngữ Văn" data-i18n="subj_van">📖 Ngữ Văn</option>
        <option value="Tiếng Anh" data-i18n="subj_anh">🇬🇧 Tiếng Anh</option>
        <option value="Vật Lý" data-i18n="subj_ly">⚛️ Vật Lý</option>
        <option value="Hóa Học" data-i18n="subj_hoa">🧪 Hóa Học</option>
        <option value="Sinh Học" data-i18n="subj_sinh">🌱 Sinh Học</option>
        <option value="Lịch sử & Địa lý" data-i18n="subj_sudia">🌍 Lịch sử & Địa lý</option>
        <option value="Tin Học" data-i18n="subj_tin">💻 Tin Học</option>
      </select>

      <select id="modeSelect" class="text-sm font-medium bg-gray-100 dark:bg-gray-800 border-0 rounded-full pl-4 pr-8 py-2 focus:outline-none focus:ring-2 focus:ring-blue-500 cursor-pointer dark:text-white">
        <option value="Giải thích" data-i18n="mode_giaithich">📘 Giải Thích Dễ Hiểu</option>
        <option value="Gợi ý" data-i18n="mode_goiy">💡 Gợi Ý Từng Bước</option>
        <option value="Kiểm tra bài làm" data-i18n="mode_kiemtra">✅ Kiểm Tra Bài Làm</option>
        <option value="Luyện tập" data-i18n="mode_luyentap">📝 Ra Bài Luyện Tập</option>
        <option value="Ôn tập" data-i18n="mode_ontap">🔄 Tổng Hợp Ôn Tập</option>
      </select>

      <div class="relative">
        <button id="thinkingModeBtn" type="button" data-i18n-title="think_tooltip" title="Chế độ suy nghĩ của AI"
          class="text-sm font-medium bg-gray-100 dark:bg-gray-800 border-0 rounded-full pl-3.5 pr-2.5 py-2 focus:outline-none focus:ring-2 focus:ring-blue-500 cursor-pointer dark:text-white flex items-center gap-1.5">
          <span id="thinkingModeIcon">💬</span>
          <span id="thinkingModeLabel" data-i18n="think_standard_label">Trợ Lý</span>
          <i class="fas fa-chevron-down text-[10px] text-gray-400"></i>
        </button>
        <div id="thinkingModeMenu" class="hidden absolute left-0 top-full mt-1.5 z-30 w-72 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-xl shadow-lg overflow-hidden">
          {% for key in thinking_mode_order %}
          {% set tm = thinking_modes[key] %}
          {% set unlocked = key in unlocked_thinking_modes %}
          <button type="button" class="thinking-mode-item w-full text-left px-3.5 py-2.5 hover:bg-gray-50 dark:hover:bg-gray-700 flex items-start gap-2.5 text-sm border-b border-gray-50 dark:border-gray-700/50 last:border-b-0 {{ '' if unlocked else 'opacity-70' }}"
            data-mode="{{ key }}" data-unlocked="{{ '1' if unlocked else '0' }}">
            <span class="text-base leading-5">{{ tm.icon }}</span>
            <span class="flex-1 min-w-0">
              <span class="font-medium flex items-center gap-1.5 flex-wrap">
                <span data-i18n="think_{{ key }}_label">{{ tm.label }}</span>
                {% if not unlocked %}
                <span class="text-[10px] font-semibold px-1.5 py-0.5 rounded-full {{ plan_meta[tm.min_plan].badge }}">
                  <i class="fas fa-lock text-[9px] mr-0.5"></i>{{ plan_meta[tm.min_plan].label }}
                </span>
                {% endif %}
              </span>
              <span class="block text-xs text-gray-400 mt-0.5 leading-snug" data-i18n="think_{{ key }}_desc">{{ tm.desc }}</span>
            </span>
          </button>
          {% endfor %}
        </div>
      </div>

      <div class="hidden lg:block lg:flex-1"></div>

      <button onclick="startVoice()" data-i18n-title="voice_tooltip" class="w-9 h-9 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800 text-gray-600 dark:text-gray-300 flex items-center justify-center" title="Trợ lý giọng nói">
        <i class="fas fa-microphone"></i>
      </button>
      <button onclick="toggleTheme()" class="w-9 h-9 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800 text-gray-600 dark:text-gray-300 flex items-center justify-center" title="Đổi giao diện">
        <i id="themeIcon" class="fas fa-moon"></i>
      </button>
    </header>

    <div id="bannerBar" class="hidden items-center gap-2 px-4 py-2 bg-amber-50 dark:bg-amber-900/30 border-b border-amber-200 dark:border-amber-800 text-amber-800 dark:text-amber-200 text-sm">
      <i class="fas fa-bullhorn flex-shrink-0"></i>
      <span id="bannerText" class="flex-1"></span>
      <button onclick="dismissBanner()" class="w-6 h-6 flex items-center justify-center rounded hover:bg-amber-200/60 dark:hover:bg-amber-800/60 flex-shrink-0"><i class="fas fa-xmark text-xs"></i></button>
    </div>

    <div id="chatPanel" class="flex-1 min-h-0 overflow-y-auto scroll-smooth">
      <div class="drag-overlay">
        <i class="fas fa-cloud-arrow-up text-4xl"></i>
        <span>Thả file hoặc ảnh vào đây</span>
      </div>
      <div id="chat" class="max-w-3xl mx-auto px-4 py-6 space-y-6"></div>
    </div>

    <div class="border-t border-gray-200 dark:border-gray-800 p-3 lg:p-4 flex-shrink-0">
      <div class="max-w-3xl mx-auto">
        <div id="attachmentsBar" class="hidden flex flex-wrap gap-2 mb-2"></div>
        <div class="flex items-end gap-2 bg-gray-100 dark:bg-gray-800 rounded-3xl px-2.5 py-2 border border-transparent focus-within:border-blue-400 dark:focus-within:border-blue-500 transition-colors">
          <button onclick="document.getElementById('fileInput').click()" class="w-10 h-10 rounded-full hover:bg-gray-200 dark:hover:bg-gray-700 flex items-center justify-center flex-shrink-0 text-gray-500 dark:text-gray-300" title="Đính kèm file hoặc ảnh">
            <i class="fas fa-paperclip"></i>
          </button>
          <input type="file" id="fileInput" class="hidden" accept="image/*,.pdf,.docx,.txt,.csv">

          <textarea id="messageInput" rows="1" class="flex-1 bg-transparent border-0 focus:outline-none focus:ring-0 resize-none py-2 text-[15px] dark:text-white" data-i18n-placeholder="message_placeholder" placeholder="Nhập câu hỏi... (Enter để gửi, Shift+Enter để xuống dòng)"></textarea>

          <button onclick="sendMessage()" id="sendBtn" class="w-10 h-10 rounded-full bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white flex items-center justify-center flex-shrink-0 transition-colors">
            <i class="fas fa-arrow-up"></i>
          </button>
        </div>
        <p class="text-center text-xs text-gray-400 dark:text-gray-500 mt-2" data-i18n="footer_disclaimer">StudyMate AI có thể mắc lỗi — em nên kiểm tra lại các thông tin quan trọng nhé.</p>
      </div>
    </div>
  </div>
</div>

<!-- ===================== MODALS ===================== -->
<div id="flashcardsOverlay" class="hidden fixed inset-0 bg-white dark:bg-[#131313] z-[70] flex flex-col">
  <div class="flex items-center justify-between px-4 lg:px-6 py-3.5 border-b border-gray-200 dark:border-gray-800 flex-shrink-0">
    <div class="flex items-center gap-2 min-w-0">
      <button id="fcBackBtn" class="hidden w-9 h-9 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800 flex items-center justify-center flex-shrink-0"><i class="fas fa-arrow-left"></i></button>
      <i class="fas fa-layer-group text-purple-500"></i>
      <h2 id="fcHeaderTitle" class="font-bold truncate">Thẻ ghi nhớ &amp; Trò chơi</h2>
    </div>
    <button onclick="closeFlashcards()" class="w-9 h-9 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800 flex items-center justify-center flex-shrink-0"><i class="fas fa-xmark"></i></button>
  </div>

  <div id="fcTabBar" class="flex items-center gap-1 px-4 lg:px-6 pt-3 border-b border-gray-200 dark:border-gray-800 flex-shrink-0 overflow-x-auto">
    <button id="fcTabDecks" onclick="switchFcTab('decks')" class="px-3.5 py-2 text-sm font-medium border-b-2 border-blue-600 text-blue-600 dark:text-blue-400 flex items-center gap-1.5 whitespace-nowrap">
      <i class="fas fa-layer-group"></i> Thẻ ghi nhớ
    </button>
    <button id="fcTabMistakes" onclick="switchFcTab('mistakes')" class="px-3.5 py-2 text-sm font-medium border-b-2 border-transparent text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 flex items-center gap-1.5 whitespace-nowrap">
      <i class="fas fa-book"></i> Sổ lỗi sai
    </button>
    <button id="fcTabQuiz" onclick="switchFcTab('quiz')" class="px-3.5 py-2 text-sm font-medium border-b-2 border-transparent text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 flex items-center gap-1.5 whitespace-nowrap">
      <i class="fas fa-list-check"></i> Quiz
    </button>
    <button id="fcTabPlans" onclick="switchFcTab('plans')" class="px-3.5 py-2 text-sm font-medium border-b-2 border-transparent text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 flex items-center gap-1.5 whitespace-nowrap">
      <i class="fas fa-calendar-check"></i> Kế hoạch ôn tập
    </button>
    <button id="fcTabGames" onclick="switchFcTab('games')" class="px-3.5 py-2 text-sm font-medium border-b-2 border-transparent text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 flex items-center gap-1.5 whitespace-nowrap">
      <i class="fas fa-gamepad"></i> Trò chơi
    </button>
    <button id="fcTabClasses" onclick="switchFcTab('classes')" class="px-3.5 py-2 text-sm font-medium border-b-2 border-transparent text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 flex items-center gap-1.5 whitespace-nowrap">
      <i class="fas fa-chalkboard-user"></i> Lớp học
    </button>
  </div>

  <div class="flex-1 overflow-y-auto">
    <!-- ============ Lớp học: danh sách ============ -->
    <div id="fcClassesListView" class="hidden max-w-3xl mx-auto p-4 lg:p-6">
      <div class="flex flex-wrap gap-2 mb-5">
        <button onclick="document.getElementById('createClassForm').classList.toggle('hidden')" class="px-4 py-2.5 rounded-xl bg-gradient-to-r from-indigo-600 to-purple-600 hover:opacity-90 text-white text-sm font-semibold flex items-center gap-2">
          <i class="fas fa-plus"></i> Tạo lớp (dạy)
        </button>
        <button onclick="document.getElementById('joinClassForm').classList.toggle('hidden')" class="px-4 py-2.5 rounded-xl border border-gray-300 dark:border-gray-700 hover:bg-gray-100 dark:hover:bg-gray-800 text-sm font-medium flex items-center gap-2">
          <i class="fas fa-right-to-bracket"></i> Vào lớp bằng mã
        </button>
      </div>

      <div id="createClassForm" class="hidden mb-5 p-4 rounded-2xl border border-indigo-200 dark:border-indigo-900 bg-indigo-50 dark:bg-indigo-900/10 space-y-2.5">
        <input id="newClassName" maxlength="80" placeholder="Tên lớp (vd: Lớp 8A2 - Toán cô Anh)" class="w-full px-3 py-2.5 rounded-xl bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 text-sm dark:text-white focus:outline-none focus:ring-2 focus:ring-indigo-500">
        <input id="newClassSubject" maxlength="60" placeholder="Môn học (tuỳ chọn)" class="w-full px-3 py-2 rounded-xl bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 text-sm dark:text-white focus:outline-none focus:ring-2 focus:ring-indigo-500">
        <button onclick="submitCreateClass()" class="px-4 py-2 rounded-xl bg-indigo-600 hover:bg-indigo-700 text-white text-sm font-semibold">Tạo lớp</button>
        <p id="createClassError" class="hidden text-xs text-red-500"></p>
      </div>

      <div id="joinClassForm" class="hidden mb-5 p-4 rounded-2xl border border-gray-200 dark:border-gray-700 space-y-2.5">
        <input id="joinClassCode" maxlength="8" placeholder="Nhập mã lớp (vd: 4TFBF5)" class="w-full px-3 py-2.5 rounded-xl bg-gray-100 dark:bg-gray-800 border-0 text-sm dark:text-white font-mono tracking-widest uppercase focus:outline-none focus:ring-2 focus:ring-indigo-500">
        <button onclick="submitJoinClass()" class="px-4 py-2 rounded-xl bg-gray-800 dark:bg-gray-700 hover:bg-gray-900 text-white text-sm font-semibold">Vào lớp</button>
        <p id="joinClassError" class="hidden text-xs text-red-500"></p>
      </div>

      <div id="teachingSection" class="hidden mb-6">
        <h3 class="text-sm font-bold mb-2.5">👩‍🏫 Lớp em dạy</h3>
        <div id="teachingList" class="space-y-2"></div>
      </div>
      <div id="learningSection" class="hidden">
        <h3 class="text-sm font-bold mb-2.5">🎒 Lớp em học</h3>
        <div id="learningList" class="space-y-2"></div>
      </div>
      <p id="classesEmptyState" class="hidden text-center text-sm text-gray-400 py-16">Chưa có lớp nào.<br>Tạo lớp để giao bài cho học sinh, hoặc nhập mã lớp để vào lớp của thầy cô.</p>
    </div>

    <!-- ============ Lớp học: chi tiết ============ -->
    <div id="fcClassDetailView" class="hidden max-w-3xl mx-auto p-4 lg:p-6 space-y-5">
      <div>
        <div class="flex flex-wrap items-start justify-between gap-2">
          <div>
            <h3 id="classDetailName" class="font-bold text-lg"></h3>
            <p id="classDetailMeta" class="text-xs text-gray-400"></p>
          </div>
          <button id="classLeaveBtn" onclick="deleteOrLeaveClass()" class="px-3 py-2 rounded-xl bg-red-50 dark:bg-red-900/20 text-red-600 dark:text-red-400 text-xs font-semibold hover:bg-red-100 dark:hover:bg-red-900/40"></button>
        </div>
        <div id="classJoinCodeBox" class="hidden mt-3 p-3 rounded-xl bg-indigo-50 dark:bg-indigo-900/20 border border-indigo-100 dark:border-indigo-900/50 flex items-center justify-between gap-2">
          <div>
            <p class="text-[11px] text-indigo-500 uppercase font-semibold">Mã mời vào lớp</p>
            <p id="classJoinCode" class="font-mono text-2xl font-bold tracking-widest text-indigo-700 dark:text-indigo-300 select-all"></p>
          </div>
          <button onclick="copyJoinCode()" id="copyJoinCodeBtn" class="text-xs font-semibold px-3 py-2 rounded-lg bg-white dark:bg-gray-800 border border-indigo-200 dark:border-indigo-800"><i class="fas fa-copy mr-1"></i>Chép</button>
        </div>
      </div>

      <!-- Tổng quan lớp (chỉ giáo viên) -->
      <div id="classOverview" class="hidden grid grid-cols-3 gap-3">
        <div class="rounded-2xl border border-gray-200 dark:border-gray-800 p-4 text-center">
          <p class="text-2xl font-bold" id="classStudentCount">0</p><p class="text-[11px] text-gray-400 uppercase mt-0.5">Học sinh</p>
        </div>
        <div class="rounded-2xl border border-gray-200 dark:border-gray-800 p-4 text-center">
          <p class="text-2xl font-bold text-emerald-500" id="classAvg">—</p><p class="text-[11px] text-gray-400 uppercase mt-0.5">Điểm TB lớp</p>
        </div>
        <div class="rounded-2xl border border-gray-200 dark:border-gray-800 p-4 text-center">
          <p class="text-2xl font-bold text-amber-500" id="classNeedAttention">0</p><p class="text-[11px] text-gray-400 uppercase mt-0.5">Cần chú ý</p>
        </div>
      </div>

      <div id="classWeakSection" class="hidden">
        <h4 class="text-sm font-bold mb-2">⚠️ Cả lớp yếu nhất phần này</h4>
        <div id="classWeakTopics" class="flex flex-wrap gap-2"></div>
      </div>

      <!-- Giao bài (chỉ giáo viên) -->
      <div id="assignSection" class="hidden">
        <h4 class="text-sm font-bold mb-2">📤 Giao bài mới</h4>
        <div class="flex flex-wrap gap-2 items-center">
          <select id="assignQuizSelect" class="flex-1 min-w-[160px] px-3 py-2 rounded-xl bg-gray-100 dark:bg-gray-800 border-0 text-sm dark:text-white"></select>
          <input id="assignDueDate" type="date" class="px-3 py-2 rounded-xl bg-gray-100 dark:bg-gray-800 border-0 text-sm dark:text-white">
          <button onclick="submitAssignment()" class="px-4 py-2 rounded-xl bg-indigo-600 hover:bg-indigo-700 text-white text-sm font-semibold">Giao</button>
        </div>
        <p class="text-[11px] text-gray-400 mt-1.5">Chỉ giao được quiz do chính em tạo (tab Quiz). Chưa có quiz nào thì tạo trước ở tab Quiz nhé.</p>
        <p id="assignError" class="hidden text-xs text-red-500 mt-1"></p>
      </div>

      <div>
        <h4 class="text-sm font-bold mb-2">📝 Bài tập</h4>
        <div id="classAssignments" class="space-y-2"></div>
      </div>

      <div id="classStudentsSection" class="hidden">
        <h4 class="text-sm font-bold mb-2">👥 Học sinh</h4>
        <div id="classStudents" class="space-y-2"></div>
      </div>
    </div>

    <!-- ============ Thư viện trò chơi ============ -->
    <div id="fcGamesListView" class="hidden max-w-3xl mx-auto p-4 lg:p-6">
      <p class="text-xs text-gray-400 mb-4">Học mà chơi — chơi xong tự động lưu điểm yếu vào Sổ lỗi sai để ôn đúng chỗ.</p>
      <div class="grid sm:grid-cols-2 gap-4">
        {% if game_flags.quick_math %}
        <div class="rounded-2xl border border-gray-200 dark:border-gray-800 p-5">
          <p class="text-3xl mb-2">⚡</p>
          <p class="font-bold">Đố Vui Tính Nhanh</p>
          <p class="text-xs text-gray-400 mt-1 mb-3">60 giây, trả lời càng nhiều phép tính đúng càng tốt. Kết quả tự phân tích em hay sai phép tính nào.</p>
          <p class="text-xs text-gray-400 mb-3" id="quickMathBestScore">Điểm cao nhất: —</p>
          <button onclick="openQuickMathSetup()" class="w-full py-2 rounded-xl bg-amber-500 hover:bg-amber-600 text-white text-sm font-semibold">Chơi ngay</button>
        </div>
        {% endif %}
        {% if game_flags.snake %}
        <div class="rounded-2xl border border-gray-200 dark:border-gray-800 p-5">
          <p class="text-3xl mb-2">🐍</p>
          <p class="font-bold">Rắn Săn Chữ</p>
          <p class="text-xs text-gray-400 mt-1 mb-3">Điều khiển rắn ăn mồi để lớn lên, tránh đâm tường/tự đâm thân — thỉnh thoảng có mồi đặc biệt là đáp số 1 phép tính, ăn được thưởng điểm gấp 3!</p>
          <p class="text-xs text-gray-400 mb-3" id="snakeBestScore">Điểm cao nhất: —</p>
          <button onclick="openSnakeSetup()" class="w-full py-2 rounded-xl bg-emerald-600 hover:bg-emerald-700 text-white text-sm font-semibold">Chơi ngay</button>
        </div>
        {% endif %}
        {% if game_flags.memory_match %}
        <div class="rounded-2xl border border-gray-200 dark:border-gray-800 p-5">
          <p class="text-3xl mb-2">🧠</p>
          <p class="font-bold">Lật thẻ ghi nhớ</p>
          <p class="text-xs text-gray-400 mt-1 mb-3">Tìm cặp thẻ khớp nhau — cần chọn 1 bộ thẻ ghi nhớ có sẵn để chơi.</p>
          <p class="text-xs text-gray-400 mb-3" id="memoryMatchBestScore">Điểm cao nhất: —</p>
          <button onclick="switchFcTab('decks')" class="w-full py-2 rounded-xl bg-blue-600 hover:bg-blue-700 text-white text-sm font-semibold">Chọn bộ thẻ để chơi</button>
        </div>
        {% endif %}
      </div>
    </div>

    {% if game_flags.snake %}
    <!-- ============ Rắn Săn Chữ: thiết lập ============ -->
    <div id="fcSnakeSetupView" class="hidden max-w-md mx-auto p-4 lg:p-6 text-center">
      <p class="text-4xl mb-3">🐍</p>
      <p class="font-bold text-lg mb-1">Rắn Săn Chữ</p>
      <p class="text-sm text-gray-400 mb-5">Dùng phím mũi tên (hoặc vuốt trên điện thoại) để điều khiển. Ăn 🔵 để lớn lên, ăn 🟡 (đáp số đúng) để được điểm gấp 3!</p>
      <div class="grid grid-cols-3 gap-2 mb-5">
        <button class="snake-diff-btn px-3 py-2.5 rounded-xl border-2 border-emerald-500 bg-emerald-50 dark:bg-emerald-900/20 text-sm font-semibold" data-diff="easy">Dễ</button>
        <button class="snake-diff-btn px-3 py-2.5 rounded-xl border-2 border-gray-200 dark:border-gray-700 text-sm font-semibold" data-diff="medium">Trung bình</button>
        <button class="snake-diff-btn px-3 py-2.5 rounded-xl border-2 border-gray-200 dark:border-gray-700 text-sm font-semibold" data-diff="hard">Khó</button>
      </div>
      <button onclick="startSnakeGame()" class="w-full py-3 rounded-xl bg-emerald-600 hover:bg-emerald-700 text-white font-semibold">Bắt đầu</button>
    </div>

    <!-- ============ Rắn Săn Chữ: đang chơi ============ -->
    <div id="fcSnakePlayView" class="hidden max-w-md mx-auto p-4 lg:p-6">
      <div class="flex items-center justify-between mb-3 text-sm">
        <span>⭐ <span id="snakeScore">0</span></span>
        <span id="snakeQuestionBox" class="hidden font-semibold text-amber-500">🟡 <span id="snakeQuestion"></span> = ?</span>
        <span>🐍 x<span id="snakeLength">1</span></span>
      </div>
      <div id="snakeBoard" class="grid gap-0 mx-auto bg-gray-100 dark:bg-gray-900 rounded-xl overflow-hidden border border-gray-200 dark:border-gray-700" style="width: min(90vw, 360px); height: min(90vw, 360px);"></div>
      <div class="grid grid-cols-3 gap-2 mt-4 max-w-[180px] mx-auto">
        <div></div>
        <button class="snake-ctrl-btn py-3 rounded-xl bg-gray-100 dark:bg-gray-800 text-lg" data-dir="up"><i class="fas fa-arrow-up"></i></button>
        <div></div>
        <button class="snake-ctrl-btn py-3 rounded-xl bg-gray-100 dark:bg-gray-800 text-lg" data-dir="left"><i class="fas fa-arrow-left"></i></button>
        <button class="snake-ctrl-btn py-3 rounded-xl bg-gray-100 dark:bg-gray-800 text-lg" data-dir="down"><i class="fas fa-arrow-down"></i></button>
        <button class="snake-ctrl-btn py-3 rounded-xl bg-gray-100 dark:bg-gray-800 text-lg" data-dir="right"><i class="fas fa-arrow-right"></i></button>
      </div>
    </div>

    <!-- ============ Rắn Săn Chữ: kết quả ============ -->
    <div id="fcSnakeResultView" class="hidden max-w-md mx-auto p-4 lg:p-6 text-center">
      <p class="text-4xl mb-2">🐍💥</p>
      <p class="font-bold text-2xl" id="snakeResultScore"></p>
      <p class="text-sm text-gray-400 mt-1" id="snakeResultDetail"></p>
      <p class="text-sm text-gray-400" id="snakeResultXp"></p>
      <div id="snakeWeakTopicsBox" class="hidden mt-4 text-left bg-amber-50 dark:bg-amber-900/10 border border-amber-200 dark:border-amber-900/50 rounded-xl p-4">
        <p class="text-sm font-semibold text-amber-700 dark:text-amber-400 mb-1"><i class="fas fa-triangle-exclamation mr-1"></i>Hay sai phép tính:</p>
        <p id="snakeWeakTopicsList" class="text-sm text-amber-700 dark:text-amber-400"></p>
        <p class="text-xs text-amber-600 dark:text-amber-500 mt-1">Đã tự lưu vào Sổ lỗi sai!</p>
      </div>
      <div class="flex gap-2 justify-center mt-5">
        <button onclick="openSnakeSetup()" class="px-4 py-2 rounded-xl bg-emerald-600 hover:bg-emerald-700 text-white text-sm font-semibold">Chơi lại</button>
        <button onclick="switchFcTab('games')" class="px-4 py-2 rounded-xl bg-gray-100 dark:bg-gray-800 text-sm font-semibold">Về thư viện</button>
      </div>
    </div>
    {% endif %}

    {% if game_flags.quick_math %}
    <!-- ============ Đố Vui Tính Nhanh: thiết lập ============ -->
    <div id="fcQuickMathSetupView" class="hidden max-w-md mx-auto p-4 lg:p-6 text-center">
      <p class="text-4xl mb-3">⚡</p>
      <p class="font-bold text-lg mb-1">Đố Vui Tính Nhanh</p>
      <p class="text-sm text-gray-400 mb-5">Trả lời càng nhiều câu đúng càng tốt trong 60 giây!</p>
      <div class="grid grid-cols-3 gap-2 mb-5">
        <button class="qm-diff-btn px-3 py-2.5 rounded-xl border-2 border-emerald-500 bg-emerald-50 dark:bg-emerald-900/20 text-sm font-semibold" data-diff="easy">Dễ</button>
        <button class="qm-diff-btn px-3 py-2.5 rounded-xl border-2 border-gray-200 dark:border-gray-700 text-sm font-semibold" data-diff="medium">Trung bình</button>
        <button class="qm-diff-btn px-3 py-2.5 rounded-xl border-2 border-gray-200 dark:border-gray-700 text-sm font-semibold" data-diff="hard">Khó</button>
      </div>
      <button onclick="startQuickMath()" class="w-full py-3 rounded-xl bg-amber-500 hover:bg-amber-600 text-white font-semibold">Bắt đầu</button>
    </div>

    <!-- ============ Đố Vui Tính Nhanh: đang chơi ============ -->
    <div id="fcQuickMathPlayView" class="hidden max-w-md mx-auto p-4 lg:p-6">
      <div class="flex items-center justify-between mb-4 text-sm">
        <span>⏱️ <span id="qmTimer">60</span>s</span>
        <span>⭐ <span id="qmScore">0</span></span>
        <span>🔥 x<span id="qmCombo">0</span></span>
      </div>
      <div class="rounded-2xl border border-gray-200 dark:border-gray-700 p-6 text-center mb-4">
        <p id="qmQuestion" class="text-3xl font-bold"></p>
      </div>
      <div id="qmAnswerGrid" class="grid grid-cols-2 gap-3"></div>
    </div>

    <!-- ============ Đố Vui Tính Nhanh: kết quả ============ -->
    <div id="fcQuickMathResultView" class="hidden max-w-md mx-auto p-4 lg:p-6 text-center">
      <p class="text-4xl mb-2" id="qmResultEmoji">🎉</p>
      <p class="font-bold text-2xl" id="qmResultScore"></p>
      <p class="text-sm text-gray-400 mt-1" id="qmResultDetail"></p>
      <p class="text-sm text-gray-400" id="qmResultXp"></p>
      <div id="qmWeakTopicsBox" class="hidden mt-4 text-left bg-amber-50 dark:bg-amber-900/10 border border-amber-200 dark:border-amber-900/50 rounded-xl p-4">
        <p class="text-sm font-semibold text-amber-700 dark:text-amber-400 mb-1"><i class="fas fa-triangle-exclamation mr-1"></i>Hay sai phép tính:</p>
        <p id="qmWeakTopicsList" class="text-sm text-amber-700 dark:text-amber-400"></p>
        <p class="text-xs text-amber-600 dark:text-amber-500 mt-1">Đã tự lưu vào Sổ lỗi sai — vào tab "Sổ lỗi sai" để luyện lại nhé!</p>
      </div>
      <div class="flex gap-2 justify-center mt-5">
        <button onclick="openQuickMathSetup()" class="px-4 py-2 rounded-xl bg-amber-500 hover:bg-amber-600 text-white text-sm font-semibold">Chơi lại</button>
        <button onclick="switchFcTab('games')" class="px-4 py-2 rounded-xl bg-gray-100 dark:bg-gray-800 text-sm font-semibold">Về thư viện</button>
      </div>
    </div>
    {% endif %}

    <!-- ============ Quiz ============ -->
    <div id="fcQuizListView" class="hidden max-w-4xl mx-auto p-4 lg:p-6">
      <button onclick="openQuizForm()" class="px-4 py-2.5 rounded-xl bg-gradient-to-r from-blue-600 to-indigo-600 hover:opacity-90 text-white text-sm font-semibold flex items-center gap-2 mb-5">
        <i class="fas fa-wand-magic-sparkles"></i> Tạo quiz bằng AI ✨
      </button>

      <div id="quizForm" class="hidden mb-5 p-4 rounded-2xl border border-blue-200 dark:border-blue-900 bg-blue-50 dark:bg-blue-900/10 space-y-2.5">

        <input id="quizTopic" type="text" maxlength="200" placeholder="Chủ đề, vd: Phương trình bậc 2"
          class="w-full px-3 py-2.5 rounded-xl bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 dark:text-white">
        <div class="flex flex-wrap gap-2">
          <input id="quizSubject" type="text" maxlength="60" placeholder="Môn học (tuỳ chọn)"
            class="flex-1 min-w-[120px] px-3 py-2 rounded-xl bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 dark:text-white">
          <select id="quizDifficulty" class="px-3 py-2 rounded-xl bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 dark:text-white">
            <option value="easy">Dễ</option>
            <option value="medium" selected>Trung bình</option>
            <option value="hard">Khó</option>
            <option value="expert">Nâng cao</option>
          </select>
          <select id="quizCount" class="px-3 py-2 rounded-xl bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 dark:text-white">
            <option value="3">3 câu</option>
            <option value="5" selected>5 câu</option>
            <option value="10">10 câu</option>
          </select>
        </div>
        <div class="flex gap-2">
          <button id="quizSubmitBtn" onclick="submitQuizGeneration()" class="px-4 py-2 rounded-xl bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white text-sm font-semibold"><span id="quizSubmitLabel">Tạo quiz</span></button>
          <button onclick="document.getElementById('quizForm').classList.add('hidden')" class="px-4 py-2 rounded-xl bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 text-sm">Huỷ</button>
        </div>
        <p id="quizFormError" class="hidden text-xs text-red-500"></p>
      </div>

      <div id="quizGrid" class="grid sm:grid-cols-2 lg:grid-cols-3 gap-3"></div>
      <p id="quizEmptyState" class="hidden text-center text-sm text-gray-400 py-16">Chưa có quiz nào — tạo bài đầu tiên ở trên nhé! 📝</p>
    </div>

    <!-- ============ Làm quiz ============ -->
    <div id="fcQuizTakeView" class="hidden max-w-2xl mx-auto p-4 lg:p-6">
      <div class="flex items-center justify-between mb-4">
        <p id="quizTakeProgress" class="text-xs text-gray-400"></p>
        <p id="quizTakeTimer" class="text-xs text-gray-400"><i class="fas fa-clock mr-1"></i><span>0</span>s</p>
      </div>
      <div id="quizQuestionCard" class="rounded-2xl border border-gray-200 dark:border-gray-700 p-5">
        <p id="quizQuestionTopic" class="text-xs text-blue-500 font-semibold uppercase mb-1"></p>
        <p id="quizQuestionText" class="font-semibold text-lg mb-4"></p>
        <div id="quizAnswerArea" class="space-y-2"></div>
      </div>
      <div class="flex justify-between mt-4">
        <button onclick="showFcView('list'); switchFcTab('quiz')" class="text-sm text-gray-400 hover:text-gray-600 dark:hover:text-gray-300">Thoát</button>
        <button id="quizNextBtn" onclick="submitQuizAnswerAndNext()" class="px-5 py-2.5 rounded-xl bg-blue-600 hover:bg-blue-700 text-white text-sm font-semibold">Tiếp theo <i class="fas fa-arrow-right ml-1"></i></button>
      </div>
    </div>

    <!-- ============ Kết quả quiz ============ -->
    <div id="fcQuizResultView" class="hidden max-w-2xl mx-auto p-4 lg:p-6 text-center">
      <p class="text-3xl mb-1" id="quizResultEmoji">🎉</p>
      <p class="font-bold text-2xl" id="quizResultScore"></p>
      <p class="text-sm text-gray-400 mt-1" id="quizResultXp"></p>
      <div id="quizWeakTopics" class="hidden mt-4 text-left mx-auto max-w-sm bg-amber-50 dark:bg-amber-900/10 border border-amber-200 dark:border-amber-900/50 rounded-xl p-4">
        <p class="text-sm font-semibold text-amber-700 dark:text-amber-400 mb-1"><i class="fas fa-triangle-exclamation mr-1"></i>Chủ đề cần ôn lại:</p>
        <p id="quizWeakTopicsList" class="text-sm text-amber-700 dark:text-amber-400"></p>
      </div>
      <div id="quizReviewList" class="mt-5 text-left space-y-2 max-w-lg mx-auto"></div>
      <button onclick="showFcView('list'); switchFcTab('quiz')" class="mt-6 px-5 py-2.5 rounded-xl bg-blue-600 hover:bg-blue-700 text-white text-sm font-semibold">Xong</button>
    </div>

    <!-- ============ Kế hoạch ôn tập ============ -->
    <div id="fcPlansListView" class="hidden max-w-4xl mx-auto p-4 lg:p-6">
      <button onclick="openPlanForm()" class="px-4 py-2.5 rounded-xl bg-gradient-to-r from-emerald-600 to-teal-600 hover:opacity-90 text-white text-sm font-semibold flex items-center gap-2 mb-5">
        <i class="fas fa-wand-magic-sparkles"></i> Tạo kế hoạch bằng AI ✨
      </button>

      <div id="planForm" class="hidden mb-5 p-4 rounded-2xl border border-emerald-200 dark:border-emerald-900 bg-emerald-50 dark:bg-emerald-900/10 space-y-2.5">
        <input id="planGoal" type="text" maxlength="300" placeholder="Mục tiêu, vd: Ôn thi học kỳ Toán 8"
          class="w-full px-3 py-2.5 rounded-xl bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 text-sm focus:outline-none focus:ring-2 focus:ring-emerald-500 dark:text-white">
        <div class="flex flex-wrap gap-2">
          <input id="planSubject" type="text" maxlength="60" placeholder="Môn học (tuỳ chọn)"
            class="flex-1 min-w-[120px] px-3 py-2 rounded-xl bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 text-sm focus:outline-none focus:ring-2 focus:ring-emerald-500 dark:text-white">
          <input id="planDays" type="number" min="3" max="60" value="14" placeholder="Số ngày"
            class="w-28 px-3 py-2 rounded-xl bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 text-sm focus:outline-none focus:ring-2 focus:ring-emerald-500 dark:text-white">
        </div>
        <div class="flex gap-2">
          <button id="planSubmitBtn" onclick="submitPlanGeneration()" class="px-4 py-2 rounded-xl bg-emerald-600 hover:bg-emerald-700 disabled:opacity-50 text-white text-sm font-semibold"><span id="planSubmitLabel">Tạo kế hoạch</span></button>
          <button onclick="document.getElementById('planForm').classList.add('hidden')" class="px-4 py-2 rounded-xl bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 text-sm">Huỷ</button>
        </div>
        <p id="planFormError" class="hidden text-xs text-red-500"></p>
      </div>

      <div id="planGrid" class="grid sm:grid-cols-2 gap-3"></div>
      <p id="planEmptyState" class="hidden text-center text-sm text-gray-400 py-16">Chưa có kế hoạch nào — tạo kế hoạch đầu tiên ở trên nhé! 🎯</p>
    </div>

    <!-- ============ Chi tiết kế hoạch ============ -->
    <div id="fcPlanDetailView" class="hidden max-w-2xl mx-auto p-4 lg:p-6">
      <div class="flex flex-wrap items-center justify-between gap-2 mb-2">
        <div>
          <h3 id="planDetailTitle" class="font-bold text-lg"></h3>
          <p id="planDetailMeta" class="text-xs text-gray-400"></p>
        </div>
        <div class="flex gap-2">
          <button id="planReorganizeBtn" onclick="reorganizeCurrentPlan()" class="hidden px-3 py-2 rounded-xl bg-amber-50 dark:bg-amber-900/20 text-amber-600 dark:text-amber-400 text-xs font-semibold hover:bg-amber-100 dark:hover:bg-amber-900/40"><i class="fas fa-arrows-rotate mr-1"></i>Sắp xếp lại</button>
          <button onclick="deleteCurrentPlan()" class="px-3 py-2 rounded-xl bg-red-50 dark:bg-red-900/20 text-red-600 dark:text-red-400 text-sm hover:bg-red-100 dark:hover:bg-red-900/40"><i class="fas fa-trash-can"></i></button>
        </div>
      </div>
      <div class="gamify-xp-track mb-4"><div id="planProgressBar" class="gamify-xp-fill" style="width:0%;"></div></div>
      <div id="planTaskList" class="space-y-2"></div>
    </div>

    <!-- ============ Sổ lỗi sai ============ -->
    <div id="fcMistakesView" class="hidden max-w-3xl mx-auto p-4 lg:p-6">
      <p class="text-xs text-gray-400 mb-4">Lỗi hay lặp lại nhiều lần được xếp lên đầu — bấm "Ôn lại ngay" để StudyMate ra bài luyện đúng chỗ em còn yếu.</p>
      <div id="mistakeGroups" class="space-y-5"></div>
      <p id="mistakeEmptyState" class="hidden text-center text-sm text-gray-400 py-16">Chưa có lỗi nào được ghi lại — đó là điều tốt! 🎉<br>Bấm "Lưu vào Sổ lỗi sai" dưới câu trả lời AI khi em phát hiện mình mắc lỗi nhé.</p>
    </div>

    <!-- ============ Danh sách bộ thẻ ============ -->
    <div id="fcDeckListView" class="max-w-4xl mx-auto p-4 lg:p-6">
      <div class="flex flex-wrap gap-2 mb-5">
        <button onclick="openCreateDeckPrompt()" class="px-4 py-2.5 rounded-xl border border-gray-300 dark:border-gray-700 hover:bg-gray-100 dark:hover:bg-gray-800 text-sm font-medium flex items-center gap-2">
          <i class="fas fa-plus"></i> Tạo bộ thẻ trống
        </button>
        <button onclick="openAiDeckForm()" class="px-4 py-2.5 rounded-xl bg-gradient-to-r from-purple-600 to-indigo-600 hover:opacity-90 text-white text-sm font-semibold flex items-center gap-2">
          <i class="fas fa-wand-magic-sparkles"></i> Tạo bằng AI ✨
        </button>
      </div>

      <div id="aiDeckForm" class="hidden mb-5 p-4 rounded-2xl border border-purple-200 dark:border-purple-900 bg-purple-50 dark:bg-purple-900/10 space-y-2.5">
        <p class="text-sm font-semibold flex items-center gap-1.5"><i class="fas fa-wand-magic-sparkles text-purple-500"></i> AI tạo bộ thẻ giúp em</p>
        <input id="aiDeckTopic" type="text" maxlength="200" placeholder="Chủ đề, vd: Từ vựng tiếng Anh unit 5, Hằng đẳng thức đáng nhớ..."
          class="w-full px-3 py-2.5 rounded-xl bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 text-sm focus:outline-none focus:ring-2 focus:ring-purple-500 dark:text-white">
        <div class="flex flex-wrap gap-2">
          <input id="aiDeckSubject" type="text" maxlength="60" placeholder="Môn học (tuỳ chọn)"
            class="flex-1 min-w-[140px] px-3 py-2 rounded-xl bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 text-sm focus:outline-none focus:ring-2 focus:ring-purple-500 dark:text-white">
          <select id="aiDeckCount" class="px-3 py-2 rounded-xl bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 text-sm focus:outline-none focus:ring-2 focus:ring-purple-500 dark:text-white">
            <option value="4">4 thẻ</option>
            <option value="8" selected>8 thẻ</option>
            <option value="12">12 thẻ</option>
          </select>
        </div>
        <div class="flex gap-2">
          <button id="aiDeckSubmitBtn" onclick="submitAiDeck()" class="px-4 py-2 rounded-xl bg-purple-600 hover:bg-purple-700 disabled:opacity-50 text-white text-sm font-semibold flex items-center gap-2">
            <span id="aiDeckSubmitLabel">Tạo bộ thẻ</span>
          </button>
          <button onclick="document.getElementById('aiDeckForm').classList.add('hidden')" class="px-4 py-2 rounded-xl bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 text-sm">Huỷ</button>
        </div>
        <p id="aiDeckError" class="hidden text-xs text-red-500"></p>
      </div>

      <div id="deckGrid" class="grid sm:grid-cols-2 lg:grid-cols-3 gap-3"></div>
      <p id="deckEmptyState" class="hidden text-center text-sm text-gray-400 py-16">Chưa có bộ thẻ nào — tạo bộ đầu tiên ở trên nhé! 🗂️</p>
    </div>

    <!-- ============ Chi tiết 1 bộ thẻ ============ -->
    <div id="fcDeckDetailView" class="hidden max-w-3xl mx-auto p-4 lg:p-6">
      <div class="flex flex-wrap items-center justify-between gap-2 mb-4">
        <div>
          <h3 id="fcDeckTitle" class="font-bold text-lg"></h3>
          <p id="fcDeckMeta" class="text-xs text-gray-400"></p>
        </div>
        <div class="flex flex-wrap gap-2">
          <button onclick="startStudyMode()" class="px-3.5 py-2 rounded-xl bg-blue-600 hover:bg-blue-700 text-white text-sm font-semibold flex items-center gap-1.5"><i class="fas fa-graduation-cap"></i> Học</button>
          {% if game_flags.memory_match %}
          <button onclick="startMemoryGame()" class="px-3.5 py-2 rounded-xl bg-amber-500 hover:bg-amber-600 text-white text-sm font-semibold flex items-center gap-1.5"><i class="fas fa-gamepad"></i> Lật thẻ</button>
          {% endif %}
          <button onclick="deleteCurrentDeck()" class="px-3 py-2 rounded-xl bg-red-50 dark:bg-red-900/20 text-red-600 dark:text-red-400 text-sm hover:bg-red-100 dark:hover:bg-red-900/40"><i class="fas fa-trash-can"></i></button>
        </div>
      </div>

      <div class="mb-4 p-3 rounded-xl bg-gray-50 dark:bg-gray-900 border border-gray-100 dark:border-gray-800 flex flex-wrap gap-2 items-end">
        <input id="newCardFront" type="text" maxlength="200" placeholder="Mặt trước..." class="flex-1 min-w-[120px] px-3 py-2 rounded-lg bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 dark:text-white">
        <input id="newCardBack" type="text" maxlength="500" placeholder="Mặt sau..." class="flex-1 min-w-[120px] px-3 py-2 rounded-lg bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 dark:text-white">
        <button onclick="addCardToCurrentDeck()" class="px-3.5 py-2 rounded-lg bg-gray-800 dark:bg-gray-700 hover:bg-gray-900 text-white text-sm font-medium">Thêm thẻ</button>
      </div>

      <div id="cardList" class="space-y-2"></div>
    </div>

    <!-- ============ Chế độ Học (lật thẻ ôn tập) ============ -->
    <div id="fcStudyView" class="hidden max-w-lg mx-auto p-4 lg:p-6 flex flex-col items-center">
      <p id="studyProgress" class="text-xs text-gray-400 mb-3">Thẻ 1/1</p>
      <div id="studyCard" onclick="flipStudyCard()" class="w-full aspect-[4/3] rounded-2xl bg-gradient-to-br from-blue-50 to-indigo-100 dark:from-gray-800 dark:to-gray-900 border border-gray-200 dark:border-gray-700 flex items-center justify-center p-6 text-center cursor-pointer select-none shadow-sm">
        <p id="studyCardText" class="text-lg font-semibold"></p>
      </div>
      <p class="text-xs text-gray-400 mt-2">Bấm vào thẻ để lật</p>
      <div id="studyAnswerBtns" class="hidden mt-5 flex gap-3 w-full">
        <button onclick="answerStudyCard(false)" class="flex-1 py-3 rounded-xl bg-red-50 dark:bg-red-900/20 text-red-600 dark:text-red-400 font-semibold hover:bg-red-100 dark:hover:bg-red-900/40"><i class="fas fa-xmark mr-1"></i> Chưa nhớ</button>
        <button onclick="answerStudyCard(true)" class="flex-1 py-3 rounded-xl bg-emerald-50 dark:bg-emerald-900/20 text-emerald-600 dark:text-emerald-400 font-semibold hover:bg-emerald-100 dark:hover:bg-emerald-900/40"><i class="fas fa-check mr-1"></i> Đã nhớ</button>
      </div>
      <div id="studySummary" class="hidden text-center mt-4">
        <p class="text-2xl mb-1">🎉</p>
        <p class="font-bold text-lg" id="studySummaryText"></p>
        <button onclick="showDeckDetail(currentDeckId)" class="mt-4 px-4 py-2 rounded-xl bg-blue-600 hover:bg-blue-700 text-white text-sm font-semibold">Xong</button>
      </div>
    </div>

    {% if game_flags.memory_match %}
    <!-- ============ Game: Lật thẻ ghi nhớ (Memory Match) ============ -->
    <div id="fcGameView" class="hidden max-w-2xl mx-auto p-4 lg:p-6">
      <div class="flex items-center justify-between mb-4 text-sm">
        <span>⏱️ <span id="gameTimer">0</span>s</span>
        <span>🔄 <span id="gameMoves">0</span> lượt lật</span>
        <span>✅ <span id="gameMatched">0</span>/<span id="gameTotalPairs">0</span> cặp</span>
      </div>
      <div id="gameGrid" class="grid grid-cols-4 gap-2 sm:gap-3"></div>
      <div id="gameWinPanel" class="hidden text-center mt-6">
        <p class="text-2xl mb-1">🏆</p>
        <p class="font-bold text-lg">Hoàn thành! <span id="gameWinTime"></span>s, <span id="gameWinMoves"></span> lượt lật</p>
        <p class="text-sm text-gray-400 mt-1" id="gameXpText"></p>
        <div class="flex gap-2 justify-center mt-4">
          <button onclick="startMemoryGame()" class="px-4 py-2 rounded-xl bg-amber-500 hover:bg-amber-600 text-white text-sm font-semibold">Chơi lại</button>
          <button onclick="showDeckDetail(currentDeckId)" class="px-4 py-2 rounded-xl bg-gray-100 dark:bg-gray-800 text-sm font-semibold">Về bộ thẻ</button>
        </div>
      </div>
    </div>
    {% endif %}
  </div>
</div>

<div id="progressOverlay" class="hidden fixed inset-0 bg-white dark:bg-[#131313] z-[70] flex flex-col">
  <div class="flex items-center justify-between px-4 lg:px-6 py-3.5 border-b border-gray-200 dark:border-gray-800 flex-shrink-0">
    <h2 class="font-bold flex items-center gap-2"><i class="fas fa-chart-line text-indigo-500"></i> Tiến độ học tập của em</h2>
    <button onclick="document.getElementById('progressOverlay').classList.add('hidden')" class="w-9 h-9 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800 flex items-center justify-center"><i class="fas fa-xmark"></i></button>
  </div>
  <div class="flex-1 overflow-y-auto">
    <div class="max-w-3xl mx-auto p-4 lg:p-6 space-y-5">

      <!-- Gợi ý hôm nay -->
      <div id="progressSuggestionBox" class="rounded-2xl p-4 bg-gradient-to-br from-indigo-50 to-purple-50 dark:from-indigo-900/20 dark:to-purple-900/20 border border-indigo-100 dark:border-indigo-900/50">
        <p class="text-xs font-semibold text-indigo-500 uppercase mb-1.5"><i class="fas fa-lightbulb mr-1"></i>Gợi ý hôm nay</p>
        <p id="progressSuggestionText" class="text-sm font-medium mb-2"></p>
        <button id="progressSuggestionAction" class="hidden text-sm font-semibold px-3.5 py-2 rounded-lg bg-indigo-600 hover:bg-indigo-700 text-white"></button>
      </div>

      <!-- Tổng quan -->
      <div class="grid grid-cols-3 gap-3">
        <div class="rounded-2xl border border-gray-200 dark:border-gray-800 p-4 text-center">
          <p class="text-2xl font-bold text-indigo-600 dark:text-indigo-400" id="progLevel">1</p>
          <p class="text-[11px] text-gray-400 uppercase mt-0.5">Cấp độ</p>
        </div>
        <div class="rounded-2xl border border-gray-200 dark:border-gray-800 p-4 text-center">
          <p class="text-2xl font-bold text-orange-500" id="progStreak">0</p>
          <p class="text-[11px] text-gray-400 uppercase mt-0.5">Streak (dài nhất <span id="progLongestStreak">0</span>)</p>
        </div>
        <div class="rounded-2xl border border-gray-200 dark:border-gray-800 p-4 text-center">
          <p class="text-2xl font-bold text-emerald-500" id="progXp">0</p>
          <p class="text-[11px] text-gray-400 uppercase mt-0.5">Tổng XP</p>
        </div>
      </div>

      <!-- Môn học hay hỏi nhất -->
      <div id="progressSubjectsSection" class="hidden">
        <h3 class="text-sm font-bold mb-2.5">📚 Môn học hay hỏi nhất</h3>
        <div id="progressSubjects" class="space-y-2"></div>
      </div>

      <!-- Điểm yếu cần ôn -->
      <div id="progressWeakSection" class="hidden">
        <h3 class="text-sm font-bold mb-2.5">⚠️ Điểm yếu cần ôn (chưa khắc phục)</h3>
        <div id="progressWeakTopics" class="space-y-2"></div>
      </div>

      <!-- Quiz -->
      <div id="progressQuizSection" class="hidden">
        <h3 class="text-sm font-bold mb-2.5">📝 Kết quả Quiz gần đây</h3>
        <p class="text-xs text-gray-400 mb-2">Điểm trung bình: <span id="progQuizAvg" class="font-semibold"></span>% (<span id="progQuizCount"></span> bài đã làm)</p>
        <div id="progressQuizList" class="flex gap-2 flex-wrap"></div>
      </div>

      <!-- Kế hoạch ôn tập -->
      <div id="progressPlansSection" class="hidden">
        <h3 class="text-sm font-bold mb-2.5">🎯 Kế hoạch ôn tập</h3>
        <div id="progressPlans" class="space-y-2"></div>
      </div>

      <!-- Trò chơi -->
      <div id="progressGamesSection" class="hidden">
        <h3 class="text-sm font-bold mb-2.5">🎮 Điểm cao trò chơi</h3>
        <div id="progressGames" class="grid grid-cols-3 gap-2"></div>
      </div>

      <!-- Thành tựu -->
      <div>
        <h3 class="text-sm font-bold mb-2.5">🏅 Thành tựu</h3>
        <div id="progressAchievements" class="grid grid-cols-4 sm:grid-cols-6 gap-2"></div>
      </div>

    </div>
  </div>
</div>

<div id="streakFireOverlay" class="hidden fixed inset-0 z-[80] flex items-center justify-center pointer-events-none">
  <div id="streakFireGlow" class="absolute inset-0"></div>
  <div id="streakFireContent" class="relative text-center">
    <div id="streakFireEmoji" class="leading-none select-none"></div>
    <p id="streakFireText" class="mt-3 font-extrabold text-white text-xl drop-shadow-[0_2px_8px_rgba(0,0,0,0.6)]"></p>
  </div>
</div>

<div id="modalBackdrop" class="hidden fixed inset-0 bg-black/50 z-[60] flex items-center justify-center p-4" onclick="if(event.target===this) closeAllModals()">

  <!-- Cài đặt -->
  <div id="settingsModal" class="hidden modal-panel bg-white dark:bg-gray-800 rounded-2xl shadow-2xl w-full max-w-md max-h-[85vh] overflow-y-auto">
    <div class="flex items-center justify-between px-5 py-4 border-b border-gray-100 dark:border-gray-700">
      <h3 class="font-bold text-lg flex items-center gap-2"><i class="fas fa-gear text-gray-400"></i> Cài đặt</h3>
      <button onclick="closeAllModals()" class="w-8 h-8 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-700 flex items-center justify-center"><i class="fas fa-xmark"></i></button>
    </div>
    <div class="p-5 space-y-5">
      <!-- Tài khoản: avatar + (khách -> tạo tài khoản chính thức | đã có mật khẩu -> đổi mật khẩu) -->
      <div class="rounded-xl border border-gray-200 dark:border-gray-700 p-3.5 space-y-3">
        <label class="text-sm font-semibold block">Avatar</label>
        <div id="avatarGrid" class="grid grid-cols-8 gap-1.5">
          {% for a in avatar_presets %}
          <button type="button" class="avatar-opt-btn w-8 h-8 rounded-full bg-gradient-to-br {{ a.color }} flex items-center justify-center text-base hover:scale-110 transition-transform {{ 'ring-2 ring-offset-2 ring-blue-500 dark:ring-offset-gray-800' if a.emoji == avatar_emoji else '' }}" data-emoji="{{ a.emoji }}" data-color="{{ a.color }}">{{ a.emoji }}</button>
          {% endfor %}
        </div>

        {% if is_guest %}
        <div class="pt-3 border-t border-gray-100 dark:border-gray-700">
          <p class="text-xs font-semibold text-amber-600 dark:text-amber-400 mb-1"><i class="fas fa-bolt mr-1"></i>Bạn đang dùng thử (tài khoản khách)</p>
          <p class="text-xs text-gray-400 mb-2.5">Tạo tài khoản chính thức để không mất dữ liệu khi xoá cookie trình duyệt — toàn bộ lịch sử chat, XP, thẻ ghi nhớ... sẽ được giữ nguyên.</p>
          <input id="guestUsername" type="text" maxlength="32" placeholder="Tên đăng nhập mới"
            class="w-full px-3 py-2 rounded-lg bg-gray-100 dark:bg-gray-900 border-0 focus:outline-none focus:ring-2 focus:ring-blue-500 text-sm dark:text-white mb-2">
          <input id="guestPassword" type="password" minlength="6" placeholder="Mật khẩu (tối thiểu 6 ký tự)"
            class="w-full px-3 py-2 rounded-lg bg-gray-100 dark:bg-gray-900 border-0 focus:outline-none focus:ring-2 focus:ring-blue-500 text-sm dark:text-white mb-2">
          <input id="guestConfirm" type="password" minlength="6" placeholder="Nhập lại mật khẩu"
            class="w-full px-3 py-2 rounded-lg bg-gray-100 dark:bg-gray-900 border-0 focus:outline-none focus:ring-2 focus:ring-blue-500 text-sm dark:text-white mb-2">
          <button id="guestUpgradeBtn" onclick="submitGuestUpgrade()" class="w-full bg-amber-500 hover:bg-amber-600 text-white font-semibold py-2 rounded-lg text-sm">Tạo tài khoản chính thức</button>
          <p id="guestUpgradeStatus" class="hidden text-xs mt-1.5"></p>
        </div>
        {% elif has_password %}
        <div class="pt-3 border-t border-gray-100 dark:border-gray-700">
          <p class="text-sm font-semibold mb-2">Đổi mật khẩu</p>
          <input id="curPassword" type="password" placeholder="Mật khẩu hiện tại"
            class="w-full px-3 py-2 rounded-lg bg-gray-100 dark:bg-gray-900 border-0 focus:outline-none focus:ring-2 focus:ring-blue-500 text-sm dark:text-white mb-2">
          <input id="newPassword" type="password" minlength="6" placeholder="Mật khẩu mới (tối thiểu 6 ký tự)"
            class="w-full px-3 py-2 rounded-lg bg-gray-100 dark:bg-gray-900 border-0 focus:outline-none focus:ring-2 focus:ring-blue-500 text-sm dark:text-white mb-2">
          <input id="newPasswordConfirm" type="password" minlength="6" placeholder="Nhập lại mật khẩu mới"
            class="w-full px-3 py-2 rounded-lg bg-gray-100 dark:bg-gray-900 border-0 focus:outline-none focus:ring-2 focus:ring-blue-500 text-sm dark:text-white mb-2">
          <button onclick="submitPasswordChange()" class="w-full bg-gray-800 dark:bg-gray-700 hover:bg-gray-900 text-white font-semibold py-2 rounded-lg text-sm">Đổi mật khẩu</button>
          <p id="passwordChangeStatus" class="hidden text-xs mt-1.5"></p>
          <button onclick="regenerateRecoveryCode()" class="w-full mt-2 bg-amber-50 dark:bg-amber-900/20 hover:bg-amber-100 dark:hover:bg-amber-900/40 text-amber-700 dark:text-amber-400 font-medium py-2 rounded-lg text-sm">
            <i class="fas fa-key mr-1"></i> Tạo mã khôi phục mật khẩu mới
          </button>
          <p class="text-[11px] text-gray-400 mt-1">Dùng để lấy lại mật khẩu nếu quên sau này. Tạo mã mới sẽ làm mã cũ (nếu có) hết hiệu lực.</p>
        </div>
        {% else %}
        <p class="text-xs text-gray-400 pt-3 border-t border-gray-100 dark:border-gray-700">Tài khoản đăng nhập bằng Google — không cần mật khẩu ở đây.</p>
        {% endif %}
      </div>

      <div class="rounded-xl border border-gray-200 dark:border-gray-700 p-3.5 flex items-center gap-3">
        <div class="flex-1 min-w-0">
          <div class="flex items-center gap-1.5">
            <span id="planBadge" class="text-[11px] font-semibold px-2 py-0.5 rounded-full {{ plan_meta[current_plan].badge }}">{{ plan_meta[current_plan].icon }} {{ plan_meta[current_plan].label }}</span>
          </div>
          <p id="planQuotaText" class="text-xs text-gray-400 mt-1.5">Đang tải thông tin gói...</p>
          <div class="w-full h-1.5 bg-gray-100 dark:bg-gray-700 rounded-full mt-1.5 overflow-hidden">
            <div id="planQuotaBar" class="h-full bg-indigo-500 rounded-full" style="width: 0%;"></div>
          </div>
        </div>
        {% if current_plan != 'max' %}
        <button type="button" onclick="openModal('upgradeModal')" class="flex-shrink-0 text-xs font-semibold px-3 py-2 rounded-lg bg-amber-50 dark:bg-amber-900/30 text-amber-600 dark:text-amber-400 hover:bg-amber-100 dark:hover:bg-amber-900/50">
          <i class="fas fa-bolt mr-1"></i> Nâng cấp
        </button>
        {% endif %}
      </div>
      <div>
        <label class="text-sm font-semibold block mb-2" data-i18n="settings_appearance">Giao diện</label>
        <div class="grid grid-cols-3 gap-2" id="themeOptions">
          <button type="button" data-theme="light" data-i18n="theme_light" class="theme-opt px-3 py-2 rounded-xl border text-sm font-medium">☀️ Sáng</button>
          <button type="button" data-theme="dark" data-i18n="theme_dark" class="theme-opt px-3 py-2 rounded-xl border text-sm font-medium">🌙 Tối</button>
          <button type="button" data-theme="system" data-i18n="theme_system" class="theme-opt px-3 py-2 rounded-xl border text-sm font-medium">💻 Hệ thống</button>
        </div>
      </div>
      <div>
        <label class="text-sm font-semibold block mb-2" data-i18n="settings_language">Ngôn ngữ / Language</label>
        <select id="languageSelect" class="w-full px-3 py-2.5 rounded-xl bg-gray-100 dark:bg-gray-700 border-0 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 dark:text-white">
          <option value="vi">Tiếng Việt</option>
          <option value="en">English</option>
        </select>
      </div>
      <div class="grid grid-cols-2 gap-3">
        <div>
          <label class="text-sm font-semibold block mb-2" data-i18n="settings_default_subject">Môn học mặc định</label>
          <select id="defaultSubjectSelect" class="w-full px-3 py-2.5 rounded-xl bg-gray-100 dark:bg-gray-700 border-0 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 dark:text-white">
            <option value="Toán" data-i18n="subj_toan_plain">Toán Học</option>
            <option value="Ngữ Văn" data-i18n="subj_van_plain">Ngữ Văn</option>
            <option value="Tiếng Anh" data-i18n="subj_anh_plain">Tiếng Anh</option>
            <option value="Vật Lý" data-i18n="subj_ly_plain">Vật Lý</option>
            <option value="Hóa Học" data-i18n="subj_hoa_plain">Hóa Học</option>
            <option value="Sinh Học" data-i18n="subj_sinh_plain">Sinh Học</option>
            <option value="Lịch sử & Địa lý" data-i18n="subj_sudia_plain">Lịch sử & Địa lý</option>
            <option value="Tin Học" data-i18n="subj_tin_plain">Tin Học</option>
          </select>
        </div>
        <div>
          <label class="text-sm font-semibold block mb-2" data-i18n="settings_default_mode">Chế độ mặc định</label>
          <select id="defaultModeSelect" class="w-full px-3 py-2.5 rounded-xl bg-gray-100 dark:bg-gray-700 border-0 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 dark:text-white">
            <option value="Giải thích" data-i18n="mode_giaithich_short">Giải Thích</option>
            <option value="Gợi ý" data-i18n="mode_goiy_short">Gợi Ý</option>
            <option value="Kiểm tra bài làm" data-i18n="mode_kiemtra_short">Kiểm Tra</option>
            <option value="Luyện tập" data-i18n="mode_luyentap_short">Luyện Tập</option>
            <option value="Ôn tập" data-i18n="mode_ontap_short">Ôn Tập</option>
          </select>
        </div>
      </div>
      <div id="settingsSavedMsg" class="hidden text-sm text-emerald-600 dark:text-emerald-400 flex items-center gap-1"><i class="fas fa-check"></i> <span data-i18n="saved">Đã lưu</span></div>
      <button onclick="savePreferences()" class="w-full bg-blue-600 hover:bg-blue-700 text-white font-semibold py-2.5 rounded-xl transition-colors" data-i18n="save_changes">Lưu thay đổi</button>

      <div class="border-t border-gray-100 dark:border-gray-700 pt-4 space-y-2">
        <p class="text-xs font-semibold text-red-500 uppercase mb-2" data-i18n="danger_zone">Khu vực nguy hiểm</p>
        <button onclick="clearAllHistory()" class="w-full bg-red-50 dark:bg-red-900/20 hover:bg-red-100 dark:hover:bg-red-900/40 text-red-600 dark:text-red-400 font-medium py-2.5 rounded-xl text-sm transition-colors">
          <i class="fas fa-trash mr-1"></i> <span data-i18n="clear_all_history">Xoá toàn bộ lịch sử trò chuyện</span>
        </button>
        <button onclick="clearMyMemories()" class="w-full bg-purple-50 dark:bg-purple-900/20 hover:bg-purple-100 dark:hover:bg-purple-900/40 text-purple-600 dark:text-purple-400 font-medium py-2.5 rounded-xl text-sm transition-colors">
          <i class="fas fa-brain mr-1"></i> <span data-i18n="clear_my_memory">Xoá bộ nhớ AI của tôi</span>
        </button>
      </div>
    </div>
  </div>

  <!-- Command Palette (Ctrl/⌘ + K) -->
  <div id="paletteModal" class="hidden modal-panel bg-white dark:bg-gray-800 rounded-2xl shadow-2xl w-full max-w-lg overflow-hidden" style="align-self: flex-start; margin-top: 10vh;">
    <div class="flex items-center gap-3 px-4 py-3.5 border-b border-gray-100 dark:border-gray-700">
      <i class="fas fa-magnifying-glass text-gray-400"></i>
      <input id="paletteInput" type="text" placeholder="Hỏi AI hoặc tìm lệnh... (vd: tạo quiz, cài đặt, sổ lỗi sai)"
        class="flex-1 bg-transparent border-0 focus:outline-none focus:ring-0 text-sm dark:text-white" autocomplete="off">
      <kbd class="text-[10px] px-1.5 py-0.5 rounded bg-gray-100 dark:bg-gray-700 text-gray-400">ESC</kbd>
    </div>
    <div id="paletteList" class="max-h-[50vh] overflow-y-auto p-2"></div>
  </div>

  <!-- Trợ giúp -->
  <div id="helpModal" class="hidden modal-panel bg-white dark:bg-gray-800 rounded-2xl shadow-2xl w-full max-w-md max-h-[85vh] overflow-y-auto">
    <div class="flex items-center justify-between px-5 py-4 border-b border-gray-100 dark:border-gray-700">
      <h3 class="font-bold text-lg flex items-center gap-2"><i class="fas fa-circle-question text-gray-400"></i> <span data-i18n="help_title">Trợ giúp &amp; phím tắt</span></h3>
      <button onclick="closeAllModals()" class="w-8 h-8 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-700 flex items-center justify-center"><i class="fas fa-xmark"></i></button>
    </div>
    <div class="p-5 space-y-5 text-sm">
      <div>
        <p class="font-semibold mb-2" data-i18n="shortcuts_title">Phím tắt</p>
        <div class="space-y-1.5">
          <div class="flex justify-between"><span class="text-gray-500" data-i18n="send_question">Gửi câu hỏi</span><kbd class="px-2 py-0.5 bg-gray-100 dark:bg-gray-700 rounded text-xs font-mono">Enter</kbd></div>
          <div class="flex justify-between"><span class="text-gray-500" data-i18n="new_line">Xuống dòng</span><kbd class="px-2 py-0.5 bg-gray-100 dark:bg-gray-700 rounded text-xs font-mono">Shift + Enter</kbd></div>
          <div class="flex justify-between"><span class="text-gray-500" data-i18n="command_palette">Bảng lệnh nhanh (đổi tên đăng nhập, tạo quiz...)</span><kbd class="px-2 py-0.5 bg-gray-100 dark:bg-gray-700 rounded text-xs font-mono">Ctrl/⌘ + K</kbd></div>
          <div class="flex justify-between"><span class="text-gray-500" data-i18n="open_help">Mở trợ giúp</span><kbd class="px-2 py-0.5 bg-gray-100 dark:bg-gray-700 rounded text-xs font-mono">Ctrl/⌘ + /</kbd></div>
        </div>
      </div>
      <div>
        <p class="font-semibold mb-2">Câu hỏi thường gặp</p>
        <div class="space-y-3 text-gray-600 dark:text-gray-300">
          <div><p class="font-medium text-gray-800 dark:text-gray-100">StudyMate đọc được file gì?</p><p>PDF, Word (.docx), .txt, .csv và ảnh (PNG/JPG/GIF/WEBP) — kéo-thả trực tiếp vào khung chat hoặc bấm nút 📎.</p></div>
          <div><p class="font-medium text-gray-800 dark:text-gray-100">Dữ liệu của em có bị mất không?</p><p>Lịch sử trò chuyện được lưu theo tài khoản, vẫn còn khi em đăng nhập lại trên thiết bị khác.</p></div>
          <div><p class="font-medium text-gray-800 dark:text-gray-100">"Dự án" dùng để làm gì?</p><p>Gom các đoạn chat cùng chủ đề (vd: "Ôn thi Học kỳ 2") lại một chỗ cho dễ tìm, giống thư mục.</p></div>
        </div>
      </div>
    </div>
  </div>

  <!-- Nâng cấp gói -->
  <div id="upgradeModal" class="hidden modal-panel bg-white dark:bg-gray-800 rounded-2xl shadow-2xl w-full max-w-3xl max-h-[85vh] overflow-y-auto">
    <div class="flex items-center justify-between px-5 py-4 border-b border-gray-100 dark:border-gray-700">
      <h3 class="font-bold text-lg flex items-center gap-2"><i class="fas fa-bolt text-amber-500"></i> Nâng cấp gói</h3>
      <button onclick="closeAllModals()" class="w-8 h-8 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-700 flex items-center justify-center"><i class="fas fa-xmark"></i></button>
    </div>

    <!-- Bước 1: chọn gói -->
    <div id="upgradePlansView" class="p-5">
      {% if not payment_methods_enabled %}
      <p class="text-xs text-center text-gray-400 mb-4">🚧 Chưa cấu hình phương thức thanh toán nào — xem README để bật.</p>
      {% endif %}
      <div class="grid sm:grid-cols-3 gap-4">
        {% for p in plan_order %}
        {% set meta = plan_meta[p] %}
        {% set limits = plan_limits[p] %}
        {% set is_current = (p == current_plan) %}
        <div class="rounded-2xl p-4 relative flex flex-col {{ 'border-2 border-blue-500' if is_current else 'border border-gray-200 dark:border-gray-700' }}">
          {% if is_current %}
          <span class="absolute -top-2.5 left-4 bg-blue-500 text-white text-[10px] font-bold px-2 py-0.5 rounded-full">GÓI HIỆN TẠI</span>
          {% endif %}
          <p class="font-bold flex items-center gap-1.5">{{ meta.icon }} {{ meta.label }}</p>
          {% if p in plan_pricing %}
            {% if is_discount_eligible %}
            <span class="inline-flex self-start items-center gap-1 text-[10px] font-bold px-2 py-0.5 rounded-full bg-rose-100 text-rose-600 dark:bg-rose-900/40 dark:text-rose-300 mt-1">
              <i class="fas fa-gift"></i> Giảm {{ discount_pct }}% — còn {{ discount_months_left }} tháng ưu đãi
            </span>
            <p class="mt-1 mb-2">
              <span class="text-xl font-extrabold">{{ discount_amounts[p]|vnd }}₫</span>
              <span class="text-sm font-medium text-gray-400 line-through ml-1">{{ plan_pricing[p]|vnd }}₫</span>
              <span class="text-xs font-normal text-gray-400">/ tháng</span>
            </p>
            {% else %}
            <p class="text-xl font-extrabold mt-1 mb-2">{{ plan_pricing[p]|vnd }}₫ <span class="text-xs font-normal text-gray-400">/ tháng</span></p>
            {% endif %}
          {% else %}
          <p class="text-xl font-extrabold mt-1 mb-2 text-gray-400">Miễn phí</p>
          {% endif %}
          <ul class="text-sm text-gray-500 dark:text-gray-400 space-y-1.5 my-1 flex-1">
            <li><i class="fas fa-check text-emerald-500 mr-1.5"></i>
              {% if limits.daily_uploads is none %}Đọc file &amp; ảnh không giới hạn{% else %}{{ limits.daily_uploads }} lượt đọc file/ảnh mỗi 24h{% endif %}
            </li>
            <li><i class="fas fa-check text-emerald-500 mr-1.5"></i> Mỗi file/ảnh tối đa {{ limits.max_file_mb }}MB</li>
            <li><i class="fas fa-check text-emerald-500 mr-1.5"></i> Chat &amp; lịch sử không giới hạn số đoạn</li>
            <li><i class="fas fa-check text-emerald-500 mr-1.5"></i> Dự án &amp; ghim đoạn chat</li>
            {% for key in unlocked_by_plan[p] %}
            <li><i class="fas fa-brain text-indigo-400 mr-1.5"></i> Chế độ suy nghĩ {{ thinking_modes[key].icon }} {{ thinking_modes[key].label }}</li>
            {% endfor %}
          </ul>
          {% if is_current %}
          <button disabled class="w-full mt-3 py-2 rounded-xl bg-gray-100 dark:bg-gray-700 text-gray-400 text-sm font-semibold">Gói hiện tại</button>
          {% elif p not in plan_pricing %}
          <button disabled class="w-full mt-3 py-2 rounded-xl bg-gray-100 dark:bg-gray-700 text-gray-400 text-sm font-semibold">—</button>
          {% elif not payment_methods_enabled %}
          <button disabled class="w-full mt-3 py-2 rounded-xl bg-blue-200 dark:bg-blue-900 text-blue-500 dark:text-blue-300 text-sm font-semibold cursor-not-allowed">Chưa khả dụng</button>
          {% else %}
          <button type="button" onclick="openCheckout('{{ p }}')" class="upgrade-buy-btn w-full mt-3 py-2 rounded-xl bg-blue-600 hover:bg-blue-700 text-white text-sm font-semibold">
            Nâng cấp — {{ (discount_amounts[p] if is_discount_eligible else plan_pricing[p])|vnd }}₫/tháng
          </button>
          {% endif %}
        </div>
        {% endfor %}
      </div>
      {% if is_plan_role_based %}
      <p class="text-xs text-center text-gray-400 mt-4"><i class="fas fa-circle-info mr-1"></i> Tài khoản {{ role_label }} được cấp gói Max vô điều kiện theo vai trò, không cần nâng cấp.</p>
      {% endif %}
    </div>

    <!-- Bước 2: chọn phương thức + thanh toán -->
    <div id="upgradeCheckoutView" class="hidden p-5">
      <button type="button" onclick="backToPlans()" class="text-sm text-gray-400 hover:text-gray-600 dark:hover:text-gray-200 mb-4 flex items-center gap-1.5">
        <i class="fas fa-arrow-left"></i> Chọn gói khác
      </button>

      <div id="checkoutMethodPicker" class="space-y-3">
        <p class="text-sm font-semibold">Chọn phương thức thanh toán cho gói <span id="checkoutPlanLabel" class="text-blue-600 dark:text-blue-400"></span> — <span id="checkoutPlanAmount" class="font-bold"></span></p>
        {% if bank_transfer_enabled %}
        <button type="button" onclick="startCheckout('bank_transfer')" class="w-full flex items-center gap-3 border border-gray-200 dark:border-gray-700 hover:border-blue-500 rounded-xl px-4 py-3 text-left transition-colors">
          <i class="fas fa-qrcode text-xl text-emerald-500 w-6"></i>
          <span class="flex-1">
            <span class="block font-medium text-sm">Chuyển khoản ngân hàng (quét mã QR)</span>
            <span class="block text-xs text-gray-400">Ngân hàng bất kỳ, hoặc quét từ app MoMo / ZaloPay</span>
          </span>
          <i class="fas fa-chevron-right text-gray-300"></i>
        </button>
        {% endif %}
        {% if vnpay_enabled %}
        <button type="button" onclick="startCheckout('vnpay')" class="w-full flex items-center gap-3 border border-gray-200 dark:border-gray-700 hover:border-blue-500 rounded-xl px-4 py-3 text-left transition-colors">
          <i class="fas fa-credit-card text-xl text-indigo-500 w-6"></i>
          <span class="flex-1">
            <span class="block font-medium text-sm">Thẻ ATM nội địa / Visa / Mastercard / JCB</span>
            <span class="block text-xs text-gray-400">Thanh toán qua cổng VNPAY, bảo mật chuẩn ngân hàng</span>
          </span>
          <i class="fas fa-chevron-right text-gray-300"></i>
        </button>
        {% endif %}
      </div>

      <!-- Kết quả: mã QR chuyển khoản -->
      <div id="checkoutBankView" class="hidden text-center">
        <p class="text-sm text-gray-500 dark:text-gray-400 mb-3">Quét mã bằng app ngân hàng bất kỳ, MoMo hoặc ZaloPay:</p>
        <img id="checkoutQrImage" src="" alt="Mã QR chuyển khoản" class="mx-auto w-56 h-56 rounded-xl border border-gray-200 dark:border-gray-700 object-contain bg-white">
        <div class="mt-4 text-sm text-left max-w-xs mx-auto space-y-1.5 bg-gray-50 dark:bg-gray-900 rounded-xl p-4">
          <p class="flex justify-between"><span class="text-gray-400">Ngân hàng thụ hưởng</span><span class="font-medium" id="checkoutBankName"></span></p>
          <p class="flex justify-between"><span class="text-gray-400">Số tài khoản</span><span class="font-mono font-medium" id="checkoutBankAccNo"></span></p>
          <p class="flex justify-between"><span class="text-gray-400">Chủ tài khoản</span><span class="font-medium" id="checkoutBankAccName"></span></p>
          <p class="flex justify-between"><span class="text-gray-400">Số tiền</span><span class="font-bold" id="checkoutBankAmount"></span></p>
          <p class="flex justify-between items-center"><span class="text-gray-400">Nội dung CK (bắt buộc)</span>
            <span class="flex items-center gap-1.5"><span class="font-mono font-bold" id="checkoutBankContent"></span>
            <button type="button" onclick="copyCheckoutContent()" class="text-gray-400 hover:text-blue-500"><i class="fas fa-copy"></i></button></span>
          </p>
        </div>
        <p class="text-xs text-amber-600 dark:text-amber-400 mt-3"><i class="fas fa-triangle-exclamation mr-1"></i>Ghi ĐÚNG nội dung chuyển khoản ở trên để hệ thống đối chiếu đúng đơn của em.</p>
        <div id="checkoutWaitingStatus" class="mt-4 text-sm text-gray-500 dark:text-gray-400 flex items-center justify-center gap-2">
          <i class="fas fa-spinner fa-spin"></i> Đang chờ Admin xác nhận đã nhận được tiền...
        </div>
      </div>

      <!-- Kết quả: VNPAY -->
      <div id="checkoutVnpayView" class="hidden text-center py-6">
        <i class="fas fa-circle-notch fa-spin text-3xl text-blue-500 mb-3"></i>
        <p class="text-sm text-gray-500 dark:text-gray-400">Đang chuyển sang cổng thanh toán VNPAY...</p>
      </div>
    </div>
  </div>

  <div id="reportIssueModal" class="hidden modal-panel bg-white dark:bg-gray-800 rounded-2xl shadow-2xl w-full max-w-md max-h-[85vh] overflow-y-auto">
    <div class="flex items-center justify-between px-5 py-4 border-b border-gray-100 dark:border-gray-700">
      <h3 class="font-bold text-lg flex items-center gap-2"><i class="fas fa-flag text-red-500"></i> Báo lỗi câu trả lời</h3>
      <button onclick="closeAllModals()" class="w-8 h-8 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-700 flex items-center justify-center"><i class="fas fa-xmark"></i></button>
    </div>
    <div class="p-5 space-y-3 text-sm">
      <p class="text-xs text-gray-400">Cho Thầy/Cô biết câu trả lời này có vấn đề gì (sai kiến thức, khó hiểu, lạc đề...) để đội ngũ StudyMate cải thiện AI nhé.</p>
      <textarea id="reportIssueText" rows="4" maxlength="1000" placeholder="Mô tả vấn đề em gặp phải..."
        class="w-full px-3 py-2.5 rounded-xl bg-gray-100 dark:bg-gray-800 border-0 focus:outline-none focus:ring-2 focus:ring-red-500 dark:text-white resize-none"></textarea>
      <button id="reportIssueSubmitBtn" onclick="submitReportIssue()" class="w-full px-4 py-2.5 rounded-xl bg-red-600 hover:bg-red-700 disabled:opacity-50 text-white text-sm font-semibold">Gửi báo cáo</button>
      <p id="reportIssueStatus" class="hidden text-xs text-emerald-600 dark:text-emerald-400 flex items-center gap-1"><i class="fas fa-check"></i> Đã gửi báo cáo, cảm ơn em! ✓</p>
    </div>
  </div>

  <div id="mistakeModal" class="hidden modal-panel bg-white dark:bg-gray-800 rounded-2xl shadow-2xl w-full max-w-md max-h-[85vh] overflow-y-auto">
    <div class="flex items-center justify-between px-5 py-4 border-b border-gray-100 dark:border-gray-700">
      <h3 class="font-bold text-lg flex items-center gap-2"><i class="fas fa-book text-amber-500"></i> Lưu vào Sổ lỗi sai</h3>
      <button onclick="closeAllModals()" class="w-8 h-8 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-700 flex items-center justify-center"><i class="fas fa-xmark"></i></button>
    </div>
    <div class="p-5 space-y-3 text-sm">
      <p class="text-xs text-gray-400">Em vừa mắc lỗi gì ở câu trả lời này? Ghi lại để StudyMate nhắc em ôn lại đúng chỗ còn yếu nhé.</p>
      <input id="mistakeSubject" type="text" maxlength="60" placeholder="Môn học (vd: Toán)"
        class="w-full px-3 py-2.5 rounded-xl bg-gray-100 dark:bg-gray-800 border-0 focus:outline-none focus:ring-2 focus:ring-amber-500 dark:text-white">
      <textarea id="mistakeDescription" rows="3" maxlength="300" placeholder="Mô tả ngắn gọn lỗi sai, vd: Chuyển vế quên đổi dấu"
        class="w-full px-3 py-2.5 rounded-xl bg-gray-100 dark:bg-gray-800 border-0 focus:outline-none focus:ring-2 focus:ring-amber-500 dark:text-white resize-none"></textarea>
      <button id="mistakeSubmitBtn" onclick="submitMistake()" class="w-full px-4 py-2.5 rounded-xl bg-amber-500 hover:bg-amber-600 disabled:opacity-50 text-white text-sm font-semibold">Lưu lại</button>
      <p id="mistakeStatus" class="hidden text-xs text-emerald-600 dark:text-emerald-400 flex items-center gap-1"><i class="fas fa-check"></i></p>
    </div>
  </div>

  <div id="recoveryCodeModal" class="hidden modal-panel bg-white dark:bg-gray-800 rounded-2xl shadow-2xl w-full max-w-sm text-center">
    <div class="p-6">
      <div class="w-14 h-14 rounded-full bg-amber-100 dark:bg-amber-900/30 text-amber-600 dark:text-amber-400 flex items-center justify-center mx-auto mb-3 text-xl">
        <i class="fas fa-key"></i>
      </div>
      <h3 class="font-bold mb-1">Mã khôi phục mới của em</h3>
      <p class="text-xs text-gray-400 mb-4">Chỉ hiện đúng 1 lần này — lưu lại ngay, mã cũ (nếu có) đã hết hiệu lực.</p>
      <div class="bg-gray-100 dark:bg-gray-900 rounded-xl py-3.5 px-3 mb-4">
        <p id="recoveryCodeText" class="font-mono text-xl font-bold tracking-wider text-indigo-600 dark:text-indigo-400 select-all"></p>
      </div>
      <button onclick="copyRecoveryCode()" id="recoveryCodeCopyBtn" class="w-full mb-2 px-4 py-2.5 rounded-xl bg-gray-100 dark:bg-gray-700 hover:bg-gray-200 dark:hover:bg-gray-600 text-sm font-semibold">
        <i class="fas fa-copy mr-1"></i> Chép mã
      </button>
      <button onclick="closeAllModals()" class="w-full px-4 py-2.5 rounded-xl bg-indigo-600 hover:bg-indigo-700 text-white text-sm font-semibold">Em đã lưu rồi</button>
    </div>
  </div>
</div>

<script>
const CURRENT_USERNAME = {{ username|tojson }};
const APP_NAME = {{ app_name|tojson }};
const PLAN_PRICING_JS = {{ plan_pricing|tojson }};
const PLAN_META_JS = {{ plan_meta|tojson }};
const DISCOUNT_AMOUNTS_JS = {{ discount_amounts|tojson }};
const IS_DISCOUNT_ELIGIBLE_JS = {{ is_discount_eligible|tojson }};
const ACHIEVEMENTS_META_JS = {{ achievements_meta|tojson }};
const AVATAR_EMOJI_JS = {{ avatar_emoji|tojson }};
const AVATAR_COLOR_JS = {{ avatar_color|tojson }};
const IS_GUEST_JS = {{ is_guest|tojson }};
const IS_DEVELOPER_JS = {{ is_developer|tojson }};
let uploadedFileContext = "";
let uploadedFileName = "";
let uploadedImageDataUrl = "";
let uploadedImageName = "";
let currentConversationId = null;
const html = document.documentElement;

marked.setOptions({ breaks: true });

// Dựng công thức toán ($$...$$, \(...\), \[...\]) thành hiển thị đẹp bằng KaTeX.
// throwOnError:false để không vỡ lỗi khi công thức đang gõ dở (lúc đang stream) —
// KaTeX sẽ tự render lại khi nội dung đầy đủ và hợp lệ.
function renderMathIn(el) {
  if (window.renderMathInElement) {
    try {
      renderMathInElement(el, {
        delimiters: [
          { left: '$$', right: '$$', display: true },
          { left: '\\[', right: '\\]', display: true },
          { left: '\\(', right: '\\)', display: false },
          { left: '$', right: '$', display: false }
        ],
        throwOnError: false
      });
    } catch (e) { /* bỏ qua, không làm hỏng luồng chat */ }
  }
  // Lưới an toàn: nếu vì lý do gì đó (công thức lệch dấu ngoặc, KaTeX chưa kịp tải...)
  // KaTeX KHÔNG render được, học sinh tuyệt đối không được thấy ký tự thô kiểu
  // "\( \sqrt{a} \)" — tự thay bằng ký hiệu Unicode dễ đọc thay thế (√, ×, ÷, ≤...).
  fallbackReadableMath(el);
}

const MATH_FALLBACK_PATTERN = /\\\(|\\\)|\\\[|\\\]|\$\$|\\sqrt|\\frac|\\times|\\div|\\le\b|\\ge\b|\\ne\b|\\pi\b|\\cdot|\\pm/;

function fallbackReadableMath(el) {
  let walker;
  try {
    walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT, {
      acceptNode: (n) => (n.parentElement && n.parentElement.closest('.katex')) ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT
    });
  } catch (e) { return; }

  const nodes = [];
  let n;
  while ((n = walker.nextNode())) {
    if (MATH_FALLBACK_PATTERN.test(n.nodeValue)) nodes.push(n);
  }
  nodes.forEach(node => {
    let t = node.nodeValue;
    // Xử lý dần từ cấu trúc có tham số (sqrt/frac) tới ký hiệu đơn — tránh sqrt{...} còn
    // sót dấu \ nếu thay ký hiệu đơn trước.
    t = t.replace(/\\sqrt\{([^{}]*)\}/g, '√($1)')
         .replace(/\\frac\{([^{}]*)\}\{([^{}]*)\}/g, '($1)/($2)')
         .replace(/\\times/g, '×').replace(/\\div/g, '÷')
         .replace(/\\le\b/g, '≤').replace(/\\ge\b/g, '≥').replace(/\\ne\b/g, '≠')
         .replace(/\\pi\b/g, 'π').replace(/\\cdot\b/g, '·').replace(/\\pm\b/g, '±')
         .replace(/\^\{([^{}]*)\}/g, '^($1)').replace(/_\{([^{}]*)\}/g, '_($1)')
         .replace(/\\\(|\\\)|\\\[|\\\]|\$\$/g, '')
         .replace(/[ \t]{2,}/g, ' ');
    if (t !== node.nodeValue) node.nodeValue = t;
  });
}

document.getElementById('userNameLabel').textContent = CURRENT_USERNAME;
applyAvatarToElement(document.getElementById('userAvatar'), AVATAR_EMOJI_JS, AVATAR_COLOR_JS, CURRENT_USERNAME);
if (IS_GUEST_JS) {
  const guestBadge = document.createElement('span');
  guestBadge.className = 'text-[9px] font-semibold px-1.5 py-0.5 rounded-full bg-amber-100 dark:bg-amber-900 text-amber-600 dark:text-amber-300 flex-shrink-0';
  guestBadge.textContent = 'KHÁCH';
  document.getElementById('userNameLabel').after(guestBadge);
}

// Đặt avatar cho 1 phần tử: dùng emoji đã chọn (nếu có) trên nền gradient tương ứng, hoặc
// mặc định về chữ cái đầu tên trên nền xanh indigo (giữ đúng hành vi cũ khi chưa chọn avatar).
// Theo dõi các class nền đã thêm qua dataset để gỡ sạch trước khi đổi, tránh chồng gradient
// cũ/mới nếu người dùng đổi avatar nhiều lần liên tiếp mà không tải lại trang.
function applyAvatarToElement(el, emoji, color, username) {
  if (!el) return;
  el.classList.remove('bg-indigo-600');
  if (el.dataset.avatarClasses) {
    el.classList.remove(...el.dataset.avatarClasses.split(' '));
  }
  let newClasses;
  if (emoji && color) {
    el.textContent = emoji;
    newClasses = ['bg-gradient-to-br', ...color.split(' ')];
  } else {
    el.textContent = (username || '?').trim().charAt(0).toUpperCase();
    newClasses = ['bg-indigo-600'];
  }
  el.classList.add(...newClasses);
  el.dataset.avatarClasses = newClasses.join(' ');
}

document.querySelectorAll('.avatar-opt-btn').forEach(btn => {
  btn.addEventListener('click', async () => {
    const emoji = btn.dataset.emoji, color = btn.dataset.color;
    document.querySelectorAll('.avatar-opt-btn').forEach(b => b.classList.remove('ring-2', 'ring-offset-2', 'ring-blue-500', 'dark:ring-offset-gray-800'));
    btn.classList.add('ring-2', 'ring-offset-2', 'ring-blue-500', 'dark:ring-offset-gray-800');
    applyAvatarToElement(document.getElementById('userAvatar'), emoji, color, CURRENT_USERNAME);
    try {
      await fetch('/api/account/avatar', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ emoji })
      });
    } catch (e) { /* im lặng bỏ qua lỗi mạng, avatar vẫn hiện đúng phía client */ }
  });
});

async function submitPasswordChange() {
  const currentPassword = document.getElementById('curPassword').value;
  const newPassword = document.getElementById('newPassword').value;
  const confirm = document.getElementById('newPasswordConfirm').value;
  const statusEl = document.getElementById('passwordChangeStatus');
  statusEl.classList.add('hidden');
  try {
    const res = await fetch('/api/account/password', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ currentPassword, newPassword, confirm })
    });
    const data = await res.json();
    statusEl.classList.remove('hidden');
    if (res.ok) {
      statusEl.className = 'text-xs mt-1.5 text-emerald-600 dark:text-emerald-400';
      statusEl.textContent = 'Đã đổi mật khẩu thành công! ✓';
      document.getElementById('curPassword').value = '';
      document.getElementById('newPassword').value = '';
      document.getElementById('newPasswordConfirm').value = '';
    } else {
      statusEl.className = 'text-xs mt-1.5 text-red-500';
      statusEl.textContent = data.error || 'Không đổi được mật khẩu.';
    }
  } catch (e) {
    statusEl.classList.remove('hidden');
    statusEl.className = 'text-xs mt-1.5 text-red-500';
    statusEl.textContent = 'Lỗi mạng, em thử lại nhé.';
  }
}

let _lastRecoveryCode = '';
async function regenerateRecoveryCode() {
  if (!confirm('Tạo mã khôi phục mới? Mã cũ (nếu có) sẽ không dùng được nữa.')) return;
  try {
    const res = await fetch('/api/account/recovery-code', { method: 'POST' });
    const data = await res.json();
    if (res.ok) {
      _lastRecoveryCode = data.code;
      document.getElementById('recoveryCodeText').textContent = data.code;
      closeAllModals();
      openModal('recoveryCodeModal');
    } else {
      alert(data.error || 'Không tạo được mã khôi phục, em thử lại nhé.');
    }
  } catch (e) {
    alert('Lỗi mạng, em thử lại nhé.');
  }
}

function copyRecoveryCode() {
  if (!_lastRecoveryCode) return;
  navigator.clipboard.writeText(_lastRecoveryCode).then(() => {
    const btn = document.getElementById('recoveryCodeCopyBtn');
    const original = btn.innerHTML;
    btn.innerHTML = '<i class="fas fa-check mr-1"></i> Đã chép!';
    setTimeout(() => { btn.innerHTML = original; }, 1500);
  });
}

async function submitGuestUpgrade() {
  const username = document.getElementById('guestUsername').value.trim();
  const password = document.getElementById('guestPassword').value;
  const confirm = document.getElementById('guestConfirm').value;
  const statusEl = document.getElementById('guestUpgradeStatus');
  const btn = document.getElementById('guestUpgradeBtn');
  statusEl.classList.add('hidden');
  btn.disabled = true;
  try {
    const res = await fetch('/guest/upgrade', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password, confirm })
    });
    const data = await res.json();
    statusEl.classList.remove('hidden');
    if (res.ok) {
      statusEl.className = 'text-xs mt-1.5 text-emerald-600 dark:text-emerald-400';
      statusEl.textContent = 'Đã tạo tài khoản chính thức! Đang tải lại trang...';
      setTimeout(() => window.location.reload(), 1200);
    } else {
      statusEl.className = 'text-xs mt-1.5 text-red-500';
      statusEl.textContent = data.error || 'Không tạo được tài khoản.';
    }
  } catch (e) {
    statusEl.classList.remove('hidden');
    statusEl.className = 'text-xs mt-1.5 text-red-500';
    statusEl.textContent = 'Lỗi mạng, em thử lại nhé.';
  } finally {
    btn.disabled = false;
  }
}

function toggleTheme() {
  // Nút bật/tắt nhanh trên thanh trên — chuyển thẳng sáng/tối và lưu lại vào Cài đặt
  // của tài khoản để lần đăng nhập sau vẫn giữ nguyên lựa chọn.
  const nowDark = !html.classList.contains('dark');
  applyTheme(nowDark ? 'dark' : 'light');
  fetch('/api/preferences', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ theme: nowDark ? 'dark' : 'light' })
  }).then(res => res.ok ? res.json() : null).then(prefs => { if (prefs) currentPreferences = prefs; }).catch(() => {});
}

// ---------- Sidebar (mobile) ----------
const sidebar = document.getElementById('sidebar');
const overlay = document.getElementById('sidebarOverlay');
function openSidebar() { sidebar.classList.add('open'); overlay.classList.remove('hidden'); }
function closeSidebar() { sidebar.classList.remove('open'); overlay.classList.add('hidden'); }
document.getElementById('openSidebarBtn').addEventListener('click', openSidebar);
document.getElementById('closeSidebarBtn').addEventListener('click', closeSidebar);
overlay.addEventListener('click', closeSidebar);

// ---------- User menu ----------
const userMenuBtn = document.getElementById('userMenuBtn');
const userMenu = document.getElementById('userMenu');
userMenuBtn.addEventListener('click', (e) => { e.stopPropagation(); userMenu.classList.toggle('hidden'); });
document.addEventListener('click', () => userMenu.classList.add('hidden'));

// ---------- Modals (Cài đặt / Trợ giúp / Nâng cấp) ----------
const modalBackdrop = document.getElementById('modalBackdrop');
function openModal(id) {
  userMenu.classList.add('hidden');
  document.querySelectorAll('.modal-panel').forEach(m => m.classList.add('hidden'));
  document.getElementById(id).classList.remove('hidden');
  modalBackdrop.classList.remove('hidden');
  modalBackdrop.classList.add('flex');
  if (id === 'upgradeModal') {
    stopCheckoutPolling();
    document.getElementById('upgradeCheckoutView').classList.add('hidden');
    document.getElementById('upgradePlansView').classList.remove('hidden');
  }
}
function closeAllModals() {
  modalBackdrop.classList.add('hidden');
  modalBackdrop.classList.remove('flex');
  document.querySelectorAll('.modal-panel').forEach(m => m.classList.add('hidden'));
  stopCheckoutPolling();
}

// ---------- Chế độ suy nghĩ (Trợ Lý / Học Giả / Giáo Sư / Thiên Tài) ----------
let currentThinkingMode = 'standard';
const thinkingModeBtn = document.getElementById('thinkingModeBtn');
const thinkingModeMenu = document.getElementById('thinkingModeMenu');
thinkingModeBtn.addEventListener('click', (e) => {
  e.stopPropagation();
  thinkingModeMenu.classList.toggle('hidden');
});
document.addEventListener('click', () => thinkingModeMenu.classList.add('hidden'));
thinkingModeMenu.addEventListener('click', (e) => e.stopPropagation());

document.querySelectorAll('.thinking-mode-item').forEach(item => {
  item.addEventListener('click', () => {
    const mode = item.dataset.mode;
    const unlocked = item.dataset.unlocked === '1';
    thinkingModeMenu.classList.add('hidden');
    if (!unlocked) {
      openModal('upgradeModal');
      return;
    }
    currentThinkingMode = mode;
    const icon = item.querySelector('.text-base').textContent;
    const label = item.querySelector('.font-medium').childNodes[0].textContent.trim();
    document.getElementById('thinkingModeIcon').textContent = icon;
    document.getElementById('thinkingModeLabel').textContent = label;
  });
});

// ---------- Gói sử dụng (Free/Premium/Max) — hiển thị ở Cài đặt ----------
async function loadPlanInfo() {
  try {
    const res = await fetch('/api/plan');
    if (!res.ok) return;
    const data = await res.json();
    const badge = document.getElementById('planBadge');
    const text = document.getElementById('planQuotaText');
    const bar = document.getElementById('planQuotaBar');
    if (badge) badge.textContent = `${data.icon} ${data.label}`;
    if (data.daily_upload_limit === null) {
      if (text) text.textContent = data.is_role_based
        ? 'Đọc file & ảnh không giới hạn (theo vai trò tài khoản).'
        : 'Đọc file & ảnh không giới hạn.';
      if (bar) bar.style.width = '100%';
    } else {
      const pct = Math.min(100, Math.round((data.daily_uploads_used / data.daily_upload_limit) * 100));
      if (text) text.textContent = `Đã dùng ${data.daily_uploads_used}/${data.daily_upload_limit} lượt đọc file/ảnh trong 24h qua`;
      if (bar) bar.style.width = pct + '%';
    }
  } catch (e) { /* im lặng bỏ qua lỗi mạng */ }
}

// ---------- Nâng cấp gói (thanh toán: VNPAY / Chuyển khoản VietQR) ----------
function formatVnd(n) { return Number(n).toLocaleString('vi-VN') + '₫'; }

let checkoutPlan = null;
let checkoutPollTimer = null;

function openCheckout(plan) {
  checkoutPlan = plan;
  document.getElementById('upgradePlansView').classList.add('hidden');
  document.getElementById('upgradeCheckoutView').classList.remove('hidden');
  document.getElementById('checkoutMethodPicker').classList.remove('hidden');
  document.getElementById('checkoutBankView').classList.add('hidden');
  document.getElementById('checkoutVnpayView').classList.add('hidden');
  const meta = PLAN_META_JS[plan];
  document.getElementById('checkoutPlanLabel').textContent = `${meta.icon} ${meta.label}`;
  const amount = IS_DISCOUNT_ELIGIBLE_JS ? DISCOUNT_AMOUNTS_JS[plan] : PLAN_PRICING_JS[plan];
  document.getElementById('checkoutPlanAmount').innerHTML = IS_DISCOUNT_ELIGIBLE_JS
    ? `${formatVnd(amount)}/tháng <span class="text-gray-400 font-normal line-through">${formatVnd(PLAN_PRICING_JS[plan])}</span>`
    : `${formatVnd(amount)}/tháng`;
}

function backToPlans() {
  stopCheckoutPolling();
  document.getElementById('upgradeCheckoutView').classList.add('hidden');
  document.getElementById('upgradePlansView').classList.remove('hidden');
}

function stopCheckoutPolling() {
  if (checkoutPollTimer) { clearInterval(checkoutPollTimer); checkoutPollTimer = null; }
}

async function startCheckout(method) {
  document.getElementById('checkoutMethodPicker').classList.add('hidden');
  try {
    const res = await fetch('/api/checkout', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ plan: checkoutPlan, method })
    });
    const data = await res.json();
    if (!res.ok) {
      alert(data.error || 'Không tạo được đơn hàng.');
      document.getElementById('checkoutMethodPicker').classList.remove('hidden');
      return;
    }

    if (method === 'vnpay') {
      document.getElementById('checkoutVnpayView').classList.remove('hidden');
      window.location.href = data.redirectUrl;  // chuyển hẳn trang sang cổng VNPAY
      return;
    }

    // bank_transfer
    document.getElementById('checkoutBankView').classList.remove('hidden');
    document.getElementById('checkoutQrImage').src = data.qrImageUrl;
    document.getElementById('checkoutBankName').textContent = data.bankId;
    document.getElementById('checkoutBankAccNo').textContent = data.bankAccountNo;
    document.getElementById('checkoutBankAccName').textContent = data.bankAccountName;
    document.getElementById('checkoutBankAmount').textContent = formatVnd(data.amount);
    document.getElementById('checkoutBankContent').textContent = data.transferContent;

    stopCheckoutPolling();
    checkoutPollTimer = setInterval(() => pollCheckoutStatus(data.orderCode), 4000);
  } catch (e) {
    alert('Lỗi mạng khi tạo đơn hàng.');
    document.getElementById('checkoutMethodPicker').classList.remove('hidden');
  }
}

async function pollCheckoutStatus(orderCode) {
  try {
    const res = await fetch(`/api/checkout/${orderCode}/status`);
    if (!res.ok) return;
    const data = await res.json();
    if (data.status === 'paid') {
      stopCheckoutPolling();
      document.getElementById('checkoutWaitingStatus').innerHTML =
        '<i class="fas fa-circle-check text-emerald-500"></i> Đã xác nhận! Gói của em đã được nâng cấp 🎉';
      loadPlanInfo();
      setTimeout(() => { closeAllModals(); window.location.reload(); }, 1800);
    } else if (data.status === 'cancelled' || data.status === 'failed') {
      stopCheckoutPolling();
      document.getElementById('checkoutWaitingStatus').innerHTML =
        '<i class="fas fa-circle-xmark text-red-500"></i> Đơn hàng đã bị huỷ/thất bại. Em thử tạo đơn mới nhé.';
    }
  } catch (e) { /* im lặng bỏ qua lỗi mạng, thử lại ở lượt poll kế tiếp */ }
}

function copyCheckoutContent() {
  const text = document.getElementById('checkoutBankContent').textContent;
  navigator.clipboard.writeText(text).catch(() => {});
}

// ---------- Ngôn ngữ (i18n nhẹ cho các nhãn chính trong giao diện) ----------
const I18N = {
  vi: {
    new_chat: 'Đoạn chat mới', search_placeholder: 'Tìm đoạn chat...', projects: 'Dự án',
    pinned: 'Đã ghim', recents: 'Gần đây', settings: 'Cài đặt', help: 'Trợ giúp & phím tắt',
    upgrade: 'Nâng cấp gói', logout: 'Đăng xuất',
    flashcards_games_btn: 'Thẻ ghi nhớ & Trò chơi',
    no_chats_yet: 'Chưa có đoạn chat nào',
    streak_days_suffix: 'ngày', level_prefix: 'Cấp',
    message_placeholder: 'Nhập câu hỏi... (Enter để gửi, Shift+Enter để xuống dòng)',
    footer_disclaimer: 'StudyMate AI có thể mắc lỗi — em nên kiểm tra lại các thông tin quan trọng nhé.',
    subj_toan: '📐 Toán Học', subj_van: '📖 Ngữ Văn', subj_anh: '🇬🇧 Tiếng Anh', subj_ly: '⚛️ Vật Lý',
    subj_hoa: '🧪 Hóa Học', subj_sinh: '🌱 Sinh Học', subj_sudia: '🌍 Lịch sử & Địa lý', subj_tin: '💻 Tin Học',
    subj_toan_plain: 'Toán Học', subj_van_plain: 'Ngữ Văn', subj_anh_plain: 'Tiếng Anh', subj_ly_plain: 'Vật Lý',
    subj_hoa_plain: 'Hóa Học', subj_sinh_plain: 'Sinh Học', subj_sudia_plain: 'Lịch sử & Địa lý', subj_tin_plain: 'Tin Học',
    mode_giaithich: '📘 Giải Thích Dễ Hiểu', mode_goiy: '💡 Gợi Ý Từng Bước', mode_kiemtra: '✅ Kiểm Tra Bài Làm',
    mode_luyentap: '📝 Ra Bài Luyện Tập', mode_ontap: '🔄 Tổng Hợp Ôn Tập',
    mode_giaithich_short: 'Giải Thích', mode_goiy_short: 'Gợi Ý', mode_kiemtra_short: 'Kiểm Tra',
    mode_luyentap_short: 'Luyện Tập', mode_ontap_short: 'Ôn Tập',
    think_tooltip: 'Chế độ suy nghĩ của AI',
    think_standard_label: 'Trợ Lý', think_standard_desc: 'Nhanh, cân bằng, phù hợp câu hỏi thường ngày.',
    think_scholar_label: 'Học Giả', think_scholar_desc: 'Suy luận từng bước kỹ hơn trước khi chốt đáp án.',
    think_professor_label: 'Giáo Sư', think_professor_desc: 'Giải thích mở rộng — nhiều ví dụ, liên hệ thực tế.',
    think_genius_label: 'Thiên Tài', think_genius_desc: 'Kết hợp suy luận sâu lẫn giải thích mở rộng — mạnh nhất.',
    voice_tooltip: 'Trợ lý giọng nói', theme_tooltip: 'Đổi giao diện',
    settings_appearance: 'Giao diện', theme_light: '☀️ Sáng', theme_dark: '🌙 Tối', theme_system: '💻 Hệ thống',
    settings_language: 'Ngôn ngữ / Language',
    settings_default_subject: 'Môn học mặc định', settings_default_mode: 'Chế độ mặc định',
    save_changes: 'Lưu thay đổi', saved: 'Đã lưu',
    danger_zone: 'Khu vực nguy hiểm', clear_all_history: 'Xoá toàn bộ lịch sử trò chuyện',
    clear_my_memory: 'Xoá bộ nhớ AI của tôi',
    help_title: 'Trợ giúp & phím tắt', shortcuts_title: 'Phím tắt',
    send_question: 'Gửi câu hỏi', new_line: 'Xuống dòng', command_palette: 'Bảng lệnh nhanh (đổi tên đăng nhập, tạo quiz...)', open_help: 'Mở trợ giúp',
  },
  en: {
    new_chat: 'New chat', search_placeholder: 'Search chats...', projects: 'Projects',
    pinned: 'Pinned', recents: 'Recents', settings: 'Settings', help: 'Help & shortcuts',
    upgrade: 'Upgrade plan', logout: 'Log out',
    flashcards_games_btn: 'Memory Cards & Games',
    no_chats_yet: 'No chat logs yet.',
    streak_days_suffix: 'day', level_prefix: 'Level',
    message_placeholder: 'Enter your question... (Press Enter to submit, Shift+Enter for a new line)',
    footer_disclaimer: 'StudyMate AI may make mistakes — you should double-check important information.',
    subj_toan: '📐 Mathematics', subj_van: '📖 Literature', subj_anh: '🇬🇧 English', subj_ly: '⚛️ Physics',
    subj_hoa: '🧪 Chemistry', subj_sinh: '🌱 Biology', subj_sudia: '🌍 History & Geography', subj_tin: '💻 Computer Science',
    subj_toan_plain: 'Mathematics', subj_van_plain: 'Literature', subj_anh_plain: 'English', subj_ly_plain: 'Physics',
    subj_hoa_plain: 'Chemistry', subj_sinh_plain: 'Biology', subj_sudia_plain: 'History & Geography', subj_tin_plain: 'Computer Science',
    mode_giaithich: '📘 Easy-to-Understand Explanation', mode_goiy: '💡 Step-by-Step Hints', mode_kiemtra: '✅ Check My Work',
    mode_luyentap: '📝 Practice Questions', mode_ontap: '🔄 Review Summary',
    mode_giaithich_short: 'Explain', mode_goiy_short: 'Hints', mode_kiemtra_short: 'Check',
    mode_luyentap_short: 'Practice', mode_ontap_short: 'Review',
    think_tooltip: "AI's thinking mode",
    think_standard_label: 'Assistant', think_standard_desc: 'Fast and balanced, good for everyday questions.',
    think_scholar_label: 'Scholar', think_scholar_desc: 'More careful step-by-step reasoning before answering.',
    think_professor_label: 'Professor', think_professor_desc: 'Extended explanations — more examples, real-world links.',
    think_genius_label: 'Genius', think_genius_desc: 'Combines deep reasoning and extended explanation — the strongest.',
    voice_tooltip: 'Voice assistant', theme_tooltip: 'Toggle theme',
    settings_appearance: 'Appearance', theme_light: '☀️ Light', theme_dark: '🌙 Dark', theme_system: '💻 System',
    settings_language: 'Ngôn ngữ / Language',
    settings_default_subject: 'Default subject', settings_default_mode: 'Default mode',
    save_changes: 'Save changes', saved: 'Saved',
    danger_zone: 'Danger zone', clear_all_history: 'Clear all chat history',
    clear_my_memory: 'Clear my AI memory',
    help_title: 'Help & shortcuts', shortcuts_title: 'Keyboard shortcuts',
    send_question: 'Send message', new_line: 'New line', command_palette: 'Command palette (rename, create quiz...)', open_help: 'Open help',
  }
};
function applyLanguage(lang) {
  const dict = I18N[lang] || I18N.vi;
  document.querySelectorAll('[data-i18n]').forEach(el => {
    const key = el.getAttribute('data-i18n');
    if (dict[key]) el.textContent = dict[key];
  });
  document.querySelectorAll('[data-i18n-placeholder]').forEach(el => {
    const key = el.getAttribute('data-i18n-placeholder');
    if (dict[key]) el.placeholder = dict[key];
  });
  document.querySelectorAll('[data-i18n-title]').forEach(el => {
    const key = el.getAttribute('data-i18n-title');
    if (dict[key]) el.title = dict[key];
  });
  // Nhãn "chế độ suy nghĩ" đang chọn (không có data-i18n cố định vì đổi theo lựa chọn của
  // học sinh) — dùng biến currentThinkingMode để đổi nhãn hiển thị theo ngôn ngữ mới.
  if (typeof currentThinkingMode !== 'undefined' && dict[`think_${currentThinkingMode}_label`]) {
    document.getElementById('thinkingModeLabel').textContent = dict[`think_${currentThinkingMode}_label`];
  }
}

// ---------- Giao diện (theme: sáng / tối / theo hệ thống) ----------
function applyTheme(theme) {
  const systemDark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
  const shouldDark = theme === 'dark' || (theme === 'system' && systemDark);
  html.classList.toggle('dark', shouldDark);
  const icon = document.getElementById('themeIcon');
  icon.classList.toggle('fa-moon', !shouldDark);
  icon.classList.toggle('fa-sun', shouldDark);
  document.querySelectorAll('.theme-opt').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.theme === theme);
  });
}
document.getElementById('themeOptions').addEventListener('click', (e) => {
  const btn = e.target.closest('.theme-opt');
  if (btn) applyTheme(btn.dataset.theme);
});

// ---------- Tuỳ chỉnh cá nhân (Cài đặt) — lưu theo tài khoản qua /api/preferences ----------
let currentPreferences = null;
async function loadPreferences() {
  try {
    const res = await fetch('/api/preferences');
    if (!res.ok) return;
    currentPreferences = await res.json();
    applyTheme(currentPreferences.theme || 'system');
    applyLanguage(currentPreferences.language || 'vi');
    document.getElementById('languageSelect').value = currentPreferences.language || 'vi';
    document.getElementById('defaultSubjectSelect').value = currentPreferences.default_subject || 'Toán';
    document.getElementById('defaultModeSelect').value = currentPreferences.default_mode || 'Giải thích';
    const subjectEl = document.getElementById('subject');
    const modeEl = document.getElementById('modeSelect');
    if (currentPreferences.default_subject) subjectEl.value = currentPreferences.default_subject;
    if (currentPreferences.default_mode) modeEl.value = currentPreferences.default_mode;
  } catch (e) { /* dùng mặc định nếu không tải được */ }
}

async function savePreferences() {
  const activeThemeBtn = document.querySelector('.theme-opt.active');
  const payload = {
    theme: activeThemeBtn ? activeThemeBtn.dataset.theme : 'system',
    language: document.getElementById('languageSelect').value,
    default_subject: document.getElementById('defaultSubjectSelect').value,
    default_mode: document.getElementById('defaultModeSelect').value,
  };
  try {
    const res = await fetch('/api/preferences', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload)
    });
    if (res.ok) {
      currentPreferences = await res.json();
      applyLanguage(currentPreferences.language);
      const msg = document.getElementById('settingsSavedMsg');
      msg.classList.remove('hidden');
      setTimeout(() => msg.classList.add('hidden'), 2000);
    }
  } catch (e) { alert('Không lưu được cài đặt, em thử lại nhé.'); }
}

async function clearAllHistory() {
  if (!confirm('Xoá TOÀN BỘ lịch sử trò chuyện? Hành động này không thể hoàn tác.')) return;
  try {
    await fetch('/api/conversations/all', { method: 'DELETE' });
    closeAllModals();
    newChat();
    loadConversations();
  } catch (e) { alert('Không xoá được lịch sử, em thử lại nhé.'); }
}

async function clearMyMemories() {
  if (!confirm('Xoá toàn bộ bộ nhớ AI về em? Hành động này không thể hoàn tác.')) return;
  try {
    await fetch('/api/memories', { method: 'DELETE' });
    alert('Đã xoá xong.');
  } catch (e) { alert('Không xoá được, em thử lại nhé.'); }
}

// ---------- Thông báo hệ thống (banner do developer đặt) ----------
let bannerDismissed = false;
async function loadBanner() {
  if (bannerDismissed) return;
  try {
    const res = await fetch('/api/banner');
    if (!res.ok) return;
    const data = await res.json();
    const bar = document.getElementById('bannerBar');
    if (data.message) {
      document.getElementById('bannerText').textContent = data.message;
      bar.classList.remove('hidden');
      bar.classList.add('flex');
    } else {
      bar.classList.add('hidden');
      bar.classList.remove('flex');
    }
  } catch (e) { /* bỏ qua */ }
}
function dismissBanner() {
  bannerDismissed = true;
  const bar = document.getElementById('bannerBar');
  bar.classList.add('hidden');
  bar.classList.remove('flex');
}

// ---------- Phím tắt bàn phím ----------
document.addEventListener('keydown', (e) => {
  const ctrlOrCmd = e.ctrlKey || e.metaKey;
  if (ctrlOrCmd && e.key.toLowerCase() === 'k') { e.preventDefault(); openPalette(); }
  else if (ctrlOrCmd && e.key === '/') { e.preventDefault(); openModal('helpModal'); }
  else if (e.key === 'Escape') { closeAllModals(); }
});

// ---------- Command Palette (Ctrl/⌘+K) ----------
// Bảng lệnh nhanh kiểu Raycast/Linear — gõ để lọc lệnh, hoặc gõ thẳng câu hỏi rồi Enter để
// hỏi AI ngay (mở đoạn chat mới). Đây là phần "Web Quick Launcher" — phiên bản trong trình
// duyệt, KHÔNG phải phím tắt toàn hệ điều hành (xem README mục 25 để biết vì sao và giới hạn
// thật sự của trình duyệt trong việc này).
function getPaletteCommands() {
  const cmds = [
    { icon: 'fa-plus', label: 'Đoạn chat mới', action: () => { closeAllModals(); newChat(); } },
    { icon: 'fa-wand-magic-sparkles', label: 'Tạo Quiz bằng AI', action: () => { closeAllModals(); openFlashcards(); switchFcTab('quiz'); setTimeout(openQuizForm, 150); } },
    { icon: 'fa-layer-group', label: 'Tạo bộ thẻ ghi nhớ bằng AI', action: () => { closeAllModals(); openFlashcards(); switchFcTab('decks'); setTimeout(openAiDeckForm, 150); } },
    { icon: 'fa-calendar-check', label: 'Tạo kế hoạch ôn tập', action: () => { closeAllModals(); openFlashcards(); switchFcTab('plans'); setTimeout(openPlanForm, 150); } },
    { icon: 'fa-book', label: 'Mở Sổ lỗi sai', action: () => { closeAllModals(); openFlashcards(); switchFcTab('mistakes'); } },
    { icon: 'fa-layer-group', label: 'Mở Thẻ ghi nhớ', action: () => { closeAllModals(); openFlashcards(); switchFcTab('decks'); } },
    { icon: 'fa-gear', label: 'Cài đặt', action: () => openModal('settingsModal') },
    { icon: 'fa-bolt', label: 'Nâng cấp gói', action: () => openModal('upgradeModal') },
    { icon: 'fa-circle-question', label: 'Trợ giúp & phím tắt', action: () => openModal('helpModal') },
  ];
  if (IS_DEVELOPER_JS) {
    cmds.push({ icon: 'fa-screwdriver-wrench', label: 'Trang Developer', action: () => { window.location.href = '/developer'; } });
  }
  cmds.push({ icon: 'fa-right-from-bracket', label: 'Đăng xuất', action: () => { window.location.href = '/logout'; } });
  return cmds;
}

let paletteSelectedIndex = 0;
let paletteFiltered = [];

// ==================================================================
// BẢNG TIẾN ĐỘ HỌC TẬP (Progress Dashboard) — tổng hợp XP/streak/Sổ lỗi
// sai/Quiz/Kế hoạch ôn tập/Game thành 1 bức tranh duy nhất.
// ==================================================================
async function openProgressDashboard() {
  document.getElementById('progressOverlay').classList.remove('hidden');
  try {
    const res = await fetch('/api/progress');
    if (!res.ok) return;
    renderProgressDashboard(await res.json());
  } catch (e) { /* im lặng bỏ qua lỗi mạng */ }
}

function renderProgressDashboard(data) {
  document.getElementById('progLevel').textContent = data.level;
  document.getElementById('progStreak').textContent = data.streak_days;
  document.getElementById('progLongestStreak').textContent = data.longest_streak;
  document.getElementById('progXp').textContent = data.xp;

  // ---- Gợi ý hôm nay ----
  document.getElementById('progressSuggestionText').textContent = data.suggestion.text;
  const actionBtn = document.getElementById('progressSuggestionAction');
  actionBtn.classList.add('hidden');
  actionBtn.onclick = null;
  if (data.suggestion.type === 'weak_topic') {
    actionBtn.textContent = 'Ôn lại ngay';
    actionBtn.classList.remove('hidden');
    actionBtn.onclick = () => { document.getElementById('progressOverlay').classList.add('hidden'); practiceMistake(data.suggestion.subject, data.suggestion.description); };
  } else if (data.suggestion.type === 'overdue_plan') {
    actionBtn.textContent = 'Xem kế hoạch';
    actionBtn.classList.remove('hidden');
    actionBtn.onclick = () => { document.getElementById('progressOverlay').classList.add('hidden'); openFlashcards(); switchFcTab('plans'); setTimeout(() => showPlanDetail(data.suggestion.plan_id), 150); };
  } else if (data.suggestion.type === 'restart_streak' || data.suggestion.type === 'generic') {
    actionBtn.textContent = 'Bắt đầu học ngay';
    actionBtn.classList.remove('hidden');
    actionBtn.onclick = () => { document.getElementById('progressOverlay').classList.add('hidden'); closeSidebar(); newChat(); };
  }

  // ---- Môn học hay hỏi nhất (thanh ngang bằng CSS, không cần thư viện biểu đồ) ----
  const subjSection = document.getElementById('progressSubjectsSection');
  const subjContainer = document.getElementById('progressSubjects');
  subjContainer.innerHTML = '';
  subjSection.classList.toggle('hidden', !data.subject_activity.length);
  const maxCount = data.subject_activity.length ? data.subject_activity[0].count : 1;
  data.subject_activity.forEach(s => {
    const pct = Math.round((s.count / maxCount) * 100);
    const row = document.createElement('div');
    row.innerHTML = `
      <div class="flex items-center justify-between text-xs mb-1">
        <span class="font-medium">${escapeHtml(s.subject)}</span>
        <span class="text-gray-400">${s.count} lượt hỏi</span>
      </div>
      <div class="gamify-xp-track"><div class="gamify-xp-fill" style="width:${pct}%;"></div></div>`;
    subjContainer.appendChild(row);
  });

  // ---- Điểm yếu cần ôn ----
  const weakSection = document.getElementById('progressWeakSection');
  const weakContainer = document.getElementById('progressWeakTopics');
  weakContainer.innerHTML = '';
  weakSection.classList.toggle('hidden', !data.weak_topics.length);
  data.weak_topics.forEach(w => {
    const row = document.createElement('div');
    row.className = 'flex items-center justify-between gap-2 rounded-xl border border-amber-100 dark:border-amber-900/40 bg-amber-50 dark:bg-amber-900/10 px-3.5 py-2.5';
    row.innerHTML = `
      <div class="min-w-0">
        <p class="text-sm truncate">${escapeHtml(w.description)} <span class="text-amber-500 font-semibold">×${w.count}</span></p>
        <p class="text-[11px] text-gray-400">${escapeHtml(w.subject)}</p>
      </div>
      <button class="prog-review-btn flex-shrink-0 text-xs font-semibold px-3 py-1.5 rounded-lg bg-amber-500 hover:bg-amber-600 text-white">Ôn lại</button>`;
    row.querySelector('.prog-review-btn').addEventListener('click', () => {
      document.getElementById('progressOverlay').classList.add('hidden');
      practiceMistake(w.subject, w.description);
    });
    weakContainer.appendChild(row);
  });

  // ---- Quiz ----
  const quizSection = document.getElementById('progressQuizSection');
  quizSection.classList.toggle('hidden', !data.quiz_stats.total_attempts);
  if (data.quiz_stats.total_attempts) {
    document.getElementById('progQuizAvg').textContent = data.quiz_stats.avg_score_pct;
    document.getElementById('progQuizCount').textContent = data.quiz_stats.total_attempts;
    const quizList = document.getElementById('progressQuizList');
    quizList.innerHTML = '';
    data.quiz_stats.recent.forEach(q => {
      const chip = document.createElement('div');
      const color = q.pct >= 80 ? 'bg-emerald-100 dark:bg-emerald-900/30 text-emerald-700 dark:text-emerald-400' :
                    q.pct >= 50 ? 'bg-amber-100 dark:bg-amber-900/30 text-amber-700 dark:text-amber-400' :
                    'bg-red-100 dark:bg-red-900/30 text-red-700 dark:text-red-400';
      chip.className = `text-xs font-semibold px-2.5 py-1.5 rounded-lg ${color}`;
      chip.textContent = `${q.score}/${q.total} (${q.pct}%) · ${q.date.slice(5)}`;
      quizList.appendChild(chip);
    });
  }

  // ---- Kế hoạch ôn tập ----
  const plansSection = document.getElementById('progressPlansSection');
  const plansContainer = document.getElementById('progressPlans');
  plansContainer.innerHTML = '';
  plansSection.classList.toggle('hidden', !data.study_plans.length);
  data.study_plans.forEach(p => {
    const row = document.createElement('div');
    row.className = 'rounded-xl border border-gray-200 dark:border-gray-800 px-3.5 py-2.5 cursor-pointer hover:border-emerald-400 dark:hover:border-emerald-600';
    row.innerHTML = `
      <div class="flex items-center justify-between text-xs mb-1">
        <span class="font-medium truncate">${escapeHtml(p.title)}</span>
        <span class="text-gray-400">${p.done}/${p.total} việc</span>
      </div>
      <div class="gamify-xp-track"><div class="gamify-xp-fill" style="width:${p.pct}%; background: linear-gradient(90deg,#10b981,#14b8a6);"></div></div>`;
    row.addEventListener('click', () => {
      document.getElementById('progressOverlay').classList.add('hidden');
      openFlashcards(); switchFcTab('plans'); setTimeout(() => showPlanDetail(p.id), 150);
    });
    plansContainer.appendChild(row);
  });

  // ---- Điểm cao trò chơi ----
  const gamesSection = document.getElementById('progressGamesSection');
  const gamesContainer = document.getElementById('progressGames');
  gamesContainer.innerHTML = '';
  const gameLabels = { quick_math: ['⚡', 'Tính Nhanh'], snake: ['🐍', 'Rắn Săn Chữ'], memory_match: ['🧠', 'Lật Thẻ'] };
  const gameKeys = Object.keys(data.game_stats);
  gamesSection.classList.toggle('hidden', !gameKeys.length);
  gameKeys.forEach(k => {
    const [emoji, label] = gameLabels[k] || ['🎮', k];
    const g = data.game_stats[k];
    const cell = document.createElement('div');
    cell.className = 'rounded-xl border border-gray-200 dark:border-gray-800 p-3 text-center';
    cell.innerHTML = `<p class="text-xl">${emoji}</p><p class="text-sm font-bold mt-1">${g.bestScore}</p><p class="text-[10px] text-gray-400">${label}</p>`;
    gamesContainer.appendChild(cell);
  });

  // ---- Thành tựu (đủ bộ, khoá/mở khoá) ----
  const achContainer = document.getElementById('progressAchievements');
  achContainer.innerHTML = '';
  const earnedCodes = new Set(data.achievements.map(a => a.code));
  Object.keys(ACHIEVEMENTS_META_JS).forEach(code => {
    const meta = ACHIEVEMENTS_META_JS[code];
    const earned = earnedCodes.has(code);
    const cell = document.createElement('div');
    cell.className = `rounded-xl border p-2.5 text-center ${earned ? 'border-indigo-200 dark:border-indigo-800 bg-indigo-50 dark:bg-indigo-900/20' : 'border-gray-100 dark:border-gray-800 opacity-40'}`;
    cell.title = meta.desc;
    cell.innerHTML = `<p class="text-lg">${meta.icon}</p><p class="text-[9px] mt-0.5 leading-tight">${escapeHtml(meta.label)}</p>`;
    achContainer.appendChild(cell);
  });
}


function openPalette() {
  closeAllModals();
  const backdrop = document.getElementById('modalBackdrop');
  document.querySelectorAll('.modal-panel').forEach(m => m.classList.add('hidden'));
  document.getElementById('paletteModal').classList.remove('hidden');
  backdrop.classList.remove('hidden');
  backdrop.classList.add('flex');
  const input = document.getElementById('paletteInput');
  input.value = '';
  renderPaletteList('');
  setTimeout(() => input.focus(), 30);
}

function renderPaletteList(query) {
  const q = query.trim().toLowerCase();
  const all = getPaletteCommands();
  paletteFiltered = q ? all.filter(c => c.label.toLowerCase().includes(q)) : all;
  paletteSelectedIndex = 0;

  const list = document.getElementById('paletteList');
  list.innerHTML = '';

  if (q) {
    const askItem = document.createElement('button');
    askItem.type = 'button';
    askItem.className = 'palette-item w-full flex items-center gap-3 px-3 py-2.5 rounded-xl text-left text-sm hover:bg-gray-100 dark:hover:bg-gray-700';
    askItem.innerHTML = `<i class="fas fa-sparkles text-indigo-500 w-4"></i> <span>Hỏi AI: <strong>"${escapeHtml(query)}"</strong></span>`;
    askItem.addEventListener('click', () => askPaletteQuery(query));
    list.appendChild(askItem);
  }

  paletteFiltered.forEach((cmd, i) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'palette-item w-full flex items-center gap-3 px-3 py-2.5 rounded-xl text-left text-sm hover:bg-gray-100 dark:hover:bg-gray-700';
    btn.dataset.idx = i;
    btn.innerHTML = `<i class="fas ${cmd.icon} text-gray-400 w-4"></i> <span>${escapeHtml(cmd.label)}</span>`;
    btn.addEventListener('click', () => { closeAllModals(); cmd.action(); });
    list.appendChild(btn);
  });

  if (!q && !paletteFiltered.length) {
    list.innerHTML = '<p class="text-sm text-gray-400 text-center py-6">Không có lệnh nào.</p>';
  } else if (q && !paletteFiltered.length) {
    // chỉ còn mục "Hỏi AI" ở trên — không cần thêm thông báo trống
  }
  updatePaletteHighlight();
}

function updatePaletteHighlight() {
  const items = document.querySelectorAll('#paletteList .palette-item');
  items.forEach((el, i) => el.classList.toggle('bg-gray-100', i === paletteSelectedIndex));
  items.forEach((el, i) => el.classList.toggle('dark:bg-gray-700', i === paletteSelectedIndex));
}

function askPaletteQuery(query) {
  closeAllModals();
  newChat();
  document.getElementById('messageInput').value = query;
  sendMessage();
}

document.getElementById('paletteInput').addEventListener('input', (e) => renderPaletteList(e.target.value));
document.getElementById('paletteInput').addEventListener('keydown', (e) => {
  const items = document.querySelectorAll('#paletteList .palette-item');
  if (e.key === 'ArrowDown') {
    e.preventDefault();
    paletteSelectedIndex = Math.min(items.length - 1, paletteSelectedIndex + 1);
    updatePaletteHighlight();
  } else if (e.key === 'ArrowUp') {
    e.preventDefault();
    paletteSelectedIndex = Math.max(0, paletteSelectedIndex - 1);
    updatePaletteHighlight();
  } else if (e.key === 'Enter') {
    e.preventDefault();
    const query = e.target.value.trim();
    const items2 = document.querySelectorAll('#paletteList .palette-item');
    if (items2.length) { items2[paletteSelectedIndex].click(); }
    else if (query) { askPaletteQuery(query); }
  }
});

function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str == null ? '' : str;
  return div.innerHTML;
}

function scrollChatToBottom() {
  const panel = document.getElementById('chatPanel');
  panel.scrollTop = panel.scrollHeight;
}

// ---------- Render tin nhắn ----------
function showTypingIndicator() {
  const chat = document.getElementById('chat');
  const wrapper = document.createElement('div');
  wrapper.id = 'typingIndicator';
  wrapper.className = 'flex gap-3 items-start';
  wrapper.innerHTML = `<div class="ai-avatar thinking w-8 h-8 rounded-full bg-gradient-to-br from-blue-600 to-indigo-600 flex items-center justify-center text-white text-sm flex-shrink-0 mt-0.5"><i class="fas fa-robot"></i></div>
    <div class="ai-content flex-1 min-w-0 leading-relaxed pt-1.5"><span class="typing-indicator inline-flex items-center gap-1 text-gray-400"><span></span><span></span><span></span></span></div>`;
  chat.appendChild(wrapper);
  scrollChatToBottom();
}
function removeTypingIndicator() {
  const el = document.getElementById('typingIndicator');
  if (el) el.remove();
}

function addMessage(sender, content, isMarkdown = false, actionsCtx = null) {
  const chat = document.getElementById('chat');
  if (sender === 'user') {
    const div = document.createElement('div');
    div.className = 'ml-auto max-w-[80%] bg-gray-100 dark:bg-gray-700 rounded-2xl px-4 py-2.5 whitespace-pre-wrap break-words';
    div.textContent = content;
    chat.appendChild(div);
    scrollChatToBottom();
    return div;
  }
  const wrapper = document.createElement('div');
  wrapper.className = 'ai-msg-group flex gap-3 items-start';
  const avatar = document.createElement('div');
  avatar.className = 'ai-avatar w-8 h-8 rounded-full bg-gradient-to-br from-blue-600 to-indigo-600 flex items-center justify-center text-white text-sm flex-shrink-0 mt-0.5';
  avatar.innerHTML = '<i class="fas fa-robot"></i>';
  const bubble = document.createElement('div');
  bubble.className = 'ai-content flex-1 min-w-0 leading-relaxed pt-1.5';
  bubble.innerHTML = isMarkdown ? renderMarkdownSafe(content) : escapeHtml(content).replace(/\n/g, '<br>');
  if (isMarkdown) renderMathIn(bubble);
  wrapper.appendChild(avatar);
  wrapper.appendChild(bubble);
  chat.appendChild(wrapper);
  if (actionsCtx) addMessageActions(wrapper, actionsCtx.conversationId, () => content);
  scrollChatToBottom();
  return bubble;
}

function createAiStreamBubble() {
  const chat = document.getElementById('chat');
  const wrapper = document.createElement('div');
  wrapper.className = 'ai-msg-group flex gap-3 items-start';
  wrapper.innerHTML = `<div class="ai-avatar thinking w-8 h-8 rounded-full bg-gradient-to-br from-blue-600 to-indigo-600 flex items-center justify-center text-white text-sm flex-shrink-0 mt-0.5"><i class="fas fa-robot"></i></div>
    <div class="ai-content flex-1 min-w-0 leading-relaxed pt-1.5"><span class="typing-indicator inline-flex items-center gap-1 text-gray-400"><span></span><span></span><span></span></span></div>`;
  chat.appendChild(wrapper);
  scrollChatToBottom();
  return wrapper.querySelector('.ai-content');
}
// Dựng lại bong bóng tin nhắn khi đang nhận từng phần (streaming).
// HIỆU NĂNG (đã sửa): trước đây MỖI token nhận về đều dựng lại TOÀN BỘ tin nhắn — chạy lại
// Markdown + KaTeX trên cả bài từ đầu tới cuối. Câu trả lời 1500 token = 1500 lần dựng lại
// một chuỗi ngày càng dài (độ phức tạp bình phương), nên càng về cuối câu trả lời càng giật,
// đúng lúc học sinh đang chờ đọc — và trên điện thoại thì tệ hơn nhiều.
// CÁCH SỬA: gom các token đến liên tiếp lại, chỉ vẽ TỐI ĐA 1 lần mỗi khung hình
// (requestAnimationFrame). Chữ vẫn hiện mượt như cũ với mắt người, nhưng khối lượng tính
// toán giảm rất nhiều. Lúc kết thúc (showCursor=false) thì vẽ NGAY, không hoãn, để đảm bảo
// nội dung cuối cùng luôn đầy đủ và chính xác.
let _streamRafId = null;
let _streamPending = null;

function _flushStreamBubble() {
  _streamRafId = null;
  if (!_streamPending) return;
  const { bubble, text, showCursor } = _streamPending;
  _streamPending = null;
  bubble.innerHTML = renderMarkdownSafe(text) + (showCursor ? '<span class="stream-cursor"></span>' : '');
  renderMathIn(bubble);
  scrollChatToBottom();
}

function updateAiStreamBubble(bubble, text, showCursor) {
  _streamPending = { bubble, text, showCursor };
  if (!showCursor) {
    // Lượt vẽ cuối cùng (hoặc báo lỗi): vẽ ngay lập tức, huỷ lượt hoãn đang chờ.
    if (_streamRafId !== null) { cancelAnimationFrame(_streamRafId); _streamRafId = null; }
    _flushStreamBubble();
    return;
  }
  if (_streamRafId === null) {
    _streamRafId = requestAnimationFrame(_flushStreamBubble);
  }
}

// ---------- Dựng Markdown mà KHÔNG làm hỏng công thức toán ----------
// LỖI GỐC (đã sửa): trước đây gọi thẳng marked.parse() rồi mới tới KaTeX. Nhưng theo chuẩn
// Markdown, dấu "\" đứng trước dấu câu là KÝ TỰ THOÁT -> marked NUỐT MẤT dấu "\" trong
// "\(", "\)", "\[", "\]". Tới lượt KaTeX thì dấu hiệu nhận biết công thức đã biến mất, nên
// nó không dựng được gì cả, và học sinh thấy nguyên "( a = 0 )", "\dfrac" mất dấu... Ngoài
// ra dấu "_" trong công thức (vd y_1) còn bị Markdown hiểu là IN NGHIÊNG, làm hỏng chỉ số dưới.
// CÁCH SỬA: tách các đoạn công thức ra, thay bằng mã giữ chỗ (@@KTX0@@) TRƯỚC khi chạy
// Markdown, rồi trả lại nguyên văn SAU khi Markdown xong -> KaTeX nhận được công thức y hệt
// những gì AI viết ra.
function protectMath(text) {
  const store = [];
  const patterns = [
    /\$\$[\s\S]+?\$\$/g,                  // $$...$$
    /\\\[[\s\S]+?\\\]/g,                   // \[...\]
    /\\\([\s\S]+?\\\)/g,                   // \(...\)
    /\$([^\s$][^$\n]*?[^\s$]|[^\s$])\$/g,  // $...$ (bỏ qua kiểu tiền tệ "50$ và 100$")
    // Lưu ý: CỐ TÌNH không dùng regex lookbehind (?<!...) ở đây — Safari trên iPhone cũ hơn
    // iOS 16.4 không hỗ trợ, và nó gây LỖI CÚ PHÁP làm chết toàn bộ JavaScript của trang.
  ];
  let out = text;
  patterns.forEach(re => {
    out = out.replace(re, (m) => {
      store.push(m);
      return `@@KTX${store.length - 1}@@`;
    });
  });
  return { text: out, store };
}

function restoreMath(html, store) {
  return html.replace(/@@KTX(\d+)@@/g, (m, i) => (store[+i] !== undefined ? store[+i] : m));
}

function renderMarkdownSafe(text) {
  const p = protectMath(text);
  return restoreMath(marked.parse(p.text), p.store);
}

// ---------- Báo lỗi câu trả lời ----------
function addMessageActions(wrapper, conversationId, getText) {
  const bar = document.createElement('div');
  bar.className = 'msg-actions flex items-center gap-1 mt-1 ml-11';

  const reportBtn = document.createElement('button');
  reportBtn.type = 'button';
  reportBtn.className = 'text-xs text-gray-400 hover:text-red-500 px-2 py-1 -ml-2 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800 flex items-center gap-1.5';
  reportBtn.title = 'Báo lỗi câu trả lời này';
  reportBtn.innerHTML = '<i class="fas fa-flag"></i> <span>Báo lỗi</span>';
  reportBtn.addEventListener('click', () => openReportModal(conversationId, getText()));
  bar.appendChild(reportBtn);

  const mistakeBtn = document.createElement('button');
  mistakeBtn.type = 'button';
  mistakeBtn.className = 'text-xs text-gray-400 hover:text-amber-600 dark:hover:text-amber-400 px-2 py-1 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800 flex items-center gap-1.5';
  mistakeBtn.title = 'Lưu lỗi sai này vào Sổ lỗi sai để ôn lại sau';
  mistakeBtn.innerHTML = '<i class="fas fa-book"></i> <span>Lưu vào Sổ lỗi sai</span>';
  mistakeBtn.addEventListener('click', () => openMistakeModal(conversationId));
  bar.appendChild(mistakeBtn);

  wrapper.after(bar);
  return bar;
}

let mistakeContext = { conversationId: null };
function openMistakeModal(conversationId) {
  mistakeContext = { conversationId: conversationId || null };
  const subjSel = document.getElementById('subject');
  document.getElementById('mistakeSubject').value = subjSel ? subjSel.value : '';
  document.getElementById('mistakeDescription').value = '';
  document.getElementById('mistakeStatus').classList.add('hidden');
  openModal('mistakeModal');
  setTimeout(() => document.getElementById('mistakeDescription').focus(), 50);
}

async function submitMistake() {
  const subject = document.getElementById('mistakeSubject').value.trim();
  const description = document.getElementById('mistakeDescription').value.trim();
  const statusEl = document.getElementById('mistakeStatus');
  const btn = document.getElementById('mistakeSubmitBtn');
  if (!description) { document.getElementById('mistakeDescription').focus(); return; }
  btn.disabled = true;
  try {
    const res = await fetch('/api/mistakes', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ subject, description, conversationId: mistakeContext.conversationId })
    });
    const data = await res.json();
    if (res.ok) {
      statusEl.textContent = data.isNew ? 'Đã lưu vào Sổ lỗi sai! 📕' : 'Đã ghi nhận — em lặp lại lỗi này rồi đó, cố lên nhé!';
      statusEl.classList.remove('hidden');
      if (data.gamify) handleGamifyEvent(data.gamify);
      setTimeout(closeAllModals, 1400);
    } else {
      alert(data.error || 'Không lưu được, em thử lại nhé.');
    }
  } catch (e) {
    alert('Lỗi mạng, em thử lại nhé.');
  } finally {
    btn.disabled = false;
  }
}

let reportContext = { conversationId: null, messageExcerpt: '' };
function openReportModal(conversationId, messageExcerpt) {
  reportContext = { conversationId: conversationId || null, messageExcerpt: (messageExcerpt || '').slice(0, 2000) };
  const textEl = document.getElementById('reportIssueText');
  const statusEl = document.getElementById('reportIssueStatus');
  if (textEl) textEl.value = '';
  if (statusEl) statusEl.classList.add('hidden');
  openModal('reportIssueModal');
  setTimeout(() => textEl && textEl.focus(), 50);
}

async function submitReportIssue() {
  const textEl = document.getElementById('reportIssueText');
  const statusEl = document.getElementById('reportIssueStatus');
  const btn = document.getElementById('reportIssueSubmitBtn');
  const description = textEl.value.trim();
  if (!description) { textEl.focus(); return; }
  btn.disabled = true;
  try {
    const res = await fetch('/api/report-issue', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        conversationId: reportContext.conversationId,
        messageExcerpt: reportContext.messageExcerpt,
        description
      })
    });
    if (res.ok) {
      statusEl.classList.remove('hidden');
      setTimeout(closeAllModals, 1200);
    } else {
      const data = await res.json().catch(() => ({}));
      alert(data.error || 'Không gửi được báo cáo.');
    }
  } catch (err) {
    alert('Lỗi mạng khi gửi báo cáo.');
  } finally {
    btn.disabled = false;
  }
}

// ---------- "Bộ nhớ" AI: toast khi ghi nhớ điều gì mới ----------
function showMemoryToast(text) {
  const toast = document.createElement('div');
  toast.className = 'memory-toast fixed bottom-24 left-1/2 z-50 bg-gray-900 dark:bg-gray-100 text-white dark:text-gray-900 text-xs px-4 py-2 rounded-full shadow-lg flex items-center gap-2 max-w-[90vw]';
  toast.innerHTML = `<i class="fas fa-brain text-purple-400"></i> <span class="truncate">Đã ghi nhớ: ${escapeHtml(text.slice(0, 80))}</span>`;
  document.body.appendChild(toast);
  setTimeout(() => toast.remove(), 3600);
}

// ---------- Gamification: XP / streak / thành tựu ----------
async function loadGamification() {
  try {
    const res = await fetch('/api/gamification');
    if (!res.ok) return;
    const data = await res.json();
    renderGamification(data);
    lastKnownStreak = data.streak_days;  // chỉ ghi nhận mốc ban đầu, KHÔNG bắn hiệu ứng lửa lúc tải trang
  } catch (e) { /* im lặng bỏ qua lỗi mạng */ }
}

function renderGamification(data) {
  const widget = document.getElementById('gamifyWidget');
  if (!widget) return;
  widget.classList.remove('hidden');
  document.getElementById('gamifyStreak').textContent = data.streak_days;
  document.getElementById('gamifyLevel').textContent = data.level;
  const pct = Math.round((data.xp_into_level / data.xp_per_level) * 100);
  document.getElementById('gamifyXpBar').style.width = pct + '%';
  document.getElementById('gamifyXpText').textContent = `${data.xp_into_level}/${data.xp_per_level} XP`;
}

function showGamifyToast(html) {
  const toast = document.createElement('div');
  toast.className = 'memory-toast fixed bottom-24 left-1/2 z-50 bg-amber-500 text-white text-xs px-4 py-2.5 rounded-full shadow-lg flex items-center gap-2 max-w-[90vw] font-semibold';
  toast.innerHTML = html;
  document.body.appendChild(toast);
  setTimeout(() => toast.remove(), 4200);
}

// Mốc streak bắn hiệu ứng ngọn lửa giữa màn hình — ngọn lửa càng ĐẬM (nhiều lớp, glow mạnh
// hơn, ngả dần sang đỏ/tím) khi mốc càng cao, đúng như yêu cầu "ngọn lửa ngày càng đậm".
const STREAK_TIER_STYLE = {
  3:    { emoji: '🔥',       size: '5rem',   color: '#fbbf24', glow: 'rgba(251,191,36,0.35)', label: 'Chuỗi 3 ngày! 🔥' },
  10:   { emoji: '🔥',       size: '6rem',   color: '#f97316', glow: 'rgba(249,115,22,0.40)', label: 'Chuỗi 10 ngày!' },
  30:   { emoji: '🔥🔥',     size: '6.5rem', color: '#f97316', glow: 'rgba(249,115,22,0.45)', label: 'Chuỗi 30 ngày!' },
  100:  { emoji: '🔥🔥',     size: '7rem',   color: '#ef4444', glow: 'rgba(239,68,68,0.50)',  label: 'Chuỗi 100 ngày! Đỉnh!' },
  200:  { emoji: '🔥🔥🔥',   size: '7.5rem', color: '#ef4444', glow: 'rgba(239,68,68,0.55)',  label: 'Chuỗi 200 ngày!' },
  300:  { emoji: '🔥🔥🔥',   size: '8rem',   color: '#dc2626', glow: 'rgba(220,38,38,0.60)',  label: 'Chuỗi 300 ngày!' },
  500:  { emoji: '🔥🔥🔥🔥', size: '8.5rem', color: '#a21caf', glow: 'rgba(162,28,175,0.60)', label: 'Chuỗi 500 ngày! Huyền thoại! 👑' },
  1000: { emoji: '🔥🔥🔥🔥🔥', size: '9rem', color: '#7c3aed', glow: 'rgba(124,58,237,0.65)', label: 'Chuỗi 1000 ngày! Không tưởng! 👑' },
};
const STREAK_MILESTONES = Object.keys(STREAK_TIER_STYLE).map(Number).sort((a, b) => a - b);

let lastKnownStreak = 0;

function maybeShowStreakFire(newStreak) {
  if (newStreak > lastKnownStreak && STREAK_MILESTONES.includes(newStreak)) {
    showStreakFireEffect(newStreak);
  }
  lastKnownStreak = newStreak;
}

function showStreakFireEffect(days) {
  const style = STREAK_TIER_STYLE[days];
  if (!style) return;
  const overlay = document.getElementById('streakFireOverlay');
  const glow = document.getElementById('streakFireGlow');
  const emojiEl = document.getElementById('streakFireEmoji');
  const textEl = document.getElementById('streakFireText');

  glow.style.background = `radial-gradient(circle, ${style.glow} 0%, transparent 70%)`;
  emojiEl.style.fontSize = style.size;
  emojiEl.style.color = style.color;
  emojiEl.textContent = style.emoji;
  textEl.textContent = style.label;

  overlay.classList.remove('hidden');
  overlay.classList.remove('showing');
  void overlay.offsetWidth;  // ép reflow để animation chạy lại được nếu bắn liên tiếp 2 mốc gần nhau
  overlay.classList.add('showing');

  clearTimeout(overlay._hideTimer);
  overlay._hideTimer = setTimeout(() => {
    overlay.classList.add('hidden');
    overlay.classList.remove('showing');
  }, 2700);
}

function handleGamifyEvent(g) {
  renderGamification({
    streak_days: g.streak_days, level: g.level,
    xp_into_level: g.xp % 100, xp_per_level: 100,
  });
  maybeShowStreakFire(g.streak_days);
  if (g.leveled_up) {
    showGamifyToast(`<i class="fas fa-arrow-up"></i> Lên cấp ${g.level}! 🎉`);
  }
  (g.new_achievements || []).forEach((code, i) => {
    const meta = ACHIEVEMENTS_META_JS[code];
    if (meta) setTimeout(() => showGamifyToast(`${meta.icon} Mở khoá thành tựu: ${meta.label}!`), 600 + i * 1500);
  });
}

function showWelcome() {
  const lang = (currentPreferences && currentPreferences.language) || 'vi';
  const msg = lang === 'en'
    ? `👋 Hi! I'm **${APP_NAME}**.\n\nPick your **Subject** and **Mode** at the top, type your question, then press Enter (or the send button)! You can also attach a file (PDF/Word/txt/csv) or image with the 📎 button, or drag and drop it straight into the chat. 🚀`
    : `👋 Chào em! Thầy/Cô là **${APP_NAME}**.\n\nEm chọn **Môn học** và **Chế độ** ở phía trên, gõ câu hỏi rồi bấm Enter (hoặc nút gửi) nhé! Em cũng có thể đính kèm file (PDF/Word/txt/csv) hoặc ảnh bằng nút 📎, hay kéo-thả trực tiếp vào khung chat. 🚀`;
  addMessage('ai', msg, true);
}

// ---------- Lịch sử hội thoại (theo tài khoản) + Dự án + Ghim + Tìm kiếm ----------
let allConversations = [];
let allProjects = [];
let activeProjectFilter = null; // null = tất cả, số = lọc theo dự án, 'none' = chưa gắn dự án nào
let openConvMenuId = null;

async function loadConversations() {
  try {
    const res = await fetch('/api/conversations');
    if (res.status === 401) { window.location.href = '/login'; return; }
    if (!res.ok) return;
    allConversations = await res.json();
    renderSidebarLists();
  } catch (e) { /* im lặng bỏ qua lỗi mạng khi tải danh sách */ }
}

async function loadProjects() {
  try {
    const res = await fetch('/api/projects');
    if (!res.ok) return;
    allProjects = await res.json();
    renderProjectList();
  } catch (e) { /* im lặng bỏ qua lỗi mạng */ }
}

function renderProjectList() {
  const container = document.getElementById('projectList');
  container.innerHTML = '';
  const allBtn = document.createElement('div');
  allBtn.className = 'flex items-center gap-2 rounded-xl px-3 py-2 cursor-pointer text-sm ' +
    (activeProjectFilter === null ? 'bg-gray-200 dark:bg-gray-800 font-medium' : 'hover:bg-gray-200 dark:hover:bg-gray-800 text-gray-500');
  allBtn.innerHTML = '<i class="fas fa-inbox w-4 text-center"></i><span>Tất cả đoạn chat</span>';
  allBtn.addEventListener('click', () => { activeProjectFilter = null; renderSidebarLists(); renderProjectList(); });
  container.appendChild(allBtn);

  allProjects.forEach(proj => {
    const row = document.createElement('div');
    row.className = 'conv-item group flex items-center gap-2 rounded-xl px-3 py-2 cursor-pointer text-sm ' +
      (activeProjectFilter === proj.id ? 'bg-gray-200 dark:bg-gray-800 font-medium' : 'hover:bg-gray-200 dark:hover:bg-gray-800');
    row.innerHTML = `<i class="fas fa-folder w-4 text-center text-amber-500"></i>
      <span class="flex-1 truncate">${escapeHtml(proj.name)}</span>
      <button class="conv-actions text-gray-400 hover:text-red-500 w-6 h-6 flex items-center justify-center flex-shrink-0" title="Xoá dự án">
        <i class="fas fa-trash-can text-xs"></i>
      </button>`;
    row.addEventListener('click', () => { activeProjectFilter = proj.id; renderSidebarLists(); renderProjectList(); });
    row.querySelector('button').addEventListener('click', async (e) => {
      e.stopPropagation();
      if (!confirm(`Xoá dự án "${proj.name}"? Các đoạn chat bên trong sẽ không bị xoá.`)) return;
      await fetch(`/api/projects/${proj.id}`, { method: 'DELETE' });
      if (activeProjectFilter === proj.id) activeProjectFilter = null;
      loadProjects();
      loadConversations();
    });
    container.appendChild(row);
  });
}

document.getElementById('newProjectBtn').addEventListener('click', async () => {
  const name = prompt('Tên dự án mới (vd: Ôn thi Học kỳ 2):');
  if (!name || !name.trim()) return;
  try {
    await fetch('/api/projects', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: name.trim() })
    });
    loadProjects();
  } catch (e) { alert('Không tạo được dự án, em thử lại nhé.'); }
});

document.getElementById('searchInput').addEventListener('input', () => renderSidebarLists());

function renderSidebarLists() {
  const search = document.getElementById('searchInput').value.trim().toLowerCase();
  let filtered = allConversations;
  if (activeProjectFilter !== null) filtered = filtered.filter(c => c.project_id === activeProjectFilter);
  if (search) filtered = filtered.filter(c => (c.title || '').toLowerCase().includes(search));

  const pinned = filtered.filter(c => c.pinned);
  const recent = filtered.filter(c => !c.pinned);

  const pinnedSection = document.getElementById('pinnedSection');
  if (pinned.length) {
    pinnedSection.classList.remove('hidden');
    renderConvGroup('pinnedList', pinned);
  } else {
    pinnedSection.classList.add('hidden');
  }
  const lang = (currentPreferences && currentPreferences.language) || 'vi';
  renderConvGroup('convList', recent, recent.length ? null : I18N[lang].no_chats_yet);
}

function renderConvGroup(containerId, list, emptyText) {
  const container = document.getElementById(containerId);
  container.innerHTML = '';
  if (!list.length) {
    if (emptyText) container.innerHTML = `<div class="text-gray-400 text-xs px-2 py-4 text-center">${emptyText}</div>`;
    return;
  }
  list.forEach(conv => {
    const item = document.createElement('div');
    const active = conv.id === currentConversationId;
    item.className = 'conv-item group relative flex items-center gap-1 rounded-xl px-3 py-2 cursor-pointer transition-colors ' +
      (active ? 'bg-gray-200 dark:bg-gray-800' : 'hover:bg-gray-200 dark:hover:bg-gray-800');
    item.innerHTML = `
      ${conv.pinned ? '<i class="fas fa-thumbtack text-[10px] text-blue-500 flex-shrink-0"></i>' : ''}
      <span class="flex-1 truncate">${escapeHtml(conv.title || 'Đoạn chat mới')}</span>
      <button class="conv-actions text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 w-6 h-6 flex items-center justify-center flex-shrink-0 transition-opacity" title="Tuỳ chọn">
        <i class="fas fa-ellipsis text-xs"></i>
      </button>`;
    item.querySelector('span').addEventListener('click', () => openConversation(conv.id));
    item.querySelector('button').addEventListener('click', (e) => { e.stopPropagation(); toggleConvMenu(conv, item); });
    container.appendChild(item);
  });
}

function toggleConvMenu(conv, anchorEl) {
  document.querySelectorAll('.conv-menu').forEach(m => m.remove());
  if (openConvMenuId === conv.id) { openConvMenuId = null; return; }
  openConvMenuId = conv.id;

  const menu = document.createElement('div');
  menu.className = 'conv-menu bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-xl shadow-lg overflow-hidden text-sm';
  const projectOptions = allProjects.map(p =>
    `<button data-project-id="${p.id}" class="move-opt w-full text-left px-4 py-2 hover:bg-gray-100 dark:hover:bg-gray-700 flex items-center gap-2"><i class="fas fa-folder text-amber-500 w-4"></i>${escapeHtml(p.name)}</button>`
  ).join('');

  menu.innerHTML = `
    <button class="pin-opt w-full text-left px-4 py-2.5 hover:bg-gray-100 dark:hover:bg-gray-700 flex items-center gap-2">
      <i class="fas fa-thumbtack w-4 text-gray-400"></i> ${conv.pinned ? 'Bỏ ghim' : 'Ghim đoạn chat'}
    </button>
    <button class="rename-opt w-full text-left px-4 py-2.5 hover:bg-gray-100 dark:hover:bg-gray-700 flex items-center gap-2 border-t border-gray-100 dark:border-gray-700">
      <i class="fas fa-pen w-4 text-gray-400"></i> Đổi tên
    </button>
    <div class="border-t border-gray-100 dark:border-gray-700">
      <div class="px-4 pt-2 pb-1 text-[11px] font-semibold text-gray-400 uppercase">Chuyển vào dự án</div>
      ${projectOptions || '<div class="px-4 py-2 text-gray-400 text-xs">Chưa có dự án nào</div>'}
      ${conv.project_id ? '<button data-project-id="" class="move-opt w-full text-left px-4 py-2 hover:bg-gray-100 dark:hover:bg-gray-700 flex items-center gap-2 text-gray-500"><i class="fas fa-inbox w-4"></i>Bỏ khỏi dự án</button>' : ''}
    </div>
    <button class="delete-opt w-full text-left px-4 py-2.5 hover:bg-gray-100 dark:hover:bg-gray-700 flex items-center gap-2 text-red-600 dark:text-red-400 border-t border-gray-100 dark:border-gray-700">
      <i class="fas fa-trash-can w-4"></i> Xoá đoạn chat
    </button>`;

  document.body.appendChild(menu);
  // Đặt vị trí SAU khi đã gắn vào trang để đo được kích thước THẬT của menu.
  // Trước đây code giả định menu luôn cao 260px — nhưng menu có nhiều dự án thì cao hơn,
  // nên phần cuối (đúng chỗ nút "Xoá đoạn chat") bị tràn ra ngoài màn hình điện thoại.
  const rect = anchorEl.getBoundingClientRect();
  const mw = menu.offsetWidth || 200;
  const mh = menu.offsetHeight || 260;
  const pad = 8;
  let left = rect.right - mw;
  left = Math.max(pad, Math.min(left, window.innerWidth - mw - pad));
  let top = rect.bottom + 4;
  if (top + mh > window.innerHeight - pad) {
    // Không đủ chỗ bên dưới -> mở NGƯỢC LÊN trên nút 3 chấm.
    top = Math.max(pad, rect.top - mh - 4);
  }
  menu.style.left = left + 'px';
  menu.style.top = top + 'px';

  menu.querySelector('.pin-opt').addEventListener('click', async (e) => {
    e.stopPropagation();
    await patchConversation(conv.id, { pinned: !conv.pinned });
    menu.remove(); openConvMenuId = null;
    loadConversations();
  });
  menu.querySelector('.rename-opt').addEventListener('click', async (e) => {
    e.stopPropagation();
    const title = prompt('Đổi tên đoạn chat:', conv.title || '');
    if (title && title.trim()) await patchConversation(conv.id, { title: title.trim() });
    menu.remove(); openConvMenuId = null;
    loadConversations();
  });
  menu.querySelectorAll('.move-opt').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      const pid = btn.dataset.projectId ? parseInt(btn.dataset.projectId, 10) : null;
      await patchConversation(conv.id, { project_id: pid });
      menu.remove(); openConvMenuId = null;
      loadConversations();
    });
  });
  menu.querySelector('.delete-opt').addEventListener('click', (e) => {
    e.stopPropagation();
    menu.remove(); openConvMenuId = null;
    deleteConversation(conv.id);
  });
}
document.addEventListener('click', () => {
  document.querySelectorAll('.conv-menu').forEach(m => m.remove());
  openConvMenuId = null;
});

async function patchConversation(id, updates) {
  try {
    await fetch(`/api/conversations/${id}`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(updates)
    });
  } catch (e) { alert('Không cập nhật được đoạn chat, em thử lại nhé.'); }
}

async function openConversation(id) {
  try {
    const res = await fetch(`/api/conversations/${id}/messages`);
    if (res.status === 401) { window.location.href = '/login'; return; }
    if (!res.ok) return;
    const messages = await res.json();
    currentConversationId = id;
    document.getElementById('chat').innerHTML = '';
    messages.forEach(m => addMessage(
      m.role === 'user' ? 'user' : 'ai',
      m.content,
      m.role !== 'user',
      m.role !== 'user' ? { conversationId: id } : null
    ));
    clearAttachments();
    closeSidebar();
    loadConversations();
  } catch (e) {
    addMessage('ai', '🔌 Không tải được đoạn chat này. Em thử lại nhé!', false);
  }
}

async function deleteConversation(id) {
  if (!confirm('Xóa đoạn chat này? Hành động này không thể hoàn tác.')) return;
  try {
    await fetch(`/api/conversations/${id}`, { method: 'DELETE' });
    if (id === currentConversationId) newChat();
    loadConversations();
  } catch (e) { /* im lặng bỏ qua lỗi mạng */ }
}

function newChat() {
  currentConversationId = null;
  document.getElementById('chat').innerHTML = '';
  clearAttachments();
  showWelcome();
  closeSidebar();
  loadConversations();
}
document.getElementById('newChatBtn').addEventListener('click', newChat);

// ==================================================================
// THẺ GHI NHỚ (FLASHCARDS) + TRÒ CHƠI LUYỆN TẬP
// ==================================================================
let currentDeckId = null;
let currentDeckCards = [];
let studyQueue = [];
let studyIndex = 0;
let studyCorrectCount = 0;
let studyFlipped = false;
let gameTimerHandle = null;
let gameState = null;

function openFlashcards() {
  document.getElementById('flashcardsOverlay').classList.remove('hidden');
  switchFcTab('decks');
}
function closeFlashcards() {
  document.getElementById('flashcardsOverlay').classList.add('hidden');
  stopGameTimer();
}
document.getElementById('openFlashcardsBtn').addEventListener('click', openFlashcards);

const FC_TABS = ['decks', 'mistakes', 'quiz', 'plans', 'games', 'classes'];
function switchFcTab(tab) {
  FC_TABS.forEach(t => {
    const btn = document.getElementById('fcTab' + t.charAt(0).toUpperCase() + t.slice(1));
    const active = t === tab;
    btn.classList.toggle('border-blue-600', active);
    btn.classList.toggle('text-blue-600', active);
    btn.classList.toggle('dark:text-blue-400', active);
    btn.classList.toggle('border-transparent', !active);
    btn.classList.toggle('text-gray-400', !active);
  });

  if (tab === 'decks') { showFcView('list'); loadDecks(); }
  else if (tab === 'mistakes') { showFcView('mistakes'); loadMistakes(); }
  else if (tab === 'quiz') { showFcView('quizList'); loadQuizzes(); }
  else if (tab === 'plans') { showFcView('plansList'); loadStudyPlans(); }
  else if (tab === 'games') { showFcView('gamesList'); loadGameStats(); }
  else if (tab === 'classes') { showFcView('classesList'); loadClasses(); }
}

const FC_VIEWS = {
  list: 'fcDeckListView', mistakes: 'fcMistakesView', detail: 'fcDeckDetailView',
  study: 'fcStudyView', game: 'fcGameView',
  quizList: 'fcQuizListView', quizTake: 'fcQuizTakeView', quizResult: 'fcQuizResultView',
  plansList: 'fcPlansListView', planDetail: 'fcPlanDetailView',
  gamesList: 'fcGamesListView', qmSetup: 'fcQuickMathSetupView', qmPlay: 'fcQuickMathPlayView', qmResult: 'fcQuickMathResultView',
  snakeSetup: 'fcSnakeSetupView', snakePlay: 'fcSnakePlayView', snakeResult: 'fcSnakeResultView',
  classesList: 'fcClassesListView', classDetail: 'fcClassDetailView',
};
const FC_TOP_LEVEL_VIEWS = ['list', 'mistakes', 'quizList', 'plansList', 'gamesList', 'classesList'];

function showFcView(view) {
  Object.values(FC_VIEWS).forEach(id => document.getElementById(id).classList.add('hidden'));
  document.getElementById(FC_VIEWS[view]).classList.remove('hidden');
  const backBtn = document.getElementById('fcBackBtn');
  const title = document.getElementById('fcHeaderTitle');
  const tabBar = document.getElementById('fcTabBar');

  if (FC_TOP_LEVEL_VIEWS.includes(view)) {
    backBtn.classList.add('hidden');
    tabBar.classList.remove('hidden');
    title.textContent = 'Thẻ ghi nhớ & Trò chơi';
    backBtn.onclick = null;
    return;
  }

  tabBar.classList.add('hidden');
  backBtn.classList.remove('hidden');
  if (view === 'detail') {
    title.textContent = 'Chi tiết bộ thẻ';
    backBtn.onclick = () => switchFcTab('decks');
  } else if (view === 'study' || view === 'game') {
    title.textContent = view === 'study' ? 'Chế độ Học' : 'Lật thẻ ghi nhớ';
    backBtn.onclick = () => { stopGameTimer(); showDeckDetail(currentDeckId); };
  } else if (view === 'quizTake' || view === 'quizResult') {
    title.textContent = view === 'quizTake' ? 'Đang làm quiz' : 'Kết quả quiz';
    backBtn.onclick = () => switchFcTab('quiz');
  } else if (view === 'planDetail') {
    title.textContent = 'Chi tiết kế hoạch';
    backBtn.onclick = () => switchFcTab('plans');
  } else if (view === 'qmSetup' || view === 'qmPlay' || view === 'qmResult') {
    title.textContent = 'Đố Vui Tính Nhanh';
    backBtn.onclick = () => { stopQuickMathTimer(); switchFcTab('games'); };
  } else if (view === 'snakeSetup' || view === 'snakePlay' || view === 'snakeResult') {
    title.textContent = 'Rắn Săn Chữ';
    backBtn.onclick = () => { stopSnakeGame(); switchFcTab('games'); };
  } else if (view === 'classDetail') {
    title.textContent = 'Lớp học';
    backBtn.onclick = () => switchFcTab('classes');
  }
}

async function loadMistakes() {
  try {
    const res = await fetch('/api/mistakes');
    if (!res.ok) return;
    renderMistakeGroups(await res.json());
  } catch (e) { /* im lặng bỏ qua lỗi mạng */ }
}

function renderMistakeGroups(mistakes) {
  const container = document.getElementById('mistakeGroups');
  const empty = document.getElementById('mistakeEmptyState');
  container.innerHTML = '';
  empty.classList.toggle('hidden', mistakes.length > 0);

  const bySubject = {};
  mistakes.forEach(m => {
    const key = m.subject || 'Khác';
    (bySubject[key] = bySubject[key] || []).push(m);
  });

  Object.keys(bySubject).forEach(subject => {
    const group = document.createElement('div');
    const items = bySubject[subject].map(m => `
      <div class="flex items-center justify-between gap-2 py-2 border-b border-gray-50 dark:border-gray-900 last:border-0 ${m.resolved ? 'opacity-40' : ''}">
        <div class="min-w-0 flex-1">
          <p class="text-sm truncate">${escapeHtml(m.description)} ${m.occurrence_count > 1 ? `<span class="text-amber-500 font-semibold">×${m.occurrence_count}</span>` : ''}</p>
        </div>
        <div class="flex items-center gap-1 flex-shrink-0">
          ${!m.resolved ? `<button class="mistake-practice-btn text-xs px-2.5 py-1.5 rounded-lg bg-blue-50 dark:bg-blue-900/30 text-blue-600 dark:text-blue-400 hover:bg-blue-100 dark:hover:bg-blue-900/50 font-medium" data-id="${m.id}">Ôn lại ngay</button>` : ''}
          <button class="mistake-resolve-btn text-xs px-2 py-1.5 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800 text-gray-400" data-id="${m.id}" data-resolved="${m.resolved}" title="${m.resolved ? 'Mở lại' : 'Đánh dấu đã khắc phục'}"><i class="fas ${m.resolved ? 'fa-rotate-left' : 'fa-check'}"></i></button>
          <button class="mistake-delete-btn text-xs px-2 py-1.5 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800 text-gray-400 hover:text-red-500" data-id="${m.id}"><i class="fas fa-trash-can"></i></button>
        </div>
      </div>`).join('');
    group.innerHTML = `<p class="text-xs font-semibold text-gray-400 uppercase mb-1.5">${escapeHtml(subject)}</p>${items}`;
    container.appendChild(group);
  });

  // Tra cứu lại subject/description gốc (KHÔNG chưa qua) từ id thay vì nhúng thẳng chuỗi
  // vào thuộc tính HTML — escapeHtml() không escape dấu " nên nhúng trực tiếp vào
  // data-attribute có thể làm vỡ HTML nếu mô tả lỗi chứa dấu ngoặc kép.
  const byId = {};
  mistakes.forEach(m => { byId[m.id] = m; });

  container.querySelectorAll('.mistake-practice-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const m = byId[btn.dataset.id];
      if (m) practiceMistake(m.subject, m.description);
    });
  });
  container.querySelectorAll('.mistake-resolve-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      const resolved = btn.dataset.resolved !== 'true';
      await fetch(`/api/mistakes/${btn.dataset.id}`, {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ resolved })
      });
      loadMistakes();
    });
  });
  container.querySelectorAll('.mistake-delete-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      if (!confirm('Xoá lỗi này khỏi Sổ lỗi sai?')) return;
      await fetch(`/api/mistakes/${btn.dataset.id}`, { method: 'DELETE' });
      loadMistakes();
    });
  });
}

function practiceMistake(subject, description) {
  closeFlashcards();
  newChat();
  const subjSel = document.getElementById('subject');
  if (subjSel) {
    for (const opt of subjSel.options) {
      if (opt.value === subject || opt.text.includes(subject)) { subjSel.value = opt.value; break; }
    }
  }
  const modeSel = document.getElementById('modeSelect');
  if (modeSel) modeSel.value = 'Luyện tập';
  document.getElementById('messageInput').value =
    `Em hay bị lỗi: "${description}". Thầy/cô cho em 3 bài tập để luyện lại đúng chỗ này với ạ.`;
  sendMessage();
}

async function loadDecks() {
  try {
    const res = await fetch('/api/decks');
    if (!res.ok) return;
    renderDeckGrid(await res.json());
  } catch (e) { /* im lặng bỏ qua lỗi mạng */ }
}

function renderDeckGrid(decks) {
  const grid = document.getElementById('deckGrid');
  const empty = document.getElementById('deckEmptyState');
  grid.innerHTML = '';
  empty.classList.toggle('hidden', decks.length > 0);
  decks.forEach(d => {
    const card = document.createElement('div');
    card.className = 'rounded-2xl border border-gray-200 dark:border-gray-800 p-4 hover:border-blue-400 dark:hover:border-blue-600 cursor-pointer transition-colors';
    card.innerHTML = `
      <div class="flex items-start justify-between gap-2">
        <p class="font-semibold truncate flex-1">${escapeHtml(d.title)}</p>
        ${d.source === 'ai' ? '<i class="fas fa-wand-magic-sparkles text-purple-400 text-xs" title="Tạo bằng AI"></i>' : ''}
      </div>
      ${d.subject ? `<p class="text-xs text-gray-400 mt-0.5">${escapeHtml(d.subject)}</p>` : ''}
      <div class="flex items-center gap-3 mt-3 text-xs text-gray-400">
        <span><i class="fas fa-layer-group mr-1"></i>${d.card_count} thẻ</span>
        <span><i class="fas fa-star mr-1 text-amber-400"></i>${d.mastered_count} thuộc</span>
      </div>`;
    card.addEventListener('click', () => showDeckDetail(d.id));
    grid.appendChild(card);
  });
}

function openCreateDeckPrompt() {
  const title = prompt('Tên bộ thẻ mới:');
  if (!title || !title.trim()) return;
  fetch('/api/decks', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title: title.trim() })
  }).then(res => res.json()).then(data => {
    if (data.id) showDeckDetail(data.id); else alert(data.error || 'Không tạo được bộ thẻ.');
  }).catch(() => alert('Lỗi mạng.'));
}

function openAiDeckForm() {
  const form = document.getElementById('aiDeckForm');
  form.classList.toggle('hidden');
  if (!form.classList.contains('hidden')) document.getElementById('aiDeckTopic').focus();
}

async function submitAiDeck() {
  const topic = document.getElementById('aiDeckTopic').value.trim();
  const subject = document.getElementById('aiDeckSubject').value.trim();
  const count = document.getElementById('aiDeckCount').value;
  const errEl = document.getElementById('aiDeckError');
  const btn = document.getElementById('aiDeckSubmitBtn');
  const label = document.getElementById('aiDeckSubmitLabel');
  errEl.classList.add('hidden');
  if (!topic) { document.getElementById('aiDeckTopic').focus(); return; }

  btn.disabled = true;
  label.innerHTML = '<i class="fas fa-spinner fa-spin"></i> AI đang tạo thẻ...';
  try {
    const res = await fetch('/api/decks/generate', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ topic, subject, count: parseInt(count, 10) })
    });
    const data = await res.json();
    if (!res.ok) {
      errEl.textContent = data.error || 'AI tạo thẻ chưa được, em thử lại nhé.';
      errEl.classList.remove('hidden');
    } else {
      document.getElementById('aiDeckForm').classList.add('hidden');
      document.getElementById('aiDeckTopic').value = '';
      if (data.gamify) handleGamifyEvent(data.gamify);
      showDeckDetail(data.deckId);
    }
  } catch (e) {
    errEl.textContent = 'Lỗi mạng, em thử lại nhé.';
    errEl.classList.remove('hidden');
  } finally {
    btn.disabled = false;
    label.textContent = 'Tạo bộ thẻ';
  }
}

async function showDeckDetail(deckId) {
  try {
    const res = await fetch(`/api/decks/${deckId}`);
    if (!res.ok) { showFcView('list'); loadDecks(); return; }
    const data = await res.json();
    currentDeckId = deckId;
    currentDeckCards = data.cards;
    document.getElementById('fcDeckTitle').textContent = data.deck.title;
    document.getElementById('fcDeckMeta').textContent =
      (data.deck.subject ? data.deck.subject + ' · ' : '') + `${data.cards.length} thẻ`;
    renderCardList(data.cards);
    showFcView('detail');
  } catch (e) {
    alert('Không tải được bộ thẻ này.');
  }
}

function renderCardList(cards) {
  const list = document.getElementById('cardList');
  list.innerHTML = '';
  if (!cards.length) {
    list.innerHTML = '<p class="text-sm text-gray-400 text-center py-8">Chưa có thẻ nào — thêm thẻ ở trên nhé!</p>';
    return;
  }
  cards.forEach(c => {
    const row = document.createElement('div');
    row.className = 'flex items-center gap-3 border border-gray-100 dark:border-gray-800 rounded-xl px-3.5 py-2.5 text-sm';
    row.innerHTML = `
      <div class="flex-1 min-w-0 grid grid-cols-2 gap-3">
        <p class="truncate"><span class="text-gray-400 mr-1">Trước:</span>${escapeHtml(c.front)}</p>
        <p class="truncate"><span class="text-gray-400 mr-1">Sau:</span>${escapeHtml(c.back)}</p>
      </div>
      <span class="text-[10px] text-gray-400 flex-shrink-0" title="Mức độ nhớ">Lv${c.box_level}</span>
      <button class="edit-card-btn text-gray-400 hover:text-blue-500 w-7 h-7 flex items-center justify-center flex-shrink-0"><i class="fas fa-pen text-xs"></i></button>
      <button class="del-card-btn text-gray-400 hover:text-red-500 w-7 h-7 flex items-center justify-center flex-shrink-0"><i class="fas fa-trash-can text-xs"></i></button>`;
    row.querySelector('.edit-card-btn').addEventListener('click', () => editCard(c));
    row.querySelector('.del-card-btn').addEventListener('click', () => deleteCard(c.id));
    list.appendChild(row);
  });
}

async function addCardToCurrentDeck() {
  const frontEl = document.getElementById('newCardFront');
  const backEl = document.getElementById('newCardBack');
  const front = frontEl.value.trim(), back = backEl.value.trim();
  if (!front || !back) return;
  try {
    const res = await fetch(`/api/decks/${currentDeckId}/cards`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ front, back })
    });
    if (res.ok) {
      frontEl.value = ''; backEl.value = '';
      showDeckDetail(currentDeckId);
    }
  } catch (e) { alert('Lỗi mạng.'); }
}

async function editCard(card) {
  const front = prompt('Mặt trước:', card.front);
  if (front === null) return;
  const back = prompt('Mặt sau:', card.back);
  if (back === null) return;
  try {
    await fetch(`/api/cards/${card.id}`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ front: front.trim(), back: back.trim() })
    });
    showDeckDetail(currentDeckId);
  } catch (e) { alert('Lỗi mạng.'); }
}

async function deleteCard(cardId) {
  if (!confirm('Xoá thẻ này?')) return;
  try {
    await fetch(`/api/cards/${cardId}`, { method: 'DELETE' });
    showDeckDetail(currentDeckId);
  } catch (e) { alert('Lỗi mạng.'); }
}

async function deleteCurrentDeck() {
  if (!confirm('Xoá toàn bộ bộ thẻ này? Hành động này không thể hoàn tác.')) return;
  try {
    await fetch(`/api/decks/${currentDeckId}`, { method: 'DELETE' });
    showFcView('list');
    loadDecks();
  } catch (e) { alert('Lỗi mạng.'); }
}

// ---------- Chế độ Học (lật thẻ ôn tập, kiểu Leitner đơn giản) ----------
function startStudyMode() {
  if (!currentDeckCards.length) { alert('Bộ thẻ này chưa có thẻ nào.'); return; }
  // Ưu tiên ôn thẻ có box_level thấp (chưa thuộc) trước — xáo trộn nhẹ trong cùng mức.
  studyQueue = [...currentDeckCards].sort((a, b) => a.box_level - b.box_level || Math.random() - 0.5);
  studyIndex = 0;
  studyCorrectCount = 0;
  showFcView('study');
  showStudyCard();
}

function showStudyCard() {
  document.getElementById('studySummary').classList.add('hidden');
  if (studyIndex >= studyQueue.length) {
    document.getElementById('studyCard').classList.add('hidden');
    document.getElementById('studyAnswerBtns').classList.add('hidden');
    document.getElementById('studyProgress').classList.add('hidden');
    document.getElementById('studySummary').classList.remove('hidden');
    document.getElementById('studySummaryText').textContent =
      `Đúng ${studyCorrectCount}/${studyQueue.length} thẻ!`;
    return;
  }
  document.getElementById('studyCard').classList.remove('hidden');
  document.getElementById('studyProgress').classList.remove('hidden');
  studyFlipped = false;
  document.getElementById('studyAnswerBtns').classList.add('hidden');
  document.getElementById('studyProgress').textContent = `Thẻ ${studyIndex + 1}/${studyQueue.length}`;
  document.getElementById('studyCardText').textContent = studyQueue[studyIndex].front;
}

function flipStudyCard() {
  if (studyIndex >= studyQueue.length) return;
  studyFlipped = !studyFlipped;
  const card = studyQueue[studyIndex];
  document.getElementById('studyCardText').textContent = studyFlipped ? card.back : card.front;
  document.getElementById('studyAnswerBtns').classList.toggle('hidden', !studyFlipped);
}

async function answerStudyCard(correct) {
  const card = studyQueue[studyIndex];
  if (correct) studyCorrectCount++;
  try { await fetch(`/api/cards/${card.id}`, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ correct })
  }); } catch (e) { /* im lặng bỏ qua lỗi mạng, vẫn cho học tiếp */ }
  studyIndex++;
  showStudyCard();
}

// ---------- Game: Lật thẻ ghi nhớ (Memory Match) ----------
function stopGameTimer() {
  if (gameTimerHandle) { clearInterval(gameTimerHandle); gameTimerHandle = null; }
}

function startMemoryGame() {
  const usable = currentDeckCards.slice(0, 8);  // tối đa 8 cặp (16 ô) cho vừa màn hình
  if (usable.length < 3) { alert('Bộ thẻ cần ít nhất 3 thẻ để chơi Lật thẻ.'); return; }

  const tiles = [];
  usable.forEach(c => {
    tiles.push({ cardId: c.id, text: c.front, matched: false });
    tiles.push({ cardId: c.id, text: c.back, matched: false });
  });
  for (let i = tiles.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [tiles[i], tiles[j]] = [tiles[j], tiles[i]];
  }

  gameState = { tiles, firstPick: null, busy: false, matchedPairs: 0, totalPairs: usable.length, moves: 0, seconds: 0 };
  document.getElementById('gameTotalPairs').textContent = usable.length;
  document.getElementById('gameMatched').textContent = '0';
  document.getElementById('gameMoves').textContent = '0';
  document.getElementById('gameTimer').textContent = '0';
  document.getElementById('gameWinPanel').classList.add('hidden');
  document.getElementById('gameGrid').classList.remove('hidden');

  renderGameGrid();
  showFcView('game');
  stopGameTimer();
  gameTimerHandle = setInterval(() => {
    gameState.seconds++;
    document.getElementById('gameTimer').textContent = gameState.seconds;
  }, 1000);
}

function renderGameGrid() {
  const grid = document.getElementById('gameGrid');
  grid.innerHTML = '';
  gameState.tiles.forEach((tile, idx) => {
    const btn = document.createElement('button');
    btn.className = 'game-tile aspect-square rounded-xl bg-gray-100 dark:bg-gray-800 hover:bg-gray-200 dark:hover:bg-gray-700 flex items-center justify-center text-center p-1.5 text-[11px] sm:text-xs font-medium leading-tight transition-colors';
    btn.textContent = '❓';
    btn.dataset.idx = idx;
    btn.addEventListener('click', () => flipGameTile(idx));
    grid.appendChild(btn);
  });
}

function flipGameTile(idx) {
  const state = gameState;
  const tile = state.tiles[idx];
  if (state.busy || tile.matched) return;
  const btn = document.querySelector(`.game-tile[data-idx="${idx}"]`);
  if (btn.classList.contains('revealed')) return;

  btn.textContent = tile.text;
  btn.classList.add('revealed', 'bg-blue-100', 'dark:bg-blue-900/40');

  if (state.firstPick === null) {
    state.firstPick = idx;
    return;
  }

  state.moves++;
  document.getElementById('gameMoves').textContent = state.moves;
  const firstIdx = state.firstPick;
  const firstTile = state.tiles[firstIdx];
  state.busy = true;

  if (firstTile.cardId === tile.cardId && firstIdx !== idx) {
    tile.matched = true; firstTile.matched = true;
    state.matchedPairs++;
    document.getElementById('gameMatched').textContent = state.matchedPairs;
    state.firstPick = null;
    state.busy = false;
    btn.classList.add('opacity-40');
    document.querySelector(`.game-tile[data-idx="${firstIdx}"]`).classList.add('opacity-40');
    if (state.matchedPairs === state.totalPairs) finishMemoryGame();
  } else {
    setTimeout(() => {
      btn.textContent = '❓';
      btn.classList.remove('revealed', 'bg-blue-100', 'dark:bg-blue-900/40');
      const firstBtn = document.querySelector(`.game-tile[data-idx="${firstIdx}"]`);
      firstBtn.textContent = '❓';
      firstBtn.classList.remove('revealed', 'bg-blue-100', 'dark:bg-blue-900/40');
      state.firstPick = null;
      state.busy = false;
    }, 700);
  }
}

async function finishMemoryGame() {
  stopGameTimer();
  document.getElementById('gameGrid').classList.add('hidden');
  document.getElementById('gameWinPanel').classList.remove('hidden');
  document.getElementById('gameWinTime').textContent = gameState.seconds;
  document.getElementById('gameWinMoves').textContent = gameState.moves;

  // Điểm càng cao khi ít lượt lật + nhanh — server sẽ tự giới hạn XP thưởng trong khoảng hợp lý.
  const score = Math.max(10, Math.round(50 - gameState.moves - gameState.seconds / 5));
  try {
    const res = await fetch('/api/games/complete', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ game: 'memory_match', score })
    });
    const data = await res.json();
    document.getElementById('gameXpText').textContent = data.xpAwarded ? `+${data.xpAwarded} XP 🎉` : '';
    if (data.gamify) handleGamifyEvent(data.gamify);
  } catch (e) { /* im lặng bỏ qua lỗi mạng */ }
}

// ==================================================================
// ĐỐ VUI TÍNH NHANH (Quick Math)
// ==================================================================
const QM_OP_LABELS = { '+': 'Phép cộng', '-': 'Phép trừ', '×': 'Phép nhân', '÷': 'Phép chia' };
let qmState = null;
let qmTimerHandle = null;
let qmSelectedDifficulty = 'easy';

// ==================================================================
// LỚP HỌC (Teacher Mode)
// ==================================================================
let currentClassId = null;
let currentClassRole = null;
let currentJoinCode = '';

async function loadClasses() {
  try {
    const res = await fetch('/api/classes');
    if (!res.ok) return;
    const d = await res.json();
    const tSec = document.getElementById('teachingSection'), lSec = document.getElementById('learningSection');
    const tList = document.getElementById('teachingList'), lList = document.getElementById('learningList');
    tList.innerHTML = ''; lList.innerHTML = '';
    tSec.classList.toggle('hidden', !d.teaching.length);
    lSec.classList.toggle('hidden', !d.learning.length);
    document.getElementById('classesEmptyState').classList.toggle('hidden', d.teaching.length || d.learning.length);

    d.teaching.forEach(c => {
      const el = document.createElement('div');
      el.className = 'rounded-2xl border border-gray-200 dark:border-gray-800 p-4 cursor-pointer hover:border-indigo-400 dark:hover:border-indigo-600 transition-colors';
      el.innerHTML = `<div class="flex items-center justify-between gap-2">
          <p class="font-semibold truncate">${escapeHtml(c.name)}</p>
          <span class="font-mono text-xs px-2 py-1 rounded-lg bg-indigo-50 dark:bg-indigo-900/30 text-indigo-600 dark:text-indigo-300 flex-shrink-0">${escapeHtml(c.join_code)}</span>
        </div>
        <p class="text-xs text-gray-400 mt-1">${escapeHtml(c.subject || 'Chung')} · ${c.student_count} học sinh · ${c.assignment_count} bài tập</p>`;
      el.addEventListener('click', () => showClassDetail(c.id));
      tList.appendChild(el);
    });
    d.learning.forEach(c => {
      const el = document.createElement('div');
      el.className = 'rounded-2xl border border-gray-200 dark:border-gray-800 p-4 cursor-pointer hover:border-emerald-400 dark:hover:border-emerald-600 transition-colors';
      el.innerHTML = `<p class="font-semibold truncate">${escapeHtml(c.name)}</p>
        <p class="text-xs text-gray-400 mt-1">GV: ${escapeHtml(c.teacher)} · ${escapeHtml(c.subject || 'Chung')} · ${c.assignment_count} bài tập</p>`;
      el.addEventListener('click', () => showClassDetail(c.id));
      lList.appendChild(el);
    });
  } catch (e) { /* im lặng bỏ qua lỗi mạng */ }
}

async function submitCreateClass() {
  const name = document.getElementById('newClassName').value.trim();
  const subject = document.getElementById('newClassSubject').value.trim();
  const err = document.getElementById('createClassError');
  err.classList.add('hidden');
  if (!name) { document.getElementById('newClassName').focus(); return; }
  try {
    const res = await fetch('/api/classes', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({name, subject}) });
    const d = await res.json();
    if (!res.ok) { err.textContent = d.error || 'Không tạo được lớp.'; err.classList.remove('hidden'); return; }
    document.getElementById('newClassName').value = '';
    document.getElementById('newClassSubject').value = '';
    document.getElementById('createClassForm').classList.add('hidden');
    showClassDetail(d.id);
  } catch (e) { err.textContent = 'Lỗi mạng.'; err.classList.remove('hidden'); }
}

async function submitJoinClass() {
  const code = document.getElementById('joinClassCode').value.trim();
  const err = document.getElementById('joinClassError');
  err.classList.add('hidden');
  if (!code) { document.getElementById('joinClassCode').focus(); return; }
  try {
    const res = await fetch('/api/classes/join', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({code}) });
    const d = await res.json();
    if (!res.ok) { err.textContent = d.error || 'Không vào được lớp.'; err.classList.remove('hidden'); return; }
    document.getElementById('joinClassCode').value = '';
    document.getElementById('joinClassForm').classList.add('hidden');
    showClassDetail(d.id);
  } catch (e) { err.textContent = 'Lỗi mạng.'; err.classList.remove('hidden'); }
}

async function showClassDetail(classId) {
  try {
    const res = await fetch(`/api/classes/${classId}`);
    if (!res.ok) { switchFcTab('classes'); return; }
    const d = await res.json();
    currentClassId = classId;
    currentClassRole = d.my_role;
    const isOwner = d.my_role === 'owner';

    document.getElementById('classDetailName').textContent = d.class.name;
    document.getElementById('classDetailMeta').textContent =
      (d.class.subject || 'Chung') + (isOwner ? ' · Em là giáo viên' : ` · GV: ${d.class.teacher}`);
    document.getElementById('classLeaveBtn').textContent = isOwner ? 'Xoá lớp' : 'Rời lớp';

    // Mã mời — chỉ giáo viên thấy
    const codeBox = document.getElementById('classJoinCodeBox');
    codeBox.classList.toggle('hidden', !isOwner);
    if (isOwner) { currentJoinCode = d.class.join_code; document.getElementById('classJoinCode').textContent = d.class.join_code; }

    document.getElementById('classOverview').classList.toggle('hidden', !isOwner);
    document.getElementById('assignSection').classList.toggle('hidden', !isOwner);
    document.getElementById('classStudentsSection').classList.toggle('hidden', !isOwner);
    document.getElementById('classWeakSection').classList.toggle('hidden', !isOwner || !(d.class_weak_topics || []).length);

    if (isOwner) {
      document.getElementById('classStudentCount').textContent = d.students.length;
      document.getElementById('classAvg').textContent = d.class_avg_pct === null ? '—' : d.class_avg_pct + '%';
      document.getElementById('classNeedAttention').textContent = d.needs_attention_count;

      const weakBox = document.getElementById('classWeakTopics');
      weakBox.innerHTML = '';
      (d.class_weak_topics || []).forEach(w => {
        const chip = document.createElement('span');
        chip.className = 'text-xs font-semibold px-3 py-1.5 rounded-lg bg-amber-100 dark:bg-amber-900/30 text-amber-700 dark:text-amber-400';
        chip.textContent = `${w.topic} (${w.count} lượt sai)`;
        weakBox.appendChild(chip);
      });

      const sBox = document.getElementById('classStudents');
      sBox.innerHTML = '';
      if (!d.students.length) sBox.innerHTML = '<p class="text-sm text-gray-400 py-3">Chưa có học sinh nào. Chia sẻ mã mời ở trên cho lớp nhé!</p>';
      d.students.forEach(s => {
        const row = document.createElement('div');
        row.className = `flex items-center justify-between gap-2 rounded-xl border px-3.5 py-2.5 ${s.needs_attention ? 'border-amber-200 dark:border-amber-900/50 bg-amber-50 dark:bg-amber-900/10' : 'border-gray-100 dark:border-gray-800'}`;
        row.innerHTML = `<div class="min-w-0">
            <p class="text-sm font-medium truncate">${escapeHtml(s.username)} ${s.needs_attention ? '<span class="text-amber-500">⚠️</span>' : ''}</p>
            <p class="text-[11px] text-gray-400">Đã làm ${s.done}/${s.total_assignments} bài</p>
          </div>
          <span class="text-sm font-bold flex-shrink-0 ${s.avg_pct === null ? 'text-gray-400' : s.avg_pct >= 80 ? 'text-emerald-500' : s.avg_pct >= 50 ? 'text-amber-500' : 'text-red-500'}">${s.avg_pct === null ? '—' : s.avg_pct + '%'}</span>`;
        sBox.appendChild(row);
      });
      loadQuizzesForAssign();
    }

    const aBox = document.getElementById('classAssignments');
    aBox.innerHTML = '';
    if (!d.assignments.length) aBox.innerHTML = '<p class="text-sm text-gray-400 py-3">Chưa có bài tập nào.</p>';
    d.assignments.forEach(a => {
      const row = document.createElement('div');
      row.className = 'rounded-xl border border-gray-200 dark:border-gray-800 px-3.5 py-3';
      let right = '';
      if (isOwner) {
        right = `<span class="text-xs text-gray-400">${a.submitted}/${a.total_students} đã nộp${a.avg_pct !== null ? ` · TB ${a.avg_pct}%` : ''}</span>`;
      } else if (a.my_result) {
        const p = a.my_result.pct;
        right = `<span class="text-sm font-bold ${p >= 80 ? 'text-emerald-500' : p >= 50 ? 'text-amber-500' : 'text-red-500'}">${a.my_result.score}/${a.my_result.total} (${p}%)</span>`;
      } else {
        right = `<button class="do-assignment-btn text-xs font-semibold px-3 py-1.5 rounded-lg bg-indigo-600 hover:bg-indigo-700 text-white" data-quiz="${a.quiz_id}" data-assignment="${a.id}">Làm bài</button>`;
      }
      row.innerHTML = `<div class="flex items-center justify-between gap-2">
          <div class="min-w-0"><p class="text-sm font-medium truncate">${escapeHtml(a.title)}</p>
          ${a.due_at ? `<p class="text-[11px] text-gray-400">Hạn nộp: ${escapeHtml(a.due_at.slice(0,10))}</p>` : ''}</div>
          <div class="flex-shrink-0">${right}</div>
        </div>`;
      const btn = row.querySelector('.do-assignment-btn');
      if (btn) btn.addEventListener('click', () => startQuiz(parseInt(btn.dataset.quiz,10), parseInt(btn.dataset.assignment,10)));
      aBox.appendChild(row);
    });

    showFcView('classDetail');
  } catch (e) { alert('Không tải được lớp học này.'); }
}

async function loadQuizzesForAssign() {
  try {
    const res = await fetch('/api/quizzes');
    if (!res.ok) return;
    const quizzes = await res.json();
    const sel = document.getElementById('assignQuizSelect');
    sel.innerHTML = quizzes.length
      ? quizzes.map(q => `<option value="${q.id}">${escapeHtml(q.title)} (${q.question_count} câu)</option>`).join('')
      : '<option value="">— Chưa có quiz nào —</option>';
  } catch (e) { /* im lặng */ }
}

async function submitAssignment() {
  const quizId = document.getElementById('assignQuizSelect').value;
  const dueAt = document.getElementById('assignDueDate').value;
  const err = document.getElementById('assignError');
  err.classList.add('hidden');
  if (!quizId) { err.textContent = 'Em cần tạo quiz ở tab Quiz trước đã.'; err.classList.remove('hidden'); return; }
  try {
    const res = await fetch(`/api/classes/${currentClassId}/assignments`, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ quizId: parseInt(quizId,10), dueAt })
    });
    const d = await res.json();
    if (!res.ok) { err.textContent = d.error || 'Không giao được bài.'; err.classList.remove('hidden'); return; }
    showClassDetail(currentClassId);
  } catch (e) { err.textContent = 'Lỗi mạng.'; err.classList.remove('hidden'); }
}

function copyJoinCode() {
  if (!currentJoinCode) return;
  navigator.clipboard.writeText(currentJoinCode).then(() => {
    const b = document.getElementById('copyJoinCodeBtn');
    const o = b.innerHTML;
    b.innerHTML = '<i class="fas fa-check mr-1"></i>Đã chép';
    setTimeout(() => { b.innerHTML = o; }, 1500);
  });
}

async function deleteOrLeaveClass() {
  const isOwner = currentClassRole === 'owner';
  if (!confirm(isOwner ? 'Xoá lớp này? Toàn bộ bài tập và danh sách học sinh sẽ mất.' : 'Rời khỏi lớp này?')) return;
  try {
    await fetch(`/api/classes/${currentClassId}`, { method: 'DELETE' });
    switchFcTab('classes');
  } catch (e) { alert('Lỗi mạng.'); }
}

async function loadGameStats() {
  try {
    const res = await fetch('/api/games/stats');
    if (!res.ok) return;
    const stats = await res.json();
    document.getElementById('quickMathBestScore').textContent =
      stats.quick_math ? `Điểm cao nhất: ${stats.quick_math.bestScore} (${stats.quick_math.playCount} lượt chơi)` : 'Điểm cao nhất: — (chưa chơi lần nào)';
    document.getElementById('memoryMatchBestScore').textContent =
      stats.memory_match ? `Điểm cao nhất: ${stats.memory_match.bestScore} (${stats.memory_match.playCount} lượt chơi)` : 'Điểm cao nhất: — (chưa chơi lần nào)';
    document.getElementById('snakeBestScore').textContent =
      stats.snake ? `Điểm cao nhất: ${stats.snake.bestScore} (${stats.snake.playCount} lượt chơi)` : 'Điểm cao nhất: — (chưa chơi lần nào)';
  } catch (e) { /* im lặng bỏ qua lỗi mạng */ }
}

function openQuickMathSetup() {
  showFcView('qmSetup');
  document.querySelectorAll('.qm-diff-btn').forEach(btn => {
    btn.classList.toggle('border-emerald-500', btn.dataset.diff === qmSelectedDifficulty);
    btn.classList.toggle('bg-emerald-50', btn.dataset.diff === qmSelectedDifficulty);
    btn.classList.toggle('dark:bg-emerald-900/20', btn.dataset.diff === qmSelectedDifficulty);
    btn.classList.toggle('border-gray-200', btn.dataset.diff !== qmSelectedDifficulty);
    btn.classList.toggle('dark:border-gray-700', btn.dataset.diff !== qmSelectedDifficulty);
  });
}
document.querySelectorAll('.qm-diff-btn').forEach(btn => {
  btn.addEventListener('click', () => { qmSelectedDifficulty = btn.dataset.diff; openQuickMathSetup(); });
});

function qmRandInt(min, max) { return Math.floor(Math.random() * (max - min + 1)) + min; }

function generateQmQuestion(difficulty) {
  const ranges = {
    easy:   { max: 10, ops: ['+', '-'] },
    medium: { max: 20, ops: ['+', '-', '×'] },
    hard:   { max: 50, ops: ['+', '-', '×', '÷'] },
  }[difficulty];

  const op = ranges.ops[qmRandInt(0, ranges.ops.length - 1)];
  let a, b, answer;
  if (op === '+') { a = qmRandInt(1, ranges.max); b = qmRandInt(1, ranges.max); answer = a + b; }
  else if (op === '-') { a = qmRandInt(1, ranges.max); b = qmRandInt(1, a); answer = a - b; }
  else if (op === '×') { a = qmRandInt(2, Math.min(12, ranges.max)); b = qmRandInt(2, 12); answer = a * b; }
  else { b = qmRandInt(2, 12); answer = qmRandInt(2, 12); a = b * answer; }  // chia hết, không có số dư

  const options = new Set([answer]);
  while (options.size < 4) {
    const delta = qmRandInt(-5, 5) || 1;
    const wrong = answer + delta;
    if (wrong >= 0) options.add(wrong);
  }
  const optionsArr = Array.from(options).sort(() => Math.random() - 0.5);
  return { text: `${a} ${op} ${b}`, answer, options: optionsArr, opLabel: QM_OP_LABELS[op] };
}

function startQuickMath() {
  qmState = {
    difficulty: qmSelectedDifficulty, score: 0, correctCount: 0, totalCount: 0,
    combo: 0, bestCombo: 0, wrongOperations: [], current: null, seconds: 60,
  };
  document.getElementById('qmScore').textContent = '0';
  document.getElementById('qmCombo').textContent = '0';
  document.getElementById('qmTimer').textContent = '60';
  showFcView('qmPlay');
  nextQmQuestion();

  stopQuickMathTimer();
  qmTimerHandle = setInterval(() => {
    qmState.seconds--;
    document.getElementById('qmTimer').textContent = qmState.seconds;
    if (qmState.seconds <= 0) finishQuickMath();
  }, 1000);
}

function stopQuickMathTimer() {
  if (qmTimerHandle) { clearInterval(qmTimerHandle); qmTimerHandle = null; }
}

function nextQmQuestion() {
  const q = generateQmQuestion(qmState.difficulty);
  qmState.current = q;
  document.getElementById('qmQuestion').textContent = q.text + ' = ?';
  const grid = document.getElementById('qmAnswerGrid');
  grid.innerHTML = '';
  q.options.forEach(opt => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'py-3 rounded-xl bg-gray-100 dark:bg-gray-800 hover:bg-gray-200 dark:hover:bg-gray-700 font-semibold text-lg';
    btn.textContent = opt;
    btn.addEventListener('click', () => answerQmQuestion(opt, btn));
    grid.appendChild(btn);
  });
}

function answerQmQuestion(selected, btn) {
  if (!qmState || qmState.seconds <= 0) return;
  const q = qmState.current;
  qmState.totalCount++;
  const correct = selected === q.answer;

  if (correct) {
    qmState.correctCount++;
    qmState.combo++;
    qmState.bestCombo = Math.max(qmState.bestCombo, qmState.combo);
    qmState.score += 10 + qmState.combo * 2;
    btn.classList.add('bg-emerald-500', 'text-white');
  } else {
    qmState.combo = 0;
    qmState.wrongOperations.push(q.opLabel);
    btn.classList.add('bg-red-500', 'text-white');
  }
  document.getElementById('qmScore').textContent = qmState.score;
  document.getElementById('qmCombo').textContent = qmState.combo;

  setTimeout(() => { if (qmState && qmState.seconds > 0) nextQmQuestion(); }, 250);
}

async function finishQuickMath() {
  stopQuickMathTimer();
  if (!qmState) return;
  const state = qmState;
  qmState = null;
  showFcView('qmResult');

  const accuracy = state.totalCount ? Math.round((state.correctCount / state.totalCount) * 100) : 0;
  document.getElementById('qmResultEmoji').textContent = accuracy === 100 && state.totalCount >= 10 ? '💯' : accuracy >= 70 ? '🎉' : accuracy >= 40 ? '👍' : '💪';
  document.getElementById('qmResultScore').textContent = `${state.score} điểm`;
  document.getElementById('qmResultDetail').textContent = `${state.correctCount}/${state.totalCount} câu đúng (${accuracy}%) · Combo tốt nhất x${state.bestCombo}`;

  try {
    const res = await fetch('/api/games/quick-math/submit', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        difficulty: state.difficulty, score: state.score, correctCount: state.correctCount,
        totalCount: state.totalCount, bestCombo: state.bestCombo, wrongOperations: state.wrongOperations,
      })
    });
    const data = await res.json();
    document.getElementById('qmResultXp').textContent = data.xpAwarded ? `+${data.xpAwarded} XP 🎉` : '';
    const weakBox = document.getElementById('qmWeakTopicsBox');
    if (data.weakTopics && data.weakTopics.length) {
      weakBox.classList.remove('hidden');
      document.getElementById('qmWeakTopicsList').textContent = data.weakTopics.join(', ');
    } else {
      weakBox.classList.add('hidden');
    }
    if (data.gamify) handleGamifyEvent(data.gamify);
  } catch (e) { /* im lặng bỏ qua lỗi mạng */ }
}

// ==================================================================
// RẮN SĂN CHỮ (Snake Quiz)
// ==================================================================
// Logic thuần (snakeCreateGame/snakeRandomEmptyCell/snakeSetDirection/snakeTick) đã được
// kiểm thử độc lập bằng Node.js: 9 nhóm test gồm di chuyển, va chạm tường/tự đâm thân, luật
// "không được quay đầu 180°", mồi không bao giờ xuất hiện trên thân rắn (5.000 lần thử), và
// mô phỏng chơi ngẫu nhiên 200 ván liên tục không phát sinh lỗi. Giữ NGUYÊN VĂN các hàm dưới
// đây giống bản đã test để không làm mất đi độ tin cậy đó.
function snakeCreateGame(gridSize) {
  const mid = Math.floor(gridSize / 2);
  return {
    gridSize, snake: [{ x: mid, y: mid }],
    direction: { x: 1, y: 0 }, pendingDirection: { x: 1, y: 0 },
    food: null, quizFood: null, score: 0, correctCount: 0, totalQuiz: 0, gameOver: false,
  };
}
function snakeRandomEmptyCell(state) {
  const occupied = new Set(state.snake.map(s => `${s.x},${s.y}`));
  if (state.food) occupied.add(`${state.food.x},${state.food.y}`);
  if (state.quizFood) occupied.add(`${state.quizFood.x},${state.quizFood.y}`);
  const free = [];
  for (let x = 0; x < state.gridSize; x++)
    for (let y = 0; y < state.gridSize; y++)
      if (!occupied.has(`${x},${y}`)) free.push({ x, y });
  if (!free.length) return null;
  return free[Math.floor(Math.random() * free.length)];
}
function snakeSetDirection(state, dx, dy) {
  if (state.snake.length > 1 && dx === -state.direction.x && dy === -state.direction.y) return false;
  state.pendingDirection = { x: dx, y: dy };
  return true;
}
function snakeTick(state) {
  if (state.gameOver) return state;
  state.direction = state.pendingDirection;
  const head = state.snake[0];
  const newHead = { x: head.x + state.direction.x, y: head.y + state.direction.y };
  if (newHead.x < 0 || newHead.x >= state.gridSize || newHead.y < 0 || newHead.y >= state.gridSize) {
    state.gameOver = true;
    return state;
  }
  if (state.snake.some(seg => seg.x === newHead.x && seg.y === newHead.y)) {
    state.gameOver = true;
    return state;
  }
  state.snake.unshift(newHead);
  let ate = false;
  if (state.food && newHead.x === state.food.x && newHead.y === state.food.y) {
    state.score += 10; state.food = null; ate = true;
  } else if (state.quizFood && newHead.x === state.quizFood.x && newHead.y === state.quizFood.y) {
    state.score += 30; state.correctCount++; state.quizFood = null; ate = true;
  }
  if (!ate) state.snake.pop();
  return state;
}

// ---- Phần tích hợp vào giao diện (không phải logic thuần, không nằm trong bộ test ở trên) ----
const SNAKE_DIFFICULTY_CONFIG = {
  easy:   { gridSize: 12, tickMs: 200 },
  medium: { gridSize: 15, tickMs: 150 },
  hard:   { gridSize: 18, tickMs: 110 },
};
let snakeGameState = null;
let snakeTimerHandle = null;
let snakeSelectedDifficulty = 'easy';
let snakeCurrentQuestion = null;   // { text, answer, opLabel } — câu hỏi đang gắn với quizFood
let snakeWrongOperations = [];
let snakeQuestionsAsked = 0;

function openSnakeSetup() {
  showFcView('snakeSetup');
  document.querySelectorAll('.snake-diff-btn').forEach(btn => {
    const active = btn.dataset.diff === snakeSelectedDifficulty;
    btn.classList.toggle('border-emerald-500', active);
    btn.classList.toggle('bg-emerald-50', active);
    btn.classList.toggle('dark:bg-emerald-900/20', active);
    btn.classList.toggle('border-gray-200', !active);
    btn.classList.toggle('dark:border-gray-700', !active);
  });
}
document.querySelectorAll('.snake-diff-btn').forEach(btn => {
  btn.addEventListener('click', () => { snakeSelectedDifficulty = btn.dataset.diff; openSnakeSetup(); });
});

function stopSnakeGame() {
  if (snakeTimerHandle) { clearInterval(snakeTimerHandle); snakeTimerHandle = null; }
}

function startSnakeGame() {
  const cfg = SNAKE_DIFFICULTY_CONFIG[snakeSelectedDifficulty];
  snakeGameState = snakeCreateGame(cfg.gridSize);
  snakeGameState.food = snakeRandomEmptyCell(snakeGameState);
  snakeCurrentQuestion = null;
  snakeWrongOperations = [];
  snakeQuestionsAsked = 0;

  const board = document.getElementById('snakeBoard');
  board.style.gridTemplateColumns = `repeat(${cfg.gridSize}, 1fr)`;
  board.style.gridTemplateRows = `repeat(${cfg.gridSize}, 1fr)`;

  document.getElementById('snakeScore').textContent = '0';
  document.getElementById('snakeLength').textContent = '1';
  document.getElementById('snakeQuestionBox').classList.add('hidden');
  showFcView('snakePlay');
  renderSnakeBoard();
  spawnSnakeQuizQuestion();

  stopSnakeGame();
  snakeTimerHandle = setInterval(() => {
    snakeTick(snakeGameState);
    if (snakeGameState.gameOver) { finishSnakeGame(); return; }
    if (!snakeGameState.food) snakeGameState.food = snakeRandomEmptyCell(snakeGameState);
    renderSnakeBoard();
  }, cfg.tickMs);
}

function renderSnakeBoard() {
  const state = snakeGameState;
  const board = document.getElementById('snakeBoard');
  board.innerHTML = '';
  const snakeSet = new Set(state.snake.map((s, i) => `${s.x},${s.y}`));
  const headKey = `${state.snake[0].x},${state.snake[0].y}`;
  for (let y = 0; y < state.gridSize; y++) {
    for (let x = 0; x < state.gridSize; x++) {
      const key = `${x},${y}`;
      const cell = document.createElement('div');
      let content = '';
      let cls = 'w-full h-full';
      if (key === headKey) { cls += ' bg-emerald-600 rounded-sm'; }
      else if (snakeSet.has(key)) { cls += ' bg-emerald-400 dark:bg-emerald-700 rounded-sm'; }
      else if (state.food && state.food.x === x && state.food.y === y) { content = '🔵'; }
      else if (state.quizFood && state.quizFood.x === x && state.quizFood.y === y) { content = '🟡'; }
      cell.className = cls + ' flex items-center justify-center text-[10px] leading-none';
      cell.textContent = content;
      board.appendChild(cell);
    }
  }
  document.getElementById('snakeScore').textContent = state.score;
  document.getElementById('snakeLength').textContent = state.snake.length;
}

function spawnSnakeQuizQuestion() {
  // Nếu câu hỏi trước CHƯA được ăn (quizFood vẫn còn trên bàn cờ) -> tính là "bỏ lỡ", ghi vào
  // danh sách phép tính hay sai để cuối ván tự lưu vào Sổ lỗi sai giống Đố Vui Tính Nhanh.
  if (snakeCurrentQuestion && snakeGameState.quizFood) {
    snakeWrongOperations.push(snakeCurrentQuestion.opLabel);
  }
  const q = generateQmQuestion(snakeSelectedDifficulty === 'hard' ? 'hard' : snakeSelectedDifficulty === 'medium' ? 'medium' : 'easy');
  snakeCurrentQuestion = q;
  snakeQuestionsAsked++;
  document.getElementById('snakeQuestionBox').classList.remove('hidden');
  document.getElementById('snakeQuestion').textContent = q.text;
  snakeGameState.quizFood = snakeRandomEmptyCell(snakeGameState);
  renderSnakeBoard();
  // Câu hỏi mới sau mỗi 8 giây (dù ăn được hay chưa) — giữ nhịp độ chơi liên tục.
  clearTimeout(window._snakeQuestionTimer);
  window._snakeQuestionTimer = setTimeout(() => {
    if (snakeGameState && !snakeGameState.gameOver) spawnSnakeQuizQuestion();
  }, 8000);
}

function snakeHandleDirection(dir) {
  if (!snakeGameState || snakeGameState.gameOver) return;
  const map = { up: [0, -1], down: [0, 1], left: [-1, 0], right: [1, 0] };
  const [dx, dy] = map[dir];
  snakeSetDirection(snakeGameState, dx, dy);
}

document.addEventListener('keydown', (e) => {
  if (!snakeGameState || snakeGameState.gameOver) return;
  const keyMap = { ArrowUp: 'up', ArrowDown: 'down', ArrowLeft: 'left', ArrowRight: 'right', w: 'up', s: 'down', a: 'left', d: 'right' };
  if (keyMap[e.key]) { e.preventDefault(); snakeHandleDirection(keyMap[e.key]); }
});
document.querySelectorAll('.snake-ctrl-btn').forEach(btn => {
  btn.addEventListener('click', () => snakeHandleDirection(btn.dataset.dir));
});
// Vuốt để điều khiển trên điện thoại
(function () {
  let touchStartX = 0, touchStartY = 0;
  const board = document.getElementById('snakeBoard');
  board.addEventListener('touchstart', (e) => {
    touchStartX = e.touches[0].clientX; touchStartY = e.touches[0].clientY;
  }, { passive: true });
  board.addEventListener('touchend', (e) => {
    const dx = e.changedTouches[0].clientX - touchStartX;
    const dy = e.changedTouches[0].clientY - touchStartY;
    if (Math.max(Math.abs(dx), Math.abs(dy)) < 20) return;
    if (Math.abs(dx) > Math.abs(dy)) snakeHandleDirection(dx > 0 ? 'right' : 'left');
    else snakeHandleDirection(dy > 0 ? 'down' : 'up');
  }, { passive: true });
})();

async function finishSnakeGame() {
  stopSnakeGame();
  clearTimeout(window._snakeQuestionTimer);
  if (!snakeGameState) return;
  const state = snakeGameState;
  // Câu hỏi cuối cùng chưa kịp ăn cũng tính là bỏ lỡ.
  if (snakeCurrentQuestion && state.quizFood) snakeWrongOperations.push(snakeCurrentQuestion.opLabel);
  snakeGameState = null;
  showFcView('snakeResult');

  document.getElementById('snakeResultScore').textContent = `${state.score} điểm`;
  document.getElementById('snakeResultDetail').textContent =
    `Độ dài rắn: ${state.snake.length} · Đáp số đúng đã ăn: ${state.correctCount}/${snakeQuestionsAsked}`;

  try {
    const res = await fetch('/api/games/snake/submit', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        difficulty: snakeSelectedDifficulty, score: state.score, correctCount: state.correctCount,
        totalCount: snakeQuestionsAsked, snakeLength: state.snake.length, wrongOperations: snakeWrongOperations,
      })
    });
    const data = await res.json();
    document.getElementById('snakeResultXp').textContent = data.xpAwarded ? `+${data.xpAwarded} XP 🎉` : '';
    const weakBox = document.getElementById('snakeWeakTopicsBox');
    if (data.weakTopics && data.weakTopics.length) {
      weakBox.classList.remove('hidden');
      document.getElementById('snakeWeakTopicsList').textContent = data.weakTopics.join(', ');
    } else {
      weakBox.classList.add('hidden');
    }
    if (data.gamify) handleGamifyEvent(data.gamify);
  } catch (e) { /* im lặng bỏ qua lỗi mạng */ }
}

// ==================================================================
// QUIZ GENERATOR
// ==================================================================
let currentQuizQuestions = [];
let currentQuizIndex = 0;
let currentQuizAnswers = [];
let currentQuizSelected = null;
let quizStartTime = 0;
let quizTimerHandle = null;

async function loadQuizzes() {
  try {
    const res = await fetch('/api/quizzes');
    if (!res.ok) return;
    renderQuizGrid(await res.json());
  } catch (e) { /* im lặng bỏ qua lỗi mạng */ }
}

function renderQuizGrid(quizzes) {
  const grid = document.getElementById('quizGrid');
  const empty = document.getElementById('quizEmptyState');
  grid.innerHTML = '';
  empty.classList.toggle('hidden', quizzes.length > 0);
  const diffLabel = { easy: 'Dễ', medium: 'Trung bình', hard: 'Khó', expert: 'Nâng cao' };
  quizzes.forEach(q => {
    const card = document.createElement('div');
    card.className = 'rounded-2xl border border-gray-200 dark:border-gray-800 p-4 hover:border-blue-400 dark:hover:border-blue-600 transition-colors';
    card.innerHTML = `
      <p class="font-semibold truncate">${escapeHtml(q.title)}</p>
      <p class="text-xs text-gray-400 mt-0.5">${escapeHtml(q.subject || 'Chung')} · ${diffLabel[q.difficulty] || q.difficulty} · ${q.question_count} câu</p>
      ${q.last_score !== null ? `<p class="text-xs text-emerald-500 mt-1.5"><i class="fas fa-check-circle mr-1"></i>Lần gần nhất: ${q.last_score}/${q.last_total}</p>` : ''}
      <div class="flex gap-2 mt-3">
        <button class="quiz-take-btn flex-1 px-3 py-1.5 rounded-lg bg-blue-600 hover:bg-blue-700 text-white text-xs font-semibold" data-id="${q.id}">Làm bài</button>
        <button class="quiz-delete-btn px-2.5 py-1.5 rounded-lg bg-gray-100 dark:bg-gray-800 text-gray-400 hover:text-red-500 text-xs" data-id="${q.id}"><i class="fas fa-trash-can"></i></button>
      </div>`;
    card.querySelector('.quiz-take-btn').addEventListener('click', () => startQuiz(q.id));
    card.querySelector('.quiz-delete-btn').addEventListener('click', async (e) => {
      e.stopPropagation();
      if (!confirm('Xoá quiz này?')) return;
      await fetch(`/api/quizzes/${q.id}`, { method: 'DELETE' });
      loadQuizzes();
    });
    grid.appendChild(card);
  });
}

function openQuizForm() {
  const form = document.getElementById('quizForm');
  form.classList.toggle('hidden');
  if (!form.classList.contains('hidden')) document.getElementById('quizTopic').focus();
}

async function submitQuizGeneration() {
  const topic = document.getElementById('quizTopic').value.trim();
  const subject = document.getElementById('quizSubject').value.trim();
  const difficulty = document.getElementById('quizDifficulty').value;
  const count = document.getElementById('quizCount').value;
  const errEl = document.getElementById('quizFormError');
  const btn = document.getElementById('quizSubmitBtn');
  const label = document.getElementById('quizSubmitLabel');
  errEl.classList.add('hidden');
  if (!topic) { document.getElementById('quizTopic').focus(); return; }

  btn.disabled = true;
  label.innerHTML = '<i class="fas fa-spinner fa-spin"></i> AI đang ra đề...';
  try {
    const res = await fetch('/api/quizzes/generate', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ topic, subject, difficulty, count: parseInt(count, 10) })
    });
    const data = await res.json();
    if (!res.ok) {
      errEl.textContent = data.error || 'AI chưa ra đề được, em thử lại nhé.';
      errEl.classList.remove('hidden');
    } else {
      document.getElementById('quizForm').classList.add('hidden');
      document.getElementById('quizTopic').value = '';
      startQuiz(data.quizId);
    }
  } catch (e) {
    errEl.textContent = 'Lỗi mạng, em thử lại nhé.';
    errEl.classList.remove('hidden');
  } finally {
    btn.disabled = false;
    label.textContent = 'Tạo quiz';
  }
}

async function startQuiz(quizId, assignmentId) {
  try {
    const res = await fetch(`/api/quizzes/${quizId}`);
    if (!res.ok) return;
    const data = await res.json();
    currentQuizId = quizId;
    currentAssignmentId = assignmentId || null;   // nếu đang làm bài tập lớp giao
    currentQuizQuestions = data.questions;
    currentQuizIndex = 0;
    currentQuizAnswers = [];
    quizStartTime = Date.now();
    showFcView('quizTake');
    renderQuizQuestion();
  } catch (e) {
    alert('Không tải được quiz này.');
  }
}
let currentQuizId = null;
let currentAssignmentId = null;

function renderQuizQuestion() {
  const q = currentQuizQuestions[currentQuizIndex];
  document.getElementById('quizTakeProgress').textContent = `Câu ${currentQuizIndex + 1}/${currentQuizQuestions.length}`;
  document.getElementById('quizQuestionTopic').textContent = q.topic || '';
  document.getElementById('quizQuestionText').textContent = q.question;
  currentQuizSelected = null;

  const area = document.getElementById('quizAnswerArea');
  area.innerHTML = '';
  if (q.q_type === 'mcq') {
    q.options.forEach(opt => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'quiz-opt-btn w-full text-left px-4 py-2.5 rounded-xl border border-gray-200 dark:border-gray-700 hover:border-blue-400 text-sm';
      btn.textContent = opt;
      btn.addEventListener('click', () => selectQuizOption(btn, opt));
      area.appendChild(btn);
    });
  } else if (q.q_type === 'true_false') {
    ['Đúng', 'Sai'].forEach(opt => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'quiz-opt-btn w-full text-left px-4 py-2.5 rounded-xl border border-gray-200 dark:border-gray-700 hover:border-blue-400 text-sm';
      btn.textContent = opt;
      btn.addEventListener('click', () => selectQuizOption(btn, opt));
      area.appendChild(btn);
    });
  } else {
    const input = document.createElement('input');
    input.type = 'text';
    input.id = 'quizFillBlankInput';
    input.placeholder = 'Nhập đáp án...';
    input.className = 'w-full px-4 py-2.5 rounded-xl bg-gray-100 dark:bg-gray-800 border-0 focus:outline-none focus:ring-2 focus:ring-blue-500 text-sm dark:text-white';
    input.addEventListener('input', () => { currentQuizSelected = input.value; });
    area.appendChild(input);
    setTimeout(() => input.focus(), 50);
  }
}

function selectQuizOption(btn, value) {
  document.querySelectorAll('.quiz-opt-btn').forEach(b => {
    b.classList.remove('border-blue-500', 'bg-blue-50', 'dark:bg-blue-900/20');
  });
  btn.classList.add('border-blue-500', 'bg-blue-50', 'dark:bg-blue-900/20');
  currentQuizSelected = value;
}

function submitQuizAnswerAndNext() {
  const q = currentQuizQuestions[currentQuizIndex];
  currentQuizAnswers.push({ questionId: q.id, given: currentQuizSelected || '' });
  currentQuizIndex++;
  if (currentQuizIndex < currentQuizQuestions.length) {
    renderQuizQuestion();
  } else {
    finishQuiz();
  }
}

async function finishQuiz() {
  const durationSeconds = Math.round((Date.now() - quizStartTime) / 1000);
  try {
    const res = await fetch(`/api/quizzes/${currentQuizId}/submit`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ answers: currentQuizAnswers, durationSeconds, saveMistakes: true, assignmentId: currentAssignmentId })
    });
    const data = await res.json();
    showFcView('quizResult');
    const pct = data.total ? Math.round((data.score / data.total) * 100) : 0;
    document.getElementById('quizResultEmoji').textContent = pct === 100 ? '💯' : pct >= 70 ? '🎉' : pct >= 40 ? '👍' : '💪';
    document.getElementById('quizResultScore').textContent = `${data.score}/${data.total} câu đúng (${pct}%)`;

    const weakBox = document.getElementById('quizWeakTopics');
    if (data.weakTopics && data.weakTopics.length) {
      weakBox.classList.remove('hidden');
      document.getElementById('quizWeakTopicsList').textContent = data.weakTopics.join(', ');
    } else {
      weakBox.classList.add('hidden');
    }

    const reviewList = document.getElementById('quizReviewList');
    reviewList.innerHTML = '';
    (data.graded || []).forEach((g, i) => {
      const q = currentQuizQuestions[i];
      const row = document.createElement('div');
      row.className = `rounded-xl border p-3 text-sm ${g.correct ? 'border-emerald-200 dark:border-emerald-900 bg-emerald-50 dark:bg-emerald-900/10' : 'border-red-200 dark:border-red-900 bg-red-50 dark:bg-red-900/10'}`;
      row.innerHTML = `
        <p class="font-medium">${escapeHtml(q ? q.question : '')}</p>
        <p class="text-xs mt-1 ${g.correct ? 'text-emerald-600 dark:text-emerald-400' : 'text-red-600 dark:text-red-400'}">
          ${g.correct ? '<i class="fas fa-check"></i> Đúng' : `<i class="fas fa-xmark"></i> Sai — đáp án đúng: ${escapeHtml(g.correctAnswer)}`}
        </p>
        ${g.explanation ? `<p class="text-xs text-gray-400 mt-1">${escapeHtml(g.explanation)}</p>` : ''}`;
      reviewList.appendChild(row);
    });

    if (data.gamify) {
      document.getElementById('quizResultXp').textContent = `+${Math.min(40, 10 + data.score * 3)} XP`;
      handleGamifyEvent(data.gamify);
    }
    if (currentAssignmentId && currentClassId) {
      // Vừa nộp bài tập của lớp -> làm mới lại lớp để thấy điểm ngay
      showClassDetail(currentClassId);
      currentAssignmentId = null;
    } else {
      loadQuizzes();
    }
  } catch (e) {
    alert('Không nộp được bài, em thử lại nhé.');
  }
}

// ==================================================================
// STUDY PLAN (Kế hoạch ôn tập)
// ==================================================================
let currentPlanId = null;

async function loadStudyPlans() {
  try {
    const res = await fetch('/api/study-plans');
    if (!res.ok) return;
    renderPlanGrid(await res.json());
  } catch (e) { /* im lặng bỏ qua lỗi mạng */ }
}

function renderPlanGrid(plans) {
  const grid = document.getElementById('planGrid');
  const empty = document.getElementById('planEmptyState');
  grid.innerHTML = '';
  empty.classList.toggle('hidden', plans.length > 0);
  plans.forEach(p => {
    const pct = p.task_count ? Math.round((p.done_count / p.task_count) * 100) : 0;
    const card = document.createElement('div');
    card.className = 'rounded-2xl border border-gray-200 dark:border-gray-800 p-4 hover:border-emerald-400 dark:hover:border-emerald-600 cursor-pointer transition-colors';
    card.innerHTML = `
      <p class="font-semibold truncate">${escapeHtml(p.title)}</p>
      <p class="text-xs text-gray-400 mt-0.5">${escapeHtml(p.subject || 'Chung')} · ${p.total_days} ngày</p>
      <div class="gamify-xp-track mt-2.5"><div class="gamify-xp-fill" style="width:${pct}%; background: linear-gradient(90deg,#10b981,#14b8a6);"></div></div>
      <p class="text-xs text-gray-400 mt-1">${p.done_count}/${p.task_count} việc đã xong (${pct}%)</p>`;
    card.addEventListener('click', () => showPlanDetail(p.id));
    grid.appendChild(card);
  });
}

function openPlanForm() {
  const form = document.getElementById('planForm');
  form.classList.toggle('hidden');
  if (!form.classList.contains('hidden')) document.getElementById('planGoal').focus();
}

async function submitPlanGeneration() {
  const goal = document.getElementById('planGoal').value.trim();
  const subject = document.getElementById('planSubject').value.trim();
  const days = document.getElementById('planDays').value;
  const errEl = document.getElementById('planFormError');
  const btn = document.getElementById('planSubmitBtn');
  const label = document.getElementById('planSubmitLabel');
  errEl.classList.add('hidden');
  if (!goal) { document.getElementById('planGoal').focus(); return; }

  btn.disabled = true;
  label.innerHTML = '<i class="fas fa-spinner fa-spin"></i> AI đang lập kế hoạch...';
  try {
    const res = await fetch('/api/study-plans/generate', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ goal, subject, days: parseInt(days, 10) || 14 })
    });
    const data = await res.json();
    if (!res.ok) {
      errEl.textContent = data.error || 'AI chưa lập được kế hoạch, em thử lại nhé.';
      errEl.classList.remove('hidden');
    } else {
      document.getElementById('planForm').classList.add('hidden');
      document.getElementById('planGoal').value = '';
      if (data.gamify) handleGamifyEvent(data.gamify);
      showPlanDetail(data.planId);
    }
  } catch (e) {
    errEl.textContent = 'Lỗi mạng, em thử lại nhé.';
    errEl.classList.remove('hidden');
  } finally {
    btn.disabled = false;
    label.textContent = 'Tạo kế hoạch';
  }
}

async function showPlanDetail(planId) {
  try {
    const res = await fetch(`/api/study-plans/${planId}`);
    if (!res.ok) return;
    const data = await res.json();
    currentPlanId = planId;
    document.getElementById('planDetailTitle').textContent = data.plan.title;
    document.getElementById('planDetailMeta').textContent = `${data.plan.subject || 'Chung'} · ${data.plan.total_days} ngày`;

    const doneCount = data.tasks.filter(t => t.status === 'done').length;
    const pct = data.tasks.length ? Math.round((doneCount / data.tasks.length) * 100) : 0;
    document.getElementById('planProgressBar').style.width = pct + '%';

    // Chỉ hiện nút "Sắp xếp lại" nếu học sinh có vẻ đang bị TRỄ tiến độ: còn việc pending ở
    // những ngày đã qua (so với hôm nay tính từ ngày bắt đầu kế hoạch).
    const startDate = new Date(data.plan.start_date);
    const daysSinceStart = Math.floor((Date.now() - startDate.getTime()) / 86400000) + 1;
    const behindSchedule = data.tasks.some(t => t.day_number < daysSinceStart && t.status === 'pending');
    document.getElementById('planReorganizeBtn').classList.toggle('hidden', !behindSchedule);

    renderPlanTasks(data.tasks);
    showFcView('planDetail');
  } catch (e) {
    alert('Không tải được kế hoạch này.');
  }
}

function renderPlanTasks(tasks) {
  const list = document.getElementById('planTaskList');
  list.innerHTML = '';
  tasks.forEach(t => {
    const row = document.createElement('div');
    row.className = `flex items-start gap-3 rounded-xl border p-3 text-sm ${t.status === 'done' ? 'border-emerald-200 dark:border-emerald-900 bg-emerald-50 dark:bg-emerald-900/10 opacity-70' : t.status === 'skipped' ? 'border-gray-200 dark:border-gray-800 opacity-50' : 'border-gray-200 dark:border-gray-800'}`;
    row.innerHTML = `
      <button class="task-toggle-btn w-6 h-6 rounded-full border-2 flex-shrink-0 mt-0.5 flex items-center justify-center ${t.status === 'done' ? 'bg-emerald-500 border-emerald-500 text-white' : 'border-gray-300 dark:border-gray-600'}" data-id="${t.id}">
        ${t.status === 'done' ? '<i class="fas fa-check text-xs"></i>' : ''}
      </button>
      <div class="min-w-0 flex-1">
        <p class="text-xs text-gray-400">Ngày ${t.day_number}</p>
        <p class="font-medium ${t.status === 'done' ? 'line-through' : ''}">${escapeHtml(t.title)}</p>
        ${t.description ? `<p class="text-xs text-gray-400 mt-0.5">${escapeHtml(t.description)}</p>` : ''}
      </div>
      <div class="flex gap-1 flex-shrink-0">
        <button class="task-ask-btn w-7 h-7 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800 text-gray-400" data-title="${t.id}" title="Hỏi AI về việc này"><i class="fas fa-comment-dots text-xs"></i></button>
        ${t.status !== 'skipped' && t.status !== 'done' ? `<button class="task-skip-btn w-7 h-7 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800 text-gray-400" data-id="${t.id}" title="Bỏ qua"><i class="fas fa-forward text-xs"></i></button>` : ''}
      </div>`;
    row.querySelector('.task-toggle-btn').addEventListener('click', () => toggleStudyTask(t.id, t.status === 'done' ? 'pending' : 'done'));
    const askBtn = row.querySelector('.task-ask-btn');
    if (askBtn) askBtn.addEventListener('click', () => askAiAboutTask(t.title));
    const skipBtn = row.querySelector('.task-skip-btn');
    if (skipBtn) skipBtn.addEventListener('click', () => toggleStudyTask(t.id, 'skipped'));
    list.appendChild(row);
  });
}

async function toggleStudyTask(taskId, status) {
  try {
    const res = await fetch(`/api/study-tasks/${taskId}`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ status })
    });
    const data = await res.json();
    if (data.gamify) handleGamifyEvent(data.gamify);
    showPlanDetail(currentPlanId);
  } catch (e) { alert('Lỗi mạng.'); }
}

function askAiAboutTask(title) {
  closeFlashcards();
  newChat();
  document.getElementById('messageInput').value = `Em muốn học về: "${title}". Thầy/cô giải thích giúp em với ạ.`;
  sendMessage();
}

async function reorganizeCurrentPlan() {
  if (!confirm('Sắp xếp lại các việc còn thiếu vào số ngày còn lại? Các việc đã hoàn thành sẽ được giữ nguyên.')) return;
  try {
    const res = await fetch(`/api/study-plans/${currentPlanId}/reorganize`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({})
    });
    const data = await res.json();
    if (!res.ok) { alert(data.error || 'Không sắp xếp lại được.'); return; }
    showPlanDetail(currentPlanId);
  } catch (e) { alert('Lỗi mạng.'); }
}

async function deleteCurrentPlan() {
  if (!confirm('Xoá kế hoạch này? Hành động này không thể hoàn tác.')) return;
  try {
    await fetch(`/api/study-plans/${currentPlanId}`, { method: 'DELETE' });
    switchFcTab('plans');
  } catch (e) { alert('Lỗi mạng.'); }
}

// ---------- Gửi tin nhắn (streaming) ----------
async function sendMessage() {
  const input = document.getElementById('messageInput');
  const sendBtn = document.getElementById('sendBtn');
  const text = input.value.trim();
  if (!text) return;

  addMessage('user', text);
  input.value = "";
  input.style.height = 'auto';

  input.disabled = true;
  sendBtn.disabled = true;
  sendBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i>';

  const subjectEl = document.getElementById('subject');
  const modeEl = document.getElementById('modeSelect');
  const subjectText = subjectEl.options[subjectEl.selectedIndex].text.replace(/^\S+\s/, '');
  const modeValue = modeEl.value;

  const aiBubble = createAiStreamBubble();
  let fullText = '';
  let gotFirstToken = false;
  let handledError = false;
  const wasNewConversation = currentConversationId === null;

  try {
    const response = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        subject: subjectText,
        mode: modeValue,
        message: text,
        fileContext: uploadedFileContext,
        fileName: uploadedFileName,
        imageData: uploadedImageDataUrl,
        conversationId: currentConversationId,
        thinkingMode: currentThinkingMode
      })
    });

    if (response.status === 401) { window.location.href = '/login'; return; }

    if (!response.ok || !response.body) {
      const data = await response.json().catch(() => ({}));
      updateAiStreamBubble(aiBubble, '⚠️ **Lỗi:** ' + (data.error || 'Không nhận được phản hồi từ server.'), false);
      handledError = true;
    } else {
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        let sepIndex;
        while ((sepIndex = buffer.indexOf('\n\n')) !== -1) {
          const rawEvent = buffer.slice(0, sepIndex);
          buffer = buffer.slice(sepIndex + 2);
          if (!rawEvent.startsWith('data: ')) continue;

          let payload;
          try { payload = JSON.parse(rawEvent.slice(6)); } catch { continue; }

          if (payload.conversationId) {
            currentConversationId = payload.conversationId;
          } else if (payload.memory) {
            showMemoryToast(payload.memory);
          } else if (payload.gamify) {
            handleGamifyEvent(payload.gamify);
          } else if (payload.error) {
            updateAiStreamBubble(aiBubble, (fullText ? fullText + '\n\n' : '') + '⚠️ **Lỗi:** ' + payload.error, false);
            handledError = true;
          } else if (payload.token) {
            gotFirstToken = true;
            fullText += payload.token;
            updateAiStreamBubble(aiBubble, fullText, true);
          } else if (payload.done) {
            updateAiStreamBubble(aiBubble, fullText, false);
          }
        }
      }

      if (!gotFirstToken && !handledError) {
        updateAiStreamBubble(aiBubble, '⚠️ Thầy/Cô chưa nhận được phản hồi. Em thử lại nhé!', false);
      }

      if (gotFirstToken && !handledError) {
        addMessageActions(aiBubble.parentElement, currentConversationId, () => fullText);
      }
    }
  } catch (error) {
    updateAiStreamBubble(aiBubble, fullText || '🔌 Đã mất kết nối. Em kiểm tra lại mạng nhé!', false);
  } finally {
    // Tắt hiệu ứng "đang suy nghĩ" trên avatar khi đã có kết quả (xong hoặc lỗi).
    const avatarEl = aiBubble.parentElement && aiBubble.parentElement.querySelector('.ai-avatar');
    if (avatarEl) avatarEl.classList.remove('thinking');
    input.disabled = false;
    sendBtn.disabled = false;
    sendBtn.innerHTML = '<i class="fas fa-arrow-up"></i>';
    input.focus();
    loadConversations();
  }
}

document.getElementById('messageInput').addEventListener('keydown', function (e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
});

document.getElementById('messageInput').addEventListener('input', function () {
  this.style.height = 'auto';
  this.style.height = (this.scrollHeight < 160 ? this.scrollHeight : 160) + 'px';
});

function startVoice() {
  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRecognition) return alert("Trình duyệt chưa hỗ trợ Giọng nói.");
  const recognition = new SpeechRecognition();
  recognition.lang = 'vi-VN';
  showTypingIndicator();
  recognition.onresult = (e) => {
    removeTypingIndicator();
    document.getElementById('messageInput').value = e.results[0][0].transcript;
    sendMessage();
  };
  recognition.onerror = () => removeTypingIndicator();
  recognition.onend = () => removeTypingIndicator();
  recognition.start();
}

// ---------- Đính kèm file / ảnh (chọn tay hoặc kéo-thả) ----------
function clearAttachments() {
  uploadedFileContext = ""; uploadedFileName = "";
  uploadedImageDataUrl = ""; uploadedImageName = "";
  const bar = document.getElementById('attachmentsBar');
  bar.innerHTML = '';
  bar.classList.add('hidden');
}

function showAttachmentChip(kind, name, thumbUrl) {
  const bar = document.getElementById('attachmentsBar');
  bar.classList.remove('hidden');
  const chipId = kind === 'image' ? 'chip-image' : 'chip-file';
  let chip = document.getElementById(chipId);
  if (!chip) {
    chip = document.createElement('div');
    chip.id = chipId;
    chip.className = 'attachment-chip flex items-center gap-2 bg-blue-50 dark:bg-gray-700 border border-blue-200 dark:border-gray-600 rounded-xl px-3 py-2 text-sm';
    bar.appendChild(chip);
  }
  const iconHtml = kind === 'image'
    ? `<img src="${thumbUrl}" class="w-7 h-7 rounded object-cover">`
    : '<i class="fas fa-file-lines text-blue-500"></i>';
  chip.innerHTML = `${iconHtml} <span class="truncate max-w-[140px]" title="${escapeHtml(name)}">${escapeHtml(name)}</span>
    <button type="button" class="ml-1 text-gray-400 hover:text-red-500" title="Gỡ đính kèm"><i class="fas fa-xmark"></i></button>`;
  chip.querySelector('button').onclick = () => removeAttachment(kind);
}

function removeAttachment(kind) {
  if (kind === 'image') { uploadedImageDataUrl = ""; uploadedImageName = ""; }
  else { uploadedFileContext = ""; uploadedFileName = ""; }
  const chip = document.getElementById(kind === 'image' ? 'chip-image' : 'chip-file');
  if (chip) chip.remove();
  const bar = document.getElementById('attachmentsBar');
  if (!bar.children.length) bar.classList.add('hidden');
}

async function processFile(file) {
  if (!file) return;
  const lower = file.name.toLowerCase();
  const isImage = /\.(png|jpe?g|gif|webp)$/.test(lower);
  const isDoc = /\.(pdf|docx|txt|csv)$/.test(lower);

  if (!isImage && !isDoc) {
    addMessage('ai', `⚠️ Định dạng file **${file.name}** chưa được hỗ trợ. Em thử PDF, Word (.docx), .txt, .csv hoặc ảnh (PNG/JPG/GIF/WEBP) nhé!`, true);
    return;
  }

  const noticeDiv = addMessage('ai', `📎 Đang đọc file **${file.name}**...`, true);

  const formData = new FormData();
  formData.append('file', file);

  try {
    const response = await fetch('/api/upload', { method: 'POST', body: formData });
    if (response.status === 401) { window.location.href = '/login'; return; }
    const data = await response.json();

    if (data.error) {
      updateAiStreamBubble(noticeDiv, '⚠️ **Lỗi đọc file:** ' + data.error, false);
      return;
    }

    if (data.type === 'image') {
      uploadedImageDataUrl = data.dataUrl;
      uploadedImageName = file.name;
      showAttachmentChip('image', file.name, data.dataUrl);
      updateAiStreamBubble(noticeDiv, `✅ Thầy/Cô đã nhận ảnh **${file.name}**. Em có thể hỏi Thầy/Cô về nội dung trong ảnh nhé! 🖼️`, false);
    } else {
      uploadedFileContext = data.text || "";
      uploadedFileName = file.name;
      showAttachmentChip('file', file.name);
      const pageInfo = data.pages ? ` (${data.pages} trang)` : '';
      updateAiStreamBubble(noticeDiv, `✅ Thầy/Cô đã đọc xong file **${file.name}**${pageInfo}. Bây giờ em có thể hỏi bất cứ điều gì về nội dung file này nhé! 📖`, false);
    }
    loadPlanInfo();
  } catch (err) {
    updateAiStreamBubble(noticeDiv, '🔌 Không tải được file lên server. Em thử lại nhé!', false);
  }
}
document.getElementById('fileInput').addEventListener('change', function (e) {
  if (e.target.files.length) processFile(e.target.files[0]);
  e.target.value = '';
});

// Kéo - thả file/ảnh vào toàn bộ khung chat
const chatPanel = document.getElementById('chatPanel');
let dragCounter = 0;

['dragenter', 'dragover'].forEach(evtName => {
  chatPanel.addEventListener(evtName, (e) => {
    e.preventDefault();
    e.stopPropagation();
    if (e.dataTransfer && Array.from(e.dataTransfer.types || []).includes('Files')) {
      dragCounter++;
      chatPanel.classList.add('drag-active');
    }
  });
});

['dragleave', 'dragend'].forEach(evtName => {
  chatPanel.addEventListener(evtName, (e) => {
    e.preventDefault();
    e.stopPropagation();
    dragCounter = Math.max(0, dragCounter - 1);
    if (dragCounter === 0) chatPanel.classList.remove('drag-active');
  });
});

chatPanel.addEventListener('drop', (e) => {
  e.preventDefault();
  e.stopPropagation();
  dragCounter = 0;
  chatPanel.classList.remove('drag-active');
  const dt = e.dataTransfer;
  if (dt && dt.files && dt.files.length) {
    processFile(dt.files[0]);
  }
});

window.onload = async () => {
  await loadPreferences();
  showWelcome();
  loadProjects();
  loadConversations();
  loadBanner();
  loadPlanInfo();
  loadGamification();

  // Xem trước hiệu ứng ngọn lửa streak từ Công cụ kiểm thử (Developer Sandbox) — chỉ hiển
  // thị hiệu ứng, KHÔNG đụng gì tới dữ liệu XP/streak thật của tài khoản đang đăng nhập.
  const previewStreak = new URLSearchParams(location.search).get('preview_streak');
  if (previewStreak && STREAK_TIER_STYLE[previewStreak]) {
    setTimeout(() => showStreakFireEffect(parseInt(previewStreak, 10)), 500);
    const cleanUrl = location.pathname;
    history.replaceState({}, '', cleanUrl);  // xoá query khỏi URL để tải lại trang không bắn lại
  }
};
</script>
</body>
</html>
'''

# ==========================================
# 3. BIẾN SECURITY_HTML (GIAO DIỆN BÁO CÁO BẢO MẬT)
# ==========================================
SECURITY_HTML = r'''
<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Bảo mật ứng dụng StudyMate AI với HTTPS</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script>tailwind.config = { darkMode: 'class' };</script>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;600;700&display=swap');
        body { font-family: 'Inter', sans-serif; background-color: #f8fafc; color: #1e293b; }
        .chart-container { position: relative; width: 100%; max-width: 700px; margin: 0 auto; height: 350px; max-height: 400px; }
        .glass-card { background: rgba(255, 255, 255, 0.9); backdrop-filter: blur(10px); border: 1px solid rgba(226, 232, 240, 0.8); }
        .secure-border { border-left: 4px solid #10b981; }
        .step-circle { width: 32px; height: 32px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-weight: bold; }
    </style>
</head>
<body class="antialiased">

    <nav class="sticky top-0 z-50 bg-white/80 backdrop-blur-md border-b border-gray-200">
        <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
            <div class="flex justify-between h-16 items-center">
                <div class="flex items-center gap-2">
                    <div class="p-2 bg-blue-600 rounded-lg text-white">
                        <span class="text-xl">🛡️</span>
                    </div>
                    <span class="font-bold text-xl tracking-tight">Security Architect</span>
                </div>
                <div class="hidden md:flex space-x-8 text-sm font-medium">
                    <a href="#analysis" class="hover:text-blue-600 transition">Phân tích App</a>
                    <a href="#proxy" class="hover:text-blue-600 transition">Reverse Proxy</a>
                    <a href="#mixed-content" class="hover:text-blue-600 transition">Mixed Content</a>
                    <a href="#verification" class="hover:text-blue-600 transition">Kiểm tra</a>
                </div>
                <a href="/" class="text-sm font-medium text-blue-600 hover:underline">← Về StudyMate</a>
            </div>
        </div>
    </nav>

    <main class="max-w-7xl mx-auto px-4 py-8 sm:px-6 lg:px-8">

        <header class="mb-12 text-center">
            <h1 class="text-4xl font-extrabold text-gray-900 mb-4">Nâng cấp Bảo mật cho StudyMate AI</h1>
            <p class="text-lg text-gray-600 max-w-3xl mx-auto">
                Làm thế nào để đưa ứng dụng <code class="bg-gray-200 px-2 py-1 rounded">app.py</code> từ môi trường Local lên môi trường Web Secured (HTTPS)
                với biểu tượng ổ khóa an toàn mà không cần thay đổi logic Flask.
            </p>
        </header>

        <section id="analysis" class="mb-16">
            <div class="bg-blue-50 p-6 rounded-2xl mb-8">
                <h2 class="text-2xl font-bold mb-3 flex items-center gap-2">
                    <span>🔍</span> 1. Phân tích Hiện trạng app.py
                </h2>
                <p class="text-gray-700">
                    Ứng dụng của bạn hiện đang chạy trên cổng <code class="font-mono text-blue-700">5000</code> qua giao thức HTTP.
                    Mặc dù code frontend sử dụng đường dẫn tương đối (<code class="font-mono">fetch('/api/chat')</code>), nhưng để đạt được trạng thái
                    "Connection is secure", chúng ta cần một lớp bao bọc bên ngoài để mã hóa dữ liệu.
                </p>
            </div>

            <div class="grid grid-cols-1 md:grid-cols-2 gap-8 items-center">
                <div class="glass-card p-6 rounded-2xl shadow-sm border-t-4 border-blue-500">
                    <h3 class="font-bold text-lg mb-4">So sánh Chỉ số Bảo mật</h3>
                    <div class="chart-container">
                        <canvas id="securityRadar"></canvas>
                    </div>
                </div>
                <div class="space-y-4">
                    <div class="glass-card p-5 rounded-xl secure-border">
                        <h4 class="font-semibold text-emerald-700">✅ Điểm mạnh hiện tại</h4>
                        <ul class="mt-2 text-sm space-y-2">
                            <li>• Sử dụng <strong>Relative Paths</strong> trong JS giúp tránh lỗi Mixed Content cơ bản.</li>
                            <li>• API Endpoint tách biệt rõ ràng (/api/chat).</li>
                            <li>• Frontend Single-page dễ dàng triển khai qua Proxy.</li>
                        </ul>
                    </div>
                    <div class="glass-card p-5 rounded-xl border-left-4 border-red-500" style="border-left: 4px solid #ef4444;">
                        <h4 class="font-semibold text-red-700">❌ Điểm cần nâng cấp</h4>
                        <ul class="mt-2 text-sm space-y-2">
                            <li>• Dữ liệu gửi lên API chưa được mã hóa trên đường truyền.</li>
                            <li>• Thiếu chứng chỉ SSL/TLS hợp lệ (Nguyên nhân mất biểu tượng ổ khóa).</li>
                            <li>• Flask Server không nên tiếp xúc trực tiếp với Internet.</li>
                        </ul>
                    </div>
                </div>
            </div>
        </section>

        <section id="proxy" class="mb-16">
            <div class="bg-indigo-50 p-6 rounded-2xl mb-8">
                <h2 class="text-2xl font-bold mb-3 flex items-center gap-2">
                    <span>🌐</span> 2. Giải pháp Reverse Proxy (Nginx)
                </h2>
                <p class="text-gray-700">
                    Để giữ nguyên <code class="font-mono">app.py</code>, chúng ta sử dụng một "người đại diện" (Reverse Proxy).
                    Nginx sẽ đón nhận kết nối HTTPS (cổng 443), giải mã nó, rồi mới gửi yêu cầu tới Flask qua HTTP (cổng 5000) ở mạng nội bộ.
                </p>
            </div>

            <div class="grid grid-cols-1 lg:grid-cols-3 gap-6">
                <div class="lg:col-span-2 glass-card p-6 rounded-2xl">
                    <h3 class="font-bold mb-4">Mô hình Luồng dữ liệu Bảo mật</h3>
                    <div class="flex flex-col space-y-4">
                        <div class="flex items-center justify-between p-4 bg-white border rounded-xl">
                            <div class="flex items-center gap-3">
                                <span class="p-2 bg-gray-100 rounded">💻</span>
                                <div>
                                    <div class="font-bold text-sm text-emerald-600 uppercase tracking-wider">Trình duyệt (Client)</div>
                                    <div class="text-xs text-gray-500">Yêu cầu qua HTTPS (Port 443)</div>
                                </div>
                            </div>
                            <span class="text-emerald-500">🔒 Kết nối Bảo mật</span>
                        </div>
                        <div class="flex justify-center py-2">
                            <span class="text-2xl">⬇️</span>
                        </div>
                        <div class="flex items-center justify-between p-4 bg-blue-600 text-white rounded-xl shadow-lg">
                            <div class="flex items-center gap-3">
                                <span class="p-2 bg-white/20 rounded">🏢</span>
                                <div>
                                    <div class="font-bold text-sm uppercase tracking-wider">Nginx Reverse Proxy</div>
                                    <div class="text-xs text-blue-100">Xử lý chứng chỉ SSL/TLS</div>
                                </div>
                            </div>
                            <span class="text-xs bg-emerald-500 px-2 py-1 rounded">SSL Termination</span>
                        </div>
                        <div class="flex justify-center py-2">
                            <span class="text-2xl">⬇️</span>
                        </div>
                        <div class="flex items-center justify-between p-4 bg-gray-800 text-gray-300 rounded-xl">
                            <div class="flex items-center gap-3">
                                <span class="p-2 bg-white/10 rounded">🐍</span>
                                <div>
                                    <div class="font-bold text-sm uppercase tracking-wider">StudyMate Flask App</div>
                                    <div class="text-xs text-gray-400">Chạy tại Localhost:5000</div>
                                </div>
                            </div>
                            <span class="text-xs border border-gray-600 px-2 py-1 rounded">Không đổi Code</span>
                        </div>
                    </div>
                </div>
                <div class="bg-gray-900 rounded-2xl p-6 text-white overflow-hidden relative">
                    <div class="absolute top-0 right-0 p-4 opacity-10 text-6xl">⚙️</div>
                    <h3 class="font-bold mb-4 text-blue-400">Cấu hình Nginx gợi ý</h3>
                    <pre class="text-xs font-mono leading-relaxed text-gray-400 overflow-x-auto">
server {
    listen 443 ssl;
    server_name study-mate.ai;

    ssl_certificate /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;
    }
}</pre>
                    <p class="mt-4 text-xs text-gray-500 italic">* proxy_buffering off giúp phản hồi dạng stream (SSE) tới trình duyệt ngay lập tức thay vì bị Nginx đệm lại.</p>
                </div>
            </div>
        </section>

        <section id="mixed-content" class="mb-16">
            <div class="bg-emerald-50 p-6 rounded-2xl mb-8">
                <h2 class="text-2xl font-bold mb-3 flex items-center gap-2">
                    <span>🛡️</span> 3. Ngăn ngừa lỗi "Mixed Content"
                </h2>
                <p class="text-gray-700">
                    Đây là lý do chính khiến biểu tượng ổ khóa biến mất hoặc có dấu chấm than. Trình duyệt chặn các yêu cầu không an toàn (HTTP) từ một trang an toàn (HTTPS).
                </p>
            </div>

            <div class="grid grid-cols-1 md:grid-cols-2 gap-8">
                <div class="space-y-6">
                    <div class="flex gap-4">
                        <div class="step-circle bg-emerald-600 text-white flex-shrink-0">1</div>
                        <div>
                            <h4 class="font-bold">Sử dụng URL tương đối</h4>
                            <p class="text-sm text-gray-600">May mắn là trong code của bạn, <code>fetch('/api/chat')</code> đã là đường dẫn tương đối. Nó sẽ tự động dùng HTTPS nếu trang web đang chạy trên HTTPS.</p>
                        </div>
                    </div>
                    <div class="flex gap-4">
                        <div class="step-circle bg-emerald-600 text-white flex-shrink-0">2</div>
                        <div>
                            <h4 class="font-bold">Cập nhật Resource từ CDN</h4>
                            <p class="text-sm text-gray-600">Đảm bảo tất cả các script (Tailwind, FontAwesome, Marked) đều bắt đầu bằng <code>https://</code>. Code của bạn đã tuân thủ điều này.</p>
                        </div>
                    </div>
                    <div class="flex gap-4">
                        <div class="step-circle bg-emerald-600 text-white flex-shrink-0">3</div>
                        <div>
                            <h4 class="font-bold">Content Security Policy (CSP)</h4>
                            <p class="text-sm text-gray-600">Thêm thẻ meta để tự động nâng cấp các yêu cầu không an toàn: <br>
                                <code class="text-xs bg-gray-100 p-1 block mt-1">Content-Security-Policy: upgrade-insecure-requests</code>
                            </p>
                        </div>
                    </div>
                </div>
                <div class="glass-card p-6 rounded-2xl flex flex-col justify-center items-center text-center">
                    <div class="text-5xl mb-4">🔐</div>
                    <h3 class="text-xl font-bold text-emerald-600">Kết quả mong đợi</h3>
                    <p class="text-gray-500 mt-2 italic">Sau khi cấu hình HTTPS + Proxy đúng cách</p>
                    <div class="mt-4 p-4 border-2 border-emerald-500 bg-emerald-50 rounded-lg inline-flex items-center gap-2">
                        <span class="text-emerald-600 font-bold">🔒 Connection is secure</span>
                    </div>
                    <p class="mt-4 text-xs text-gray-400">Chứng chỉ SSL hợp lệ (Let's Encrypt / ZeroSSL) đã được tích hợp qua Proxy.</p>
                </div>
            </div>
        </section>

        <section id="verification" class="mb-16">
            <h2 class="text-2xl font-bold mb-8 text-center">Bảng điều khiển Trạng thái Bảo mật (Mô phỏng)</h2>
            <div class="grid grid-cols-2 md:grid-cols-4 gap-4">
                <div class="glass-card p-4 rounded-xl text-center">
                    <div class="text-sm text-gray-500 mb-1">Giao thức</div>
                    <div class="text-xl font-bold text-emerald-600">HTTPS (TLS 1.3)</div>
                </div>
                <div class="glass-card p-4 rounded-xl text-center">
                    <div class="text-sm text-gray-500 mb-1">SSL Certificate</div>
                    <div class="text-xl font-bold text-emerald-600">Hợp lệ (90 ngày)</div>
                </div>
                <div class="glass-card p-4 rounded-xl text-center">
                    <div class="text-sm text-gray-500 mb-1">Mixed Content</div>
                    <div class="text-xl font-bold text-emerald-600">Không phát hiện</div>
                </div>
                <div class="glass-card p-4 rounded-xl text-center">
                    <div class="text-sm text-gray-500 mb-1">Điểm bảo mật</div>
                    <div class="text-xl font-bold text-emerald-600">A+</div>
                </div>
            </div>
        </section>

    </main>

    <footer class="bg-gray-900 text-gray-400 py-12 px-4">
        <div class="max-w-7xl mx-auto text-center">
            <p class="mb-4">Báo cáo được thực hiện cho dự án StudyMate AI</p>
            <div class="flex justify-center gap-6 text-sm">
                <span>Tình trạng mã nguồn: <span class="text-emerald-500">Giữ nguyên logic gốc</span></span>
                <span>Tiêu chuẩn: <span class="text-blue-500">Web Secured 2024</span></span>
            </div>
        </div>
    </footer>

    <script>
        const ctx = document.getElementById('securityRadar').getContext('2d');
        new Chart(ctx, {
            type: 'radar',
            data: {
                labels: ['Mã hóa dữ liệu', 'Danh tính (SSL)', 'Chống Sniffing', 'Tin cậy trình duyệt', 'Chống Mixed Content'],
                datasets: [{
                    label: 'HTTP (Hiện tại)',
                    data: [10, 5, 10, 20, 90],
                    fill: true,
                    backgroundColor: 'rgba(239, 68, 68, 0.2)',
                    borderColor: 'rgb(239, 68, 68)',
                    pointBackgroundColor: 'rgb(239, 68, 68)',
                }, {
                    label: 'HTTPS + Proxy (Đề xuất)',
                    data: [95, 100, 95, 95, 98],
                    fill: true,
                    backgroundColor: 'rgba(16, 185, 129, 0.2)',
                    borderColor: 'rgb(16, 185, 129)',
                    pointBackgroundColor: 'rgb(16, 185, 129)',
                }]
            },
            options: {
                maintainAspectRatio: false,
                scales: {
                    r: {
                        angleLines: { display: true },
                        suggestedMin: 0,
                        suggestedMax: 100
                    }
                },
                plugins: {
                    legend: { position: 'bottom' }
                }
            }
        });

        document.querySelectorAll('a[href^="#"]').forEach(anchor => {
            anchor.addEventListener('click', function (e) {
                e.preventDefault();
                document.querySelector(this.getAttribute('href')).scrollIntoView({
                    behavior: 'smooth'
                });
            });
        });
    </script>
</body>
</html>
'''

# ==========================================
# 3.1 BIẾN DEV_STATS_HTML (TRANG THỐNG KÊ — CHỈ DÀNH CHO DEVELOPER)
# ==========================================
DEV_STATS_HTML = r'''
<!DOCTYPE html>
<html lang="vi" class="light">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Thống kê sử dụng — StudyMate AI Max</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script>tailwind.config = { darkMode: 'class' };</script>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.6.0/css/all.min.css">
  <style>
    body { font-family: 'Segoe UI', system-ui, sans-serif; }
    .bar-track { display: flex; align-items: flex-end; gap: 6px; height: 140px; }
    .bar { flex: 1; background: linear-gradient(180deg, #6366f1, #4338ca); border-radius: 6px 6px 2px 2px; min-height: 3px; position: relative; }
    .dark .bar { background: linear-gradient(180deg, #818cf8, #4f46e5); }
    .bar:hover::after {
      content: attr(data-tip); position: absolute; bottom: 100%; left: 50%; transform: translateX(-50%);
      background: #111827; color: #fff; font-size: 11px; padding: 3px 7px; border-radius: 6px; white-space: nowrap; margin-bottom: 4px;
    }
    .progress-track { background: #e5e7eb; border-radius: 999px; height: 8px; overflow: hidden; }
    .dark .progress-track { background: #374151; }
    .progress-fill { background: linear-gradient(90deg, #4f46e5, #6366f1); height: 100%; border-radius: 999px; }
  </style>
</head>
<body class="min-h-screen bg-gray-50 dark:bg-[#131313] text-gray-800 dark:text-gray-100 transition-colors">

  <header class="sticky top-0 z-20 bg-white/80 dark:bg-[#171717]/80 backdrop-blur-xl border-b border-gray-200 dark:border-gray-800">
    <div class="max-w-6xl mx-auto px-4 sm:px-6 py-3.5 flex items-center gap-3">
      <div class="w-9 h-9 rounded-xl bg-gradient-to-br from-indigo-600 to-blue-600 flex items-center justify-center text-white font-bold">S</div>
      <div class="flex-1 min-w-0">
        <h1 class="font-bold text-base sm:text-lg leading-tight">Thống kê sử dụng</h1>
        <p class="text-xs text-gray-400">Khu vực Developer — chỉ tài khoản có quyền developer mới xem được</p>
      </div>
      <button onclick="document.documentElement.classList.toggle('dark')" class="w-9 h-9 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800 flex items-center justify-center text-gray-500 dark:text-gray-300">
        <i class="fas fa-moon"></i>
      </button>
      <a href="{{ url_for('developer_lab') }}" class="text-sm font-semibold px-3.5 py-2 rounded-xl bg-pink-50 dark:bg-pink-900/30 text-pink-600 dark:text-pink-400 hover:bg-pink-100 dark:hover:bg-pink-900/50 whitespace-nowrap">
        <i class="fas fa-flask mr-1"></i> Dev Lab
      </a>
      <a href="{{ url_for('home') }}" class="text-sm font-semibold px-3.5 py-2 rounded-xl bg-gray-100 dark:bg-gray-800 hover:bg-gray-200 dark:hover:bg-gray-700 whitespace-nowrap">
        <i class="fas fa-arrow-left mr-1"></i> Về trang chat
      </a>
    </div>
  </header>

  <main class="max-w-6xl mx-auto px-4 sm:px-6 py-6 space-y-6">

    {% with flashed = get_flashed_messages() %}
    {% if flashed %}
    <div class="space-y-2">
      {% for msg in flashed %}
      <div class="px-4 py-3 rounded-xl bg-indigo-50 dark:bg-indigo-900/30 border border-indigo-200 dark:border-indigo-800 text-indigo-700 dark:text-indigo-300 text-sm flex items-center gap-2">
        <i class="fas fa-circle-info"></i> {{ msg }}
      </div>
      {% endfor %}
    </div>
    {% endif %}
    {% endwith %}

    <!-- Thẻ tổng quan -->
    <div class="grid grid-cols-2 lg:grid-cols-3 xl:grid-cols-5 gap-4">
      <div class="bg-white dark:bg-[#1c1c1c] rounded-2xl border border-gray-100 dark:border-gray-800 p-5 shadow-sm">
        <div class="flex items-center justify-between">
          <span class="text-xs font-semibold text-gray-400 uppercase">Tổng tài khoản</span>
          <i class="fas fa-users text-indigo-500"></i>
        </div>
        <p class="text-3xl font-extrabold mt-2">{{ total_users }}</p>
        <p class="text-xs text-gray-400 mt-1">{{ new_users_7d }} tài khoản mới trong 7 ngày qua</p>
      </div>
      <div class="bg-white dark:bg-[#1c1c1c] rounded-2xl border border-gray-100 dark:border-gray-800 p-5 shadow-sm">
        <div class="flex items-center justify-between">
          <span class="text-xs font-semibold text-gray-400 uppercase">Lượt hỏi AI (tổng)</span>
          <i class="fas fa-comments text-blue-500"></i>
        </div>
        <p class="text-3xl font-extrabold mt-2">{{ total_usage }}</p>
        <p class="text-xs text-gray-400 mt-1">{{ usage_today }} lượt hôm nay</p>
      </div>
      <div class="bg-white dark:bg-[#1c1c1c] rounded-2xl border border-gray-100 dark:border-gray-800 p-5 shadow-sm">
        <div class="flex items-center justify-between">
          <span class="text-xs font-semibold text-gray-400 uppercase">7 ngày qua</span>
          <i class="fas fa-chart-line text-emerald-500"></i>
        </div>
        <p class="text-3xl font-extrabold mt-2">{{ usage_7d }}</p>
        <p class="text-xs text-gray-400 mt-1">Trung bình {{ avg_per_day_7d }} lượt / ngày</p>
      </div>
      <div class="bg-white dark:bg-[#1c1c1c] rounded-2xl border border-gray-100 dark:border-gray-800 p-5 shadow-sm">
        <div class="flex items-center justify-between">
          <span class="text-xs font-semibold text-gray-400 uppercase">Tỉ lệ lỗi</span>
          <i class="fas fa-triangle-exclamation text-amber-500"></i>
        </div>
        <p class="text-3xl font-extrabold mt-2">{{ error_rate }}%</p>
        <p class="text-xs text-gray-400 mt-1">{{ error_count }} lượt gặp lỗi / {{ total_usage }} lượt</p>
      </div>
      <a href="#issue-reports" class="bg-white dark:bg-[#1c1c1c] rounded-2xl border border-gray-100 dark:border-gray-800 p-5 shadow-sm hover:border-red-300 dark:hover:border-red-800 transition-colors">
        <div class="flex items-center justify-between">
          <span class="text-xs font-semibold text-gray-400 uppercase">Báo lỗi đang mở</span>
          <i class="fas fa-flag text-red-500"></i>
        </div>
        <p class="text-3xl font-extrabold mt-2">{{ open_issues_count }}</p>
        <p class="text-xs text-gray-400 mt-1">Bấm để xem chi tiết ↓</p>
      </a>
    </div>

    <!-- Biểu đồ 14 ngày gần nhất -->
    <div class="bg-white dark:bg-[#1c1c1c] rounded-2xl border border-gray-100 dark:border-gray-800 p-5 shadow-sm">
      <h2 class="font-bold mb-4 flex items-center gap-2"><i class="fas fa-chart-column text-indigo-500"></i> Lượt sử dụng theo ngày (14 ngày gần nhất)</h2>
      {% if daily_counts %}
      <div class="bar-track">
        {% for d in daily_counts %}
        <div class="bar" style="height: {{ d.height }}%;" data-tip="{{ d.label }}: {{ d.count }} lượt"></div>
        {% endfor %}
      </div>
      <div class="flex justify-between text-[10px] text-gray-400 mt-2">
        <span>{{ daily_counts[0].label }}</span>
        <span>{{ daily_counts[-1].label }}</span>
      </div>
      {% else %}
      <p class="text-sm text-gray-400">Chưa có dữ liệu sử dụng.</p>
      {% endif %}
    </div>

    <div class="grid lg:grid-cols-2 gap-6">
      <!-- Theo môn học -->
      <div class="bg-white dark:bg-[#1c1c1c] rounded-2xl border border-gray-100 dark:border-gray-800 p-5 shadow-sm">
        <h2 class="font-bold mb-4 flex items-center gap-2"><i class="fas fa-book text-blue-500"></i> Theo môn học</h2>
        <div class="space-y-3">
          {% for s in subject_stats %}
          <div>
            <div class="flex justify-between text-sm mb-1"><span>{{ s.subject }}</span><span class="text-gray-400">{{ s.count }} ({{ s.pct }}%)</span></div>
            <div class="progress-track"><div class="progress-fill" style="width: {{ s.pct }}%;"></div></div>
          </div>
          {% else %}
          <p class="text-sm text-gray-400">Chưa có dữ liệu.</p>
          {% endfor %}
        </div>
      </div>

      <!-- Theo chế độ học tập -->
      <div class="bg-white dark:bg-[#1c1c1c] rounded-2xl border border-gray-100 dark:border-gray-800 p-5 shadow-sm">
        <h2 class="font-bold mb-4 flex items-center gap-2"><i class="fas fa-sliders text-emerald-500"></i> Theo chế độ học tập</h2>
        <div class="space-y-3">
          {% for m in mode_stats %}
          <div>
            <div class="flex justify-between text-sm mb-1"><span>{{ m.mode }}</span><span class="text-gray-400">{{ m.count }} ({{ m.pct }}%)</span></div>
            <div class="progress-track"><div class="progress-fill" style="width: {{ m.pct }}%;"></div></div>
          </div>
          {% else %}
          <p class="text-sm text-gray-400">Chưa có dữ liệu.</p>
          {% endfor %}
        </div>
      </div>
    </div>

    <!-- Người dùng hoạt động nhiều nhất -->
    <div class="bg-white dark:bg-[#1c1c1c] rounded-2xl border border-gray-100 dark:border-gray-800 p-5 shadow-sm overflow-x-auto">
      <h2 class="font-bold mb-4 flex items-center gap-2"><i class="fas fa-ranking-star text-amber-500"></i> Người dùng hoạt động nhiều nhất</h2>
      <table class="w-full text-sm min-w-[420px]">
        <thead>
          <tr class="text-left text-gray-400 text-xs uppercase border-b border-gray-100 dark:border-gray-800">
            <th class="py-2 pr-3">Người dùng</th>
            <th class="py-2 pr-3">Vai trò</th>
            <th class="py-2 pr-3">Số lượt hỏi</th>
            <th class="py-2">Lần dùng gần nhất</th>
          </tr>
        </thead>
        <tbody>
          {% for u in top_users %}
          <tr class="border-b border-gray-50 dark:border-gray-900">
            <td class="py-2.5 pr-3 font-medium flex items-center gap-2">
              <div class="w-6 h-6 rounded-full bg-indigo-100 dark:bg-indigo-900 text-indigo-600 dark:text-indigo-300 flex items-center justify-center text-xs font-bold">{{ u.username[0]|upper }}</div>
              {{ u.username }}
            </td>
            <td class="py-2.5 pr-3">
              {% if u.role == 'developer' %}
                <span class="text-[11px] font-semibold px-2 py-0.5 rounded-full bg-indigo-100 dark:bg-indigo-900 text-indigo-600 dark:text-indigo-300">developer</span>
              {% else %}
                <span class="text-[11px] font-semibold px-2 py-0.5 rounded-full bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400">user</span>
              {% endif %}
            </td>
            <td class="py-2.5 pr-3">{{ u.usage_count }}</td>
            <td class="py-2.5 text-gray-400">{{ u.last_used or '—' }}</td>
          </tr>
          {% else %}
          <tr><td colspan="4" class="py-4 text-center text-gray-400">Chưa có lượt sử dụng nào.</td></tr>
          {% endfor %}
        </tbody>
      </table>
    </div>

    <!-- Quản lý hệ thống -->
    <div class="grid lg:grid-cols-3 gap-6">
      <div class="bg-white dark:bg-[#1c1c1c] rounded-2xl border border-gray-100 dark:border-gray-800 p-5 shadow-sm">
        <h2 class="font-bold mb-1 flex items-center gap-2"><i class="fas fa-bullhorn text-amber-500"></i> Thông báo hệ thống</h2>
        <p class="text-xs text-gray-400 mb-3">Hiển thị dạng banner cho tất cả người dùng ngay khi vào trang chat. Để trống rồi bấm Lưu để xoá thông báo.</p>
        <form method="POST" action="{{ url_for('developer_set_banner') }}" class="space-y-3">
          <textarea name="banner_message" rows="2" maxlength="300" placeholder="VD: Server sẽ bảo trì lúc 22h tối nay..."
            class="w-full px-3 py-2.5 rounded-xl bg-gray-100 dark:bg-gray-800 border-0 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500 dark:text-white">{{ banner_message }}</textarea>
          <button type="submit" class="px-4 py-2 rounded-xl bg-indigo-600 hover:bg-indigo-700 text-white text-sm font-semibold">Lưu thông báo</button>
        </form>
      </div>

      <div class="bg-white dark:bg-[#1c1c1c] rounded-2xl border border-gray-100 dark:border-gray-800 p-5 shadow-sm">
        <h2 class="font-bold mb-1 flex items-center gap-2"><i class="fas fa-toggle-on text-emerald-500"></i> Đăng nhập bằng Google</h2>
        <p class="text-xs text-gray-400 mb-3">
          Cấu hình trong .env: <strong>{{ "đã thiết lập" if google_configured else "chưa thiết lập" }}</strong>.
          {% if not google_configured %}Cần đặt GOOGLE_CLIENT_ID/SECRET trước khi có thể bật.{% endif %}
        </p>
        <form method="POST" action="{{ url_for('developer_toggle_google_login') }}" class="flex flex-wrap gap-2">
          <button name="value" value="" type="submit" class="px-3 py-2 rounded-xl text-sm font-semibold {{ 'bg-gray-800 text-white' if google_override == '' else 'bg-gray-100 dark:bg-gray-800 text-gray-500' }}">Theo .env</button>
          <button name="value" value="on" type="submit" {{ 'disabled' if not google_configured else '' }} class="px-3 py-2 rounded-xl text-sm font-semibold disabled:opacity-40 {{ 'bg-emerald-600 text-white' if google_override == 'on' else 'bg-gray-100 dark:bg-gray-800 text-gray-500' }}">Bật</button>
          <button name="value" value="off" type="submit" class="px-3 py-2 rounded-xl text-sm font-semibold {{ 'bg-red-600 text-white' if google_override == 'off' else 'bg-gray-100 dark:bg-gray-800 text-gray-500' }}">Tắt</button>
        </form>
      </div>

      <div class="bg-white dark:bg-[#1c1c1c] rounded-2xl border border-gray-100 dark:border-gray-800 p-5 shadow-sm">
        <h2 class="font-bold mb-1 flex items-center gap-2"><i class="fas fa-user-secret text-purple-500"></i> Đăng nhập khách</h2>
        <p class="text-xs text-gray-400 mb-3">
          "Dùng thử ngay, không cần đăng ký" ở trang đăng nhập. Mặc định BẬT, không cần cấu hình gì thêm.
        </p>
        <form method="POST" action="{{ url_for('developer_toggle_guest_login') }}" class="flex flex-wrap gap-2">
          <button name="value" value="on" type="submit" class="px-3 py-2 rounded-xl text-sm font-semibold {{ 'bg-emerald-600 text-white' if guest_login_on else 'bg-gray-100 dark:bg-gray-800 text-gray-500' }}">Bật</button>
          <button name="value" value="off" type="submit" class="px-3 py-2 rounded-xl text-sm font-semibold {{ 'bg-red-600 text-white' if not guest_login_on else 'bg-gray-100 dark:bg-gray-800 text-gray-500' }}">Tắt</button>
        </form>
      </div>
    </div>

    <!-- Công cụ kiểm thử (Sandbox) -->
    <div class="bg-white dark:bg-[#1c1c1c] rounded-2xl border border-gray-100 dark:border-gray-800 p-5 shadow-sm">
      <h2 class="font-bold mb-1 flex items-center gap-2"><i class="fas fa-flask text-pink-500"></i> Công cụ kiểm thử (Sandbox)</h2>
      <p class="text-xs text-gray-400 mb-4">Chỉnh trực tiếp XP/streak của 1 tài khoản để test giao diện (không cần đợi dùng thật nhiều ngày), và xem trước hiệu ứng ngọn lửa streak ở từng mốc.</p>

      <div class="grid md:grid-cols-2 gap-6">
        <div>
          <p class="text-xs font-semibold text-gray-400 uppercase mb-2">Chỉnh dữ liệu XP / Streak</p>
          <form method="POST" action="{{ url_for('developer_sandbox_user_stats') }}" class="space-y-2.5">
            <select name="user_id" class="w-full px-3 py-2 rounded-xl bg-gray-100 dark:bg-gray-800 border-0 text-sm focus:outline-none focus:ring-2 focus:ring-pink-500 dark:text-white">
              {% for u in all_users %}
              <option value="{{ u.id }}" {{ 'selected' if u.id == current_user_id_val else '' }}>{{ u.username }}{{ ' (tôi)' if u.id == current_user_id_val else '' }}</option>
              {% endfor %}
            </select>
            <div class="grid grid-cols-3 gap-2">
              <div>
                <label class="text-[10px] text-gray-400">XP</label>
                <input type="number" name="xp" min="0" value="0" class="w-full px-2.5 py-2 rounded-lg bg-gray-100 dark:bg-gray-800 border-0 text-sm focus:outline-none focus:ring-2 focus:ring-pink-500 dark:text-white">
              </div>
              <div>
                <label class="text-[10px] text-gray-400">Streak hiện tại</label>
                <input type="number" name="streak_days" min="0" value="0" class="w-full px-2.5 py-2 rounded-lg bg-gray-100 dark:bg-gray-800 border-0 text-sm focus:outline-none focus:ring-2 focus:ring-pink-500 dark:text-white">
              </div>
              <div>
                <label class="text-[10px] text-gray-400">Streak dài nhất</label>
                <input type="number" name="longest_streak" min="0" value="0" class="w-full px-2.5 py-2 rounded-lg bg-gray-100 dark:bg-gray-800 border-0 text-sm focus:outline-none focus:ring-2 focus:ring-pink-500 dark:text-white">
              </div>
            </div>
            <button type="submit" class="w-full px-4 py-2 rounded-xl bg-pink-600 hover:bg-pink-700 text-white text-sm font-semibold">Áp dụng</button>
          </form>
        </div>

        <div>
          <p class="text-xs font-semibold text-gray-400 uppercase mb-2">Xem trước hiệu ứng ngọn lửa (chỉ hiển thị, không lưu dữ liệu)</p>
          <div class="flex flex-wrap gap-2">
            {% for milestone in [3, 10, 30, 100, 200, 300, 500, 1000] %}
            <a href="{{ url_for('home') }}?preview_streak={{ milestone }}" target="_blank" rel="noopener"
              class="px-3 py-2 rounded-xl bg-gray-100 dark:bg-gray-800 hover:bg-gray-200 dark:hover:bg-gray-700 text-sm font-medium">🔥 {{ milestone }}</a>
            {% endfor %}
          </div>
          <p class="text-xs text-gray-400 mt-2">Mở trang chat ở tab mới và tự bắn hiệu ứng ngay khi tải trang xong.</p>
        </div>
      </div>
    </div>

    <!-- Đơn nâng cấp gói (thanh toán) -->
    <div class="bg-white dark:bg-[#1c1c1c] rounded-2xl border border-gray-100 dark:border-gray-800 p-5 shadow-sm">
      <div class="flex flex-wrap items-center justify-between gap-3 mb-1">
        <h2 class="font-bold flex items-center gap-2"><i class="fas fa-credit-card text-indigo-500"></i> Thanh toán nâng cấp gói</h2>
        <div class="flex items-center gap-2 text-xs">
          <span class="px-2 py-1 rounded-full {{ 'bg-emerald-100 text-emerald-600 dark:bg-emerald-900 dark:text-emerald-300' if vnpay_enabled else 'bg-gray-100 text-gray-400 dark:bg-gray-800' }}">
            <i class="fas fa-circle text-[6px] mr-1"></i>VNPAY {{ 'BẬT' if vnpay_enabled else 'TẮT' }}
          </span>
          <span class="px-2 py-1 rounded-full {{ 'bg-emerald-100 text-emerald-600 dark:bg-emerald-900 dark:text-emerald-300' if bank_transfer_enabled else 'bg-gray-100 text-gray-400 dark:bg-gray-800' }}">
            <i class="fas fa-circle text-[6px] mr-1"></i>VietQR {{ 'BẬT' if bank_transfer_enabled else 'TẮT' }}
          </span>
        </div>
      </div>
      <p class="text-xs text-gray-400 mb-4">
        Giá gói: Premium {{ plan_pricing.premium|vnd }}₫ · Max {{ plan_pricing.max|vnd }}₫ · Đã thu (đã xác nhận): <strong>{{ total_revenue|vnd }}₫</strong>.
        Đơn qua VNPAY tự động kích hoạt gói (không cần bấm gì); đơn Chuyển khoản VietQR cần Admin bấm xác nhận thủ công sau khi kiểm tra đã nhận được tiền.
      </p>

      {% if pending_orders %}
      <div class="mb-4">
        <div class="text-xs font-semibold text-amber-600 dark:text-amber-400 uppercase mb-2">
          <i class="fas fa-clock mr-1"></i> Đang chờ xác nhận ({{ pending_orders|length }})
        </div>
        <div class="space-y-2">
          {% for o in pending_orders %}
          <div class="flex flex-wrap items-center justify-between gap-2 border border-amber-200 dark:border-amber-900/50 bg-amber-50 dark:bg-amber-900/10 rounded-xl px-4 py-2.5 text-sm">
            <div>
              <span class="font-mono font-semibold">{{ o.order_code }}</span>
              — {{ o.username or '—' }} · {{ plan_meta[o.plan].icon }} {{ plan_meta[o.plan].label }} ·
              {{ o.amount|vnd }}₫ ·
              <span class="text-gray-400">{{ 'VietQR' if o.method == 'bank_transfer' else 'VNPAY' }}</span>
              <span class="text-gray-400">· {{ o.created_at[:16].replace('T', ' ') }}</span>
            </div>
            {% if o.method == 'bank_transfer' %}
            <div class="flex items-center gap-1.5">
              <form method="POST" action="{{ url_for('developer_confirm_payment', order_code=o.order_code) }}">
                <button type="submit" class="px-3 py-1.5 rounded-lg bg-emerald-600 hover:bg-emerald-700 text-white text-xs font-semibold">
                  <i class="fas fa-check mr-1"></i>Xác nhận đã nhận tiền
                </button>
              </form>
              <form method="POST" action="{{ url_for('developer_cancel_payment', order_code=o.order_code) }}">
                <button type="submit" class="px-3 py-1.5 rounded-lg bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600 text-xs font-semibold">Huỷ</button>
              </form>
            </div>
            {% else %}
            <span class="text-xs text-gray-400 italic">Chờ VNPAY xác nhận tự động...</span>
            {% endif %}
          </div>
          {% endfor %}
        </div>
      </div>
      {% endif %}

      <div class="text-xs font-semibold text-gray-400 uppercase mb-2">20 đơn gần nhất</div>
      <div class="overflow-x-auto">
        <table class="w-full text-sm min-w-[560px]">
          <thead>
            <tr class="text-left text-gray-400 text-xs uppercase border-b border-gray-100 dark:border-gray-800">
              <th class="py-2 pr-3">Mã đơn</th>
              <th class="py-2 pr-3">Người dùng</th>
              <th class="py-2 pr-3">Gói</th>
              <th class="py-2 pr-3">Số tiền</th>
              <th class="py-2 pr-3">Phương thức</th>
              <th class="py-2 pr-3">Trạng thái</th>
              <th class="py-2">Thời gian</th>
            </tr>
          </thead>
          <tbody>
            {% for o in recent_orders %}
            <tr class="border-b border-gray-50 dark:border-gray-900">
              <td class="py-2 pr-3 font-mono text-xs">{{ o.order_code }}</td>
              <td class="py-2 pr-3">{{ o.username or '—' }}</td>
              <td class="py-2 pr-3">{{ plan_meta[o.plan].icon }} {{ plan_meta[o.plan].label }}</td>
              <td class="py-2 pr-3">{{ o.amount|vnd }}₫</td>
              <td class="py-2 pr-3 text-gray-400">{{ 'VietQR' if o.method == 'bank_transfer' else 'VNPAY' }}</td>
              <td class="py-2 pr-3">
                {% if o.status == 'paid' %}<span class="text-emerald-600 dark:text-emerald-400 font-semibold">Đã thanh toán</span>
                {% elif o.status == 'pending' %}<span class="text-amber-600 dark:text-amber-400 font-semibold">Đang chờ</span>
                {% elif o.status == 'cancelled' %}<span class="text-gray-400">Đã huỷ</span>
                {% else %}<span class="text-red-500">Thất bại</span>{% endif %}
              </td>
              <td class="py-2 text-gray-400">{{ o.created_at[:16].replace('T', ' ') }}</td>
            </tr>
            {% else %}
            <tr><td colspan="7" class="py-4 text-center text-gray-400">Chưa có đơn nào.</td></tr>
            {% endfor %}
          </tbody>
        </table>
      </div>
    </div>

    <!-- Báo cáo lỗi từ học sinh -->
    <div id="issue-reports" class="bg-white dark:bg-[#1c1c1c] rounded-2xl border border-gray-100 dark:border-gray-800 p-5 shadow-sm scroll-mt-20">
      <div class="flex items-center justify-between mb-1 flex-wrap gap-2">
        <h2 class="font-bold flex items-center gap-2"><i class="fas fa-flag text-red-500"></i> Báo cáo lỗi từ học sinh</h2>
        <span class="text-xs text-gray-400">{{ open_issues_count }} đang mở / {{ issue_reports|length }} hiển thị</span>
      </div>
      <p class="text-xs text-gray-400 mb-4">Học sinh bấm "Báo lỗi" dưới 1 câu trả lời trong khung chat để gửi báo cáo về đây.</p>
      <div class="space-y-3">
        {% for r in issue_reports %}
        <div class="border border-gray-100 dark:border-gray-800 rounded-xl p-4 {{ 'opacity-50' if r.status == 'resolved' else '' }}" data-issue-id="{{ r.id }}">
          <div class="flex items-start justify-between gap-3 flex-wrap">
            <div class="min-w-0">
              <div class="flex items-center gap-2 text-xs text-gray-400 mb-1 flex-wrap">
                <span class="font-semibold text-gray-600 dark:text-gray-300">{{ r.username or '—' }}</span>
                <span>•</span><span>{{ r.created_at[:16].replace('T', ' ') }}</span>
                {% if r.status == 'resolved' %}
                <span class="px-1.5 py-0.5 rounded-full bg-emerald-100 dark:bg-emerald-900 text-emerald-600 dark:text-emerald-300 text-[10px] font-semibold uppercase">Đã xử lý</span>
                {% else %}
                <span class="px-1.5 py-0.5 rounded-full bg-red-100 dark:bg-red-900 text-red-600 dark:text-red-300 text-[10px] font-semibold uppercase">Đang mở</span>
                {% endif %}
              </div>
              <p class="text-sm font-medium">{{ r.description }}</p>
              {% if r.message_excerpt %}
              <p class="text-xs text-gray-400 mt-1.5 italic">Liên quan tới câu trả lời: "{{ r.message_excerpt[:160] }}{{ '…' if r.message_excerpt|length > 160 else '' }}"</p>
              {% endif %}
            </div>
            <form method="POST" action="{{ url_for('developer_resolve_issue', issue_id=r.id) }}">
              <button type="submit" class="flex-shrink-0 text-xs font-medium px-3 py-1.5 rounded-lg bg-gray-100 dark:bg-gray-800 hover:bg-gray-200 dark:hover:bg-gray-700 whitespace-nowrap">
                {{ 'Mở lại' if r.status == 'resolved' else 'Đánh dấu đã xử lý' }}
              </button>
            </form>
          </div>
        </div>
        {% else %}
        <p class="text-sm text-gray-400">Chưa có báo cáo lỗi nào. 🎉</p>
        {% endfor %}
      </div>
    </div>

    <!-- Toàn bộ tài khoản -->
    <div class="bg-white dark:bg-[#1c1c1c] rounded-2xl border border-gray-100 dark:border-gray-800 p-5 shadow-sm overflow-x-auto">
      <div class="flex flex-wrap items-center justify-between gap-3 mb-4">
        <h2 class="font-bold flex items-center gap-2"><i class="fas fa-address-card text-gray-500"></i> Toàn bộ tài khoản ({{ total_users }})</h2>
        <div class="flex items-center gap-2">
          <input id="userSearchInput" type="text" placeholder="Tìm theo tên đăng nhập..." oninput="filterUserTable()"
            class="px-3 py-1.5 rounded-lg bg-gray-100 dark:bg-gray-800 border-0 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500 dark:text-white">
          <a href="{{ url_for('developer_export_csv') }}" class="text-xs font-semibold px-3 py-1.5 rounded-lg bg-gray-100 dark:bg-gray-800 hover:bg-gray-200 dark:hover:bg-gray-700 whitespace-nowrap">
            <i class="fas fa-download mr-1"></i> Xuất CSV
          </a>
          {% if is_super_admin %}
          <a href="{{ url_for('developer_audit_log') }}" class="text-xs font-semibold px-3 py-1.5 rounded-lg bg-gray-100 dark:bg-gray-800 hover:bg-gray-200 dark:hover:bg-gray-700 whitespace-nowrap">
            <i class="fas fa-scroll mr-1"></i> Nhật ký hệ thống
          </a>
          {% endif %}
        </div>
      </div>
      <table class="w-full text-sm min-w-[720px]">
        <thead>
          <tr class="text-left text-gray-400 text-xs uppercase border-b border-gray-100 dark:border-gray-800">
            <th class="py-2 pr-3">ID</th>
            <th class="py-2 pr-3">Người dùng</th>
            <th class="py-2 pr-3">Vai trò</th>
            <th class="py-2 pr-3">Gói</th>
            <th class="py-2 pr-3">Ngày tạo</th>
            <th class="py-2 text-right">Hành động</th>
          </tr>
        </thead>
        <tbody id="usersTableBody">
          {% for u in all_users %}
          <tr class="border-b border-gray-50 dark:border-gray-900" data-username="{{ u.username|lower }}">
            <td class="py-2.5 pr-3 text-gray-400">#{{ u.id }}</td>
            <td class="py-2.5 pr-3 font-medium">{{ u.username }}</td>
            <td class="py-2.5 pr-3">
              <span class="text-[11px] font-semibold px-2 py-0.5 rounded-full {{ role_meta[u.role].badge }}">
                {{ role_meta[u.role].icon }} {{ role_meta[u.role].label }}
              </span>
              {% if u.is_locked %}<span class="ml-1 text-[10px] font-semibold px-1.5 py-0.5 rounded bg-red-600 text-white">ĐÃ KHOÁ</span>{% endif %}
            </td>
            <td class="py-2.5 pr-3">
              <span class="text-[11px] font-semibold px-2 py-0.5 rounded-full {{ plan_meta[u.effective_plan].badge }}">
                {{ plan_meta[u.effective_plan].icon }} {{ plan_meta[u.effective_plan].label }}
              </span>
              {% if u.plan_is_role_based %}<span class="ml-1 text-[10px] text-gray-400 italic">(theo vai trò)</span>{% endif %}
            </td>
            <td class="py-2.5 pr-3 text-gray-400">{{ u.created_at[:10] }}</td>
            <td class="py-2.5 text-right">
              {% if u.id == current_user_id_val %}
                <span class="text-xs text-gray-400 italic">(bạn)</span>
              {% elif u.role == 'super_admin' and not is_super_admin %}
                <span class="text-xs text-gray-400 italic">Chỉ Super Admin khác mới quản lý được</span>
              {% elif u.role == 'admin' and not is_super_admin %}
                <span class="text-xs text-gray-400 italic">Chỉ Super Admin quản lý được</span>
              {% else %}
              <div class="flex items-center justify-end gap-1.5 flex-wrap">
                <form method="POST" action="{{ url_for('developer_change_role', user_id=u.id) }}" class="inline-flex items-center gap-1"
                  {% if u.role == 'super_admin' %}onsubmit="return confirm('Hạ quyền Super Admin của {{ u.username }}? Tài khoản này sẽ không còn toàn quyền hệ thống nữa.');"{% endif %}>
                  <select name="role" class="text-xs px-2 py-1.5 rounded-lg bg-gray-100 dark:bg-gray-800 border-0 focus:outline-none focus:ring-1 focus:ring-indigo-500">
                    <option value="user" {{ 'selected' if u.role == 'user' else '' }}>Người dùng</option>
                    <option value="developer" {{ 'selected' if u.role == 'developer' else '' }}>Developer</option>
                    {% if is_super_admin %}
                    <option value="admin" {{ 'selected' if u.role == 'admin' else '' }}>Admin</option>
                    {% endif %}
                  </select>
                  <button type="submit" class="text-xs font-semibold px-2.5 py-1.5 rounded-lg bg-indigo-50 dark:bg-indigo-900/30 text-indigo-600 dark:text-indigo-400 hover:bg-indigo-100">Cập nhật</button>
                </form>

                {% if u.role == 'user' %}
                <form method="POST" action="{{ url_for('developer_change_plan', user_id=u.id) }}" class="inline-flex items-center gap-1">
                  <select name="plan" class="text-xs px-2 py-1.5 rounded-lg bg-gray-100 dark:bg-gray-800 border-0 focus:outline-none focus:ring-1 focus:ring-indigo-500">
                    {% for p in plan_order %}
                    <option value="{{ p }}" {{ 'selected' if u.plan == p else '' }}>{{ plan_meta[p].icon }} {{ plan_meta[p].label }}</option>
                    {% endfor %}
                  </select>
                  <button type="submit" class="text-xs font-semibold px-2.5 py-1.5 rounded-lg bg-emerald-50 dark:bg-emerald-900/30 text-emerald-600 dark:text-emerald-400 hover:bg-emerald-100">Đổi gói</button>
                </form>
                {% endif %}

                <form method="POST" action="{{ url_for('developer_toggle_lock', user_id=u.id) }}" class="inline"
                  onsubmit="return {{ 'true' if u.is_locked else 'confirm(\'Khoá tài khoản này?\')' }};">
                  <button type="submit" class="text-xs font-semibold px-2.5 py-1.5 rounded-lg {{ 'bg-emerald-50 dark:bg-emerald-900/30 text-emerald-600 hover:bg-emerald-100' if u.is_locked else 'bg-amber-50 dark:bg-amber-900/30 text-amber-600 hover:bg-amber-100' }}">
                    {{ 'Mở khoá' if u.is_locked else 'Khoá' }}
                  </button>
                </form>

                <form method="POST" action="{{ url_for('developer_reset_session', user_id=u.id) }}" class="inline" onsubmit="return confirm('Đăng xuất mọi phiên đăng nhập của tài khoản này?');">
                  <button type="submit" class="text-xs font-semibold px-2.5 py-1.5 rounded-lg bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-300 hover:bg-gray-200">Reset session</button>
                </form>

                {% if u.role not in ('admin', 'super_admin') %}
                <form method="POST" action="{{ url_for('developer_delete_user', user_id=u.id) }}" class="inline" onsubmit="return confirm('XOÁ VĨNH VIỄN tài khoản này? Không thể hoàn tác.');">
                  <button type="submit" class="text-xs font-semibold px-2.5 py-1.5 rounded-lg bg-red-600 text-white hover:bg-red-700">Xoá</button>
                </form>
                {% endif %}
              </div>
              {% endif %}
            </td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>

    <p class="text-center text-xs text-gray-400 pb-4">
      Trang này chỉ hiển thị số liệu tổng hợp (số lượt, độ dài, môn học, chế độ) — không lưu/hiển thị nội dung câu hỏi hay câu trả lời của học sinh.
    </p>
  </main>

  <script>
    function filterUserTable() {
      const q = document.getElementById('userSearchInput').value.trim().toLowerCase();
      document.querySelectorAll('#usersTableBody tr').forEach(row => {
        row.style.display = row.dataset.username.includes(q) ? '' : 'none';
      });
    }
  </script>
</body>
</html>
'''

# ==========================================
# 3.1 ĐĂNG NHẬP BẰNG GOOGLE — helper dùng chung
# ==========================================
def _slugify_username(base):
    """Chuyển email/tên thành username hợp lệ (chỉ chữ, số, gạch dưới, 3-32 ký tự)."""
    base = re.sub(r'[^A-Za-z0-9_]', '', (base or '').split('@')[0]) or 'user'
    base = base[:24] or 'user'
    if len(base) < 3:
        base = (base + '_user')[:24]
    return base


def get_or_create_oauth_user(provider, oauth_id, email, display_name):
    """Tìm tài khoản đã liên kết với (provider, oauth_id); nếu chưa có thì tạo mới.
    Không bao giờ lưu hay yêu cầu mật khẩu Google — chỉ nhận id/email/tên
    do chính Google xác thực và trả về qua OAuth, KHÔNG đụng tới thông tin
    đăng nhập thật của người dùng ở phía Google."""
    db = get_db()
    existing = db.execute(
        'SELECT * FROM users WHERE oauth_provider = ? AND oauth_id = ?', (provider, oauth_id)
    ).fetchone()
    if existing:
        return existing

    # Nếu email đã có tài khoản (đăng ký bằng mật khẩu trước đó) -> liên kết thêm OAuth vào đó
    if email:
        existing_email = db.execute('SELECT * FROM users WHERE email = ?', (email,)).fetchone()
        if existing_email:
            db.execute(
                'UPDATE users SET oauth_provider = ?, oauth_id = ? WHERE id = ?',
                (provider, oauth_id, existing_email['id'])
            )
            db.commit()
            return db.execute('SELECT * FROM users WHERE id = ?', (existing_email['id'],)).fetchone()

    base_username = _slugify_username(display_name or email or provider)
    username = base_username
    suffix = 0
    while db.execute('SELECT id FROM users WHERE username = ?', (username,)).fetchone():
        suffix += 1
        username = f"{base_username}{suffix}"[:32]

    cur = db.execute(
        '''INSERT INTO users (username, password_hash, role, created_at, email, oauth_provider, oauth_id)
           VALUES (?, '', 'user', ?, ?, ?, ?)''',
        (username, now_iso(), email, provider, oauth_id)
    )
    db.commit()
    return db.execute('SELECT * FROM users WHERE id = ?', (cur.lastrowid,)).fetchone()


def _login_session_for(user):
    session.clear()
    session['user_id'] = user['id']
    session['username'] = user['username']
    session['session_version'] = user['session_version'] if 'session_version' in user.keys() else 0


AVATAR_PRESETS = [
    {'emoji': '🦊', 'color': 'from-orange-400 to-amber-500'},
    {'emoji': '🐱', 'color': 'from-slate-400 to-slate-600'},
    {'emoji': '🐼', 'color': 'from-gray-700 to-gray-900'},
    {'emoji': '🦁', 'color': 'from-yellow-400 to-orange-500'},
    {'emoji': '🐸', 'color': 'from-emerald-400 to-green-600'},
    {'emoji': '🐧', 'color': 'from-sky-500 to-blue-700'},
    {'emoji': '🦉', 'color': 'from-amber-600 to-yellow-800'},
    {'emoji': '🐢', 'color': 'from-teal-400 to-emerald-600'},
    {'emoji': '🐬', 'color': 'from-cyan-400 to-blue-500'},
    {'emoji': '🦄', 'color': 'from-pink-400 to-purple-500'},
    {'emoji': '🐙', 'color': 'from-purple-500 to-fuchsia-600'},
    {'emoji': '🦋', 'color': 'from-indigo-400 to-purple-500'},
    {'emoji': '🐨', 'color': 'from-gray-400 to-gray-600'},
    {'emoji': '🐯', 'color': 'from-orange-500 to-red-600'},
    {'emoji': '🐰', 'color': 'from-pink-300 to-rose-500'},
    {'emoji': '🐳', 'color': 'from-blue-400 to-indigo-600'},
]
AVATAR_EMOJI_SET = {a['emoji'] for a in AVATAR_PRESETS}


def generate_guest_username():
    """Tên tài khoản khách — ngẫu nhiên, không đoán được, không trùng tài khoản có sẵn."""
    db = get_db()
    for _ in range(10):
        candidate = 'khach_' + secrets.token_hex(4)
        if not db.execute('SELECT id FROM users WHERE username = ?', (candidate,)).fetchone():
            return candidate
    return 'khach_' + secrets.token_hex(8)  # cực hiếm khi tới đây


# Bảng chữ cái dùng cho mã khôi phục — CỐ TÌNH bỏ các ký tự dễ nhầm lẫn khi chép tay/đọc lại:
# 0/O, 1/I/L — giảm khả năng học sinh chép sai rồi không dùng lại được mã của chính mình.
RECOVERY_CODE_ALPHABET = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789'


def generate_recovery_code():
    """Mã khôi phục dạng XXXX-XXXX-XXXX (12 ký tự thật, có gạch ngang cho dễ đọc/chép tay).
    Chỉ hiện đúng 1 LẦN cho học sinh lúc tạo ra (lúc đăng ký, hoặc lúc tự tạo lại) — sau đó
    CHỈ lưu bản băm (hash), giống hệt cách xử lý mật khẩu, không bao giờ lưu lại bản gốc."""
    parts = [''.join(secrets.choice(RECOVERY_CODE_ALPHABET) for _ in range(4)) for _ in range(3)]
    return '-'.join(parts)


@app.route('/guest-login', methods=['POST'])
def guest_login():
    """'Dùng thử ngay, không cần đăng ký' — tạo 1 tài khoản khách THẬT trong DB (dùng lại
    toàn bộ hạ tầng sẵn có: chat, gamification, flashcards...) nhưng không có mật khẩu nên
    không đăng nhập lại được nếu mất cookie — có nút 'Tạo tài khoản chính thức' để nâng cấp
    tại chỗ, giữ nguyên toàn bộ dữ liệu đã có (xem guest_upgrade())."""
    if current_user_id():
        return redirect(url_for('home'))
    if not guest_login_effective_enabled():
        flash('Chế độ dùng thử khách hiện đang tắt.')
        return redirect(url_for('login_page'))

    username = generate_guest_username()
    db = get_db()
    cur = db.execute(
        'INSERT INTO users (username, password_hash, role, is_guest, created_at) VALUES (?, ?, ?, 1, ?)',
        (username, '', 'user', now_iso())
    )
    db.commit()
    new_user = db.execute('SELECT * FROM users WHERE id = ?', (cur.lastrowid,)).fetchone()
    _login_session_for(new_user)
    return redirect(url_for('home'))


@app.route('/guest/upgrade', methods=['POST'])
@login_required
def guest_upgrade():
    """Tài khoản khách tự nâng cấp thành tài khoản chính thức (đặt username + mật khẩu) —
    CẬP NHẬT NGAY TRÊN DÒNG DỮ LIỆU HIỆN TẠI (không tạo tài khoản mới) để giữ nguyên toàn bộ
    lịch sử chat, XP, thẻ ghi nhớ, sổ lỗi sai... đã có trong lúc dùng thử."""
    user = current_user()
    if not user or not user['is_guest']:
        return jsonify({"error": "Tài khoản này không phải tài khoản khách."}), 400

    data = request.get_json(silent=True) or {}
    username = (data.get('username') or '').strip()
    password = data.get('password') or ''
    confirm = data.get('confirm') or ''

    if not USERNAME_RE.match(username):
        return jsonify({"error": "Tên đăng nhập phải từ 3-32 ký tự, chỉ gồm chữ cái, số hoặc dấu gạch dưới."}), 400
    if len(password) < 6:
        return jsonify({"error": "Mật khẩu phải có ít nhất 6 ký tự."}), 400
    if password != confirm:
        return jsonify({"error": "Mật khẩu nhập lại không khớp."}), 400

    db = get_db()
    existing = db.execute('SELECT id FROM users WHERE username = ? AND id != ?', (username, user['id'])).fetchone()
    if existing:
        return jsonify({"error": "Tên đăng nhập này đã được sử dụng."}), 400

    pw_hash = generate_password_hash(password)
    db.execute('UPDATE users SET username = ?, password_hash = ?, is_guest = 0 WHERE id = ?',
               (username, pw_hash, user['id']))
    db.commit()
    session['username'] = username
    return jsonify({"success": True})


@app.route('/auth/<provider>')
def oauth_start(provider):
    if provider != 'google' or not oauth or not hasattr(oauth, provider) or not google_login_effective_enabled():
        flash('Phương thức đăng nhập này hiện chưa được bật.')
        return redirect(url_for('login_page'))
    redirect_uri = url_for('oauth_callback', provider=provider, _external=True)
    client = getattr(oauth, provider)
    return client.authorize_redirect(redirect_uri)


@app.route('/auth/<provider>/callback')
def oauth_callback(provider):
    if provider != 'google' or not oauth or not hasattr(oauth, provider) or not google_login_effective_enabled():
        flash('Phương thức đăng nhập này hiện chưa được bật.')
        return redirect(url_for('login_page'))

    client = getattr(oauth, provider)
    try:
        token = client.authorize_access_token()
    except Exception:
        flash('Đăng nhập bị huỷ hoặc hết hạn phiên OAuth, em thử lại nhé.')
        return redirect(url_for('login_page'))

    try:
        userinfo = token.get('userinfo') or client.userinfo()
        oauth_id = userinfo.get('sub')
        email = userinfo.get('email')
        name = userinfo.get('name') or (email.split('@')[0] if email else 'google_user')
    except Exception:
        flash('Không lấy được thông tin tài khoản từ nhà cung cấp, em thử lại nhé.')
        return redirect(url_for('login_page'))

    if not oauth_id:
        flash('Đăng nhập không thành công, em thử lại nhé.')
        return redirect(url_for('login_page'))

    user = get_or_create_oauth_user(provider, str(oauth_id), email, name)
    if user['is_locked']:
        reason = (user['lock_reason'] or '').strip()
        flash('Tài khoản này đã bị khoá.' + (f' Lý do: {reason}' if reason else ''))
        return redirect(url_for('login_page'))
    _login_session_for(user)
    return redirect(url_for('home'))


# ==========================================
# 4. ĐỊNH TUYẾN TÀI KHOẢN (Đăng ký / Đăng nhập / Đăng xuất)
# ==========================================
def _auth_ctx(**extra):
    ctx = {'google_enabled': google_login_effective_enabled(), 'guest_enabled': guest_login_effective_enabled()}
    ctx.update(extra)
    return ctx


@app.route('/register', methods=['GET', 'POST'])
def register_page():
    if current_user_id():
        return redirect(url_for('home'))

    if request.method == 'POST':
        if not _rate_limit_check(f"register:{client_ip()}", max_attempts=20, window_seconds=3600):
            flash('Có quá nhiều lượt đăng ký từ mạng này trong 1 giờ qua. Em thử lại sau nhé.')
            return render_template_string(AUTH_HTML, mode='register', username='', **_auth_ctx())

        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''
        confirm = request.form.get('confirm') or ''

        if not USERNAME_RE.match(username):
            flash('Tên đăng nhập phải từ 3-32 ký tự, chỉ gồm chữ cái, số hoặc dấu gạch dưới.')
        elif len(password) < 6:
            flash('Mật khẩu phải có ít nhất 6 ký tự.')
        elif password != confirm:
            flash('Mật khẩu nhập lại không khớp.')
        else:
            db = get_db()
            existing = db.execute('SELECT id FROM users WHERE username = ?', (username,)).fetchone()
            if existing:
                flash('Tên đăng nhập này đã được sử dụng.')
            else:
                pw_hash = generate_password_hash(password)
                recovery_code = generate_recovery_code()
                cur = db.execute(
                    'INSERT INTO users (username, password_hash, recovery_code_hash, created_at) VALUES (?, ?, ?, ?)',
                    (username, pw_hash, generate_password_hash(recovery_code), now_iso())
                )
                db.commit()
                new_user = db.execute('SELECT * FROM users WHERE id = ?', (cur.lastrowid,)).fetchone()
                _login_session_for(new_user)
                return render_template_string(RECOVERY_CODE_HTML, code=recovery_code, context='register')

        return render_template_string(AUTH_HTML, mode='register', username=username, **_auth_ctx())

    return render_template_string(AUTH_HTML, mode='register', username='', **_auth_ctx())


@app.route('/login', methods=['GET', 'POST'])
def login_page():
    if current_user_id():
        return redirect(url_for('home'))

    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''
        ip = client_ip()

        # Giới hạn theo CẶP (IP, username) — 1 học sinh gõ sai mật khẩu nhiều lần không khoá
        # luôn cả lớp dùng chung WiFi trường. Có thêm giới hạn tổng theo IP (ngưỡng cao hơn
        # hẳn) chỉ để chặn kiểu bot dò quét nhiều tài khoản khác nhau từ 1 địa chỉ.
        pair_key = f"login_pair:{ip}:{username.lower()}"
        ip_key = f"login_ip:{ip}"
        if not _rate_limit_check(pair_key, max_attempts=8, window_seconds=900):
            flash('Em đã thử sai quá nhiều lần cho tài khoản này. Vui lòng đợi vài phút rồi thử lại.')
            return render_template_string(AUTH_HTML, mode='login', username=username, **_auth_ctx())
        if not _rate_limit_check(ip_key, max_attempts=60, window_seconds=900):
            flash('Có quá nhiều lượt đăng nhập từ mạng này. Vui lòng đợi vài phút rồi thử lại.')
            return render_template_string(AUTH_HTML, mode='login', username=username, **_auth_ctx())

        db = get_db()
        user = db.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()

        # Tài khoản tạo qua Google không có mật khẩu (password_hash rỗng)
        # -> không cho đăng nhập bằng form mật khẩu, hướng dẫn dùng lại nút OAuth.
        if user and not user['password_hash']:
            flash('Tài khoản này đăng nhập bằng Google. Vui lòng dùng nút tương ứng bên dưới.')
            return render_template_string(AUTH_HTML, mode='login', username=username, **_auth_ctx())

        if user and user['password_hash'] and check_password_hash(user['password_hash'], password):
            if user['is_locked']:
                reason = (user['lock_reason'] or '').strip()
                flash('Tài khoản này đã bị khoá.' + (f' Lý do: {reason}' if reason else ''))
                return render_template_string(AUTH_HTML, mode='login', username=username, **_auth_ctx())
            _rate_limit_reset(pair_key)  # đăng nhập đúng -> xoá bộ đếm sai cho cặp này
            _login_session_for(user)
            return redirect(url_for('home'))

        flash('Tên đăng nhập hoặc mật khẩu không đúng.')
        return render_template_string(AUTH_HTML, mode='login', username=username, **_auth_ctx())

    return render_template_string(AUTH_HTML, mode='login', username='', **_auth_ctx())


@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password_page():
    if current_user_id():
        return redirect(url_for('home'))

    if request.method == 'POST':
        if not _rate_limit_check(f"forgot_pw:{client_ip()}", max_attempts=10, window_seconds=900):
            flash('Có quá nhiều lượt thử từ mạng này. Vui lòng đợi vài phút rồi thử lại.')
            return render_template_string(FORGOT_PASSWORD_HTML, username='')

        username = (request.form.get('username') or '').strip()
        code = (request.form.get('code') or '').strip().upper()
        new_password = request.form.get('new_password') or ''
        confirm = request.form.get('confirm') or ''

        db = get_db()
        user = db.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()

        # Thông báo LỖI CHUNG CHUNG dù sai username hay sai mã — không để lộ "username này có
        # tồn tại không" cho người dò (tránh dò được danh sách tài khoản thật trong hệ thống).
        generic_error = 'Tên đăng nhập hoặc mã khôi phục không đúng.'

        if not user or not user['recovery_code_hash']:
            flash(generic_error)
        elif not check_password_hash(user['recovery_code_hash'], code):
            flash(generic_error)
        elif len(new_password) < 6:
            flash('Mật khẩu mới phải có ít nhất 6 ký tự.')
        elif new_password != confirm:
            flash('Mật khẩu mới nhập lại không khớp.')
        else:
            # Dùng mã khôi phục xong thì ĐỔI LUÔN sang mã mới (không cho dùng lại mã cũ nữa) —
            # đúng thông lệ bảo mật cho các mã dùng-một-lần dạng này.
            new_code = generate_recovery_code()
            db.execute(
                'UPDATE users SET password_hash = ?, recovery_code_hash = ? WHERE id = ?',
                (generate_password_hash(new_password), generate_password_hash(new_code), user['id'])
            )
            db.commit()
            write_audit('password_reset_via_recovery_code', target=user['username'])
            return render_template_string(RECOVERY_CODE_HTML, code=new_code, context='reset')

        return render_template_string(FORGOT_PASSWORD_HTML, username=username)

    return render_template_string(FORGOT_PASSWORD_HTML, username='')


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login_page'))


# ==========================================
# 5. ĐỊNH TUYẾN CHÍNH (Trang chủ gia sư AI)
# ==========================================
@app.route('/health')
def health_check():
    """Endpoint 'sống hay chết' — CỐ TÌNH cực nhẹ (không đăng nhập, không đụng database),
    dùng cho 2 việc: (1) health check tự động của nền tảng deploy (Render/Railway/...), và
    (2) để 1 dịch vụ ping bên ngoài (UptimeRobot, cron-job.org...) gọi định kỳ giữ server
    free tier không bị 'ngủ' — xem README mục 'Deploy' để biết cách dùng đúng."""
    return jsonify({"status": "ok", "time": now_iso()})


@app.route('/manifest.json')
def pwa_manifest():
    """PWA manifest — cho phép 'Cài đặt' StudyMate AI như 1 app (Add to Home Screen) trên
    điện thoại/máy tính. Icon lấy từ static/icons/ (xem README nếu cần đổi icon)."""
    return send_from_directory(
        os.path.dirname(os.path.abspath(__file__)), 'manifest.json',
        mimetype='application/manifest+json'
    )


@app.route('/')
@login_required
def home():
    role = current_user_role()
    user = current_user()
    plan = effective_plan(user)
    unlocked_by_plan = {
        p: [k for k in THINKING_MODE_ORDER if thinking_mode_unlocked(k, p)] for p in PLAN_ORDER
    }
    # Ưu đãi lần đầu chỉ phụ thuộc số đơn ĐÃ TRẢ TIỀN của tài khoản (không phụ thuộc đang xem
    # gói nào), nên chỉ cần gọi 1 lần với 1 gói bất kỳ trong PLAN_PRICING để lấy is_discounted.
    discount_amounts = {}
    is_discount_eligible, discount_months_left = False, 0
    if user:
        for p, base in PLAN_PRICING.items():
            amount, base_amount, is_discounted, paid_count = compute_checkout_price(user['id'], p)
            discount_amounts[p] = amount
            is_discount_eligible = is_discounted
            discount_months_left = max(0, FIRST_TIME_DISCOUNT_MONTHS - paid_count)
    # Ví dụ thật cho StudyMate Lab: Rắn Săn Chữ được nối vào flag 'game_snake_quiz' — Developer
    # có thể ẩn/thử nghiệm % người dùng thấy trực tiếp từ /developer/lab, không cần deploy lại.
    game_flags = {
        'snake': is_feature_enabled('game_snake_quiz', user),
        'quick_math': is_feature_enabled('game_quick_math', user),
        'memory_match': is_feature_enabled('game_memory_match', user),
    }
    return render_template_string(
        HTML,
        username=session.get('username', ''),
        role=role,
        game_flags=game_flags,
        role_icon=role_meta(role)['icon'],
        role_label=role_meta(role)['label'],
        is_developer=(role_rank(role) >= role_rank('developer')),
        is_admin=(role_rank(role) >= role_rank('admin')),
        app_name=app_display_name(user),
        current_plan=plan,
        plan_order=PLAN_ORDER,
        plan_meta=PLAN_META,
        plan_limits=PLAN_LIMITS,
        is_plan_role_based=(role_rank(role) >= role_rank('developer')),
        thinking_modes=THINKING_MODES,
        thinking_mode_order=THINKING_MODE_ORDER,
        unlocked_thinking_modes=unlocked_by_plan[plan],
        unlocked_by_plan=unlocked_by_plan,
        plan_pricing=PLAN_PRICING,
        discount_amounts=discount_amounts,
        is_discount_eligible=is_discount_eligible,
        discount_pct=FIRST_TIME_DISCOUNT_PCT,
        discount_months_left=discount_months_left,
        vnpay_enabled=VNPAY_ENABLED,
        bank_transfer_enabled=BANK_TRANSFER_ENABLED,
        payment_methods_enabled=PAYMENT_METHODS_ENABLED,
        achievements_meta=ACHIEVEMENTS_META,
        avatar_presets=AVATAR_PRESETS,
        avatar_emoji=(user['avatar_emoji'] if user and user['avatar_emoji'] else '') or '',
        avatar_color=(user['avatar_color'] if user and user['avatar_color'] else '') or '',
        is_guest=bool(user and user['is_guest']),
        has_password=bool(user and user['password_hash']),
    )


# ==========================================
# 6. ĐỊNH TUYẾN BẢO MẬT (Trang báo cáo)
# ==========================================
@app.route('/security')
def security_report():
    return render_template_string(SECURITY_HTML)


# ==========================================
# 6.1 ĐỊNH TUYẾN THỐNG KÊ (Chỉ dành cho tài khoản developer)
# ==========================================
@app.route('/developer')
@developer_required
def developer_stats():
    db = get_db()

    total_users = db.execute('SELECT COUNT(*) c FROM users').fetchone()['c']
    total_usage = db.execute('SELECT COUNT(*) c FROM usage_logs').fetchone()['c']
    error_count = db.execute("SELECT COUNT(*) c FROM usage_logs WHERE status = 'error'").fetchone()['c']
    error_rate = round((error_count / total_usage) * 100, 1) if total_usage else 0

    seven_days_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    new_users_7d = db.execute(
        'SELECT COUNT(*) c FROM users WHERE created_at >= ?', (seven_days_ago,)
    ).fetchone()['c']
    usage_7d = db.execute(
        'SELECT COUNT(*) c FROM usage_logs WHERE created_at >= ?', (seven_days_ago,)
    ).fetchone()['c']
    avg_per_day_7d = round(usage_7d / 7, 1)

    today_str = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    usage_today = db.execute(
        "SELECT COUNT(*) c FROM usage_logs WHERE substr(created_at, 1, 10) = ?", (today_str,)
    ).fetchone()['c']

    # Số lượt sử dụng theo từng ngày trong 14 ngày gần nhất (kể cả ngày = 0 lượt).
    rows = db.execute('''
        SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS c
        FROM usage_logs
        WHERE created_at >= ?
        GROUP BY day
    ''', ((datetime.now(timezone.utc) - timedelta(days=13)).isoformat(),)).fetchall()
    counts_by_day = {r['day']: r['c'] for r in rows}

    daily_counts = []
    for i in range(13, -1, -1):
        day = (datetime.now(timezone.utc) - timedelta(days=i)).strftime('%Y-%m-%d')
        daily_counts.append({'label': day[5:], 'count': counts_by_day.get(day, 0)})
    max_count = max((d['count'] for d in daily_counts), default=0)
    for d in daily_counts:
        d['height'] = round((d['count'] / max_count) * 100, 1) if max_count else 2

    # Phân bổ theo môn học.
    subj_rows = db.execute('''
        SELECT subject, COUNT(*) AS c FROM usage_logs
        WHERE subject IS NOT NULL AND subject != ''
        GROUP BY subject ORDER BY c DESC
    ''').fetchall()
    subject_stats = []
    for r in subj_rows:
        pct = round((r['c'] / total_usage) * 100, 1) if total_usage else 0
        subject_stats.append({'subject': r['subject'], 'count': r['c'], 'pct': pct})

    # Phân bổ theo chế độ học tập.
    mode_rows = db.execute('''
        SELECT mode, COUNT(*) AS c FROM usage_logs
        WHERE mode IS NOT NULL AND mode != ''
        GROUP BY mode ORDER BY c DESC
    ''').fetchall()
    mode_stats = []
    for r in mode_rows:
        pct = round((r['c'] / total_usage) * 100, 1) if total_usage else 0
        mode_stats.append({'mode': r['mode'], 'count': r['c'], 'pct': pct})

    # Top người dùng hoạt động nhiều nhất.
    top_rows = db.execute('''
        SELECT u.username AS username, u.role AS role,
               COUNT(l.id) AS usage_count, MAX(l.created_at) AS last_used
        FROM users u
        LEFT JOIN usage_logs l ON l.user_id = u.id
        GROUP BY u.id
        ORDER BY usage_count DESC, last_used DESC
        LIMIT 8
    ''').fetchall()
    top_users = []
    for r in top_rows:
        last_used = r['last_used']
        top_users.append({
            'username': r['username'],
            'role': r['role'],
            'usage_count': r['usage_count'],
            'last_used': last_used[:16].replace('T', ' ') if last_used else None,
        })

    all_users = db.execute(
        'SELECT id, username, role, plan, created_at, is_locked, lock_reason FROM users ORDER BY id ASC'
    ).fetchall()

    token_row = db.execute(
        'SELECT COALESCE(SUM(message_chars),0) AS mc, COALESCE(SUM(response_chars),0) AS rc FROM usage_logs'
    ).fetchone()
    estimated_tokens = round((token_row['mc'] + token_row['rc']) / 4)

    # ---- Đơn nâng cấp gói (thanh toán) ----
    pending_orders_rows = db.execute('''
        SELECT o.*, u.username AS username FROM payment_orders o
        LEFT JOIN users u ON u.id = o.user_id
        WHERE o.status = 'pending' ORDER BY o.created_at ASC
    ''').fetchall()
    recent_orders_rows = db.execute('''
        SELECT o.*, u.username AS username FROM payment_orders o
        LEFT JOIN users u ON u.id = o.user_id
        ORDER BY o.created_at DESC LIMIT 20
    ''').fetchall()
    total_revenue = db.execute(
        "SELECT COALESCE(SUM(amount), 0) r FROM payment_orders WHERE status = 'paid'"
    ).fetchone()['r']

    # ---- Báo lỗi từ học sinh ----
    open_issues_count = db.execute("SELECT COUNT(*) c FROM issue_reports WHERE status = 'open'").fetchone()['c']
    issue_rows = db.execute('''
        SELECT r.*, u.username AS username FROM issue_reports r
        LEFT JOIN users u ON u.id = r.user_id
        ORDER BY (r.status = 'open') DESC, r.created_at DESC LIMIT 30
    ''').fetchall()

    role = current_user_role()

    return render_template_string(
        DEV_STATS_HTML,
        total_users=total_users,
        new_users_7d=new_users_7d,
        total_usage=total_usage,
        usage_today=usage_today,
        usage_7d=usage_7d,
        avg_per_day_7d=avg_per_day_7d,
        error_rate=error_rate,
        error_count=error_count,
        estimated_tokens=estimated_tokens,
        daily_counts=daily_counts,
        subject_stats=subject_stats,
        mode_stats=mode_stats,
        top_users=top_users,
        all_users=[dict(u, effective_plan=effective_plan(u), plan_is_role_based=(role_rank(u['role']) >= role_rank('developer'))) for u in all_users],
        role_meta=ROLE_META,
        role_rank_map={r: role_rank(r) for r in ROLE_ORDER},
        plan_meta=PLAN_META,
        plan_order=PLAN_ORDER,
        plan_pricing=PLAN_PRICING,
        current_role=role,
        is_admin=(role_rank(role) >= role_rank('admin')),
        is_super_admin=(role_rank(role) >= role_rank('super_admin')),
        current_user_id_val=current_user_id(),
        banner_message=get_setting('banner_message', '') or '',
        google_configured=bool(GOOGLE_OAUTH_ENABLED),
        google_override=get_setting('google_login_override', ''),
        guest_login_on=guest_login_effective_enabled(),
        maintenance_mode=(get_setting('maintenance_mode', 'off') == 'on'),
        ai_model_override=get_setting('ai_model_override', '') or '',
        ai_temperature_override=get_setting('ai_temperature_override', '') or '',
        global_system_addendum=get_setting('global_system_addendum', '') or '',
        default_model=CONSOLEX_MODEL,
        pending_orders=[dict(o) for o in pending_orders_rows],
        recent_orders=[dict(o) for o in recent_orders_rows],
        total_revenue=total_revenue,
        vnpay_enabled=VNPAY_ENABLED,
        bank_transfer_enabled=BANK_TRANSFER_ENABLED,
        issue_reports=[dict(r) for r in issue_rows],
        open_issues_count=open_issues_count,
    )


@app.route('/developer/users/<int:user_id>/role', methods=['POST'])
@admin_required
def developer_change_role(user_id):
    """Đổi vai trò 1 tài khoản. Admin chỉ đổi qua lại User<->Developer; chỉ Super Admin mới
    được cấp/thu hồi Admin trở lên. Luôn giữ lại ít nhất 1 tài khoản Admin/Super Admin."""
    db = get_db()
    target = db.execute('SELECT id, role, username FROM users WHERE id = ?', (user_id,)).fetchone()
    if not target:
        flash('Không tìm thấy tài khoản.')
        return redirect(url_for('developer_stats'))

    new_role = (request.form.get('role') or '').strip()
    actor = current_user()
    ok, err = can_manage_role(actor['role'], target['role'], new_role,
                               is_self=(target['id'] == current_user_id()), target_username=target['username'])
    if not ok:
        flash(err)
        return redirect(url_for('developer_stats'))

    if role_rank(target['role']) >= role_rank('admin') and role_rank(new_role) < role_rank('admin'):
        remaining = db.execute(
            "SELECT COUNT(*) c FROM users WHERE role IN ('admin','super_admin') AND id != ?", (user_id,)
        ).fetchone()['c']
        if remaining == 0:
            flash('Không thể hạ quyền — đây là tài khoản Admin/Super Admin cuối cùng của hệ thống.')
            return redirect(url_for('developer_stats'))

    db.execute('UPDATE users SET role = ? WHERE id = ?', (new_role, user_id))
    db.commit()
    write_audit('change_role', target['username'], f"{target['role']} → {new_role}")
    flash(f"Đã đổi vai trò của '{target['username']}' thành {role_meta(new_role)['label']}.")
    return redirect(url_for('developer_stats'))


@app.route('/developer/users/<int:user_id>/plan', methods=['POST'])
@admin_required
def developer_change_plan(user_id):
    """Gán gói Free/Premium/Max cho 1 tài khoản (không có cổng thanh toán thật — đây là cách
    Admin "nâng cấp" thủ công cho học sinh). Không áp dụng cho Developer trở lên vì các vai
    trò đó đã luôn có Max vô điều kiện (xem effective_plan())."""
    db = get_db()
    target = db.execute('SELECT id, role, username, plan FROM users WHERE id = ?', (user_id,)).fetchone()
    if not target:
        flash('Không tìm thấy tài khoản.')
        return redirect(url_for('developer_stats'))

    if role_rank(target['role']) >= role_rank('developer'):
        flash(f"'{target['username']}' đã tự động có gói Max theo vai trò, không cần đổi gói.")
        return redirect(url_for('developer_stats'))

    new_plan = (request.form.get('plan') or '').strip()
    if new_plan not in PLAN_ORDER:
        flash('Gói không hợp lệ.')
        return redirect(url_for('developer_stats'))

    if new_plan == 'free':
        # Hạ về Free thì xoá luôn hạn dùng — không cần grant_plan_upgrade() (hàm đó chỉ dùng để
        # CẤP gói có phí theo tháng, hạ về Free không có khái niệm "hạn dùng").
        db.execute('UPDATE users SET plan = ?, plan_expires_at = NULL WHERE id = ?', (new_plan, user_id))
        db.commit()
        write_audit('change_plan', target['username'], f"{target['plan']} → {new_plan}")
    else:
        # Admin "tặng" gói: CHỈ 1 THÁNG miễn phí (không phải vĩnh viễn) — dùng chung
        # grant_plan_upgrade() với cổng thanh toán thật để hạn dùng được tính nhất quán và tự
        # rơi về Free khi hết hạn (xem effective_plan()).
        grant_plan_upgrade(user_id, new_plan, order_code=f"gift_by_{session.get('username','admin')}",
                            actor=session.get('username', 'admin'), months=1)
    flash(f"Đã đổi gói của '{target['username']}' thành {plan_meta(new_plan)['label']}"
          + ('' if new_plan == 'free' else ' (tặng miễn phí 1 tháng).'))
    return redirect(url_for('developer_stats'))


@app.route('/developer/payments/<order_code>/confirm', methods=['POST'])
@admin_required
def developer_confirm_payment(order_code):
    """Admin bấm xác nhận ĐÃ NHẬN ĐƯỢC TIỀN cho 1 đơn chuyển khoản VietQR (thủ công, vì app
    không có quyền đọc sao kê ngân hàng tự động). Đơn qua VNPAY thì KHÔNG cần bấm tay — đã tự
    chốt qua IPN (xem vnpay_ipn())."""
    db = get_db()
    order = db.execute('SELECT * FROM payment_orders WHERE order_code = ?', (order_code,)).fetchone()
    if not order:
        flash('Không tìm thấy đơn hàng.')
        return redirect(url_for('developer_stats'))
    if order['status'] == 'paid':
        flash('Đơn này đã được xác nhận trước đó.')
        return redirect(url_for('developer_stats'))

    db.execute("UPDATE payment_orders SET status = 'paid', paid_at = ? WHERE order_code = ?",
               (now_iso(), order_code))
    db.commit()
    grant_plan_upgrade(order['user_id'], order['plan'], order_code, actor=session.get('username', ''))
    flash(f"Đã xác nhận thanh toán & nâng cấp gói cho đơn {order_code}.")
    return redirect(url_for('developer_stats'))


@app.route('/developer/payments/<order_code>/cancel', methods=['POST'])
@admin_required
def developer_cancel_payment(order_code):
    db = get_db()
    order = db.execute('SELECT * FROM payment_orders WHERE order_code = ?', (order_code,)).fetchone()
    if not order:
        flash('Không tìm thấy đơn hàng.')
        return redirect(url_for('developer_stats'))
    db.execute("UPDATE payment_orders SET status = 'cancelled' WHERE order_code = ?", (order_code,))
    db.commit()
    write_audit('cancel_payment_order', target=order_code)
    flash(f"Đã huỷ đơn {order_code}.")
    return redirect(url_for('developer_stats'))


@app.route('/developer/issues/<int:issue_id>/resolve', methods=['POST'])
@admin_required
def developer_resolve_issue(issue_id):
    """Đánh dấu 1 báo cáo lỗi là đã xử lý (hoặc mở lại nếu bấm lần nữa)."""
    db = get_db()
    row = db.execute('SELECT id, status FROM issue_reports WHERE id = ?', (issue_id,)).fetchone()
    if not row:
        flash('Không tìm thấy báo cáo này.')
        return redirect(url_for('developer_stats'))
    new_status = 'open' if row['status'] == 'resolved' else 'resolved'
    resolved_at = now_iso() if new_status == 'resolved' else None
    db.execute('UPDATE issue_reports SET status = ?, resolved_at = ?, resolved_by = ? WHERE id = ?',
               (new_status, resolved_at, session.get('username', '') if new_status == 'resolved' else None, issue_id))
    db.commit()
    if new_status == 'resolved':
        write_audit('resolve_issue_report', target=str(issue_id))
    return redirect(url_for('developer_stats'))


@app.route('/developer/users/<int:user_id>/lock', methods=['POST'])
@admin_required
def developer_toggle_lock(user_id):
    """Khoá / mở khoá tài khoản. Chỉ Super Admin được khoá tài khoản Admin/Super Admin."""
    db = get_db()
    target = db.execute('SELECT id, role, username, is_locked FROM users WHERE id = ?', (user_id,)).fetchone()
    if not target:
        flash('Không tìm thấy tài khoản.')
        return redirect(url_for('developer_stats'))

    actor = current_user()
    if actor['role'] != 'super_admin' and role_rank(target['role']) >= role_rank('admin'):
        flash('Chỉ Super Admin mới có thể khoá tài khoản Admin/Super Admin.')
        return redirect(url_for('developer_stats'))
    if target['id'] == current_user_id():
        flash('Không thể tự khoá tài khoản của chính mình.')
        return redirect(url_for('developer_stats'))

    new_locked = 0 if target['is_locked'] else 1
    reason = (request.form.get('reason') or '').strip()[:200] if new_locked else ''
    db.execute('UPDATE users SET is_locked = ?, lock_reason = ? WHERE id = ?', (new_locked, reason, user_id))
    db.commit()
    write_audit('lock' if new_locked else 'unlock', target['username'], reason)
    flash(('Đã khoá' if new_locked else 'Đã mở khoá') + f" tài khoản '{target['username']}'.")
    return redirect(url_for('developer_stats'))


@app.route('/developer/users/<int:user_id>/reset-session', methods=['POST'])
@admin_required
def developer_reset_session(user_id):
    """Đăng xuất tài khoản này khỏi TẤT CẢ thiết bị đang đăng nhập (tăng session_version)."""
    db = get_db()
    target = db.execute('SELECT id, role, username FROM users WHERE id = ?', (user_id,)).fetchone()
    if not target:
        flash('Không tìm thấy tài khoản.')
        return redirect(url_for('developer_stats'))

    actor = current_user()
    if actor['role'] != 'super_admin' and role_rank(target['role']) >= role_rank('admin'):
        flash('Chỉ Super Admin mới có thể reset session của tài khoản Admin/Super Admin.')
        return redirect(url_for('developer_stats'))

    db.execute('UPDATE users SET session_version = session_version + 1 WHERE id = ?', (user_id,))
    db.commit()
    write_audit('reset_session', target['username'])
    flash(f"Đã đăng xuất toàn bộ phiên đăng nhập của '{target['username']}'.")
    return redirect(url_for('developer_stats'))


@app.route('/developer/users/<int:user_id>/delete', methods=['POST'])
@admin_required
def developer_delete_user(user_id):
    """Xoá tài khoản + toàn bộ dữ liệu liên quan. Không cho xoá Admin/Super Admin qua UI này
    (an toàn hệ thống) và không cho tự xoá chính mình."""
    db = get_db()
    target = db.execute('SELECT id, role, username FROM users WHERE id = ?', (user_id,)).fetchone()
    if not target:
        flash('Không tìm thấy tài khoản.')
        return redirect(url_for('developer_stats'))
    if role_rank(target['role']) >= role_rank('admin'):
        flash('Không thể xoá tài khoản Admin/Super Admin qua giao diện này.')
        return redirect(url_for('developer_stats'))
    if target['id'] == current_user_id():
        flash('Không thể tự xoá tài khoản của chính mình.')
        return redirect(url_for('developer_stats'))

    conv_ids = [r['id'] for r in db.execute(
        'SELECT id FROM conversations WHERE user_id = ?', (user_id,)
    ).fetchall()]
    if conv_ids:
        placeholders = ','.join('?' * len(conv_ids))
        db.execute(f'DELETE FROM messages WHERE conversation_id IN ({placeholders})', conv_ids)
    db.execute('DELETE FROM conversations WHERE user_id = ?', (user_id,))
    db.execute('DELETE FROM projects WHERE user_id = ?', (user_id,))
    db.execute('DELETE FROM custom_tutors WHERE owner_id = ?', (user_id,))
    db.execute('DELETE FROM api_keys WHERE user_id = ?', (user_id,))
    db.execute('DELETE FROM users WHERE id = ?', (user_id,))
    db.commit()
    write_audit('delete_account', target['username'])
    flash(f"Đã xoá tài khoản '{target['username']}'.")
    return redirect(url_for('developer_stats'))


@app.route('/developer/banner', methods=['POST'])
@admin_required
def developer_set_banner():
    message = (request.form.get('banner_message') or '').strip()[:300]
    set_setting('banner_message', message)
    write_audit('set_banner', detail=message)
    flash('Đã cập nhật thông báo hệ thống.' if message else 'Đã xoá thông báo hệ thống.')
    return redirect(url_for('developer_stats'))


@app.route('/developer/maintenance', methods=['POST'])
@admin_required
def developer_toggle_maintenance():
    """Chế độ bảo trì: chặn học sinh thường gửi câu hỏi AI, Admin/Super Admin vẫn dùng được
    bình thường để kiểm tra hệ thống trước khi mở lại cho tất cả."""
    value = 'on' if (request.form.get('value') == 'on') else 'off'
    set_setting('maintenance_mode', value)
    write_audit('toggle_maintenance', detail=value)
    flash('Đã bật chế độ BẢO TRÌ — học sinh tạm thời không gửi được câu hỏi.' if value == 'on'
          else 'Đã tắt chế độ bảo trì.')
    return redirect(url_for('developer_stats'))


@app.route('/developer/ai-config', methods=['POST'])
@admin_required
def developer_ai_config():
    """Ghi đè model / temperature / hướng dẫn hệ thống chung mà KHÔNG cần sửa .env hay restart."""
    model_override = (request.form.get('model_override') or '').strip()[:80]
    temp_raw = (request.form.get('temperature_override') or '').strip()
    addendum = (request.form.get('system_addendum') or '').strip()[:2000]

    set_setting('ai_model_override', model_override)
    set_setting('global_system_addendum', addendum)
    try:
        if temp_raw:
            t = max(0.0, min(1.0, float(temp_raw)))
            set_setting('ai_temperature_override', str(t))
        else:
            set_setting('ai_temperature_override', '')
    except ValueError:
        flash('Giá trị temperature không hợp lệ (phải là số từ 0 đến 1).')
        return redirect(url_for('developer_stats'))

    write_audit('update_ai_config', detail=f"model={model_override or '(mặc định)'}; temp={temp_raw or '(mặc định)'}")
    flash('Đã cập nhật cấu hình AI.')
    return redirect(url_for('developer_stats'))


@app.route('/developer/google-login', methods=['POST'])
@admin_required
def developer_toggle_google_login():
    value = (request.form.get('value') or '').strip()
    if value not in ('on', 'off', ''):
        value = ''
    set_setting('google_login_override', value)
    write_audit('toggle_google_login', detail=value or '(theo .env)')
    flash('Đã cập nhật trạng thái đăng nhập Google.')
    return redirect(url_for('developer_stats'))


@app.route('/developer/guest-login', methods=['POST'])
@admin_required
def developer_toggle_guest_login():
    value = (request.form.get('value') or '').strip()
    if value not in ('on', 'off'):
        value = 'on'
    set_setting('guest_login_override', value)
    write_audit('toggle_guest_login', detail=value)
    flash('Đã cập nhật trạng thái đăng nhập khách.')
    return redirect(url_for('developer_stats'))


@app.route('/developer/sandbox/user-stats', methods=['POST'])
@admin_required
def developer_sandbox_user_stats():
    """Công cụ kiểm thử: gán thẳng XP/streak cho 1 tài khoản để xem giao diện phản ứng ra
    sao (mở khoá thành tựu, hiệu ứng ngọn lửa...) mà không cần đợi dùng thật nhiều ngày.
    Không dùng open_write_db() vì route này KHÔNG chạy trong generator streaming — route
    admin/form POST bình thường, get_db() (gắn với `g`) là đủ và đúng chuẩn."""
    user_id = request.form.get('user_id', type=int)
    xp = max(0, request.form.get('xp', type=int) or 0)
    streak = max(0, request.form.get('streak_days', type=int) or 0)
    longest = max(streak, request.form.get('longest_streak', type=int) or 0)

    if not user_id:
        flash('Thiếu tài khoản mục tiêu.')
        return redirect(url_for('developer_stats'))

    db = get_db()
    target = db.execute('SELECT id, username FROM users WHERE id = ?', (user_id,)).fetchone()
    if not target:
        flash('Không tìm thấy tài khoản.')
        return redirect(url_for('developer_stats'))

    existing = db.execute('SELECT user_id FROM user_stats WHERE user_id = ?', (user_id,)).fetchone()
    today = datetime.now(timezone(timedelta(hours=7))).strftime('%Y-%m-%d')
    if existing:
        db.execute('UPDATE user_stats SET xp = ?, streak_days = ?, longest_streak = ? WHERE user_id = ?',
                   (xp, streak, longest, user_id))
    else:
        db.execute(
            'INSERT INTO user_stats (user_id, xp, streak_days, longest_streak, last_active_date) VALUES (?, ?, ?, ?, ?)',
            (user_id, xp, streak, longest, today)
        )
    db.commit()
    write_audit('sandbox_override_user_stats', target=target['username'],
                detail=f"xp={xp} streak={streak} longest_streak={longest}")
    flash(f"Đã cập nhật dữ liệu kiểm thử cho '{target['username']}'.")
    return redirect(url_for('developer_stats'))


@app.route('/developer/export.csv')
@admin_required
def developer_export_csv():
    """Xuất toàn bộ usage_logs ra CSV (chỉ số liệu tổng hợp, không có nội dung câu hỏi/trả lời)."""
    db = get_db()
    rows = db.execute('SELECT * FROM usage_logs ORDER BY id DESC').fetchall()
    header = ['id', 'user_id', 'endpoint', 'subject', 'mode', 'message_chars',
              'response_chars', 'had_file', 'had_image', 'status', 'created_at']

    def _csv_field(value):
        s = '' if value is None else str(value)
        if any(c in s for c in (',', '"', '\n')):
            s = '"' + s.replace('"', '""') + '"'
        return s

    def generate_csv():
        yield ','.join(header) + '\n'
        for r in rows:
            yield ','.join(_csv_field(r[h]) for h in header) + '\n'

    return Response(
        generate_csv(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=studymate_usage_logs.csv'},
    )


AUDIT_LOG_HTML = r'''
<!DOCTYPE html>
<html lang="vi">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Nhật ký hệ thống - StudyMate AI Max</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script>tailwind.config = { darkMode: 'class' };</script>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.6.0/css/all.min.css">
</head>
<body class="bg-[#0f0f0f] text-gray-200 min-h-screen">
  <nav class="sticky top-0 z-10 bg-[#0f0f0f]/90 backdrop-blur border-b border-gray-800 px-4 sm:px-6 py-3 flex items-center justify-between">
    <div class="flex items-center gap-2 font-bold"><i class="fas fa-scroll text-red-400"></i> Nhật ký hệ thống (Super Admin)</div>
    <a href="/developer" class="text-sm text-indigo-400 hover:underline"><i class="fas fa-arrow-left mr-1"></i>Về Dashboard</a>
  </nav>
  <main class="max-w-5xl mx-auto px-4 sm:px-6 py-6">
    <p class="text-xs text-gray-500 mb-4">Ghi lại các thao tác nhạy cảm: đổi vai trò, khoá/mở khoá, xoá tài khoản, reset session, cấu hình hệ thống. 200 dòng gần nhất.</p>
    <div class="bg-[#1a1a1a] rounded-2xl border border-gray-800 overflow-x-auto">
      <table class="w-full text-sm min-w-[640px]">
        <thead>
          <tr class="text-left text-gray-500 text-xs uppercase border-b border-gray-800">
            <th class="py-2.5 px-4">Thời gian</th>
            <th class="py-2.5 px-4">Người thực hiện</th>
            <th class="py-2.5 px-4">Hành động</th>
            <th class="py-2.5 px-4">Đối tượng</th>
            <th class="py-2.5 px-4">Chi tiết</th>
          </tr>
        </thead>
        <tbody>
          {% for log in logs %}
          <tr class="border-b border-gray-900">
            <td class="py-2.5 px-4 text-gray-500 whitespace-nowrap">{{ log.created_at[:16].replace('T',' ') }}</td>
            <td class="py-2.5 px-4 font-medium">{{ log.actor_username or '(hệ thống)' }}</td>
            <td class="py-2.5 px-4"><span class="text-xs font-semibold px-2 py-0.5 rounded-full bg-gray-800">{{ log.action }}</span></td>
            <td class="py-2.5 px-4 text-gray-400">{{ log.target }}</td>
            <td class="py-2.5 px-4 text-gray-500">{{ log.detail }}</td>
          </tr>
          {% else %}
          <tr><td colspan="5" class="py-8 text-center text-gray-500">Chưa có nhật ký nào.</td></tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
  </main>
</body>
</html>
'''


DEV_LAB_HTML = r'''
<!DOCTYPE html>
<html lang="vi">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>StudyMate Lab - StudyMate AI Max</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script>tailwind.config = { darkMode: 'class' };</script>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.6.0/css/all.min.css">
</head>
<body class="bg-[#0f0f0f] text-gray-200 min-h-screen">
  <nav class="sticky top-0 z-10 bg-[#0f0f0f]/90 backdrop-blur border-b border-gray-800 px-4 sm:px-6 py-3 flex items-center justify-between">
    <div class="flex items-center gap-2 font-bold"><i class="fas fa-flask text-pink-400"></i> 🧪 StudyMate Lab</div>
    <a href="/developer" class="text-sm text-indigo-400 hover:underline"><i class="fas fa-arrow-left mr-1"></i>Về Dashboard</a>
  </nav>
  <main class="max-w-5xl mx-auto px-4 sm:px-6 py-6 space-y-6">
    {% with messages = get_flashed_messages() %}
      {% if messages %}
        <div class="space-y-2">
          {% for m in messages %}
          <div class="text-sm text-emerald-300 bg-emerald-900/30 border border-emerald-800 rounded-xl px-4 py-2.5">{{ m }}</div>
          {% endfor %}
        </div>
      {% endif %}
    {% endwith %}

    <div>
      <h1 class="text-xl font-bold flex items-center gap-2"><i class="fas fa-toggle-on text-pink-400"></i> Feature Flags</h1>
      <p class="text-sm text-gray-500 mt-1">Đăng ký, thử nghiệm, và tăng dần tỉ lệ ra mắt tính năng mới một cách an toàn — không cần sửa code hay khởi động lại server.</p>
    </div>

    <!-- Đăng ký tính năng mới -->
    <div class="bg-[#1a1a1a] rounded-2xl border border-gray-800 p-5">
      <h2 class="font-bold mb-3 flex items-center gap-2 text-sm"><i class="fas fa-plus text-pink-400"></i> Đăng ký tính năng mới</h2>
      <form method="POST" action="{{ url_for('developer_lab_create_flag') }}" class="grid sm:grid-cols-2 gap-2.5">
        <input name="key" required maxlength="60" placeholder="feature.key (vd: game.snake_quiz)"
          class="px-3 py-2 rounded-lg bg-[#0f0f0f] border border-gray-700 text-sm focus:outline-none focus:ring-2 focus:ring-pink-500 font-mono">
        <input name="name" maxlength="80" placeholder="Tên hiển thị (vd: Snake Quiz)"
          class="px-3 py-2 rounded-lg bg-[#0f0f0f] border border-gray-700 text-sm focus:outline-none focus:ring-2 focus:ring-pink-500">
        <select name="category" class="px-3 py-2 rounded-lg bg-[#0f0f0f] border border-gray-700 text-sm focus:outline-none focus:ring-2 focus:ring-pink-500">
          {% for c in categories %}<option value="{{ c }}">{{ c }}</option>{% endfor %}
        </select>
        <input name="description" maxlength="300" placeholder="Mô tả ngắn"
          class="px-3 py-2 rounded-lg bg-[#0f0f0f] border border-gray-700 text-sm focus:outline-none focus:ring-2 focus:ring-pink-500">
        <button type="submit" class="sm:col-span-2 px-4 py-2.5 rounded-lg bg-pink-600 hover:bg-pink-700 text-white text-sm font-semibold">
          Đăng ký (luôn bắt đầu ở INTERNAL — không bao giờ tự động công khai)
        </button>
      </form>
    </div>

    <!-- Tìm kiếm / lọc -->
    <form method="GET" class="flex flex-wrap gap-2">
      <input name="q" value="{{ search_q }}" placeholder="Tìm theo tên, key, người tạo..."
        class="flex-1 min-w-[180px] px-3 py-2 rounded-lg bg-[#1a1a1a] border border-gray-700 text-sm focus:outline-none focus:ring-2 focus:ring-pink-500">
      <select name="status" onchange="this.form.submit()" class="px-3 py-2 rounded-lg bg-[#1a1a1a] border border-gray-700 text-sm">
        <option value="">Mọi trạng thái</option>
        {% for s in statuses %}<option value="{{ s }}" {{ 'selected' if s == status_filter else '' }}>{{ s }}</option>{% endfor %}
      </select>
      <select name="category" onchange="this.form.submit()" class="px-3 py-2 rounded-lg bg-[#1a1a1a] border border-gray-700 text-sm">
        <option value="">Mọi danh mục</option>
        {% for c in categories %}<option value="{{ c }}" {{ 'selected' if c == category_filter else '' }}>{{ c }}</option>{% endfor %}
      </select>
      <button type="submit" class="px-4 py-2 rounded-lg bg-gray-800 hover:bg-gray-700 text-sm font-semibold">Lọc</button>
    </form>

    <!-- Danh sách flags -->
    <div class="space-y-3">
      {% for f in flags %}
      <div class="bg-[#1a1a1a] rounded-2xl border border-gray-800 p-4">
        <div class="flex flex-wrap items-start justify-between gap-3">
          <div class="min-w-0">
            <div class="flex items-center gap-2 flex-wrap">
              <a href="{{ url_for('developer_lab_feature_detail', key=f.key) }}" class="font-semibold hover:text-pink-400">{{ f.name or f.key }}</a>
              <span class="text-[10px] font-mono text-gray-500">{{ f.key }}</span>
              <span class="text-[10px] font-semibold px-2 py-0.5 rounded-full
                {{ 'bg-red-900/50 text-red-300' if f.status in ('off','archived') else 'bg-amber-900/50 text-amber-300' if f.status in ('internal','beta') else 'bg-emerald-900/50 text-emerald-300' }}">
                {{ f.status|upper }}{{ ' ' ~ f.rollout_pct ~ '%' if f.status == 'beta' else '' }}
              </span>
              {% if f.is_expiring_soon %}<span class="text-[10px] font-semibold px-2 py-0.5 rounded-full bg-orange-900/50 text-orange-300"><i class="fas fa-clock"></i> Sắp hết hạn</span>{% endif %}
            </div>
            {% if f.description %}<p class="text-xs text-gray-500 mt-1">{{ f.description }}</p>{% endif %}
            <p class="text-[11px] text-gray-600 mt-1">{{ f.category }} · v{{ f.version }} · {{ f.environment }} · chủ: {{ f.owner_username or '—' }} · cập nhật {{ f.updated_at[:16].replace('T',' ') }}</p>
          </div>
          <a href="{{ url_for('developer_lab_feature_detail', key=f.key) }}" class="flex-shrink-0 text-xs font-semibold px-3 py-1.5 rounded-lg bg-gray-800 hover:bg-gray-700">Quản lý →</a>
        </div>
      </div>
      {% else %}
      <p class="text-sm text-gray-500 text-center py-10">Chưa có tính năng nào được đăng ký — đăng ký tính năng đầu tiên ở trên.</p>
      {% endfor %}
    </div>

    <div class="bg-[#1a1a1a] rounded-2xl border border-gray-800 p-5">
      <h2 class="font-bold mb-2 flex items-center gap-2"><i class="fas fa-gamepad text-indigo-400"></i> Trò chơi học tập</h2>
      <p class="text-xs text-gray-500 mb-3">Số liệu tổng hợp toàn hệ thống cho các trò chơi đã có (Lật thẻ ghi nhớ, Đố Vui Tính Nhanh).</p>
      <div class="grid sm:grid-cols-3 gap-3">
        <div class="rounded-xl border border-gray-800 p-3.5">
          <p class="text-xs text-gray-500 uppercase">Tổng lượt chơi</p>
          <p class="text-2xl font-bold mt-1">{{ game_stats.total_sessions }}</p>
        </div>
        <div class="rounded-xl border border-gray-800 p-3.5">
          <p class="text-xs text-gray-500 uppercase">Điểm trung bình</p>
          <p class="text-2xl font-bold mt-1">{{ game_stats.avg_score }}</p>
        </div>
        <div class="rounded-xl border border-gray-800 p-3.5">
          <p class="text-xs text-gray-500 uppercase">Độ chính xác TB</p>
          <p class="text-2xl font-bold mt-1">{{ game_stats.avg_accuracy }}%</p>
        </div>
      </div>
    </div>
  </main>
</body>
</html>
'''


DEV_LAB_FEATURE_HTML = r'''
<!DOCTYPE html>
<html lang="vi">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{{ flag.name or flag.key }} · StudyMate Lab</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script>tailwind.config = { darkMode: 'class' };</script>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.6.0/css/all.min.css">
</head>
<body class="bg-[#0f0f0f] text-gray-200 min-h-screen">
  <nav class="sticky top-0 z-10 bg-[#0f0f0f]/90 backdrop-blur border-b border-gray-800 px-4 sm:px-6 py-3 flex items-center justify-between">
    <div class="flex items-center gap-2 font-bold"><i class="fas fa-flask text-pink-400"></i> 🧪 StudyMate Lab</div>
    <a href="{{ url_for('developer_lab') }}" class="text-sm text-indigo-400 hover:underline"><i class="fas fa-arrow-left mr-1"></i>Tất cả tính năng</a>
  </nav>
  <main class="max-w-3xl mx-auto px-4 sm:px-6 py-6 space-y-6">
    {% with messages = get_flashed_messages() %}
      {% if messages %}
        <div class="space-y-2">
          {% for m in messages %}
          <div class="text-sm text-emerald-300 bg-emerald-900/30 border border-emerald-800 rounded-xl px-4 py-2.5">{{ m }}</div>
          {% endfor %}
        </div>
      {% endif %}
    {% endwith %}

    <div>
      <div class="flex items-center gap-2 flex-wrap">
        <h1 class="text-xl font-bold">{{ flag.name or flag.key }}</h1>
        <span class="text-xs font-mono text-gray-500">{{ flag.key }}</span>
        <span class="text-[10px] font-semibold px-2 py-0.5 rounded-full
          {{ 'bg-red-900/50 text-red-300' if flag.status in ('off','archived') else 'bg-amber-900/50 text-amber-300' if flag.status in ('internal','beta') else 'bg-emerald-900/50 text-emerald-300' }}">
          {{ flag.status|upper }}
        </span>
      </div>
      {% if flag.description %}<p class="text-sm text-gray-400 mt-1">{{ flag.description }}</p>{% endif %}
    </div>

    <!-- Trạng thái + rollout -->
    <div class="bg-[#1a1a1a] rounded-2xl border border-gray-800 p-5">
      <h2 class="font-bold text-sm mb-3">Trạng thái</h2>
      <form method="POST" action="{{ url_for('developer_lab_update_status', flag_id=flag.id) }}" id="statusForm" class="space-y-3">
        <div class="flex flex-wrap gap-2">
          {% for s in statuses %}
          <button type="submit" name="status" value="{{ s }}"
            {% if s == 'off' and flag.status == 'public' %}onclick="return confirm('Đây là kill switch — TẮT NGAY tính năng này cho TẤT CẢ người dùng đang dùng thật. Chắc chắn chứ?');"{% endif %}
            class="px-3.5 py-2 rounded-lg text-sm font-semibold {{ 'bg-pink-600 text-white' if s == flag.status else 'bg-gray-800 text-gray-400 hover:bg-gray-700' }}">
            {{ s }}
          </button>
          {% endfor %}
        </div>
        <div id="rolloutRow" class="flex items-center gap-3 {{ '' if flag.status == 'beta' else 'opacity-40' }}">
          <label class="text-xs text-gray-400">Rollout (chỉ áp dụng khi ở BETA):</label>
          <input type="range" name="rollout_pct" min="0" max="100" step="1" value="{{ flag.rollout_pct }}"
            oninput="document.getElementById('rolloutVal').textContent = this.value + '%';"
            class="flex-1">
          <span id="rolloutVal" class="text-sm font-semibold w-12 text-right">{{ flag.rollout_pct }}%</span>
        </div>
        <p class="text-xs text-gray-500">Developer trở lên LUÔN thấy được tính năng (trừ khi ở <strong>off</strong>/<strong>archived</strong>) để tự test bất cứ lúc nào.</p>
      </form>
    </div>

    <!-- Cấu hình -->
    <div class="bg-[#1a1a1a] rounded-2xl border border-gray-800 p-5">
      <h2 class="font-bold text-sm mb-3">Cấu hình</h2>
      <form method="POST" action="{{ url_for('developer_lab_configure_flag', flag_id=flag.id) }}" class="grid sm:grid-cols-2 gap-2.5">
        <div>
          <label class="text-xs text-gray-500">Tên hiển thị</label>
          <input name="name" value="{{ flag.name }}" maxlength="80" class="w-full px-3 py-2 rounded-lg bg-[#0f0f0f] border border-gray-700 text-sm mt-1">
        </div>
        <div>
          <label class="text-xs text-gray-500">Phiên bản</label>
          <input name="version" value="{{ flag.version }}" maxlength="20" class="w-full px-3 py-2 rounded-lg bg-[#0f0f0f] border border-gray-700 text-sm mt-1">
        </div>
        <div>
          <label class="text-xs text-gray-500">Danh mục</label>
          <select name="category" class="w-full px-3 py-2 rounded-lg bg-[#0f0f0f] border border-gray-700 text-sm mt-1">
            {% for c in categories %}<option value="{{ c }}" {{ 'selected' if c == flag.category else '' }}>{{ c }}</option>{% endfor %}
          </select>
        </div>
        <div>
          <label class="text-xs text-gray-500">Môi trường</label>
          <select name="environment" class="w-full px-3 py-2 rounded-lg bg-[#0f0f0f] border border-gray-700 text-sm mt-1">
            {% for e in environments %}<option value="{{ e }}" {{ 'selected' if e == flag.environment else '' }}>{{ e }}</option>{% endfor %}
          </select>
          <p class="text-[10px] text-gray-600 mt-1">Chỉ mang tính ghi chú — app chạy 1 server duy nhất, không có hạ tầng tách môi trường thật.</p>
        </div>
        <div class="sm:col-span-2">
          <label class="text-xs text-gray-500">Mô tả</label>
          <textarea name="description" rows="2" maxlength="300" class="w-full px-3 py-2 rounded-lg bg-[#0f0f0f] border border-gray-700 text-sm mt-1">{{ flag.description }}</textarea>
        </div>
        <div>
          <label class="text-xs text-gray-500">Phụ thuộc vào (feature key, cách nhau bởi dấu phẩy)</label>
          <input name="depends_on" value="{{ flag.depends_on }}" placeholder="vd: quiz_engine,xp_system" class="w-full px-3 py-2 rounded-lg bg-[#0f0f0f] border border-gray-700 text-sm mt-1 font-mono">
        </div>
        <div>
          <label class="text-xs text-gray-500">Ngày hết hạn (tuỳ chọn)</label>
          <input type="date" name="expires_at" value="{{ flag.expires_at[:10] if flag.expires_at else '' }}" class="w-full px-3 py-2 rounded-lg bg-[#0f0f0f] border border-gray-700 text-sm mt-1">
        </div>
        <button type="submit" class="sm:col-span-2 px-4 py-2.5 rounded-lg bg-gray-800 hover:bg-gray-700 text-white text-sm font-semibold">Lưu cấu hình</button>
      </form>
    </div>

    <!-- Phụ thuộc -->
    {% if dependencies %}
    <div class="bg-[#1a1a1a] rounded-2xl border border-gray-800 p-5">
      <h2 class="font-bold text-sm mb-3">Phụ thuộc</h2>
      <div class="space-y-2">
        {% for d in dependencies %}
        <div class="flex items-center justify-between text-sm rounded-lg border border-gray-800 px-3 py-2">
          <span class="font-mono">{{ d.key }}</span>
          {% if d.found %}
            <span class="text-xs px-2 py-0.5 rounded-full {{ 'bg-emerald-900/50 text-emerald-300' if d.status == 'public' else 'bg-amber-900/50 text-amber-300' }}">{{ d.status }}</span>
          {% else %}
            <span class="text-xs px-2 py-0.5 rounded-full bg-red-900/50 text-red-300"><i class="fas fa-triangle-exclamation mr-1"></i>không tìm thấy</span>
          {% endif %}
        </div>
        {% endfor %}
      </div>
    </div>
    {% endif %}

    <!-- Xoá hẳn -->
    <div class="bg-[#1a1a1a] rounded-2xl border border-red-900/50 p-5">
      <h2 class="font-bold text-sm mb-2 text-red-400">Vùng nguy hiểm</h2>
      <p class="text-xs text-gray-500 mb-3">Xoá hẳn tính năng này khỏi hệ thống (khác với "archived" — archived vẫn giữ lại lịch sử để tra cứu). Chỉ Admin trở lên thực hiện được.</p>
      <form method="POST" action="{{ url_for('developer_lab_delete_flag', flag_id=flag.id) }}" onsubmit="return confirm('Xoá hẳn \'' + {{ flag.key|tojson }} + '\'? Không thể hoàn tác.');">
        <button type="submit" class="px-4 py-2 rounded-lg bg-red-900/50 hover:bg-red-900 text-red-300 text-sm font-semibold">Xoá hẳn tính năng này</button>
      </form>
    </div>

    <!-- Audit log riêng của flag này -->
    <div class="bg-[#1a1a1a] rounded-2xl border border-gray-800 p-5">
      <h2 class="font-bold text-sm mb-3">Nhật ký thay đổi</h2>
      <div class="space-y-2">
        {% for log in logs %}
        <div class="text-xs border-b border-gray-900 pb-2">
          <span class="text-gray-500">{{ log.created_at[:16].replace('T',' ') }}</span> ·
          <span class="font-medium">{{ log.actor_username or '(hệ thống)' }}</span> ·
          {{ log.detail }}
        </div>
        {% else %}
        <p class="text-sm text-gray-500 text-center py-4">Chưa có thay đổi nào được ghi lại.</p>
        {% endfor %}
      </div>
    </div>
  </main>
</body>
</html>
'''


@app.route('/developer/lab')
@developer_required
def developer_lab():
    db = get_db()
    q = (request.args.get('q') or '').strip()
    status_filter = (request.args.get('status') or '').strip()
    category_filter = (request.args.get('category') or '').strip()

    query = 'SELECT * FROM feature_flags WHERE 1=1'
    params = []
    if q:
        query += ' AND (key LIKE ? OR name LIKE ? OR owner_username LIKE ?)'
        like = f'%{q}%'
        params += [like, like, like]
    if status_filter in FEATURE_FLAG_STATUSES:
        query += ' AND status = ?'
        params.append(status_filter)
    if category_filter in FEATURE_CATEGORIES:
        query += ' AND category = ?'
        params.append(category_filter)
    query += ' ORDER BY updated_at DESC'
    flags = db.execute(query, params).fetchall()

    today = now_iso()[:10]
    flags_out = []
    for f in flags:
        d = dict(f)
        d['is_expiring_soon'] = bool(d['expires_at'] and d['status'] in ('internal', 'beta') and d['expires_at'][:10] <= today)
        flags_out.append(d)

    sessions = db.execute('SELECT score, correct_count, total_count FROM game_sessions').fetchall()
    total_sessions = len(sessions)
    avg_score = round(sum(s['score'] for s in sessions) / total_sessions) if total_sessions else 0
    total_answered = sum(s['total_count'] for s in sessions)
    total_correct = sum(s['correct_count'] for s in sessions)
    avg_accuracy = round(100 * total_correct / total_answered) if total_answered else 0

    return render_template_string(
        DEV_LAB_HTML,
        flags=flags_out,
        all_flag_keys=[f['key'] for f in flags],
        search_q=q, status_filter=status_filter, category_filter=category_filter,
        statuses=FEATURE_FLAG_STATUSES, categories=FEATURE_CATEGORIES, environments=FEATURE_ENVIRONMENTS,
        rollout_steps=FEATURE_ROLLOUT_STEPS,
        game_stats={'total_sessions': total_sessions, 'avg_score': avg_score, 'avg_accuracy': avg_accuracy},
    )


@app.route('/developer/lab/features/<key>')
@developer_required
def developer_lab_feature_detail(key):
    db = get_db()
    flag = db.execute('SELECT * FROM feature_flags WHERE key = ?', (key,)).fetchone()
    if not flag:
        flash(f"Không tìm thấy tính năng '{key}'.")
        return redirect(url_for('developer_lab'))

    logs = db.execute(
        'SELECT * FROM audit_logs WHERE target = ? AND action LIKE ? ORDER BY id DESC LIMIT 100',
        (key, '%feature%')
    ).fetchall()

    deps = [d.strip() for d in (flag['depends_on'] or '').split(',') if d.strip()]
    dep_rows = []
    for dep_key in deps:
        dep_flag = db.execute('SELECT key, name, status FROM feature_flags WHERE key = ?', (dep_key,)).fetchone()
        dep_rows.append({'key': dep_key, 'found': bool(dep_flag), 'status': dep_flag['status'] if dep_flag else None,
                          'name': dep_flag['name'] if dep_flag else ''})

    return render_template_string(
        DEV_LAB_FEATURE_HTML,
        flag=dict(flag), logs=[dict(l) for l in logs], dependencies=dep_rows,
        statuses=FEATURE_FLAG_STATUSES, categories=FEATURE_CATEGORIES, environments=FEATURE_ENVIRONMENTS,
        rollout_steps=FEATURE_ROLLOUT_STEPS,
    )


@app.route('/developer/lab/flags', methods=['POST'])
@developer_required
def developer_lab_create_flag():
    key = (request.form.get('key') or '').strip().lower().replace(' ', '_')
    name = (request.form.get('name') or '').strip()[:80] or key
    category = (request.form.get('category') or 'other').strip()
    description = (request.form.get('description') or '').strip()[:300]
    if category not in FEATURE_CATEGORIES:
        category = 'other'

    if not re.match(r'^[a-z0-9_.]{2,60}$', key):
        flash('Tên flag chỉ gồm chữ thường/số/gạch dưới/dấu chấm, 2-60 ký tự (vd: game.snake_quiz).')
        return redirect(url_for('developer_lab'))

    db = get_db()
    existing = db.execute('SELECT id FROM feature_flags WHERE key = ?', (key,)).fetchone()
    if existing:
        flash(f"Flag '{key}' đã tồn tại.")
        return redirect(url_for('developer_lab'))

    # Luật bắt buộc: tính năng mới đăng ký KHÔNG BAO GIỜ tự động public — luôn bắt đầu ở
    # 'internal' (chỉ Developer trở lên thấy được) để tự test an toàn trước.
    db.execute(
        '''INSERT INTO feature_flags (key, name, status, category, description, owner_username,
           environment, version, rollout_pct, created_at, updated_at)
           VALUES (?, ?, 'internal', ?, ?, ?, 'development', '1.0.0', 0, ?, ?)''',
        (key, name, category, description, session.get('username', ''), now_iso(), now_iso())
    )
    db.commit()
    write_audit('create_feature_flag', target=key, detail=f"đăng ký mới, bắt đầu ở INTERNAL ({name})")
    flash(f"Đã đăng ký tính năng '{key}' — bắt đầu ở trạng thái INTERNAL (an toàn mặc định).")
    return redirect(url_for('developer_lab_feature_detail', key=key))


@app.route('/developer/lab/flags/<int:flag_id>/status', methods=['POST'])
@developer_required
def developer_lab_update_status(flag_id):
    status = (request.form.get('status') or '').strip()
    try:
        rollout_pct = int(request.form.get('rollout_pct', 0))
    except (TypeError, ValueError):
        rollout_pct = 0
    rollout_pct = max(0, min(100, rollout_pct))

    if status not in FEATURE_FLAG_STATUSES:
        flash('Trạng thái không hợp lệ.')
        return redirect(url_for('developer_lab'))

    db = get_db()
    flag = db.execute('SELECT * FROM feature_flags WHERE id = ?', (flag_id,)).fetchone()
    if not flag:
        flash('Không tìm thấy flag này.')
        return redirect(url_for('developer_lab'))

    # "Kill switch" / thao tác nguy hiểm: chuyển 1 tính năng ĐANG public về off/archived —
    # ghi audit chi tiết hơn (log rõ giá trị cũ -> mới) vì đây là hành động ảnh hưởng ngay tới
    # người dùng thật đang dùng tính năng đó.
    old_status = flag['status']
    db.execute(
        'UPDATE feature_flags SET status = ?, rollout_pct = ?, updated_at = ? WHERE id = ?',
        (status, rollout_pct if status == 'beta' else flag['rollout_pct'], now_iso(), flag_id)
    )
    db.commit()
    detail = f"{old_status} → {status}" + (f" (rollout {rollout_pct}%)" if status == 'beta' else '')
    write_audit('update_feature_status', target=flag['key'], detail=detail)
    flash(f"Đã đổi '{flag['key']}': {detail}.")
    return redirect(request.referrer or url_for('developer_lab'))


@app.route('/developer/lab/flags/<int:flag_id>/configure', methods=['POST'])
@developer_required
def developer_lab_configure_flag(flag_id):
    db = get_db()
    flag = db.execute('SELECT * FROM feature_flags WHERE id = ?', (flag_id,)).fetchone()
    if not flag:
        flash('Không tìm thấy flag này.')
        return redirect(url_for('developer_lab'))

    name = (request.form.get('name') or '').strip()[:80] or flag['key']
    category = (request.form.get('category') or flag['category']).strip()
    if category not in FEATURE_CATEGORIES:
        category = flag['category']
    environment = (request.form.get('environment') or flag['environment']).strip()
    if environment not in FEATURE_ENVIRONMENTS:
        environment = flag['environment']
    description = (request.form.get('description') or '').strip()[:300]
    version = (request.form.get('version') or flag['version']).strip()[:20]
    expires_at = (request.form.get('expires_at') or '').strip() or None

    raw_deps = (request.form.get('depends_on') or '').strip()
    dep_keys = [d.strip().lower() for d in raw_deps.split(',') if d.strip()]
    if flag['key'] in dep_keys:
        flash('Một tính năng không thể tự phụ thuộc vào chính nó.')
        return redirect(url_for('developer_lab_feature_detail', key=flag['key']))
    depends_on = ','.join(dep_keys)

    db.execute(
        '''UPDATE feature_flags SET name = ?, category = ?, environment = ?, description = ?,
           version = ?, expires_at = ?, depends_on = ?, updated_at = ? WHERE id = ?''',
        (name, category, environment, description, version, expires_at, depends_on, now_iso(), flag_id)
    )
    db.commit()
    write_audit('configure_feature', target=flag['key'], detail=f"cập nhật cấu hình (v{version}, {environment})")
    flash(f"Đã cập nhật cấu hình '{flag['key']}'.")
    return redirect(url_for('developer_lab_feature_detail', key=flag['key']))


@app.route('/developer/lab/flags/<int:flag_id>/delete', methods=['POST'])
@admin_required
def developer_lab_delete_flag(flag_id):
    """Xoá hẳn 1 flag (khác với 'archived' — archived vẫn giữ lại lịch sử/audit log để tra
    cứu sau này, delete là xoá thật). Chỉ Admin trở lên được xoá, để tránh Developer lỡ tay
    xoá mất tính năng đang chạy production."""
    db = get_db()
    flag = db.execute('SELECT key FROM feature_flags WHERE id = ?', (flag_id,)).fetchone()
    if not flag:
        flash('Không tìm thấy flag này.')
        return redirect(url_for('developer_lab'))
    db.execute('DELETE FROM feature_flags WHERE id = ?', (flag_id,))
    db.commit()
    write_audit('delete_feature_flag', target=flag['key'], detail='đã xoá hẳn')
    flash(f"Đã xoá flag '{flag['key']}'.")
    return redirect(url_for('developer_lab'))


@app.route('/developer/audit')
@super_admin_required
def developer_audit_log():
    db = get_db()
    logs = db.execute('SELECT * FROM audit_logs ORDER BY id DESC LIMIT 200').fetchall()
    return render_template_string(AUDIT_LOG_HTML, logs=[dict(l) for l in logs])


# ==========================================
# 7. GỌI API AI (xAI / Consolex-compatible) — STREAMING
# ==========================================
def stream_consolex_ai(system_prompt: str, user_content, max_tokens: int = 800):
    """Gọi xAI API ở chế độ stream=True và yield từng đoạn token nhận được.

    Dùng SESSION (requests.Session) để tái sử dụng kết nối TCP/TLS,
    giúp giảm độ trễ so với việc tạo kết nối mới mỗi lần gọi.

    `max_tokens` thay đổi theo "Chế độ suy nghĩ" đang chọn (Trợ Lý/Học Giả/Giáo Sư/Thiên Tài)
    — chế độ càng sâu thì ngân sách token càng cao để AI có "chỗ" suy luận/giải thích kỹ hơn.
    """
    if not XAI_API_KEY:
        raise RuntimeError("Thiếu XAI_API_KEY. Vui lòng thiết lập biến môi trường trước khi chạy server.")

    url = f"{CONSOLEX_API_BASE.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {XAI_API_KEY}",
        "Content-Type": "application/json",
    }

    # Admin/Super Admin có thể ghi đè model & temperature từ /developer mà không cần sửa .env
    # hay khởi động lại server (đọc từ bảng settings, có cache 1 request qua get_setting -> get_db/g).
    model = get_setting('ai_model_override', '') or CONSOLEX_MODEL
    temp_override = get_setting('ai_temperature_override', '')
    try:
        temperature = float(temp_override) if temp_override else 0.7
    except ValueError:
        temperature = 0.7

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": True,
    }

    with SESSION.post(url, headers=headers, json=payload, timeout=60, stream=True) as resp:
        resp.raise_for_status()
        # QUAN TRỌNG: xAI trả JSON dạng UTF-8, nhưng header Content-Type của response
        # streaming thường không khai báo rõ "charset=utf-8". Khi đó thư viện `requests`
        # tự mặc định resp.encoding = 'ISO-8859-1' (theo chuẩn HTTP cũ cho text/*),
        # khiến decode_unicode=True bên dưới đọc sai từng byte UTF-8 của tiếng Việt
        # -> ra ký tự lạ kiểu "Æ¡", "áº§"... Ép rõ encoding='utf-8' để sửa tận gốc lỗi này.
        resp.encoding = 'utf-8'
        for raw_line in resp.iter_lines(decode_unicode=True):
            if not raw_line:
                continue
            if not raw_line.startswith("data: "):
                continue
            data_str = raw_line[len("data: "):].strip()
            if data_str == "[DONE]":
                break
            try:
                chunk = json.loads(data_str)
                delta = chunk["choices"][0]["delta"].get("content")
            except (json.JSONDecodeError, KeyError, IndexError):
                continue
            if delta:
                yield delta


# ==========================================
# 8.6 "THẺ GHI NHỚ" (FLASHCARDS) — TẠO BẰNG AI
# ==========================================
FLASHCARD_MAX_COUNT = 12
FLASHCARD_MIN_COUNT = 4


def generate_flashcards_via_ai(topic, subject, count=8):
    """Gọi AI 1 LẦN (không streaming) để tạo bộ thẻ ghi nhớ front/back cho 1 chủ đề. Trả về
    list[(front, back)]. Ném lỗi (ValueError/json.JSONDecodeError/RuntimeError/...) nếu AI trả
    về không đúng định dạng — bên gọi (api_generate_deck) chịu trách nhiệm bắt và báo lỗi
    thân thiện cho học sinh."""
    count = max(FLASHCARD_MIN_COUNT, min(int(count or 8), FLASHCARD_MAX_COUNT))
    subject_line = f" (môn {subject})" if subject else ""
    system_prompt = f"""
    Bạn là công cụ tạo thẻ ghi nhớ (flashcard) học tập cho học sinh THCS{subject_line}.
    Hãy tạo đúng {count} thẻ ghi nhớ cho chủ đề học sinh đưa ra. Mỗi thẻ gồm:
    - "front": câu hỏi hoặc thuật ngữ NGẮN GỌN (dưới 15 từ).
    - "back": câu trả lời/định nghĩa ngắn gọn, dễ hiểu, đúng trọng tâm (dưới 40 từ).
    CHỈ trả lời bằng JSON hợp lệ đúng định dạng mảng dưới đây — KHÔNG thêm ```markdown```,
    KHÔNG thêm lời giải thích nào khác ngoài JSON:
    [{{"front": "...", "back": "..."}}, ...]
    """
    raw = ''.join(stream_consolex_ai(system_prompt, f"Chủ đề: {topic}", max_tokens=1600))
    text = raw.strip()
    if text.startswith('```'):
        text = re.sub(r'^```[a-zA-Z]*\n?', '', text)
        text = re.sub(r'```\s*$', '', text).strip()

    data = json.loads(text)  # có thể ném json.JSONDecodeError — caller xử lý
    if not isinstance(data, list):
        raise ValueError("AI không trả về danh sách thẻ hợp lệ.")

    cards = []
    for item in data:
        if not isinstance(item, dict):
            continue
        front = str(item.get('front', '')).strip()[:200]
        back = str(item.get('back', '')).strip()[:500]
        if front and back:
            cards.append((front, back))
    return cards[:count]


# ==========================================
# 8.9 "QUIZ GENERATOR" (Phase 1) — sinh đề bằng AI, chấm tự động
# ==========================================
QUIZ_TYPE_LABELS = {'mcq': 'Trắc nghiệm', 'true_false': 'Đúng/Sai', 'fill_blank': 'Điền khuyết'}
QUIZ_DIFFICULTIES = ('easy', 'medium', 'hard', 'expert')
QUIZ_MIN_COUNT, QUIZ_MAX_COUNT = 3, 15


def generate_quiz_via_ai(topic, subject, difficulty, count=5, source_text=None):
    """Gọi AI 1 LẦN để sinh đề quiz — CHỈ 3 dạng chấm được tự động, chính xác, không tốn
    thêm lượt gọi AI lúc chấm: trắc nghiệm (mcq), đúng/sai (true_false), điền khuyết
    (fill_blank). Mỗi câu có "topic" riêng để sau này thống kê điểm mạnh/yếu theo chủ đề con.
    Trả về list[dict]. Ném lỗi nếu AI trả JSON không hợp lệ — caller xử lý."""
    count = max(QUIZ_MIN_COUNT, min(int(count or 5), QUIZ_MAX_COUNT))
    subject_line = f" (môn {subject})" if subject else ""
    diff_label = {'easy': 'Dễ', 'medium': 'Trung bình', 'hard': 'Khó', 'expert': 'Nâng cao'}.get(difficulty, 'Trung bình')

    context_block = ""
    if source_text:
        context_block = f"""
    Dựa trên nội dung sau đây (trích từ đoạn hội thoại đã học):
    ---
    {source_text[:4000]}
    ---
    """

    system_prompt = f"""
    Bạn là công cụ tạo đề kiểm tra cho học sinh THCS{subject_line}. Độ khó: {diff_label}.
    {context_block}
    Hãy tạo đúng {count} câu hỏi cho chủ đề học sinh đưa ra, CHỈ dùng 3 dạng sau (trộn hợp lý):
    - "mcq": trắc nghiệm 4 lựa chọn, field "options" là mảng 4 chuỗi, "correct_answer" là
      NGUYÊN VĂN 1 trong 4 lựa chọn đó (không phải số thứ tự).
    - "true_false": đúng/sai, "correct_answer" là "Đúng" hoặc "Sai", không cần "options".
    - "fill_blank": điền khuyết (dùng "___" trong câu hỏi để chỉ chỗ trống), "correct_answer"
      là đáp án ngắn gọn (1-3 từ), không cần "options".
    Mỗi câu đều cần "explanation" (giải thích ngắn gọn đáp án) và "topic" (chủ đề con của câu
    hỏi đó, vd "Hằng đẳng thức", để sau này biết học sinh yếu phần nào).
    CHỈ trả lời bằng JSON hợp lệ đúng định dạng mảng dưới đây — KHÔNG thêm ```markdown```,
    KHÔNG thêm lời giải thích nào khác ngoài JSON:
    [{{"q_type": "mcq", "question": "...", "options": ["...","...","...","..."], "correct_answer": "...", "explanation": "...", "topic": "..."}}, ...]
    """
    raw = ''.join(stream_consolex_ai(system_prompt, f"Chủ đề: {topic}", max_tokens=2400))
    text = raw.strip()
    if text.startswith('```'):
        text = re.sub(r'^```[a-zA-Z]*\n?', '', text)
        text = re.sub(r'```\s*$', '', text).strip()

    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError("AI không trả về danh sách câu hỏi hợp lệ.")

    questions = []
    for item in data:
        if not isinstance(item, dict):
            continue
        q_type = str(item.get('q_type', '')).strip()
        if q_type not in QUIZ_TYPE_LABELS:
            continue
        question = str(item.get('question', '')).strip()[:500]
        correct = str(item.get('correct_answer', '')).strip()[:200]
        if not question or not correct:
            continue
        options = item.get('options') if q_type == 'mcq' else None
        if q_type == 'mcq':
            if not isinstance(options, list) or len(options) < 2:
                continue
            options = [str(o).strip()[:200] for o in options][:6]
        questions.append({
            'q_type': q_type, 'question': question, 'options': options,
            'correct_answer': correct, 'explanation': str(item.get('explanation', '')).strip()[:400],
            'topic': str(item.get('topic', '')).strip()[:60] or (subject or 'Chung'),
        })
    return questions[:count]


def grade_quiz_answer(q_type, correct_answer, given_answer):
    """So khớp đáp án — chuẩn hoá (bỏ dấu cách thừa, không phân biệt hoa/thường) để chấm công
    bằng hơn, không cần gọi AI chấm (giữ việc chấm bài NHANH và MIỄN PHÍ)."""
    a = (correct_answer or '').strip().lower()
    b = (given_answer or '').strip().lower()
    a = re.sub(r'\s+', ' ', a)
    b = re.sub(r'\s+', ' ', b)
    return a == b


# ==========================================
# 8.10 "STUDY PLAN" (Phase 1) — AI chia mục tiêu ôn tập thành kế hoạch theo ngày
# ==========================================
STUDY_PLAN_MIN_DAYS, STUDY_PLAN_MAX_DAYS = 3, 60


def generate_study_plan_via_ai(goal, subject, days, remaining_topics=None):
    """Gọi AI 1 LẦN để chia 1 mục tiêu ôn tập thành các việc theo từng ngày. Nếu
    `remaining_topics` được truyền vào (trường hợp "Sắp xếp lại kế hoạch" khi học sinh bị
    trễ tiến độ), AI sẽ phân bổ LẠI đúng các việc còn thiếu đó vào số ngày còn lại, thay vì
    bịa ra kế hoạch hoàn toàn mới. Trả về list[(title, description)] theo đúng thứ tự ngày."""
    days = max(STUDY_PLAN_MIN_DAYS, min(int(days or 14), STUDY_PLAN_MAX_DAYS))
    subject_line = f" (môn {subject})" if subject else ""

    if remaining_topics:
        topics_block = "\n".join(f"- {t}" for t in remaining_topics)
        user_content = (
            f"Mục tiêu: {goal}\nSố ngày còn lại: {days}\n"
            f"Các việc CHƯA hoàn thành cần phân bổ lại vào đúng {days} ngày còn lại "
            f"(có thể gộp/rút gọn nếu không đủ ngày, ưu tiên phần quan trọng trước):\n{topics_block}"
        )
    else:
        user_content = f"Mục tiêu: {goal}\nSố ngày: {days}"

    system_prompt = f"""
    Bạn là công cụ lập kế hoạch ôn tập cho học sinh THCS{subject_line}.
    Hãy chia mục tiêu học sinh đưa ra thành đúng {days} việc, mỗi việc cho 1 ngày (DAY 1 tới
    DAY {days}), sắp xếp từ nền tảng tới nâng cao, ngày cuối nên là ôn tập tổng hợp/kiểm tra thử.
    Mỗi việc gồm "title" (chủ đề ngắn gọn, dưới 8 từ) và "description" (1 câu mô tả việc cần
    làm hôm đó, dưới 30 từ).
    CHỈ trả lời bằng JSON hợp lệ đúng định dạng mảng dưới đây, ĐÚNG {days} phần tử theo thứ tự
    ngày — KHÔNG thêm ```markdown```, KHÔNG thêm lời giải thích nào khác ngoài JSON:
    [{{"title": "...", "description": "..."}}, ...]
    """
    raw = ''.join(stream_consolex_ai(system_prompt, user_content, max_tokens=2200))
    text = raw.strip()
    if text.startswith('```'):
        text = re.sub(r'^```[a-zA-Z]*\n?', '', text)
        text = re.sub(r'```\s*$', '', text).strip()

    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError("AI không trả về kế hoạch hợp lệ.")

    tasks = []
    for item in data:
        if not isinstance(item, dict):
            continue
        title = str(item.get('title', '')).strip()[:120]
        description = str(item.get('description', '')).strip()[:300]
        if title:
            tasks.append((title, description))
    return tasks[:days]


# 8. UPLOAD FILE / ẢNH
# ==========================================
def _truncate_text(full_text: str, text_limit=None) -> str:
    if text_limit is None:
        return full_text
    truncated = full_text[:text_limit]
    if len(full_text) > text_limit:
        truncated += "\n\n[... nội dung bị cắt bớt do quá dài — nâng cấp gói để trích xuất được nhiều hơn ...]"
    return truncated


def handle_pdf_upload(raw, text_limit=None):
    try:
        reader = PdfReader(io.BytesIO(raw))
        num_pages = len(reader.pages)
        text_parts = []
        for page in reader.pages:
            page_text = page.extract_text() or ""
            if page_text.strip():
                text_parts.append(page_text)
        full_text = "\n\n".join(text_parts).strip()

        if not full_text:
            return jsonify({
                "error": "File PDF này có vẻ là bản scan/ảnh nên chưa trích được chữ. "
                         "Em thử gõ trực tiếp câu hỏi hoặc nội dung cần hỏi nhé!"
            }), 200

        return jsonify({"text": _truncate_text(full_text, text_limit), "pages": num_pages})
    except Exception as e:
        return jsonify({"error": f"Không đọc được file PDF: {e}"}), 500


def handle_docx_upload(raw, text_limit=None):
    if docx_lib is None:
        return jsonify({
            "error": "Server chưa cài thư viện đọc Word. Vui lòng chạy: pip install python-docx"
        }), 500
    try:
        document = docx_lib.Document(io.BytesIO(raw))
        paragraphs = [p.text for p in document.paragraphs if p.text.strip()]
        # Cũng lấy nội dung trong các bảng (table) nếu có.
        for table in document.tables:
            for row in table.rows:
                row_text = " | ".join(cell.text.strip() for cell in row.cells if cell.text.strip())
                if row_text:
                    paragraphs.append(row_text)

        full_text = "\n".join(paragraphs).strip()
        if not full_text:
            return jsonify({"error": "File Word này không có nội dung văn bản để đọc."}), 200

        return jsonify({"text": _truncate_text(full_text, text_limit), "pages": None})
    except Exception as e:
        return jsonify({"error": f"Không đọc được file Word: {e}"}), 500


def handle_text_upload(raw, text_limit=None):
    try:
        try:
            text = raw.decode('utf-8')
        except UnicodeDecodeError:
            text = raw.decode('utf-8', errors='ignore')
        text = text.strip()

        if not text:
            return jsonify({"error": "File này không có nội dung."}), 200

        return jsonify({"text": _truncate_text(text, text_limit), "pages": None})
    except Exception as e:
        return jsonify({"error": f"Không đọc được file: {e}"}), 500


def handle_image_upload(raw, filename, ext):
    # Dung lượng đã được kiểm tra theo gói (Free/Premium/Max) ở route /api/upload trước khi
    # gọi tới đây, nên không cần kiểm tra lại giới hạn cứng ở bước này nữa.
    mime_map = {
        '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
        '.gif': 'image/gif', '.webp': 'image/webp',
    }
    mime = mime_map.get(ext, 'image/jpeg')

    # Nếu có Pillow: thu nhỏ ảnh quá lớn để giảm dung lượng gửi lên AI -> phản hồi nhanh hơn.
    if Image is not None:
        try:
            img = Image.open(io.BytesIO(raw))
            img.load()
            if max(img.size) > MAX_IMAGE_DIMENSION:
                ratio = MAX_IMAGE_DIMENSION / max(img.size)
                new_size = (max(1, int(img.size[0] * ratio)), max(1, int(img.size[1] * ratio)))
                img = img.resize(new_size, Image.LANCZOS)

            buf = io.BytesIO()
            if mime == 'image/gif':
                # Giữ nguyên GIF (kể cả animation) thay vì convert.
                img.save(buf, format='GIF')
            elif img.mode in ('RGBA', 'P', 'LA'):
                img = img.convert('RGB')
                img.save(buf, format='JPEG', quality=85)
                mime = 'image/jpeg'
            else:
                img.save(buf, format='JPEG', quality=85)
                mime = 'image/jpeg'
            raw = buf.getvalue()
        except Exception:
            pass  # nếu xử lý ảnh lỗi, dùng bytes gốc

    b64 = base64.b64encode(raw).decode('utf-8')
    data_url = f"data:{mime};base64,{b64}"
    return jsonify({"type": "image", "dataUrl": data_url, "name": filename})


def _uploads_used_last_24h(user_id):
    window_start = (datetime.now(timezone.utc) - timedelta(hours=UPLOAD_QUOTA_WINDOW_HOURS)).isoformat()
    return get_db().execute(
        'SELECT COUNT(*) c FROM file_uploads WHERE user_id = ? AND created_at >= ?',
        (user_id, window_start)
    ).fetchone()['c']


@app.route('/api/upload', methods=['POST'])
@login_required
def upload_file():
    if 'file' not in request.files:
        return jsonify({"error": "Không tìm thấy file trong yêu cầu."}), 400

    f = request.files['file']
    filename = f.filename or ''
    ext = os.path.splitext(filename.lower())[1]

    if ext not in ALLOWED_IMAGE_EXT and ext not in ALLOWED_DOC_EXT:
        return jsonify({
            "error": f"Định dạng {ext or 'không xác định'} chưa được hỗ trợ. "
                     "Em thử PDF, Word (.docx), .txt, .csv hoặc ảnh (PNG/JPG/GIF/WEBP) nhé!"
        }), 400

    user = current_user()
    plan = effective_plan(user)
    limits = plan_limits(plan)
    label = plan_meta(plan)['label']

    # 1) Giới hạn dung lượng MỖI file/ảnh theo gói (Free ≤20MB, Premium ≤500MB, Max ≤1GB).
    raw = f.read()
    size_mb = len(raw) / (1024 * 1024)
    if size_mb > limits['max_file_mb']:
        return jsonify({
            "error": f"File/ảnh này khoảng {size_mb:.1f}MB, vượt quá giới hạn {limits['max_file_mb']}MB "
                     f"của gói {label}. " + ("Em thử file nhỏ hơn nhé!" if plan == 'max'
                                              else "Em thử file nhỏ hơn hoặc nâng cấp gói nhé!")
        }), 400

    # 2) Giới hạn SỐ LƯỢT tải file/ảnh trong 24h gần nhất theo gói (Free: 20, Premium: 50,
    #    Max: không giới hạn). Đếm theo cửa sổ trượt 24h, tự "làm mới" dần theo thời gian.
    if limits['daily_uploads'] is not None:
        used = _uploads_used_last_24h(user['id'])
        if used >= limits['daily_uploads']:
            return jsonify({
                "error": f"Em đã dùng hết {limits['daily_uploads']} lượt tải file/ảnh trong 24h qua "
                         f"(gói {label}). Giới hạn sẽ tự làm mới dần trong vòng 24h tới, hoặc nâng cấp "
                         "gói để có thêm lượt tải nhé!"
            }), 429

    kind = 'image' if ext in ALLOWED_IMAGE_EXT else 'file'
    db = get_db()
    db.execute(
        'INSERT INTO file_uploads (user_id, kind, size_bytes, created_at) VALUES (?, ?, ?, ?)',
        (user['id'], kind, len(raw), now_iso())
    )
    db.commit()

    if ext in ALLOWED_IMAGE_EXT:
        return handle_image_upload(raw, filename, ext)
    if ext == '.pdf':
        return handle_pdf_upload(raw, limits['text_chars'])
    if ext == '.docx':
        return handle_docx_upload(raw, limits['text_chars'])
    return handle_text_upload(raw, limits['text_chars'])


@app.route('/api/plan', methods=['GET'])
@login_required
def api_plan():
    """Thông tin gói hiện tại + số lượt tải file/ảnh đã dùng trong 24h — dùng để hiển thị
    ở màn hình Cài đặt và hộp thoại Nâng cấp gói phía client."""
    user = current_user()
    plan = effective_plan(user)
    limits = plan_limits(plan)
    used = _uploads_used_last_24h(user['id']) if limits['daily_uploads'] is not None else 0
    is_role_based = role_rank(user['role']) >= role_rank('developer')

    days_remaining = None
    expires_at_iso = None
    if plan != 'free' and not is_role_based:
        try:
            raw = user['plan_expires_at']
            if raw:
                exp_dt = datetime.fromisoformat(raw)
                if exp_dt.tzinfo is None:
                    exp_dt = exp_dt.replace(tzinfo=timezone.utc)
                days_remaining = max(0, (exp_dt - datetime.now(timezone.utc)).days)
                expires_at_iso = exp_dt.isoformat()
        except Exception:
            pass

    _, _, is_discount_eligible, paid_months = compute_checkout_price(user['id'], 'premium')

    return jsonify({
        'plan': plan,
        'label': plan_meta(plan)['label'],
        'icon': plan_meta(plan)['icon'],
        'is_role_based': is_role_based,
        'daily_upload_limit': limits['daily_uploads'],
        'daily_uploads_used': used,
        'max_file_mb': limits['max_file_mb'],
        'unlocked_thinking_modes': [
            k for k in THINKING_MODE_ORDER if thinking_mode_unlocked(k, plan)
        ],
        'plan_expires_at': expires_at_iso,
        'days_remaining': days_remaining,
        'is_discount_eligible': is_discount_eligible,
        'discount_pct': FIRST_TIME_DISCOUNT_PCT,
        'discount_months_used': paid_months,
        'discount_months_total': FIRST_TIME_DISCOUNT_MONTHS,
    })


@app.route('/api/gamification', methods=['GET'])
@login_required
def api_gamification():
    """XP / streak / thành tựu của tài khoản đang đăng nhập — hiển thị ở sidebar + Cài đặt."""
    return jsonify(get_user_stats(current_user_id()))


STREAK_MILESTONES_LIST = [3, 10, 30, 100, 200, 300, 500, 1000]


def _compute_progress_suggestion(user_id, db, stats, weak_topics, overdue_plan):
    """Gợi ý 'hôm nay nên làm gì' — HOÀN TOÀN dựa trên luật (rule-based) từ dữ liệu đã có sẵn,
    KHÔNG gọi thêm AI nào (nhanh, miễn phí, dễ kiểm thử/dự đoán được kết quả). Trả về dict có
    'type' để frontend biết hiện nút hành động nào tương ứng. Thứ tự ưu tiên PHẢN ÁNH đúng mức
    độ khẩn cấp thực tế: lỗi sai lặp lại nhiều nhất trước, rồi tới streak sắp mất, rồi tới kế
    hoạch bị trễ, rồi tới mốc streak sắp đạt, cuối cùng mới là lời động viên chung chung."""
    if weak_topics and weak_topics[0]['count'] >= 2:
        w = weak_topics[0]
        return {
            'type': 'weak_topic',
            'text': f"Em hay sai \"{w['description']}\" ở môn {w['subject']} ({w['count']} lần) — ôn lại ngay nhé?",
            'subject': w['subject'], 'description': w['description'],
        }

    if stats['streak_days'] == 0 and stats['longest_streak'] > 0:
        return {'type': 'restart_streak',
                'text': f"Em từng đạt streak {stats['longest_streak']} ngày — quay lại học hôm nay để bắt đầu chuỗi mới nhé!"}

    if overdue_plan:
        return {'type': 'overdue_plan', 'text': f"Kế hoạch \"{overdue_plan['title']}\" đang bị trễ tiến độ — sắp xếp lại nhé?",
                'plan_id': overdue_plan['id']}

    if stats['streak_days'] > 0:
        next_milestones = [m for m in STREAK_MILESTONES_LIST if m > stats['streak_days']]
        if next_milestones and next_milestones[0] - stats['streak_days'] == 1:
            return {'type': 'streak_milestone',
                    'text': f"Chỉ còn 1 ngày nữa là em đạt mốc streak {next_milestones[0]} rồi — đừng bỏ lỡ hôm nay! 🔥"}

    return {'type': 'generic',
            'text': f"Em đang ở cấp {stats['level']} với {stats['xp']} XP — tiếp tục hỏi bài hoặc làm 1 quiz để lên cấp nhé!"}


@app.route('/api/progress', methods=['GET'])
@login_required
def api_progress():
    """Tổng hợp TOÀN BỘ dữ liệu học tập rải rác ở nhiều hệ thống khác nhau (XP/streak, Sổ lỗi
    sai, điểm Quiz, tiến độ Kế hoạch ôn tập, điểm cao trò chơi, môn học hay hỏi nhất) thành 1
    bức tranh DUY NHẤT cho chính học sinh xem — trước đây mỗi thứ nằm 1 nơi riêng biệt, không
    có gì kết nối lại. Kèm 1 gợi ý 'hôm nay nên làm gì' tính bằng luật đơn giản (không gọi AI)."""
    user_id = current_user_id()
    db = get_db()
    stats = get_user_stats(user_id)

    subject_rows = db.execute(
        "SELECT subject, COUNT(*) c FROM usage_logs WHERE user_id = ? AND status = 'ok' AND subject IS NOT NULL "
        "GROUP BY subject ORDER BY c DESC LIMIT 6", (user_id,)
    ).fetchall()
    subject_activity = [{'subject': r['subject'], 'count': r['c']} for r in subject_rows]

    mistake_rows = db.execute(
        "SELECT subject, description, occurrence_count FROM mistakes WHERE user_id = ? AND resolved = 0 "
        "ORDER BY occurrence_count DESC LIMIT 5", (user_id,)
    ).fetchall()
    weak_topics = [{'subject': r['subject'] or 'Khác', 'description': r['description'], 'count': r['occurrence_count']}
                    for r in mistake_rows]

    quiz_rows = db.execute(
        "SELECT score, total, created_at FROM quiz_attempts WHERE user_id = ? ORDER BY created_at DESC LIMIT 10",
        (user_id,)
    ).fetchall()
    quiz_recent = [{'score': r['score'], 'total': r['total'],
                     'pct': round(100 * r['score'] / r['total']) if r['total'] else 0,
                     'date': r['created_at'][:10]} for r in quiz_rows]
    quiz_avg_pct = round(sum(q['pct'] for q in quiz_recent) / len(quiz_recent)) if quiz_recent else None

    plan_rows = db.execute('''
        SELECT p.id, p.title, p.start_date, COUNT(t.id) AS total,
               COALESCE(SUM(CASE WHEN t.status = 'done' THEN 1 ELSE 0 END), 0) AS done
        FROM study_plans p LEFT JOIN study_tasks t ON t.plan_id = p.id
        WHERE p.user_id = ? GROUP BY p.id ORDER BY p.created_at DESC
    ''', (user_id,)).fetchall()
    study_plans, overdue_plan = [], None
    today = now_iso()[:10]
    for r in plan_rows:
        pct = round(100 * r['done'] / r['total']) if r['total'] else 0
        study_plans.append({'id': r['id'], 'title': r['title'], 'done': r['done'], 'total': r['total'], 'pct': pct})
        if pct < 100 and not overdue_plan:
            try:
                days_since = (datetime.strptime(today, '%Y-%m-%d') - datetime.strptime(r['start_date'][:10], '%Y-%m-%d')).days + 1
                pending_overdue = db.execute(
                    "SELECT COUNT(*) c FROM study_tasks WHERE plan_id = ? AND status = 'pending' AND day_number < ?",
                    (r['id'], days_since)
                ).fetchone()['c']
                if pending_overdue > 0:
                    overdue_plan = {'id': r['id'], 'title': r['title']}
            except (ValueError, TypeError):
                pass

    game_rows = db.execute(
        'SELECT game, MAX(score) AS best_score, COUNT(*) AS play_count FROM game_sessions WHERE user_id = ? GROUP BY game',
        (user_id,)
    ).fetchall()
    game_stats = {r['game']: {'bestScore': r['best_score'], 'playCount': r['play_count']} for r in game_rows}

    suggestion = _compute_progress_suggestion(user_id, db, stats, weak_topics, overdue_plan)

    return jsonify({
        **stats,
        'subject_activity': subject_activity,
        'weak_topics': weak_topics,
        'quiz_stats': {'total_attempts': len(quiz_recent), 'avg_score_pct': quiz_avg_pct, 'recent': quiz_recent[:5]},
        'study_plans': study_plans,
        'game_stats': game_stats,
        'suggestion': suggestion,
    })


# ==========================================
# 8.7 API "THẺ GHI NHỚ" (FLASHCARDS) + GAME LUYỆN TẬP
# ==========================================
def _get_owned_deck(deck_id, user_id):
    db = get_db()
    return db.execute(
        'SELECT * FROM flashcard_decks WHERE id = ? AND user_id = ?', (deck_id, user_id)
    ).fetchone()


def _get_owned_card(card_id, user_id):
    db = get_db()
    return db.execute('''
        SELECT c.* FROM flashcards c
        JOIN flashcard_decks d ON d.id = c.deck_id
        WHERE c.id = ? AND d.user_id = ?
    ''', (card_id, user_id)).fetchone()


@app.route('/api/decks', methods=['GET'])
@login_required
def api_list_decks():
    db = get_db()
    rows = db.execute('''
        SELECT d.id, d.title, d.subject, d.source, d.created_at, d.updated_at,
               COUNT(c.id) AS card_count,
               COALESCE(SUM(CASE WHEN c.box_level >= 5 THEN 1 ELSE 0 END), 0) AS mastered_count
        FROM flashcard_decks d
        LEFT JOIN flashcards c ON c.deck_id = d.id
        WHERE d.user_id = ?
        GROUP BY d.id
        ORDER BY d.updated_at DESC
    ''', (current_user_id(),)).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route('/api/decks', methods=['POST'])
@login_required
def api_create_deck():
    data = request.get_json(silent=True) or {}
    title = (data.get('title') or '').strip()[:120]
    subject = (data.get('subject') or '').strip()[:60]
    if not title:
        return jsonify({"error": "Em đặt tên cho bộ thẻ nhé."}), 400
    db = get_db()
    cur = db.execute(
        'INSERT INTO flashcard_decks (user_id, title, subject, source, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)',
        (current_user_id(), title, subject, 'manual', now_iso(), now_iso())
    )
    db.commit()
    return jsonify({"id": cur.lastrowid, "title": title, "subject": subject})


@app.route('/api/decks/generate', methods=['POST'])
@login_required
def api_generate_deck():
    """Nhờ AI tạo giúp cả bộ thẻ ghi nhớ chỉ từ 1 chủ đề — gọi AI đúng 1 lần (không streaming),
    parse JSON kết quả, rồi lưu thành 1 bộ thẻ mới."""
    data = request.get_json(silent=True) or {}
    topic = (data.get('topic') or '').strip()
    subject = (data.get('subject') or '').strip()[:60]
    count = data.get('count', 8)

    if not topic:
        return jsonify({"error": "Em nhập chủ đề muốn tạo thẻ ghi nhớ nhé."}), 400
    if len(topic) > 200:
        return jsonify({"error": "Chủ đề hơi dài, em rút gọn lại nhé."}), 400

    try:
        cards = generate_flashcards_via_ai(topic, subject, count)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 502
    except Exception:
        return jsonify({"error": "AI tạo thẻ chưa đúng định dạng, em thử lại nhé!"}), 502

    if not cards:
        return jsonify({"error": "AI chưa tạo được thẻ nào, em thử đổi chủ đề hoặc thử lại nhé."}), 502

    user_id = current_user_id()
    db = get_db()
    cur = db.execute(
        'INSERT INTO flashcard_decks (user_id, title, subject, source, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)',
        (user_id, topic[:120], subject, 'ai', now_iso(), now_iso())
    )
    deck_id = cur.lastrowid
    for front, back in cards:
        db.execute('INSERT INTO flashcards (deck_id, front, back, created_at) VALUES (?, ?, ?, ?)',
                   (deck_id, front, back, now_iso()))
    db.commit()

    deck_count = db.execute('SELECT COUNT(*) c FROM flashcard_decks WHERE user_id = ?', (user_id,)).fetchone()['c']
    gamify = award_xp_and_streak(user_id, xp_amount=15, extra_achievement_checks={'first_deck': deck_count >= 1})

    return jsonify({"deckId": deck_id, "cardCount": len(cards), "gamify": gamify})


@app.route('/api/decks/<int:deck_id>', methods=['GET'])
@login_required
def api_get_deck(deck_id):
    deck = _get_owned_deck(deck_id, current_user_id())
    if not deck:
        return jsonify({"error": "Không tìm thấy bộ thẻ này."}), 404
    db = get_db()
    cards = db.execute('SELECT * FROM flashcards WHERE deck_id = ? ORDER BY id ASC', (deck_id,)).fetchall()
    return jsonify({"deck": dict(deck), "cards": [dict(c) for c in cards]})


@app.route('/api/decks/<int:deck_id>', methods=['PATCH'])
@login_required
def api_update_deck(deck_id):
    deck = _get_owned_deck(deck_id, current_user_id())
    if not deck:
        return jsonify({"error": "Không tìm thấy bộ thẻ này."}), 404
    data = request.get_json(silent=True) or {}
    title = (data.get('title') or '').strip()[:120]
    if not title:
        return jsonify({"error": "Tên bộ thẻ không được để trống."}), 400
    db = get_db()
    db.execute('UPDATE flashcard_decks SET title = ?, updated_at = ? WHERE id = ?', (title, now_iso(), deck_id))
    db.commit()
    return jsonify({"success": True})


@app.route('/api/decks/<int:deck_id>', methods=['DELETE'])
@login_required
def api_delete_deck(deck_id):
    deck = _get_owned_deck(deck_id, current_user_id())
    if not deck:
        return jsonify({"error": "Không tìm thấy bộ thẻ này."}), 404
    db = get_db()
    db.execute('DELETE FROM flashcards WHERE deck_id = ?', (deck_id,))
    db.execute('DELETE FROM flashcard_decks WHERE id = ?', (deck_id,))
    db.commit()
    return jsonify({"success": True})


@app.route('/api/decks/<int:deck_id>/cards', methods=['POST'])
@login_required
def api_add_card(deck_id):
    deck = _get_owned_deck(deck_id, current_user_id())
    if not deck:
        return jsonify({"error": "Không tìm thấy bộ thẻ này."}), 404
    data = request.get_json(silent=True) or {}
    front = (data.get('front') or '').strip()[:200]
    back = (data.get('back') or '').strip()[:500]
    if not front or not back:
        return jsonify({"error": "Em nhập đủ mặt trước và mặt sau nhé."}), 400
    db = get_db()
    cur = db.execute('INSERT INTO flashcards (deck_id, front, back, created_at) VALUES (?, ?, ?, ?)',
                      (deck_id, front, back, now_iso()))
    db.execute('UPDATE flashcard_decks SET updated_at = ? WHERE id = ?', (now_iso(), deck_id))
    db.commit()
    return jsonify({"id": cur.lastrowid, "front": front, "back": back})


@app.route('/api/cards/<int:card_id>', methods=['PATCH'])
@login_required
def api_update_card(card_id):
    """Sửa nội dung thẻ, HOẶC (nếu body có "correct") ghi nhận 1 lượt ôn tập ở Chế độ Học —
    dùng Leitner đơn giản: đúng thì tăng box (tối đa 5), sai thì về box 1 để ôn lại sớm hơn."""
    card = _get_owned_card(card_id, current_user_id())
    if not card:
        return jsonify({"error": "Không tìm thấy thẻ này."}), 404
    data = request.get_json(silent=True) or {}
    db = get_db()

    if 'correct' in data:
        correct = bool(data.get('correct'))
        new_box = min(5, card['box_level'] + 1) if correct else 1
        db.execute(
            'UPDATE flashcards SET box_level = ?, times_reviewed = times_reviewed + 1, '
            'times_correct = times_correct + ?, last_reviewed_at = ? WHERE id = ?',
            (new_box, 1 if correct else 0, now_iso(), card_id)
        )
        db.commit()
        return jsonify({"success": True, "box_level": new_box})

    set_clauses, values = [], []
    if 'front' in data:
        set_clauses.append('front = ?'); values.append(str(data.get('front') or '').strip()[:200])
    if 'back' in data:
        set_clauses.append('back = ?'); values.append(str(data.get('back') or '').strip()[:500])
    if not set_clauses:
        return jsonify({"error": "Không có nội dung nào để cập nhật."}), 400
    values.append(card_id)
    db.execute(f'UPDATE flashcards SET {", ".join(set_clauses)} WHERE id = ?', values)
    db.commit()
    return jsonify({"success": True})


@app.route('/api/cards/<int:card_id>', methods=['DELETE'])
@login_required
def api_delete_card(card_id):
    card = _get_owned_card(card_id, current_user_id())
    if not card:
        return jsonify({"error": "Không tìm thấy thẻ này."}), 404
    db = get_db()
    db.execute('DELETE FROM flashcards WHERE id = ?', (card_id,))
    db.commit()
    return jsonify({"success": True})


@app.route('/api/games/complete', methods=['POST'])
@login_required
def api_game_complete():
    """Học sinh hoàn thành 1 ván game luyện tập (vd: Lật thẻ ghi nhớ) — cộng XP thưởng (tách
    riêng khỏi XP mỗi lượt chat) + mở thành tựu 'Người chơi mới' nếu đây là lần đầu."""
    data = request.get_json(silent=True) or {}
    game = (data.get('game') or '').strip()
    if game == 'memory_match' and not is_feature_enabled('game_memory_match', current_user()):
        return jsonify({"error": "Trò chơi này hiện chưa khả dụng."}), 403
    try:
        score = int(data.get('score') or 0)
    except (TypeError, ValueError):
        score = 0
    if game not in ('memory_match',):
        return jsonify({"error": "Trò chơi không hợp lệ."}), 400

    bonus_xp = min(50, max(10, score))
    gamify = award_xp_and_streak(current_user_id(), xp_amount=bonus_xp,
                                  extra_achievement_checks={'game_player': True})
    return jsonify({"success": True, "xpAwarded": bonus_xp, "gamify": gamify})


@app.route('/api/games/quick-math/submit', methods=['POST'])
@login_required
def api_quick_math_submit():
    """Nộp kết quả 1 ván 'Đố Vui Tính Nhanh'. Không cần gọi AI để biết học sinh yếu phép tính
    nào — client tự gửi lên danh sách phép tính đã trả lời SAI (vd: ["Phép nhân", "Phép
    chia"]), server đếm và tự động lưu vào Sổ lỗi sai (dùng lại đúng cơ chế gộp trùng của
    Mistake Book) — đây chính là 'Post-Game Learning Report' của trò chơi này, không cần
    thêm 1 bài quiz AI riêng vì bản thân ván chơi đã LÀ 1 chuỗi câu hỏi rồi."""
    if not is_feature_enabled('game_quick_math', current_user()):
        return jsonify({"error": "Trò chơi này hiện chưa khả dụng."}), 403

    data = request.get_json(silent=True) or {}
    difficulty = (data.get('difficulty') or 'medium').strip()
    if difficulty not in ('easy', 'medium', 'hard'):
        difficulty = 'medium'
    try:
        score = max(0, int(data.get('score') or 0))
        correct_count = max(0, int(data.get('correctCount') or 0))
        total_count = max(0, int(data.get('totalCount') or 0))
        best_combo = max(0, int(data.get('bestCombo') or 0))
    except (TypeError, ValueError):
        return jsonify({"error": "Dữ liệu ván chơi không hợp lệ."}), 400

    wrong_ops = data.get('wrongOperations') or []
    if not isinstance(wrong_ops, list):
        wrong_ops = []

    user_id = current_user_id()
    db = get_db()

    op_counts = {}
    for op in wrong_ops:
        if isinstance(op, str) and op.strip():
            op_counts[op] = op_counts.get(op, 0) + 1
    weak_topics = [op for op, _ in sorted(op_counts.items(), key=lambda kv: kv[1], reverse=True)][:3]

    db.execute(
        '''INSERT INTO game_sessions (user_id, game, difficulty, score, correct_count, total_count,
           best_combo, weak_topics, created_at) VALUES (?, 'quick_math', ?, ?, ?, ?, ?, ?, ?)''',
        (user_id, difficulty, score, correct_count, total_count, best_combo, json.dumps(weak_topics), now_iso())
    )

    for topic in weak_topics:
        desc = f"Hay tính sai: {topic}"
        norm_desc = desc.strip().lower()
        existing = db.execute(
            "SELECT id FROM mistakes WHERE user_id = ? AND subject = 'Toán' AND LOWER(TRIM(description)) = ? AND resolved = 0",
            (user_id, norm_desc)
        ).fetchone()
        if existing:
            db.execute('UPDATE mistakes SET occurrence_count = occurrence_count + 1, last_occurred_at = ? WHERE id = ?',
                       (now_iso(), existing['id']))
        else:
            db.execute(
                '''INSERT INTO mistakes (user_id, subject, description, occurrence_count, conversation_id,
                   resolved, created_at, last_occurred_at) VALUES (?, 'Toán', ?, 1, NULL, 0, ?, ?)''',
                (user_id, desc, now_iso(), now_iso())
            )
    db.commit()

    is_perfect = total_count >= 10 and correct_count == total_count
    bonus_xp = min(60, 10 + correct_count * 2)
    gamify = award_xp_and_streak(
        user_id, xp_amount=bonus_xp,
        extra_achievement_checks={
            'game_player': True,
            'speed_demon': best_combo >= 10,
            'perfect_run': is_perfect,
        }
    )

    return jsonify({"success": True, "xpAwarded": bonus_xp, "weakTopics": weak_topics, "gamify": gamify})


@app.route('/api/games/snake/submit', methods=['POST'])
@login_required
def api_snake_submit():
    """Nộp kết quả 1 ván 'Rắn Săn Chữ' (Snake Quiz) — con rắn cổ điển, ăn "mồi thường" để lớn
    lên, thỉnh thoảng có "mồi câu hỏi" hiện đúng đáp số của 1 phép tính đang hỏi (dùng lại
    generateQmQuestion() — cùng bộ sinh câu hỏi đã kiểm thử kỹ ở Đố Vui Tính Nhanh). Ăn mồi
    câu hỏi = cộng điểm cao hơn + tính 1 câu đúng; game-over khi đâm tường/tự đâm thân — không
    liên quan gì tới việc trả lời đúng/sai (khác Quick Math, chơi vẫn tiếp tục dù trả lời sai
    1 câu hỏi, chỉ đơn giản là bỏ lỡ điểm thưởng lần đó)."""
    if not is_feature_enabled('game_snake_quiz', current_user()):
        return jsonify({"error": "Trò chơi này hiện chưa khả dụng."}), 403

    data = request.get_json(silent=True) or {}
    difficulty = (data.get('difficulty') or 'medium').strip()
    if difficulty not in ('easy', 'medium', 'hard'):
        difficulty = 'medium'
    try:
        score = max(0, int(data.get('score') or 0))
        correct_count = max(0, int(data.get('correctCount') or 0))
        total_count = max(0, int(data.get('totalCount') or 0))
        snake_length = max(1, int(data.get('snakeLength') or 1))
    except (TypeError, ValueError):
        return jsonify({"error": "Dữ liệu ván chơi không hợp lệ."}), 400

    wrong_ops = data.get('wrongOperations') or []
    if not isinstance(wrong_ops, list):
        wrong_ops = []

    user_id = current_user_id()
    db = get_db()

    op_counts = {}
    for op in wrong_ops:
        if isinstance(op, str) and op.strip():
            op_counts[op] = op_counts.get(op, 0) + 1
    weak_topics = [op for op, _ in sorted(op_counts.items(), key=lambda kv: kv[1], reverse=True)][:3]

    db.execute(
        '''INSERT INTO game_sessions (user_id, game, difficulty, score, correct_count, total_count,
           best_combo, weak_topics, created_at) VALUES (?, 'snake', ?, ?, ?, ?, ?, ?, ?)''',
        (user_id, difficulty, score, correct_count, total_count, snake_length, json.dumps(weak_topics), now_iso())
    )

    for topic in weak_topics:
        desc = f"Hay tính sai: {topic}"
        norm_desc = desc.strip().lower()
        existing = db.execute(
            "SELECT id FROM mistakes WHERE user_id = ? AND subject = 'Toán' AND LOWER(TRIM(description)) = ? AND resolved = 0",
            (user_id, norm_desc)
        ).fetchone()
        if existing:
            db.execute('UPDATE mistakes SET occurrence_count = occurrence_count + 1, last_occurred_at = ? WHERE id = ?',
                       (now_iso(), existing['id']))
        else:
            db.execute(
                '''INSERT INTO mistakes (user_id, subject, description, occurrence_count, conversation_id,
                   resolved, created_at, last_occurred_at) VALUES (?, 'Toán', ?, 1, NULL, 0, ?, ?)''',
                (user_id, desc, now_iso(), now_iso())
            )
    db.commit()

    bonus_xp = min(60, 10 + correct_count * 3 + snake_length // 2)
    gamify = award_xp_and_streak(
        user_id, xp_amount=bonus_xp,
        extra_achievement_checks={
            'game_player': True,
            'snake_master': snake_length >= 15,
        }
    )

    return jsonify({"success": True, "xpAwarded": bonus_xp, "weakTopics": weak_topics, "gamify": gamify})


@app.route('/api/games/stats', methods=['GET'])
@login_required
def api_game_stats():
    """Điểm cao nhất + số lượt chơi của CHÍNH tài khoản đang đăng nhập, theo từng trò chơi —
    hiển thị ở thư viện trò chơi."""
    db = get_db()
    rows = db.execute(
        'SELECT game, MAX(score) AS best_score, COUNT(*) AS play_count FROM game_sessions WHERE user_id = ? GROUP BY game',
        (current_user_id(),)
    ).fetchall()
    return jsonify({r['game']: {'bestScore': r['best_score'], 'playCount': r['play_count']} for r in rows})


# ==========================================
# 8.12 API "LỚP HỌC" (Teacher Mode)
# ==========================================
CLASS_CODE_ALPHABET = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789'  # bỏ ký tự dễ nhầm 0/O/1/I/L


def generate_join_code():
    """Mã vào lớp 6 ký tự, dễ đọc to trong lớp / chép tay lên bảng."""
    db = get_db()
    for _ in range(20):
        code = ''.join(secrets.choice(CLASS_CODE_ALPHABET) for _ in range(6))
        if not db.execute('SELECT id FROM classes WHERE join_code = ?', (code,)).fetchone():
            return code
    return ''.join(secrets.choice(CLASS_CODE_ALPHABET) for _ in range(8))


def _class_role(class_id, user_id):
    """Trả về 'owner' | 'member' | None — dùng để phân quyền mọi thao tác trên lớp."""
    db = get_db()
    row = db.execute('SELECT owner_id FROM classes WHERE id = ?', (class_id,)).fetchone()
    if not row:
        return None
    if row['owner_id'] == user_id:
        return 'owner'
    m = db.execute('SELECT id FROM class_members WHERE class_id = ? AND user_id = ?', (class_id, user_id)).fetchone()
    return 'member' if m else None


@app.route('/api/classes', methods=['GET'])
@login_required
def api_list_classes():
    """Lớp mình DẠY (owner) và lớp mình HỌC (member) — tách riêng để giao diện hiển thị đúng vai trò."""
    uid = current_user_id()
    db = get_db()
    teaching = db.execute('''
        SELECT c.id, c.name, c.subject, c.join_code, c.created_at,
               (SELECT COUNT(*) FROM class_members m WHERE m.class_id = c.id) AS student_count,
               (SELECT COUNT(*) FROM assignments a WHERE a.class_id = c.id) AS assignment_count
        FROM classes c WHERE c.owner_id = ? AND c.archived = 0 ORDER BY c.created_at DESC
    ''', (uid,)).fetchall()
    learning = db.execute('''
        SELECT c.id, c.name, c.subject, u.username AS teacher,
               (SELECT COUNT(*) FROM assignments a WHERE a.class_id = c.id) AS assignment_count
        FROM class_members m JOIN classes c ON c.id = m.class_id JOIN users u ON u.id = c.owner_id
        WHERE m.user_id = ? AND c.archived = 0 ORDER BY m.joined_at DESC
    ''', (uid,)).fetchall()
    return jsonify({'teaching': [dict(r) for r in teaching], 'learning': [dict(r) for r in learning]})


@app.route('/api/classes', methods=['POST'])
@login_required
def api_create_class():
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()[:80]
    subject = (data.get('subject') or '').strip()[:60]
    if not name:
        return jsonify({"error": "Em đặt tên cho lớp nhé."}), 400
    db = get_db()
    code = generate_join_code()
    cur = db.execute(
        'INSERT INTO classes (owner_id, name, subject, join_code, created_at) VALUES (?, ?, ?, ?, ?)',
        (current_user_id(), name, subject, code, now_iso())
    )
    db.commit()
    return jsonify({"id": cur.lastrowid, "name": name, "join_code": code})


@app.route('/api/classes/join', methods=['POST'])
@login_required
def api_join_class():
    data = request.get_json(silent=True) or {}
    code = (data.get('code') or '').strip().upper()
    if not code:
        return jsonify({"error": "Em nhập mã lớp nhé."}), 400

    db = get_db()
    cls = db.execute('SELECT id, owner_id, name FROM classes WHERE join_code = ? AND archived = 0', (code,)).fetchone()
    if not cls:
        return jsonify({"error": "Mã lớp không đúng hoặc lớp đã đóng."}), 404
    if cls['owner_id'] == current_user_id():
        return jsonify({"error": "Đây là lớp của chính em — em đang là giáo viên của lớp này rồi."}), 400
    try:
        db.execute('INSERT INTO class_members (class_id, user_id, joined_at) VALUES (?, ?, ?)',
                   (cls['id'], current_user_id(), now_iso()))
        db.commit()
    except sqlite3.IntegrityError:
        return jsonify({"error": "Em đã ở trong lớp này rồi."}), 400
    return jsonify({"success": True, "id": cls['id'], "name": cls['name']})


@app.route('/api/classes/<int:class_id>', methods=['GET'])
@login_required
def api_get_class(class_id):
    """Bảng điều khiển lớp học. Giáo viên thấy TOÀN BỘ (danh sách học sinh, điểm từng người,
    điểm yếu chung của lớp, ai cần chú ý). Học sinh chỉ thấy bài tập của mình + điểm của
    CHÍNH MÌNH — không xem được điểm bạn khác."""
    uid = current_user_id()
    role = _class_role(class_id, uid)
    if not role:
        return jsonify({"error": "Em không có quyền xem lớp này."}), 403

    db = get_db()
    cls = db.execute('''SELECT c.*, u.username AS teacher FROM classes c
                        JOIN users u ON u.id = c.owner_id WHERE c.id = ?''', (class_id,)).fetchone()
    assignments = db.execute(
        'SELECT id, quiz_id, title, due_at, created_at FROM assignments WHERE class_id = ? ORDER BY created_at DESC',
        (class_id,)
    ).fetchall()
    member_rows = db.execute('''
        SELECT u.id, u.username FROM class_members m JOIN users u ON u.id = m.user_id
        WHERE m.class_id = ? ORDER BY u.username
    ''', (class_id,)).fetchall()

    out_assignments = []
    weak_counter = {}
    for a in assignments:
        attempts = db.execute(
            'SELECT user_id, score, total, weak_topics FROM quiz_attempts WHERE assignment_id = ?', (a['id'],)
        ).fetchall()
        # Mỗi học sinh chỉ tính LẦN LÀM TỐT NHẤT cho 1 bài tập (công bằng khi cho làm lại).
        best = {}
        for at in attempts:
            pct = round(100 * at['score'] / at['total']) if at['total'] else 0
            if at['user_id'] not in best or pct > best[at['user_id']]['pct']:
                best[at['user_id']] = {'pct': pct, 'score': at['score'], 'total': at['total']}
            for t in (json.loads(at['weak_topics']) if at['weak_topics'] else []):
                weak_counter[t] = weak_counter.get(t, 0) + 1
        submitted = len(best)
        avg = round(sum(b['pct'] for b in best.values()) / submitted) if submitted else None
        item = {'id': a['id'], 'quiz_id': a['quiz_id'], 'title': a['title'], 'due_at': a['due_at'],
                'submitted': submitted, 'total_students': len(member_rows), 'avg_pct': avg}
        if role == 'member':
            mine = best.get(uid)
            item['my_result'] = mine
            item['submitted'] = None      # học sinh không xem được số bạn đã nộp
            item['avg_pct'] = None
            item['total_students'] = None
        out_assignments.append(item)

    payload = {'class': {'id': cls['id'], 'name': cls['name'], 'subject': cls['subject'],
                          'teacher': cls['teacher'], 'join_code': cls['join_code'] if role == 'owner' else None},
                'my_role': role, 'assignments': out_assignments}

    if role == 'owner':
        # Bảng học sinh + "ai cần chú ý": điểm TB dưới 50% hoặc chưa nộp bài nào.
        students = []
        for m in member_rows:
            rows = db.execute('''SELECT a.id AS aid, qa.score, qa.total FROM assignments a
                                 LEFT JOIN quiz_attempts qa ON qa.assignment_id = a.id AND qa.user_id = ?
                                 WHERE a.class_id = ?''', (m['id'], class_id)).fetchall()
            best_by_a = {}
            for r in rows:
                if r['score'] is None or not r['total']:
                    continue
                pct = round(100 * r['score'] / r['total'])
                best_by_a[r['aid']] = max(best_by_a.get(r['aid'], 0), pct)
            done = len(best_by_a)
            avg = round(sum(best_by_a.values()) / done) if done else None
            students.append({'id': m['id'], 'username': m['username'], 'done': done,
                              'total_assignments': len(assignments), 'avg_pct': avg,
                              'needs_attention': (avg is not None and avg < 50) or (len(assignments) > 0 and done == 0)})
        payload['students'] = students
        payload['class_weak_topics'] = [{'topic': t, 'count': c} for t, c in
                                          sorted(weak_counter.items(), key=lambda kv: kv[1], reverse=True)[:5]]
        graded = [s['avg_pct'] for s in students if s['avg_pct'] is not None]
        payload['class_avg_pct'] = round(sum(graded) / len(graded)) if graded else None
        payload['needs_attention_count'] = sum(1 for s in students if s['needs_attention'])
    return jsonify(payload)


@app.route('/api/classes/<int:class_id>/assignments', methods=['POST'])
@login_required
def api_create_assignment(class_id):
    """Giao 1 bài quiz ĐÃ CÓ cho lớp. Bản sao đề không được tạo ra — cả lớp làm chung 1 quiz,
    điểm tách nhau nhờ assignment_id trên từng bài làm."""
    if _class_role(class_id, current_user_id()) != 'owner':
        return jsonify({"error": "Chỉ giáo viên của lớp mới giao được bài."}), 403

    data = request.get_json(silent=True) or {}
    quiz_id = data.get('quizId')
    due_at = (data.get('dueAt') or '').strip() or None

    db = get_db()
    quiz = db.execute('SELECT id, title FROM quizzes WHERE id = ? AND user_id = ?',
                       (quiz_id, current_user_id())).fetchone()
    if not quiz:
        return jsonify({"error": "Không tìm thấy quiz này trong danh sách quiz của em."}), 404

    title = (data.get('title') or quiz['title']).strip()[:120]
    cur = db.execute(
        'INSERT INTO assignments (class_id, quiz_id, title, due_at, created_at) VALUES (?, ?, ?, ?, ?)',
        (class_id, quiz['id'], title, due_at, now_iso())
    )
    db.commit()
    return jsonify({"id": cur.lastrowid, "title": title})


@app.route('/api/classes/<int:class_id>', methods=['DELETE'])
@login_required
def api_delete_class(class_id):
    """Giáo viên xoá lớp (xoá luôn thành viên + bài tập). Học sinh gọi API này = RỜI lớp."""
    uid = current_user_id()
    role = _class_role(class_id, uid)
    if not role:
        return jsonify({"error": "Không tìm thấy lớp này."}), 404
    db = get_db()
    if role == 'owner':
        db.execute('DELETE FROM assignments WHERE class_id = ?', (class_id,))
        db.execute('DELETE FROM class_members WHERE class_id = ?', (class_id,))
        db.execute('DELETE FROM classes WHERE id = ?', (class_id,))
    else:
        db.execute('DELETE FROM class_members WHERE class_id = ? AND user_id = ?', (class_id, uid))
    db.commit()
    return jsonify({"success": True, "action": 'deleted' if role == 'owner' else 'left'})


# ==========================================
# 8.9 API "QUIZ GENERATOR"
# ==========================================
def _get_owned_quiz(quiz_id, user_id):
    db = get_db()
    return db.execute('SELECT * FROM quizzes WHERE id = ? AND user_id = ?', (quiz_id, user_id)).fetchone()


@app.route('/api/quizzes', methods=['GET'])
@login_required
def api_list_quizzes():
    db = get_db()
    rows = db.execute('''
        SELECT q.id, q.title, q.subject, q.difficulty, q.created_at,
               COUNT(qq.id) AS question_count,
               (SELECT score FROM quiz_attempts a WHERE a.quiz_id = q.id ORDER BY a.created_at DESC LIMIT 1) AS last_score,
               (SELECT total FROM quiz_attempts a WHERE a.quiz_id = q.id ORDER BY a.created_at DESC LIMIT 1) AS last_total
        FROM quizzes q
        LEFT JOIN quiz_questions qq ON qq.quiz_id = q.id
        WHERE q.user_id = ?
        GROUP BY q.id
        ORDER BY q.created_at DESC
    ''', (current_user_id(),)).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route('/api/quizzes/generate', methods=['POST'])
@login_required
def api_generate_quiz():
    data = request.get_json(silent=True) or {}
    topic = (data.get('topic') or '').strip()
    subject = (data.get('subject') or '').strip()[:60]
    difficulty = (data.get('difficulty') or 'medium').strip()
    count = data.get('count', 5)
    raw_conv_id = data.get('conversationId')

    if not topic:
        return jsonify({"error": "Em nhập chủ đề muốn làm quiz nhé."}), 400
    if len(topic) > 200:
        return jsonify({"error": "Chủ đề hơi dài, em rút gọn lại nhé."}), 400
    if difficulty not in QUIZ_DIFFICULTIES:
        difficulty = 'medium'

    source_text = None
    source = 'topic'
    if raw_conv_id is not None:
        try:
            conv_id = int(raw_conv_id)
        except (TypeError, ValueError):
            conv_id = None
        if conv_id is not None:
            db = get_db()
            conv = db.execute('SELECT id FROM conversations WHERE id = ? AND user_id = ?',
                               (conv_id, current_user_id())).fetchone()
            if conv:
                msgs = db.execute(
                    'SELECT role, content FROM messages WHERE conversation_id = ? ORDER BY id ASC', (conv_id,)
                ).fetchall()
                source_text = "\n".join(f"{m['role']}: {m['content']}" for m in msgs)[:4000]
                source = 'conversation'

    try:
        questions = generate_quiz_via_ai(topic, subject, difficulty, count, source_text=source_text)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 502
    except Exception:
        return jsonify({"error": "AI tạo đề chưa đúng định dạng, em thử lại nhé!"}), 502

    if not questions:
        return jsonify({"error": "AI chưa tạo được câu hỏi nào, em thử đổi chủ đề hoặc thử lại nhé."}), 502

    user_id = current_user_id()
    db = get_db()
    cur = db.execute(
        'INSERT INTO quizzes (user_id, title, subject, difficulty, source, created_at) VALUES (?, ?, ?, ?, ?, ?)',
        (user_id, topic[:120], subject, difficulty, source, now_iso())
    )
    quiz_id = cur.lastrowid
    for i, q in enumerate(questions):
        db.execute(
            '''INSERT INTO quiz_questions (quiz_id, q_type, question, options, correct_answer,
               explanation, topic, order_index) VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
            (quiz_id, q['q_type'], q['question'], json.dumps(q['options']) if q['options'] else None,
             q['correct_answer'], q['explanation'], q['topic'], i)
        )
    db.commit()
    return jsonify({"quizId": quiz_id, "questionCount": len(questions)})


@app.route('/api/quizzes/<int:quiz_id>', methods=['GET'])
@login_required
def api_get_quiz(quiz_id):
    """Cho xem đề nếu: (a) mình là chủ quiz, HOẶC (b) quiz này được GIAO cho 1 lớp mà mình
    đang là thành viên — nếu không có nhánh (b) thì học sinh không mở nổi bài tập giáo viên
    giao (đã phát hiện đúng lỗi này khi kiểm thử Teacher Mode)."""
    uid = current_user_id()
    quiz = _get_owned_quiz(quiz_id, uid)
    if not quiz:
        db = get_db()
        allowed = db.execute('''
            SELECT 1 FROM assignments a JOIN class_members m ON m.class_id = a.class_id
            WHERE a.quiz_id = ? AND m.user_id = ? LIMIT 1
        ''', (quiz_id, uid)).fetchone()
        if allowed:
            quiz = db.execute('SELECT * FROM quizzes WHERE id = ?', (quiz_id,)).fetchone()
    if not quiz:
        return jsonify({"error": "Không tìm thấy quiz này."}), 404
    db = get_db()
    rows = db.execute(
        'SELECT * FROM quiz_questions WHERE quiz_id = ? ORDER BY order_index ASC', (quiz_id,)
    ).fetchall()
    questions = []
    for r in rows:
        d = dict(r)
        d['options'] = json.loads(d['options']) if d['options'] else None
        questions.append(d)
    return jsonify({"quiz": dict(quiz), "questions": questions})


@app.route('/api/quizzes/<int:quiz_id>', methods=['DELETE'])
@login_required
def api_delete_quiz(quiz_id):
    quiz = _get_owned_quiz(quiz_id, current_user_id())
    if not quiz:
        return jsonify({"error": "Không tìm thấy quiz này."}), 404
    db = get_db()
    db.execute('DELETE FROM quiz_attempts WHERE quiz_id = ?', (quiz_id,))
    db.execute('DELETE FROM quiz_questions WHERE quiz_id = ?', (quiz_id,))
    db.execute('DELETE FROM quizzes WHERE id = ?', (quiz_id,))
    db.commit()
    return jsonify({"success": True})


@app.route('/api/quizzes/<int:quiz_id>/submit', methods=['POST'])
@login_required
def api_submit_quiz(quiz_id):
    """Chấm bài NGAY, KHÔNG gọi thêm AI nào (so khớp đáp án đã chuẩn hoá — xem
    grade_quiz_answer()). Tự lưu câu sai vào Sổ lỗi sai (nếu học sinh đồng ý, mặc định có)
    để nối liền 2 hệ thống, và cộng XP + kiểm tra thành tựu 'Điểm tuyệt đối'."""
    uid = current_user_id()
    quiz = _get_owned_quiz(quiz_id, uid)
    if not quiz:
        # Học sinh nộp bài tập được giao: cho phép nếu quiz này thuộc 1 bài tập của lớp mình.
        db_chk = get_db()
        allowed = db_chk.execute('''
            SELECT 1 FROM assignments a JOIN class_members m ON m.class_id = a.class_id
            WHERE a.quiz_id = ? AND m.user_id = ? LIMIT 1
        ''', (quiz_id, uid)).fetchone()
        if allowed:
            quiz = db_chk.execute('SELECT * FROM quizzes WHERE id = ?', (quiz_id,)).fetchone()
    if not quiz:
        return jsonify({"error": "Không tìm thấy quiz này."}), 404

    data = request.get_json(silent=True) or {}
    answers = data.get('answers') or []
    duration = data.get('durationSeconds', 0)
    save_mistakes = data.get('saveMistakes', True)
    if not isinstance(answers, list):
        return jsonify({"error": "Dữ liệu bài làm không hợp lệ."}), 400

    db = get_db()
    q_rows = db.execute('SELECT * FROM quiz_questions WHERE quiz_id = ?', (quiz_id,)).fetchall()
    q_by_id = {q['id']: q for q in q_rows}

    graded = []
    correct_count = 0
    weak_topic_counter = {}
    user_id = current_user_id()

    for a in answers:
        qid = a.get('questionId')
        given = a.get('given', '')
        q = q_by_id.get(qid)
        if not q:
            continue
        is_correct = grade_quiz_answer(q['q_type'], q['correct_answer'], given)
        if is_correct:
            correct_count += 1
        else:
            weak_topic_counter[q['topic']] = weak_topic_counter.get(q['topic'], 0) + 1
            if save_mistakes:
                # Dùng lại đúng logic dedup của Sổ lỗi sai (xem api_add_mistake) để lỗi lặp
                # lại vẫn tăng occurrence_count thay vì tạo dòng mới.
                desc = f"{q['question']} (đáp án đúng: {q['correct_answer']})"[:300]
                norm_desc = desc.strip().lower()
                existing = db.execute(
                    "SELECT id FROM mistakes WHERE user_id = ? AND subject = ? AND LOWER(TRIM(description)) = ? AND resolved = 0",
                    (user_id, quiz['subject'] or q['topic'], norm_desc)
                ).fetchone()
                if existing:
                    db.execute('UPDATE mistakes SET occurrence_count = occurrence_count + 1, last_occurred_at = ? WHERE id = ?',
                               (now_iso(), existing['id']))
                else:
                    db.execute(
                        '''INSERT INTO mistakes (user_id, subject, description, occurrence_count, conversation_id,
                           resolved, created_at, last_occurred_at) VALUES (?, ?, ?, 1, NULL, 0, ?, ?)''',
                        (user_id, quiz['subject'] or q['topic'], desc, now_iso(), now_iso())
                    )
        graded.append({
            'questionId': qid, 'given': given, 'correct': is_correct,
            'correctAnswer': q['correct_answer'], 'explanation': q['explanation'],
        })

    total = len(q_rows)
    weak_topics = sorted(weak_topic_counter, key=weak_topic_counter.get, reverse=True)

    # Nếu bài làm này là để nộp cho 1 BÀI TẬP ĐƯỢC GIAO trong lớp -> gắn assignment_id.
    # Chỉ chấp nhận khi bài tập đó có thật, đúng quiz đang làm, VÀ học sinh thật sự ở trong
    # lớp đó — tránh việc gửi assignment_id bừa để chèn điểm vào lớp mình không tham gia.
    assignment_id = None
    raw_assignment = data.get('assignmentId')
    if raw_assignment is not None:
        try:
            cand = int(raw_assignment)
        except (TypeError, ValueError):
            cand = None
        if cand is not None:
            a_row = db.execute('SELECT id, class_id, quiz_id FROM assignments WHERE id = ?', (cand,)).fetchone()
            if a_row and a_row['quiz_id'] == quiz_id and _class_role(a_row['class_id'], user_id):
                assignment_id = a_row['id']

    db.execute(
        '''INSERT INTO quiz_attempts (quiz_id, user_id, score, total, duration_seconds, answers, weak_topics, assignment_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (quiz_id, user_id, correct_count, total, int(duration or 0), json.dumps(graded),
         json.dumps(weak_topics), assignment_id, now_iso())
    )
    db.commit()

    is_perfect = total > 0 and correct_count == total
    gamify = award_xp_and_streak(
        user_id, xp_amount=min(40, 10 + correct_count * 3),
        extra_achievement_checks={'first_quiz': True, 'perfect_quiz': is_perfect}
    )

    return jsonify({
        "score": correct_count, "total": total, "weakTopics": weak_topics,
        "graded": graded, "gamify": gamify,
    })


# ==========================================
# 8.10 API "STUDY PLAN"
# ==========================================
def _get_owned_plan(plan_id, user_id):
    db = get_db()
    return db.execute('SELECT * FROM study_plans WHERE id = ? AND user_id = ?', (plan_id, user_id)).fetchone()


@app.route('/api/study-plans', methods=['GET'])
@login_required
def api_list_study_plans():
    db = get_db()
    rows = db.execute('''
        SELECT p.id, p.title, p.subject, p.total_days, p.start_date, p.created_at,
               COUNT(t.id) AS task_count,
               COALESCE(SUM(CASE WHEN t.status = 'done' THEN 1 ELSE 0 END), 0) AS done_count
        FROM study_plans p
        LEFT JOIN study_tasks t ON t.plan_id = p.id
        WHERE p.user_id = ?
        GROUP BY p.id
        ORDER BY p.created_at DESC
    ''', (current_user_id(),)).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route('/api/study-plans/generate', methods=['POST'])
@login_required
def api_generate_study_plan():
    data = request.get_json(silent=True) or {}
    goal = (data.get('goal') or '').strip()
    subject = (data.get('subject') or '').strip()[:60]
    days = data.get('days', 14)

    if not goal:
        return jsonify({"error": "Em nhập mục tiêu ôn tập nhé (vd: Ôn thi Toán 8 trong 14 ngày)."}), 400
    if len(goal) > 300:
        return jsonify({"error": "Mục tiêu hơi dài, em rút gọn lại nhé."}), 400

    try:
        tasks = generate_study_plan_via_ai(goal, subject, days)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 502
    except Exception:
        return jsonify({"error": "AI tạo kế hoạch chưa đúng định dạng, em thử lại nhé!"}), 502

    if not tasks:
        return jsonify({"error": "AI chưa tạo được kế hoạch nào, em thử lại nhé."}), 502

    user_id = current_user_id()
    db = get_db()
    cur = db.execute(
        'INSERT INTO study_plans (user_id, title, subject, total_days, start_date, created_at) VALUES (?, ?, ?, ?, ?, ?)',
        (user_id, goal[:120], subject, len(tasks), now_iso(), now_iso())
    )
    plan_id = cur.lastrowid
    for i, (title, desc) in enumerate(tasks):
        db.execute(
            'INSERT INTO study_tasks (plan_id, day_number, title, description, status) VALUES (?, ?, ?, ?, ?)',
            (plan_id, i + 1, title, desc, 'pending')
        )
    db.commit()

    gamify = award_xp_and_streak(user_id, xp_amount=15, extra_achievement_checks={'first_plan': True})
    return jsonify({"planId": plan_id, "dayCount": len(tasks), "gamify": gamify})


@app.route('/api/study-plans/<int:plan_id>', methods=['GET'])
@login_required
def api_get_study_plan(plan_id):
    plan = _get_owned_plan(plan_id, current_user_id())
    if not plan:
        return jsonify({"error": "Không tìm thấy kế hoạch này."}), 404
    db = get_db()
    tasks = db.execute(
        'SELECT * FROM study_tasks WHERE plan_id = ? ORDER BY day_number ASC', (plan_id,)
    ).fetchall()
    return jsonify({"plan": dict(plan), "tasks": [dict(t) for t in tasks]})


@app.route('/api/study-plans/<int:plan_id>', methods=['DELETE'])
@login_required
def api_delete_study_plan(plan_id):
    plan = _get_owned_plan(plan_id, current_user_id())
    if not plan:
        return jsonify({"error": "Không tìm thấy kế hoạch này."}), 404
    db = get_db()
    db.execute('DELETE FROM study_tasks WHERE plan_id = ?', (plan_id,))
    db.execute('DELETE FROM study_plans WHERE id = ?', (plan_id,))
    db.commit()
    return jsonify({"success": True})


@app.route('/api/study-tasks/<int:task_id>', methods=['PATCH'])
@login_required
def api_update_study_task(task_id):
    db = get_db()
    task = db.execute('''
        SELECT t.* FROM study_tasks t JOIN study_plans p ON p.id = t.plan_id
        WHERE t.id = ? AND p.user_id = ?
    ''', (task_id, current_user_id())).fetchone()
    if not task:
        return jsonify({"error": "Không tìm thấy việc này."}), 404

    data = request.get_json(silent=True) or {}
    status = (data.get('status') or '').strip()
    if status not in ('pending', 'done', 'skipped'):
        return jsonify({"error": "Trạng thái không hợp lệ."}), 400

    completed_at = now_iso() if status == 'done' else None
    db.execute('UPDATE study_tasks SET status = ?, completed_at = ? WHERE id = ?', (status, completed_at, task_id))
    db.commit()

    gamify = None
    if status == 'done':
        # Nếu đây là việc CUỐI CÙNG của kế hoạch được hoàn thành -> thành tựu "Về đích".
        remaining = db.execute(
            "SELECT COUNT(*) c FROM study_tasks WHERE plan_id = ? AND status != 'done'", (task['plan_id'],)
        ).fetchone()['c']
        gamify = award_xp_and_streak(current_user_id(), xp_amount=8,
                                      extra_achievement_checks={'plan_finisher': remaining == 0})
    return jsonify({"success": True, "gamify": gamify})


@app.route('/api/study-plans/<int:plan_id>/reorganize', methods=['POST'])
@login_required
def api_reorganize_study_plan(plan_id):
    """Học sinh bị trễ tiến độ -> gọi AI phân bổ lại các việc CÒN LẠI (chưa 'done') vào số
    ngày còn lại tính từ hôm nay, thay vì giữ nguyên kế hoạch cũ không còn hợp lý."""
    plan = _get_owned_plan(plan_id, current_user_id())
    if not plan:
        return jsonify({"error": "Không tìm thấy kế hoạch này."}), 404

    db = get_db()
    remaining_tasks = db.execute(
        "SELECT title FROM study_tasks WHERE plan_id = ? AND status != 'done' ORDER BY day_number ASC", (plan_id,)
    ).fetchall()
    if not remaining_tasks:
        return jsonify({"error": "Kế hoạch đã hoàn thành hết, không còn gì để sắp xếp lại."}), 400

    data = request.get_json(silent=True) or {}
    new_days = data.get('days', len(remaining_tasks))

    try:
        new_tasks = generate_study_plan_via_ai(
            plan['title'], plan['subject'], new_days,
            remaining_topics=[t['title'] for t in remaining_tasks]
        )
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 502
    except Exception:
        return jsonify({"error": "AI sắp xếp lại chưa đúng định dạng, em thử lại nhé!"}), 502

    if not new_tasks:
        return jsonify({"error": "AI chưa sắp xếp lại được, em thử lại nhé."}), 502

    # Xoá các việc CHƯA hoàn thành cũ, thay bằng kế hoạch mới — giữ nguyên các việc ĐÃ hoàn
    # thành để không mất tiến độ đã có.
    done_count = db.execute(
        "SELECT COUNT(*) c FROM study_tasks WHERE plan_id = ? AND status = 'done'", (plan_id,)
    ).fetchone()['c']
    db.execute("DELETE FROM study_tasks WHERE plan_id = ? AND status != 'done'", (plan_id,))
    for i, (title, desc) in enumerate(new_tasks):
        db.execute(
            'INSERT INTO study_tasks (plan_id, day_number, title, description, status) VALUES (?, ?, ?, ?, ?)',
            (plan_id, done_count + i + 1, title, desc, 'pending')
        )
    db.execute('UPDATE study_plans SET total_days = ? WHERE id = ?', (done_count + len(new_tasks), plan_id))
    db.commit()
    return jsonify({"success": True, "newTaskCount": len(new_tasks)})


# ==========================================
# 8.8 API "SỔ LỖI SAI" (MISTAKE BOOK)
# ==========================================
@app.route('/api/mistakes', methods=['GET'])
@login_required
def api_list_mistakes():
    db = get_db()
    rows = db.execute(
        'SELECT * FROM mistakes WHERE user_id = ? ORDER BY resolved ASC, occurrence_count DESC, last_occurred_at DESC',
        (current_user_id(),)
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route('/api/mistakes', methods=['POST'])
@login_required
def api_add_mistake():
    """Ghi 1 lỗi sai vào Sổ lỗi sai. Lỗi TRÙNG (cùng môn + mô tả sau khi chuẩn hoá, chưa
    'Đã khắc phục') chỉ tăng occurrence_count — không tạo dòng mới — để ra đúng kiểu hiển thị
    "Chuyển vế sai dấu ×3" thay vì liệt kê lặp lại từng dòng riêng."""
    data = request.get_json(silent=True) or {}
    subject = (data.get('subject') or '').strip()[:60]
    description = (data.get('description') or '').strip()[:300]
    raw_conv_id = data.get('conversationId')
    if not description:
        return jsonify({"error": "Em mô tả ngắn gọn lỗi sai của mình nhé."}), 400

    conv_id = None
    if raw_conv_id is not None:
        try:
            conv_id = int(raw_conv_id)
        except (TypeError, ValueError):
            conv_id = None

    user_id = current_user_id()
    db = get_db()
    norm_desc = description.strip().lower()
    existing = db.execute(
        "SELECT id, occurrence_count FROM mistakes WHERE user_id = ? AND subject = ? "
        "AND LOWER(TRIM(description)) = ? AND resolved = 0",
        (user_id, subject, norm_desc)
    ).fetchone()

    if existing:
        db.execute(
            'UPDATE mistakes SET occurrence_count = occurrence_count + 1, last_occurred_at = ? WHERE id = ?',
            (now_iso(), existing['id'])
        )
        db.commit()
        mistake_id = existing['id']
        is_first = False
    else:
        cur = db.execute(
            '''INSERT INTO mistakes (user_id, subject, description, occurrence_count, conversation_id,
               resolved, created_at, last_occurred_at) VALUES (?, ?, ?, 1, ?, 0, ?, ?)''',
            (user_id, subject, description, conv_id, now_iso(), now_iso())
        )
        db.commit()
        mistake_id = cur.lastrowid
        is_first = True

    total_mistakes = db.execute('SELECT COUNT(*) c FROM mistakes WHERE user_id = ?', (user_id,)).fetchone()['c']
    gamify = award_xp_and_streak(user_id, xp_amount=5,
                                  extra_achievement_checks={'first_mistake': total_mistakes >= 1})
    return jsonify({"success": True, "id": mistake_id, "isNew": is_first, "gamify": gamify})


@app.route('/api/mistakes/<int:mistake_id>', methods=['PATCH'])
@login_required
def api_update_mistake(mistake_id):
    db = get_db()
    row = db.execute('SELECT id FROM mistakes WHERE id = ? AND user_id = ?', (mistake_id, current_user_id())).fetchone()
    if not row:
        return jsonify({"error": "Không tìm thấy lỗi này."}), 404
    data = request.get_json(silent=True) or {}
    if 'resolved' not in data:
        return jsonify({"error": "Không có nội dung nào để cập nhật."}), 400
    db.execute('UPDATE mistakes SET resolved = ? WHERE id = ?', (1 if data.get('resolved') else 0, mistake_id))
    db.commit()
    return jsonify({"success": True})


@app.route('/api/mistakes/<int:mistake_id>', methods=['DELETE'])
@login_required
def api_delete_mistake(mistake_id):
    db = get_db()
    row = db.execute('SELECT id FROM mistakes WHERE id = ? AND user_id = ?', (mistake_id, current_user_id())).fetchone()
    if not row:
        return jsonify({"error": "Không tìm thấy lỗi này."}), 404
    db.execute('DELETE FROM mistakes WHERE id = ?', (mistake_id,))
    db.commit()
    return jsonify({"success": True})


@app.route('/api/memories', methods=['GET'])
@login_required
def api_list_memories():
    conn = open_write_db()
    try:
        rows = conn.execute(
            'SELECT id, content, category, source, created_at FROM memories WHERE user_id = ? ORDER BY created_at DESC',
            (current_user_id(),)
        ).fetchall()
    finally:
        conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/memories', methods=['DELETE'])
@login_required
def api_clear_memories():
    """Xoá toàn bộ 'bộ nhớ' AI của chính học sinh này (quyền riêng tư — mỗi người chỉ xoá
    được bộ nhớ của mình)."""
    conn = open_write_db()
    try:
        conn.execute('DELETE FROM memories WHERE user_id = ?', (current_user_id(),))
        conn.commit()
    finally:
        conn.close()
    return jsonify({"success": True})


@app.route('/api/report-issue', methods=['POST'])
@login_required
def api_report_issue():
    """Học sinh báo lỗi 1 câu trả lời cụ thể (hoặc báo lỗi chung). Lưu lại để Admin xem và
    xử lý ở trang /developer."""
    data = request.get_json(silent=True) or {}
    description = (data.get('description') or '').strip()
    message_excerpt = (data.get('messageExcerpt') or '').strip()[:2000]
    raw_conv_id = data.get('conversationId')

    if not description:
        return jsonify({"error": "Em mô tả lỗi cụ thể giúp Thầy/Cô nhé."}), 400
    if len(description) > 1000:
        return jsonify({"error": "Mô tả hơi dài, em rút gọn lại giúp Thầy/Cô nhé!"}), 400

    conv_id = None
    if raw_conv_id is not None:
        try:
            conv_id = int(raw_conv_id)
        except (TypeError, ValueError):
            conv_id = None

    db = get_db()
    db.execute(
        '''INSERT INTO issue_reports
           (user_id, conversation_id, message_excerpt, description, status, created_at)
           VALUES (?, ?, ?, ?, 'open', ?)''',
        (current_user_id(), conv_id, message_excerpt, description, now_iso())
    )
    db.commit()
    return jsonify({"success": True})


# ==========================================
# 8.5 THANH TOÁN NÂNG CẤP GÓI (VNPAY + Chuyển khoản VietQR)
# ==========================================
@app.route('/api/checkout', methods=['POST'])
@login_required
def api_checkout():
    """Tạo 1 đơn nâng cấp gói THEO THÁNG. method = 'vnpay' (thẻ ATM/Visa/Mastercard/JCB,
    redirect sang VNPAY) hoặc 'bank_transfer' (quét mã VietQR, Admin xác nhận thủ công sau
    khi nhận tiền). Tự áp dụng ưu đãi lần đầu (xem compute_checkout_price())."""
    user = current_user()
    role = current_user_role()
    if role_rank(role) >= role_rank('developer'):
        return jsonify({"error": "Tài khoản của em đã có gói Max theo vai trò, không cần nâng cấp."}), 400

    data = request.get_json(silent=True) or {}
    plan = (data.get('plan') or '').strip()
    method = (data.get('method') or '').strip()

    if plan not in PLAN_PRICING:
        return jsonify({"error": "Gói không hợp lệ."}), 400
    if plan_rank(plan) < plan_rank(effective_plan(user)):
        return jsonify({"error": "Không thể hạ xuống gói thấp hơn gói đang dùng qua đây."}), 400
    if method not in ('vnpay', 'bank_transfer'):
        return jsonify({"error": "Phương thức thanh toán không hợp lệ."}), 400
    if method == 'vnpay' and not VNPAY_ENABLED:
        return jsonify({"error": "Thanh toán qua thẻ (VNPAY) hiện chưa khả dụng."}), 400
    if method == 'bank_transfer' and not BANK_TRANSFER_ENABLED:
        return jsonify({"error": "Thanh toán chuyển khoản hiện chưa khả dụng."}), 400

    amount, base_amount, is_discounted, paid_so_far = compute_checkout_price(user['id'], plan)
    order_code = generate_order_code()
    db = get_db()
    db.execute(
        '''INSERT INTO payment_orders
           (order_code, user_id, plan, amount, base_amount, is_discounted, method, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)''',
        (order_code, user['id'], plan, amount, base_amount, int(is_discounted), method, now_iso())
    )
    db.commit()

    if method == 'bank_transfer':
        return jsonify({
            'orderCode': order_code,
            'amount': amount,
            'baseAmount': base_amount,
            'isDiscounted': is_discounted,
            'method': 'bank_transfer',
            'qrImageUrl': vietqr_image_url(amount, order_code),
            'bankAccountName': VIETQR_ACCOUNT_NAME,
            'bankAccountNo': VIETQR_ACCOUNT_NO,
            'bankId': VIETQR_BANK_ID,
            'transferContent': order_code,
        })

    # method == 'vnpay'
    ip_addr = (request.headers.get('X-Forwarded-For', '') or request.remote_addr or '127.0.0.1').split(',')[0].strip()
    return_url = url_for('vnpay_return', _external=True)
    order_info = f"Nang cap {plan_meta(plan)['label']} 1 thang StudyMate AI - {order_code}"
    payment_url = vnpay_build_payment_url(order_code, amount, order_info, ip_addr, return_url)
    return jsonify({
        'orderCode': order_code, 'amount': amount, 'baseAmount': base_amount,
        'isDiscounted': is_discounted, 'method': 'vnpay', 'redirectUrl': payment_url,
    })


@app.route('/api/checkout/<order_code>/status', methods=['GET'])
@login_required
def api_checkout_status(order_code):
    """Client gọi định kỳ (polling) trong lúc chờ xác nhận chuyển khoản — hoặc để kiểm tra
    kết quả sau khi quay lại từ VNPAY. Chỉ trả về đơn của CHÍNH tài khoản đang đăng nhập."""
    db = get_db()
    order = db.execute(
        'SELECT * FROM payment_orders WHERE order_code = ? AND user_id = ?',
        (order_code, current_user_id())
    ).fetchone()
    if not order:
        return jsonify({"error": "Không tìm thấy đơn hàng."}), 404
    return jsonify({
        'orderCode': order['order_code'], 'plan': order['plan'], 'amount': order['amount'],
        'method': order['method'], 'status': order['status'],
    })


@app.route('/vnpay/return')
def vnpay_return():
    """VNPAY chuyển hướng trình duyệt của học sinh về đây sau khi thanh toán xong. Đây CHỈ
    là màn hình hiển thị kết quả cho người dùng xem — việc CHỐT đơn hàng (cộng gói) luôn dựa
    vào IPN (vnpay_ipn, server-to-server) bên dưới, vì Return URL có thể bị người dùng đóng
    trình duyệt giữa chừng hoặc giả mạo query string."""
    args = request.args.to_dict()
    valid = VNPAY_ENABLED and vnpay_verify_return(args)
    success = valid and args.get('vnp_ResponseCode') == '00'
    order_code = args.get('vnp_TxnRef', '')

    db = get_db()
    order = db.execute('SELECT * FROM payment_orders WHERE order_code = ?', (order_code,)).fetchone()
    status = order['status'] if order else None

    return render_template_string(
        VNPAY_RETURN_HTML,
        success=success, valid=valid, order_code=order_code, status=status,
        plan_label=plan_meta(order['plan'])['label'] if order else '',
    )


@app.route('/vnpay/ipn')
def vnpay_ipn():
    """IPN (Instant Payment Notification) — VNPAY tự gọi endpoint này từ SERVER của họ (không
    qua trình duyệt người dùng) để báo kết quả thanh toán CHÍNH THỨC. Đây là nơi DUY NHẤT được
    phép cộng gói cho tài khoản. Phải trả lời đúng định dạng JSON RspCode/Message VNPAY yêu cầu,
    nếu không VNPAY sẽ coi là thất bại và gọi lại nhiều lần."""
    args = request.args.to_dict()

    if not VNPAY_ENABLED or not vnpay_verify_return(args):
        return jsonify({"RspCode": "97", "Message": "Invalid signature"})

    order_code = args.get('vnp_TxnRef', '')
    db = get_db()
    order = db.execute('SELECT * FROM payment_orders WHERE order_code = ?', (order_code,)).fetchone()
    if not order:
        return jsonify({"RspCode": "01", "Message": "Order not found"})

    # Số tiền VNPAY gửi về đã nhân 100 — đối chiếu lại đúng số tiền đơn hàng gốc để tránh
    # trường hợp bị sửa amount trên đường truyền.
    try:
        vnp_amount = int(args.get('vnp_Amount', '0')) // 100
    except ValueError:
        vnp_amount = -1
    if vnp_amount != order['amount']:
        return jsonify({"RspCode": "04", "Message": "Invalid amount"})

    if order['status'] == 'paid':
        return jsonify({"RspCode": "02", "Message": "Order already confirmed"})

    if args.get('vnp_ResponseCode') == '00':
        db.execute(
            "UPDATE payment_orders SET status = 'paid', provider_txn_id = ?, paid_at = ? WHERE order_code = ?",
            (args.get('vnp_TransactionNo', ''), now_iso(), order_code)
        )
        db.commit()
        grant_plan_upgrade(order['user_id'], order['plan'], order_code, actor='vnpay_ipn')
        return jsonify({"RspCode": "00", "Message": "Confirm Success"})
    else:
        db.execute("UPDATE payment_orders SET status = 'failed' WHERE order_code = ?", (order_code,))
        db.commit()
        return jsonify({"RspCode": "00", "Message": "Confirm Success"})


# ==========================================
# 9. LỊCH SỬ HỘI THOẠI (theo tài khoản đăng nhập)
# ==========================================
@app.route('/api/conversations', methods=['GET'])
@login_required
def list_conversations():
    db = get_db()
    rows = db.execute(
        'SELECT id, title, updated_at, pinned, project_id FROM conversations '
        'WHERE user_id = ? ORDER BY pinned DESC, updated_at DESC',
        (current_user_id(),)
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route('/api/conversations/<int:conv_id>/messages', methods=['GET'])
@login_required
def get_conversation_messages(conv_id):
    db = get_db()
    conv = db.execute(
        'SELECT id FROM conversations WHERE id = ? AND user_id = ?', (conv_id, current_user_id())
    ).fetchone()
    if not conv:
        return jsonify({"error": "Không tìm thấy đoạn chat này."}), 404

    rows = db.execute(
        'SELECT role, content FROM messages WHERE conversation_id = ? ORDER BY id ASC', (conv_id,)
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route('/api/conversations/<int:conv_id>', methods=['PATCH'])
@login_required
def update_conversation(conv_id):
    """Cập nhật 1 đoạn chat: đổi tên, ghim/bỏ ghim, hoặc chuyển vào một dự án."""
    db = get_db()
    conv = db.execute(
        'SELECT id FROM conversations WHERE id = ? AND user_id = ?', (conv_id, current_user_id())
    ).fetchone()
    if not conv:
        return jsonify({"error": "Không tìm thấy đoạn chat này."}), 404

    data = request.get_json(silent=True) or {}
    set_clauses, values = [], []

    if 'title' in data:
        title = (data.get('title') or '').strip()[:120]
        if title:
            set_clauses.append('title = ?')
            values.append(title)

    if 'pinned' in data:
        set_clauses.append('pinned = ?')
        values.append(1 if data.get('pinned') else 0)

    if 'project_id' in data:
        project_id = data.get('project_id')
        if project_id is not None:
            proj = db.execute(
                'SELECT id FROM projects WHERE id = ? AND user_id = ?', (project_id, current_user_id())
            ).fetchone()
            if not proj:
                return jsonify({"error": "Không tìm thấy dự án."}), 404
        set_clauses.append('project_id = ?')
        values.append(project_id)

    if not set_clauses:
        return jsonify({"error": "Không có nội dung nào để cập nhật."}), 400

    values.append(conv_id)
    db.execute(f'UPDATE conversations SET {", ".join(set_clauses)} WHERE id = ?', values)
    db.commit()
    return jsonify({"success": True})


@app.route('/api/conversations/<int:conv_id>', methods=['DELETE'])
@login_required
def delete_conversation(conv_id):
    db = get_db()
    conv = db.execute(
        'SELECT id FROM conversations WHERE id = ? AND user_id = ?', (conv_id, current_user_id())
    ).fetchone()
    if not conv:
        return jsonify({"error": "Không tìm thấy đoạn chat này."}), 404

    db.execute('DELETE FROM messages WHERE conversation_id = ?', (conv_id,))
    db.execute('DELETE FROM conversations WHERE id = ?', (conv_id,))
    db.commit()
    return jsonify({"success": True})


@app.route('/api/conversations/all', methods=['DELETE'])
@login_required
def delete_all_conversations():
    """Xoá toàn bộ lịch sử trò chuyện của tài khoản hiện tại (dùng trong Cài đặt)."""
    db = get_db()
    conv_ids = [r['id'] for r in db.execute(
        'SELECT id FROM conversations WHERE user_id = ?', (current_user_id(),)
    ).fetchall()]
    if conv_ids:
        placeholders = ','.join('?' * len(conv_ids))
        db.execute(f'DELETE FROM messages WHERE conversation_id IN ({placeholders})', conv_ids)
        db.execute('DELETE FROM conversations WHERE user_id = ?', (current_user_id(),))
        db.commit()
    return jsonify({"success": True, "deleted": len(conv_ids)})


# ==========================================
# 9.1 "DỰ ÁN" (giống Claude Projects) — nhóm các đoạn chat theo chủ đề
# ==========================================
@app.route('/api/projects', methods=['GET'])
@login_required
def list_projects():
    db = get_db()
    rows = db.execute(
        'SELECT id, name FROM projects WHERE user_id = ? ORDER BY name ASC', (current_user_id(),)
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route('/api/projects', methods=['POST'])
@login_required
def create_project():
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()[:60]
    if not name:
        return jsonify({"error": "Tên dự án không hợp lệ."}), 400
    db = get_db()
    cur = db.execute(
        'INSERT INTO projects (user_id, name, created_at) VALUES (?, ?, ?)',
        (current_user_id(), name, now_iso())
    )
    db.commit()
    return jsonify({"id": cur.lastrowid, "name": name})


@app.route('/api/projects/<int:proj_id>', methods=['DELETE'])
@login_required
def delete_project(proj_id):
    db = get_db()
    proj = db.execute(
        'SELECT id FROM projects WHERE id = ? AND user_id = ?', (proj_id, current_user_id())
    ).fetchone()
    if not proj:
        return jsonify({"error": "Không tìm thấy dự án."}), 404
    # Xoá dự án không xoá đoạn chat bên trong — chỉ gỡ nhóm, đoạn chat quay lại mục "Gần đây".
    db.execute('UPDATE conversations SET project_id = NULL WHERE project_id = ?', (proj_id,))
    db.execute('DELETE FROM projects WHERE id = ?', (proj_id,))
    db.commit()
    return jsonify({"success": True})


# ==========================================
# 9.2 TUỲ CHỈNH CÁ NHÂN (Cài đặt) + THÔNG BÁO HỆ THỐNG
# ==========================================
@app.route('/api/preferences', methods=['GET'])
@login_required
def get_preferences():
    return jsonify(get_user_preferences(current_user_id()))


@app.route('/api/preferences', methods=['POST'])
@login_required
def update_preferences():
    data = request.get_json(silent=True) or {}
    prefs = save_user_preferences(current_user_id(), data)
    return jsonify(prefs)


# ==========================================
# 8.11 API "QUẢN LÝ TÀI KHOẢN" (avatar, đổi mật khẩu) — dành cho MỌI người dùng
# ==========================================
@app.route('/api/account/avatar', methods=['POST'])
@login_required
def api_set_avatar():
    data = request.get_json(silent=True) or {}
    emoji = (data.get('emoji') or '').strip()
    preset = next((a for a in AVATAR_PRESETS if a['emoji'] == emoji), None)
    if not preset:
        return jsonify({"error": "Avatar không hợp lệ."}), 400
    db = get_db()
    db.execute('UPDATE users SET avatar_emoji = ?, avatar_color = ? WHERE id = ?',
               (preset['emoji'], preset['color'], current_user_id()))
    db.commit()
    return jsonify({"success": True, "emoji": preset['emoji'], "color": preset['color']})


@app.route('/api/account/password', methods=['POST'])
@login_required
def api_change_password():
    """Đổi mật khẩu — chỉ áp dụng cho tài khoản ĐÃ có mật khẩu (không phải OAuth thuần, không
    phải tài khoản khách — khách dùng /guest/upgrade để ĐẶT mật khẩu lần đầu thay vì đổi)."""
    user = current_user()
    if not user or not user['password_hash']:
        return jsonify({"error": "Tài khoản này chưa có mật khẩu để đổi (đăng nhập bằng Google, hoặc là tài khoản khách)."}), 400

    data = request.get_json(silent=True) or {}
    current_pw = data.get('currentPassword') or ''
    new_pw = data.get('newPassword') or ''
    confirm = data.get('confirm') or ''

    if not check_password_hash(user['password_hash'], current_pw):
        return jsonify({"error": "Mật khẩu hiện tại không đúng."}), 400
    if len(new_pw) < 6:
        return jsonify({"error": "Mật khẩu mới phải có ít nhất 6 ký tự."}), 400
    if new_pw != confirm:
        return jsonify({"error": "Mật khẩu mới nhập lại không khớp."}), 400

    db = get_db()
    db.execute('UPDATE users SET password_hash = ? WHERE id = ?',
               (generate_password_hash(new_pw), user['id']))
    db.commit()
    return jsonify({"success": True})


@app.route('/api/account/recovery-code', methods=['POST'])
@login_required
def api_regenerate_recovery_code():
    """Tạo mã khôi phục MỚI cho tài khoản đang đăng nhập — dùng cho: (1) tài khoản tạo TRƯỚC
    khi có tính năng này nên chưa từng có mã, (2) học sinh làm mất/quên mã cũ, muốn tạo lại.
    Mã cũ (nếu có) sẽ bị VÔ HIỆU ngay khi tạo mã mới. Không áp dụng cho tài khoản khách/OAuth
    thuần vì các tài khoản đó không dùng mật khẩu nên khái niệm 'khôi phục mật khẩu' không áp dụng."""
    user = current_user()
    if not user or not user['password_hash']:
        return jsonify({"error": "Tài khoản này không dùng mật khẩu (đăng nhập Google, hoặc tài khoản khách)."}), 400

    new_code = generate_recovery_code()
    db = get_db()
    db.execute('UPDATE users SET recovery_code_hash = ? WHERE id = ?',
               (generate_password_hash(new_code), user['id']))
    db.commit()
    write_audit('regenerate_recovery_code', target=user['username'])
    return jsonify({"success": True, "code": new_code})


@app.route('/api/banner', methods=['GET'])
@login_required
def get_banner():
    return jsonify({
        "message": get_setting('banner_message', '') or '',
        "maintenance": get_setting('maintenance_mode', 'off') == 'on',
    })


# ==========================================
# 9.3 AI TUTOR TUỲ CHỈNH (Developer trở lên)
# ==========================================
@app.route('/api/tutors', methods=['GET'])
@developer_required
def list_tutors():
    db = get_db()
    rows = db.execute(
        'SELECT id, name, system_prompt, created_at FROM custom_tutors WHERE owner_id = ? ORDER BY id DESC',
        (current_user_id(),)
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route('/api/tutors', methods=['POST'])
@developer_required
def create_tutor():
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()[:60]
    system_prompt = (data.get('system_prompt') or '').strip()[:4000]
    if not name or not system_prompt:
        return jsonify({"error": "Cần nhập tên và nội dung hướng dẫn (system prompt) cho Tutor."}), 400
    db = get_db()
    cur = db.execute(
        'INSERT INTO custom_tutors (owner_id, name, system_prompt, created_at) VALUES (?, ?, ?, ?)',
        (current_user_id(), name, system_prompt, now_iso())
    )
    db.commit()
    return jsonify({"id": cur.lastrowid, "name": name, "system_prompt": system_prompt})


@app.route('/api/tutors/<int:tutor_id>', methods=['DELETE'])
@developer_required
def delete_tutor(tutor_id):
    db = get_db()
    tutor = db.execute(
        'SELECT id FROM custom_tutors WHERE id = ? AND owner_id = ?', (tutor_id, current_user_id())
    ).fetchone()
    if not tutor:
        return jsonify({"error": "Không tìm thấy AI Tutor này."}), 404
    db.execute('DELETE FROM custom_tutors WHERE id = ?', (tutor_id,))
    db.commit()
    return jsonify({"success": True})


# ==========================================
# 9.4 API KEY (Developer trở lên) — quản lý key + endpoint xác thực demo
# ==========================================
def _hash_api_key(raw_key):
    import hashlib
    return hashlib.sha256(raw_key.encode('utf-8')).hexdigest()


@app.route('/api/keys', methods=['GET'])
@developer_required
def list_api_keys():
    db = get_db()
    rows = db.execute(
        'SELECT id, name, key_prefix, created_at, last_used_at, revoked FROM api_keys '
        'WHERE user_id = ? ORDER BY id DESC',
        (current_user_id(),)
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route('/api/keys', methods=['POST'])
@developer_required
def create_api_key():
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or 'Key không tên').strip()[:60]
    raw_key = 'sm_' + secrets.token_urlsafe(32)
    key_hash = _hash_api_key(raw_key)
    key_prefix = raw_key[:12] + '…'
    db = get_db()
    cur = db.execute(
        'INSERT INTO api_keys (user_id, name, key_prefix, key_hash, created_at) VALUES (?, ?, ?, ?, ?)',
        (current_user_id(), name, key_prefix, key_hash, now_iso())
    )
    db.commit()
    # Key gốc CHỈ hiển thị đúng 1 lần lúc tạo — sau đó server chỉ còn giữ bản băm (hash).
    return jsonify({"id": cur.lastrowid, "name": name, "key": raw_key, "key_prefix": key_prefix})


@app.route('/api/keys/<int:key_id>', methods=['DELETE'])
@developer_required
def revoke_api_key(key_id):
    db = get_db()
    key_row = db.execute(
        'SELECT id FROM api_keys WHERE id = ? AND user_id = ?', (key_id, current_user_id())
    ).fetchone()
    if not key_row:
        return jsonify({"error": "Không tìm thấy API Key này."}), 404
    db.execute('UPDATE api_keys SET revoked = 1 WHERE id = ?', (key_id,))
    db.commit()
    return jsonify({"success": True})


@app.route('/api/v1/ping', methods=['GET'])
def api_v1_ping():
    """Endpoint demo để xác nhận cơ chế xác thực bằng API Key hoạt động thật (không phải giả lập).
    Header: Authorization: Bearer <api_key>. Đây là điểm khởi đầu hạ tầng — chưa có endpoint
    /api/v1/chat đầy đủ (xem ghi chú 'Chưa làm' trong README)."""
    auth_header = request.headers.get('Authorization', '')
    if not auth_header.startswith('Bearer '):
        return jsonify({"error": "Thiếu API Key (header Authorization: Bearer <key>)."}), 401
    raw_key = auth_header[len('Bearer '):].strip()
    key_hash = _hash_api_key(raw_key)
    db = get_db()
    row = db.execute(
        'SELECT ak.id, u.username FROM api_keys ak JOIN users u ON u.id = ak.user_id '
        'WHERE ak.key_hash = ? AND ak.revoked = 0',
        (key_hash,)
    ).fetchone()
    if not row:
        return jsonify({"error": "API Key không hợp lệ hoặc đã bị thu hồi."}), 401
    db.execute('UPDATE api_keys SET last_used_at = ? WHERE id = ?', (now_iso(), row['id']))
    db.commit()
    return jsonify({"ok": True, "user": row['username'], "message": "API Key hợp lệ."})


# ==========================================
# 9.5 PLAYGROUND (Developer trở lên) — thử prompt trực tiếp, không lưu vào lịch sử chat
# ==========================================
@app.route('/api/playground', methods=['POST'])
@developer_required
def playground_run():
    data = request.get_json(silent=True) or {}
    system_prompt = (data.get('system_prompt') or 'Bạn là một trợ lý AI hữu ích.').strip()[:4000]
    user_message = (data.get('message') or '').strip()[:4000]
    if not user_message:
        return jsonify({"error": "Nhập nội dung để thử nghiệm."}), 400

    def generate():
        try:
            for token in stream_consolex_ai(system_prompt, user_message):
                yield f"data: {json.dumps({'token': token})}\n\n"
            yield f"data: {json.dumps({'done': True})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no', 'Connection': 'keep-alive'},
    )


# ==========================================
# 10. CHAT (STREAMING QUA SERVER-SENT EVENTS) + LƯU LỊCH SỬ
# ==========================================
@app.route('/api/chat', methods=['POST'])
@login_required
def chat():
    data = request.get_json(silent=True) or {}
    subject = data.get('subject', 'Toán Học')
    mode = data.get('mode', 'Giải thích')
    user_message = (data.get('message') or '').strip()
    file_context = (data.get('fileContext') or '').strip()
    file_name = (data.get('fileName') or '').strip()
    image_data = (data.get('imageData') or '').strip()
    raw_conv_id = data.get('conversationId')
    raw_tutor_id = data.get('tutorId')
    raw_thinking_mode = (data.get('thinkingMode') or 'standard').strip()

    role = current_user_role()
    unlimited = role_rank(role) >= role_rank('admin')  # Admin/Super Admin: không giới hạn độ dài tin nhắn

    user_for_plan = current_user()
    plan = effective_plan(user_for_plan)
    # Chốt lại chế độ suy nghĩ hợp lệ theo gói — nếu client cố gửi thẳng 1 chế độ đang bị khoá
    # (vd sửa tay request API), tự động rơi về "Trợ Lý" (standard) thay vì tin tưởng client.
    thinking_mode = resolve_thinking_mode(raw_thinking_mode, plan)
    tm_conf = THINKING_MODES[thinking_mode]
    app_name = app_display_name(user_for_plan)

    # Chế độ bảo trì: chặn học sinh thường, Admin trở lên vẫn dùng được để kiểm tra hệ thống.
    if get_setting('maintenance_mode', 'off') == 'on' and role_rank(role) < role_rank('admin'):
        return jsonify({"error": "Hệ thống đang bảo trì, em quay lại sau ít phút nhé! 🛠️"}), 503

    if not user_message:
        return jsonify({"error": "Em chưa nhập câu hỏi nào cả."}), 400

    # Input validation cơ bản để tránh payload bất thường.
    if not unlimited and len(user_message) > 4000:
        return jsonify({"error": "Câu hỏi quá dài, em rút gọn lại giúp Thầy/Cô nhé!"}), 400
    if image_data and not image_data.startswith('data:image/'):
        return jsonify({"error": "Dữ liệu ảnh không hợp lệ."}), 400

    user_id = current_user_id()
    db = get_db()

    # AI Tutor tuỳ chỉnh (Developer trở lên): nếu chọn 1 tutor riêng, dùng system prompt của
    # tutor đó thay cho prompt mặc định theo Môn học/Chế độ.
    custom_tutor = None
    if raw_tutor_id and role_rank(role) >= role_rank('developer'):
        try:
            tutor_id = int(raw_tutor_id)
        except (TypeError, ValueError):
            tutor_id = None
        if tutor_id is not None:
            custom_tutor = db.execute(
                'SELECT id, name, system_prompt FROM custom_tutors WHERE id = ? AND owner_id = ?',
                (tutor_id, user_id)
            ).fetchone()

    # Xác định (hoặc tạo mới) đoạn hội thoại để lưu lịch sử theo tài khoản.
    conv_id = None
    if raw_conv_id is not None:
        try:
            candidate_id = int(raw_conv_id)
        except (TypeError, ValueError):
            candidate_id = None
        if candidate_id is not None:
            existing = db.execute(
                'SELECT id FROM conversations WHERE id = ? AND user_id = ?', (candidate_id, user_id)
            ).fetchone()
            if existing:
                conv_id = existing['id']

    if conv_id is None:
        title = user_message if len(user_message) <= 40 else (user_message[:40] + '…')
        cur = db.execute(
            'INSERT INTO conversations (user_id, title, created_at, updated_at) VALUES (?, ?, ?, ?)',
            (user_id, title or 'Đoạn chat mới', now_iso(), now_iso())
        )
        db.commit()
        conv_id = cur.lastrowid

    db.execute(
        'INSERT INTO messages (conversation_id, role, content, created_at) VALUES (?, ?, ?, ?)',
        (conv_id, 'user', user_message, now_iso())
    )
    db.commit()

    # "Bộ nhớ" AI: phát hiện + lưu 1 mục mới từ tin nhắn này (nếu có), rồi lấy những gì đã
    # ghi nhớ trước đó để cá nhân hoá câu trả lời (xem mục 0.27).
    new_memory = extract_and_save_memory(user_id, user_message)
    recent_memories = get_recent_memories(user_id)

    if custom_tutor:
        system_prompt = f"""
    Bạn là "{custom_tutor['name']}", một AI Tutor tuỳ chỉnh do chính người dùng tạo ra trên {app_name}.
    Hãy làm theo đúng hướng dẫn/vai trò sau đây do người tạo đặt ra:
    ---
    {custom_tutor['system_prompt']}
    ---
    Vẫn dùng Markdown để trình bày rõ ràng, dễ đọc.
    Công thức toán: dùng LaTeX ("$$...$$" cho dòng riêng, "\\(...\\)" cho công thức ngắn
    giữa câu), đóng đủ dấu ngoặc. Nếu học sinh gõ sai ký hiệu, hiểu ý rồi viết lại cho đúng.
    """
    else:
        system_prompt = f"""
    Bạn là {app_name}, một gia sư AI tận tâm cho học sinh.
    Môn học: {subject}. Chế độ: {mode}.
    Quy tắc:
    1. Xưng "Thầy/Cô", gọi "em".
    2. Dùng Markdown, Emoji, giải thích dễ hiểu, không quá học thuật.
    3. Tuân thủ chế độ:
       - Giải thích: Giải thích bản chất.
       - Gợi ý: Chỉ gợi ý bước làm, KHÔNG giải hộ.
       - Kiểm tra: Sửa lỗi, khen ngợi.
       - Luyện tập: Cho 1-2 bài tập.
       - Ôn tập: Tóm tắt trọng tâm.
    4. Công thức toán: dùng LaTeX — "$$...$$" cho công thức riêng dòng, "\\(...\\)" cho
       công thức ngắn giữa câu, đóng đủ dấu ngoặc. Nếu học sinh gõ sai ký hiệu, hiểu ý rồi
       viết lại cho đúng, đừng chép nguyên văn chỗ sai.
    """

    # "Chế độ suy nghĩ" (Trợ Lý/Học Giả/Giáo Sư/Thiên Tài) — Học Giả/Giáo Sư mở khoá từ gói
    # Premium, Thiên Tài độc quyền gói Max. Chỉ thêm hướng dẫn khi khác "Trợ Lý" mặc định.
    if tm_conf['prompt_hint']:
        system_prompt += f"""

    Chế độ suy nghĩ đang bật: "{tm_conf['icon']} {tm_conf['label']}". {tm_conf['prompt_hint']}
    """

    # Admin có thể thêm 1 đoạn hướng dẫn chung áp dụng cho MỌI cuộc trò chuyện (vd: quy định
    # riêng của trường/lớp) từ trang /developer, không cần sửa code.
    global_addendum = get_setting('global_system_addendum', '')
    if global_addendum:
        system_prompt += f"""

    Hướng dẫn bổ sung từ quản trị viên hệ thống (áp dụng cho mọi cuộc trò chuyện):
    ---
    {global_addendum}
    ---
    """

    if recent_memories:
        mem_lines = "\n".join(f"    - {m}" for m in recent_memories)
        system_prompt += f"""

    Những điều Thầy/Cô đã ghi nhớ về học sinh này từ các lần trò chuyện trước:
{mem_lines}
    Hãy tận dụng thông tin này để cá nhân hoá câu trả lời khi phù hợp (vd: nếu biết học sinh
    hay nhầm 1 lỗi cụ thể, hãy giải thích kỹ hơn ở phần đó), nhưng đừng nhắc lại y nguyên nếu
    không cần thiết.
    """

    if file_context:
        system_prompt += f"""

    Học sinh đã tải lên file "{file_name}" với nội dung trích xuất như sau (có thể không đầy đủ):
    ---
    {file_context}
    ---
    Hãy dùng nội dung này để trả lời câu hỏi của học sinh khi liên quan.
    """

    if image_data:
        system_prompt += """

    Học sinh đã đính kèm một hình ảnh (ví dụ: đề bài chụp, bài làm viết tay, biểu đồ...).
    Hãy quan sát kỹ nội dung trong ảnh để trả lời câu hỏi của học sinh.
    """

    if image_data:
        user_content = [
            {"type": "text", "text": user_message},
            {"type": "image_url", "image_url": {"url": image_data}},
        ]
    else:
        user_content = user_message

    def generate():
        yield f"data: {json.dumps({'conversationId': conv_id, 'thinkingMode': thinking_mode})}\n\n"
        if new_memory:
            yield f"data: {json.dumps({'memory': new_memory})}\n\n"
        collected = []
        try:
            for token in stream_consolex_ai(system_prompt, user_content, max_tokens=tm_conf['max_tokens']):
                collected.append(token)
                yield f"data: {json.dumps({'token': token})}\n\n"

            assistant_text = ''.join(collected).strip()
            if assistant_text:
                # Kết nối riêng (không dùng `db`/`g` của request) — xem giải thích chi tiết
                # ở docstring của open_write_db(): tới lúc này request context gốc có thể
                # đã bị teardown (đóng kết nối `db`) trước khi generator chạy tới đây.
                write_conn = open_write_db()
                try:
                    write_conn.execute(
                        'INSERT INTO messages (conversation_id, role, content, created_at) VALUES (?, ?, ?, ?)',
                        (conv_id, 'assistant', assistant_text, now_iso())
                    )
                    write_conn.execute('UPDATE conversations SET updated_at = ? WHERE id = ?', (now_iso(), conv_id))
                    write_conn.commit()
                finally:
                    write_conn.close()

            log_usage(user_id, subject, mode, len(user_message), len(assistant_text),
                      bool(file_context), bool(image_data), 'ok' if assistant_text else 'empty')

            if assistant_text:
                track_topic_practice(user_id, subject, mode)
                gamify = award_xp_and_streak(user_id)
                yield f"data: {json.dumps({'gamify': gamify})}\n\n"

            yield f"data: {json.dumps({'done': True})}\n\n"
        except RuntimeError as e:
            log_usage(user_id, subject, mode, len(user_message), 0, bool(file_context), bool(image_data), 'error')
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
        except requests.exceptions.RequestException as e:
            log_usage(user_id, subject, mode, len(user_message), 0, bool(file_context), bool(image_data), 'error')
            yield f"data: {json.dumps({'error': f'Lỗi kết nối tới xAI: {e}'})}\n\n"
        except (KeyError, IndexError, ValueError) as e:
            log_usage(user_id, subject, mode, len(user_message), 0, bool(file_context), bool(image_data), 'error')
            yield f"data: {json.dumps({'error': f'Phản hồi từ xAI không đúng định dạng: {e}'})}\n\n"
        except Exception as e:
            log_usage(user_id, subject, mode, len(user_message), 0, bool(file_context), bool(image_data), 'error')
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',  # tắt buffer ở Nginx nếu có, để stream tới ngay
            'Connection': 'keep-alive',
        },
    )

# Cuối file, TRƯỚC if __name__ == '__main__':
# hoặc ngay sau khi define xong init_db + app routes cũng được,
# miễn là chạy 1 lần lúc load module:
try:
    init_db()
except Exception as e:
    print(f"⚠️ init_db failed: {e}")
    raise

if __name__ == '__main__':
    init_db()
    print("🚀 StudyMate AI đang chạy... Truy cập: http://localhost:5000")
    print("👤 Trang đăng nhập: http://localhost:5000/login")
    print(f"🔑 Đăng nhập Google: {'BẬT' if GOOGLE_OAUTH_ENABLED else 'tắt (chưa cấu hình .env)'}")
    print("🛡️ Để xem bảng báo cáo bảo mật... Truy cập: http://localhost:5000/security")
    # debug=True chỉ dùng khi phát triển trên máy cá nhân — KHÔNG bật khi deploy thật
    # (xem README phần "Deploy lên production" để chạy bằng gunicorn thay vì app.run).
    # use_reloader=False: khi debug=True, Werkzeug mặc định tự khởi động lại
    # (restart) tiến trình mỗi khi phát hiện một file trong thư mục dự án thay
    # đổi. studymate.db (SQLite) bị ghi liên tục mỗi khi có tin nhắn mới, nên
    # nó cũng bị coi là "file thay đổi" và làm server tự restart ngay giữa lúc
    # đang stream câu trả lời — kết nối SQLite của request đó bị đóng đột ngột,
    # gây lỗi "Cannot operate on a closed database.". Tắt use_reloader để tránh
    # restart ngoài ý muốn này (vẫn giữ debug=True để còn thấy traceback lỗi khi
    # phát triển). Khi sửa code .py, chỉ cần dừng (Ctrl+C) và chạy lại thủ công.
    app.run(host='0.0.0.0', port=5000, debug=True, threaded=True, use_reloader=False)
