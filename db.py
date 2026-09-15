"""SQLite 数据库操作：问答缓存 + 简历集合（分支）。

- collections 表：每份「简历」一个集合，拥有独立的文件目录与缓存分支
- qa_cache 表：存「问题 - 解答」问答对，按 collection_id 分分支
- settings 表：记录当前激活的简历集合 id
"""
import os
import sqlite3

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data", "sqlite")
DB_PATH = os.path.join(DATA_DIR, "qa_cache.db")


def _connect():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = _connect()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS collections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            create_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS qa_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_query TEXT NOT NULL,
            query_embedding BLOB,
            ai_answer TEXT NOT NULL,
            collection_id INTEGER,
            create_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    # 兼容旧库：qa_cache 缺 collection_id 列则补上
    cols = [r[1] for r in conn.execute("PRAGMA table_info(qa_cache)").fetchall()]
    if "collection_id" not in cols:
        conn.execute("ALTER TABLE qa_cache ADD COLUMN collection_id INTEGER")
    # 确保存在默认简历集合
    if conn.execute("SELECT id FROM collections ORDER BY id LIMIT 1").fetchone() is None:
        conn.execute("INSERT INTO collections (name) VALUES (?)", ("默认简历",))
    default_id = conn.execute("SELECT id FROM collections ORDER BY id LIMIT 1").fetchone()[0]
    # 旧缓存（无归属）归入默认集合
    conn.execute("UPDATE qa_cache SET collection_id = ? WHERE collection_id IS NULL", (default_id,))
    # 确保有当前集合设置
    if conn.execute("SELECT value FROM settings WHERE key='current_collection'").fetchone() is None:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('current_collection', ?)",
            (str(default_id),),
        )
    conn.commit()
    conn.close()


# ================= 简历集合 =================
def list_collections():
    conn = _connect()
    rows = conn.execute("SELECT id, name, create_at FROM collections ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def create_collection(name):
    conn = _connect()
    cur = conn.execute("INSERT INTO collections (name) VALUES (?)", (name,))
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id


def delete_collection(cid):
    """删除集合及其全部缓存（文件由 document 层处理）。"""
    conn = _connect()
    conn.execute("DELETE FROM qa_cache WHERE collection_id = ?", (cid,))
    conn.execute("DELETE FROM collections WHERE id = ?", (cid,))
    conn.commit()
    conn.close()


def get_current_collection():
    conn = _connect()
    row = conn.execute("SELECT value FROM settings WHERE key='current_collection'").fetchone()
    conn.close()
    return int(row["value"]) if row else None


def set_current_collection(cid):
    conn = _connect()
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES ('current_collection', ?)",
        (str(cid),),
    )
    conn.commit()
    conn.close()


# ================= 问答缓存（按集合分支） =================
def save_qa(user_query, ai_answer, collection_id, query_embedding=None):
    conn = _connect()
    cur = conn.execute(
        "INSERT INTO qa_cache (user_query, query_embedding, ai_answer, collection_id) VALUES (?, ?, ?, ?)",
        (user_query, query_embedding, ai_answer, collection_id),
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id


def find_by_query(user_query, collection_id):
    """按「问题 + 简历集合」精确匹配。命中返回记录 dict，否则 None。"""
    conn = _connect()
    row = conn.execute(
        "SELECT * FROM qa_cache WHERE user_query = ? AND collection_id = ? ORDER BY id DESC LIMIT 1",
        (user_query, collection_id),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def list_qa(collection_id=None):
    """返回问答对；collection_id 为 None 时返回全部（含集合名）。"""
    conn = _connect()
    if collection_id is None:
        rows = conn.execute(
            """
            SELECT q.id, q.user_query, q.ai_answer, q.create_at, q.collection_id,
                   c.name AS collection_name
            FROM qa_cache q LEFT JOIN collections c ON q.collection_id = c.id
            ORDER BY q.id DESC
            """
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, user_query, ai_answer, create_at, collection_id "
            "FROM qa_cache WHERE collection_id = ? ORDER BY id DESC",
            (collection_id,),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_qa(qa_id, ai_answer):
    conn = _connect()
    conn.execute("UPDATE qa_cache SET ai_answer = ? WHERE id = ?", (ai_answer, qa_id))
    conn.commit()
    conn.close()


def delete_qa(qa_id):
    conn = _connect()
    conn.execute("DELETE FROM qa_cache WHERE id = ?", (qa_id,))
    conn.commit()
    conn.close()
