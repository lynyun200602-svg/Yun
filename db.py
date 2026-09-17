"""SQLite 数据库操作：用户 + 会话 + 简历集合 + 问答缓存（全部按用户隔离）。"""
import os
import sqlite3
import hashlib
import secrets

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data", "sqlite")
DB_PATH = os.path.join(DATA_DIR, "qa_cache.db")

PBKDF2_ITERATIONS = 100_000


def _connect():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _has_column(conn, table, column):
    return column in [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def hash_password(password):
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return salt.hex() + "$" + dk.hex()


def verify_password(password, stored):
    try:
        salt_hex, dk_hex = stored.split("$", 1)
        salt = bytes.fromhex(salt_hex)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
        return secrets.compare_digest(dk.hex(), dk_hex)
    except Exception:
        return False


def init_db():
    conn = _connect()
    # 用户表
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            api_key TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT 'deepseek-v4-pro',
            current_collection_id INTEGER,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    # 会话表
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    # 简历集合表（含 user_id）
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS collections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            create_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    if not _has_column(conn, "collections", "user_id"):
        conn.execute("ALTER TABLE collections ADD COLUMN user_id INTEGER")
    # 问答缓存表（含 user_id）
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS qa_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            user_query TEXT NOT NULL,
            query_embedding BLOB,
            ai_answer TEXT NOT NULL,
            collection_id INTEGER,
            create_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    if not _has_column(conn, "qa_cache", "user_id"):
        conn.execute("ALTER TABLE qa_cache ADD COLUMN user_id INTEGER")
    if not _has_column(conn, "qa_cache", "collection_id"):
        conn.execute("ALTER TABLE qa_cache ADD COLUMN collection_id INTEGER")
    conn.commit()
    conn.close()


# ================= 用户 =================
def create_user(username, password, api_key="", model="deepseek-v4-pro"):
    """创建用户，并为其初始化一个默认简历集合。返回新用户 id。"""
    conn = _connect()
    cur = conn.execute(
        "INSERT INTO users (username, password_hash, api_key, model) VALUES (?, ?, ?, ?)",
        (username, hash_password(password), api_key, model),
    )
    user_id = cur.lastrowid
    ccur = conn.execute(
        "INSERT INTO collections (user_id, name) VALUES (?, ?)", (user_id, "默认简历")
    )
    conn.execute(
        "UPDATE users SET current_collection_id = ? WHERE id = ?", (ccur.lastrowid, user_id)
    )
    conn.commit()
    conn.close()
    return user_id


def get_user_by_username(username):
    conn = _connect()
    row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_user_by_id(user_id):
    conn = _connect()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def update_user_setting(user_id, api_key=None, model=None):
    conn = _connect()
    if api_key is not None:
        conn.execute("UPDATE users SET api_key = ? WHERE id = ?", (api_key, user_id))
    if model is not None:
        conn.execute("UPDATE users SET model = ? WHERE id = ?", (model, user_id))
    conn.commit()
    conn.close()


# ================= 会话 =================
def create_session(user_id):
    token = secrets.token_hex(32)
    conn = _connect()
    conn.execute("INSERT INTO sessions (token, user_id) VALUES (?, ?)", (token, user_id))
    conn.commit()
    conn.close()
    return token


def get_session_user(token):
    if not token:
        return None
    conn = _connect()
    row = conn.execute(
        "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token = ?",
        (token,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def delete_session(token):
    conn = _connect()
    conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
    conn.commit()
    conn.close()


# ================= 简历集合 =================
def list_collections(user_id):
    conn = _connect()
    rows = conn.execute(
        "SELECT id, name, create_at FROM collections WHERE user_id = ? ORDER BY id",
        (user_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def create_collection(user_id, name):
    conn = _connect()
    cur = conn.execute(
        "INSERT INTO collections (user_id, name) VALUES (?, ?)", (user_id, name)
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id


def delete_collection(user_id, cid):
    conn = _connect()
    conn.execute(
        "DELETE FROM qa_cache WHERE user_id = ? AND collection_id = ?", (user_id, cid)
    )
    conn.execute("DELETE FROM collections WHERE id = ? AND user_id = ?", (cid, user_id))
    conn.commit()
    conn.close()


def get_current_collection(user_id):
    conn = _connect()
    row = conn.execute(
        "SELECT current_collection_id FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    conn.close()
    return row["current_collection_id"] if row and row["current_collection_id"] else None


def set_current_collection(user_id, cid):
    conn = _connect()
    conn.execute("UPDATE users SET current_collection_id = ? WHERE id = ?", (cid, user_id))
    conn.commit()
    conn.close()


# ================= 问答缓存（按用户 + 集合） =================
def save_qa(user_id, user_query, ai_answer, collection_id, query_embedding=None):
    conn = _connect()
    cur = conn.execute(
        "INSERT INTO qa_cache (user_id, user_query, query_embedding, ai_answer, collection_id) VALUES (?, ?, ?, ?, ?)",
        (user_id, user_query, query_embedding, ai_answer, collection_id),
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id


def find_by_query(user_id, user_query, collection_id):
    conn = _connect()
    row = conn.execute(
        "SELECT * FROM qa_cache WHERE user_id = ? AND user_query = ? AND collection_id = ? ORDER BY id DESC LIMIT 1",
        (user_id, user_query, collection_id),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def list_qa(user_id):
    conn = _connect()
    rows = conn.execute(
        """
        SELECT q.id, q.user_query, q.ai_answer, q.create_at, q.collection_id,
               c.name AS collection_name
        FROM qa_cache q LEFT JOIN collections c ON q.collection_id = c.id
        WHERE q.user_id = ?
        ORDER BY q.id DESC
        """,
        (user_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_qa(user_id, qa_id, ai_answer):
    conn = _connect()
    conn.execute(
        "UPDATE qa_cache SET ai_answer = ? WHERE id = ? AND user_id = ?",
        (ai_answer, qa_id, user_id),
    )
    conn.commit()
    conn.close()


def delete_qa(user_id, qa_id):
    conn = _connect()
    conn.execute("DELETE FROM qa_cache WHERE id = ? AND user_id = ?", (qa_id, user_id))
    conn.commit()
    conn.close()
