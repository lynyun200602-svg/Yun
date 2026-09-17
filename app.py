"""发面馒头 —— FastAPI 主应用（多用户版）。

- 账号密码登录：会话通过 HttpOnly Cookie（sessions 表）识别当前用户
- 每个用户独立：简历集合、问答缓存、API Key/模型、当前简历均按用户隔离
- 各端登录同一账号，数据自动同步；不同账号之间互不可见
"""
import json
import os
import queue
import threading
import time
from pathlib import Path
from typing import List

from fastapi import FastAPI, File, UploadFile, HTTPException, Request, Response, Depends
from fastapi.responses import FileResponse, StreamingResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from dotenv import load_dotenv

import db
import document
import claude_client

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="发面馒头")

db.init_db()

SESSION_COOKIE = "session"


# ================= 认证 =================
def _current_user(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    return db.get_session_user(token) if token else None


def require_user(request: Request):
    user = _current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="未登录或登录已过期")
    return user


def _set_session(response: Response, user_id: int):
    token = db.create_session(user_id)
    response.set_cookie(
        SESSION_COOKIE, token,
        httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30,
    )


# ================= 请求模型 =================
class ChatRequest(BaseModel):
    question: str


class UpdateRequest(BaseModel):
    ai_answer: str


class SettingRequest(BaseModel):
    api_key: str = ""
    model: str = "deepseek-v4-pro"


class CollectionCreate(BaseModel):
    name: str


class CollectionSwitch(BaseModel):
    id: int


class AuthRequest(BaseModel):
    username: str
    password: str


# ================= 页面 =================
@app.get("/login")
def page_login():
    return FileResponse(STATIC_DIR / "login.html")


@app.get("/")
@app.get("/index")
def page_index(request: Request):
    if not _current_user(request):
        return RedirectResponse("/login")
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/cache-manage")
def page_cache(request: Request):
    if not _current_user(request):
        return RedirectResponse("/login")
    return FileResponse(STATIC_DIR / "cache.html")


@app.get("/setting")
def page_setting(request: Request):
    if not _current_user(request):
        return RedirectResponse("/login")
    return FileResponse(STATIC_DIR / "setting.html")


# ================= 认证接口 =================
@app.post("/api/register")
def api_register(req: AuthRequest, response: Response):
    username = req.username.strip()
    password = req.password
    if not (2 <= len(username) <= 30):
        raise HTTPException(400, "用户名需 2-30 个字符")
    if len(password) < 4:
        raise HTTPException(400, "密码至少 4 位")
    if db.get_user_by_username(username):
        raise HTTPException(400, "用户名已存在")
    # 新用户默认继承全局 .env 的 Key 与模型，可后续在设置页改为自己的
    default_key = os.getenv("ANTHROPIC_API_KEY", "")
    default_model = os.getenv("CLAUDE_MODEL", "deepseek-v4-pro")
    user_id = db.create_user(username, password, default_key, default_model)
    _set_session(response, user_id)
    return {"ok": True}


@app.post("/api/login")
def api_login(req: AuthRequest, response: Response):
    user = db.get_user_by_username(req.username.strip())
    if not user or not db.verify_password(req.password, user["password_hash"]):
        raise HTTPException(400, "用户名或密码错误")
    _set_session(response, user["id"])
    return {"ok": True}


@app.post("/api/logout")
def api_logout(request: Request, response: Response):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        db.delete_session(token)
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}


@app.get("/api/me")
def api_me(request: Request):
    user = _current_user(request)
    if not user:
        raise HTTPException(401, "未登录")
    return {"username": user["username"]}


# ================= 上传 =================
@app.post("/api/upload")
async def api_upload(files: List[UploadFile] = File(...), user: dict = Depends(require_user)):
    cid = db.get_current_collection(user["id"])
    saved = []
    for f in files:
        ext = Path(f.filename).suffix.lower()
        if ext not in document.SUPPORTED:
            continue
        dest = Path(document.upload_dir(cid)) / f.filename
        dest.write_bytes(await f.read())
        saved.append(f.filename)
    return {"saved": saved}


@app.get("/api/files")
def api_files(user: dict = Depends(require_user)):
    cid = db.get_current_collection(user["id"])
    return {"files": document.list_files(cid)}


@app.delete("/api/files/{name}")
def api_delete_file(name: str, user: dict = Depends(require_user)):
    cid = db.get_current_collection(user["id"])
    d = Path(document.upload_dir(cid)).resolve()
    p = (d / name).resolve()
    # 防止路径穿越：只允许删除 upload 目录内的文件
    if not str(p).startswith(str(d)):
        raise HTTPException(400, "非法文件名")
    if p.exists() and p.is_file():
        p.unlink()
        return {"ok": True}
    raise HTTPException(404, "文件不存在")


@app.delete("/api/files")
def api_clear_files(user: dict = Depends(require_user)):
    cid = db.get_current_collection(user["id"])
    d = Path(document.upload_dir(cid))
    removed = 0
    if d.exists():
        for p in d.iterdir():
            if p.is_file():
                p.unlink()
                removed += 1
    return {"removed": removed}


# ================= 简历集合（分支） =================
@app.get("/api/collections")
def api_collections(user: dict = Depends(require_user)):
    uid = user["id"]
    cid = db.get_current_collection(uid)
    items = db.list_collections(uid)
    for it in items:
        it["file_count"] = len(document.list_files(it["id"]))
        it["current"] = (it["id"] == cid)
    return {"collections": items, "current_id": cid}


@app.post("/api/collections")
def api_create_collection(req: CollectionCreate, user: dict = Depends(require_user)):
    name = req.name.strip()
    if not name:
        raise HTTPException(400, "名称不能为空")
    new_id = db.create_collection(user["id"], name)
    db.set_current_collection(user["id"], new_id)
    return {"ok": True, "id": new_id}


@app.post("/api/collections/switch")
def api_switch_collection(req: CollectionSwitch, user: dict = Depends(require_user)):
    db.set_current_collection(user["id"], req.id)
    return {"ok": True}


@app.delete("/api/collections/{cid}")
def api_delete_collection(cid: int, user: dict = Depends(require_user)):
    uid = user["id"]
    if len(db.list_collections(uid)) <= 1:
        raise HTTPException(400, "至少保留一个简历")
    document.delete_collection_files(cid)
    db.delete_collection(uid, cid)
    if db.get_current_collection(uid) == cid:
        db.set_current_collection(uid, db.list_collections(uid)[0]["id"])
    return {"ok": True}


# ================= 问答核心 =================
@app.post("/api/chat")
def api_chat(req: ChatRequest, user: dict = Depends(require_user)):
    question = req.question.strip()
    if not question:
        raise HTTPException(400, "问题不能为空")

    uid = user["id"]
    cid = db.get_current_collection(uid)

    # 1. 缓存精确匹配（完全相等，按当前用户 + 简历分支）
    hit = db.find_by_query(uid, question, cid)
    if hit:
        return {"answer": hit["ai_answer"], "from_cache": True, "id": hit["id"]}

    # 2. 读取当前简历集合的全部文档作为上下文
    context = document.read_documents(cid)
    if not context:
        raise HTTPException(400, "当前简历尚未上传资料，请先上传 pdf/docx/图片 文件")

    # 3. 调用模型（使用当前用户自己的 Key 与模型）
    answer, err = claude_client.answer_question(question, context, user["api_key"], user["model"])
    if err:
        raise HTTPException(500, err)

    # 4. 写入缓存（归属当前用户 + 简历分支）
    qa_id = db.save_qa(uid, question, answer, cid)

    return {"answer": answer, "from_cache": False, "id": qa_id}


# ================= 问答核心（流式） =================
def _sse(payload):
    """把一个 dict 包装成 SSE 事件文本。"""
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


# 正在生成中的问题（(uid, cid, question)），用于防重复生成 + 切走后完成后可从缓存取回
_generating = set()
_generating_lock = threading.Lock()


def _is_generating(key):
    with _generating_lock:
        return key in _generating


def _add_generating(key):
    with _generating_lock:
        _generating.add(key)


def _remove_generating(key):
    with _generating_lock:
        _generating.discard(key)


@app.post("/api/chat/stream")
def api_chat_stream(req: ChatRequest, user: dict = Depends(require_user)):
    question = req.question.strip()
    if not question:
        raise HTTPException(400, "问题不能为空")

    uid = user["id"]
    cid = db.get_current_collection(uid)
    key = (uid, cid, question)

    # 1. 缓存精确匹配（按当前用户 + 简历分支）→ 直接流式返回历史答案
    hit = db.find_by_query(uid, question, cid)
    if hit:
        def gen_cache():
            yield _sse({"type": "delta", "text": hit["ai_answer"]})
            yield _sse({"type": "done", "from_cache": True, "id": hit["id"]})
        return StreamingResponse(gen_cache(), media_type="text/event-stream")

    # 2. 正在生成中（比如用户切走又回来重新提交）→ 等它完成，从缓存取回，避免重复调用模型
    if _is_generating(key):
        def gen_wait():
            yield _sse({"type": "thinking"})
            while _is_generating(key):
                time.sleep(0.3)
            hit2 = db.find_by_query(uid, question, cid)
            if hit2:
                yield _sse({"type": "delta", "text": hit2["ai_answer"]})
                yield _sse({"type": "done", "from_cache": True, "id": hit2["id"]})
            else:
                yield _sse({"type": "error", "text": "回答生成失败，请重试"})
        return StreamingResponse(gen_wait(), media_type="text/event-stream")

    # 3. 读取当前简历集合的全部文档作为上下文
    context = document.read_documents(cid)
    if not context:
        raise HTTPException(400, "当前简历尚未上传资料，请先上传 pdf/docx/图片 文件")

    # 4. 在后台线程生成并落库（与 SSE 连接解耦：用户中途切走也不丢回答）
    _add_generating(key)
    q = queue.Queue()

    def worker():
        parts = []
        failed = False
        try:
            for kind, text in claude_client.stream_answer(question, context, user["api_key"], user["model"]):
                if kind == "thinking":
                    q.put(("thinking", ""))
                elif kind == "delta":
                    parts.append(text)
                    q.put(("delta", text))
                elif kind == "error":
                    failed = True
                    q.put(("error", text))
                    return
                elif kind == "done":
                    break
        except Exception as e:  # 兜底：stream_answer 内部已捕获，这里双保险
            failed = True
            q.put(("error", str(e)))
        finally:
            if failed:
                q.put(("done", None))
            else:
                answer = "".join(parts).strip()
                if answer:
                    qa_id = db.save_qa(uid, question, answer, cid)
                    q.put(("done", qa_id))
                else:
                    q.put(("done", None))
            q.put(None)  # 结束哨兵
            _remove_generating(key)

    threading.Thread(target=worker, daemon=True).start()

    def gen():
        while True:
            item = q.get()
            if item is None:
                break
            kind, val = item
            if kind == "thinking":
                yield _sse({"type": "thinking"})
            elif kind == "delta":
                yield _sse({"type": "delta", "text": val})
            elif kind == "error":
                yield _sse({"type": "error", "text": val})
                return
            elif kind == "done":
                yield _sse({"type": "done", "id": val})

    return StreamingResponse(gen(), media_type="text/event-stream")


# ================= 缓存管理 =================
@app.get("/api/cache")
def api_cache(user: dict = Depends(require_user)):
    return {"items": db.list_qa(user["id"])}


@app.put("/api/cache/{qa_id}")
def api_cache_update(qa_id: int, req: UpdateRequest, user: dict = Depends(require_user)):
    db.update_qa(user["id"], qa_id, req.ai_answer)
    return {"ok": True}


@app.delete("/api/cache/{qa_id}")
def api_cache_delete(qa_id: int, user: dict = Depends(require_user)):
    db.delete_qa(user["id"], qa_id)
    return {"ok": True}


@app.get("/api/cache/export")
def api_cache_export(user: dict = Depends(require_user)):
    items = db.list_qa(user["id"])
    lines = ["# 面试问答缓存", ""]
    for it in items:
        lines.append(f"## Q：{it['user_query']}")
        lines.append("")
        lines.append(it["ai_answer"])
        lines.append("")
        lines.append(f"> 记录时间：{it['create_at']}")
        lines.append("")
        lines.append("---")
        lines.append("")
    return {"content": "\n".join(lines), "count": len(items)}


# ================= 设置（按用户） =================
@app.post("/api/setting")
def api_save_setting(req: SettingRequest, user: dict = Depends(require_user)):
    key = req.api_key.strip()
    model = req.model.strip() or user["model"]
    if key:
        db.update_user_setting(user["id"], api_key=key)
    db.update_user_setting(user["id"], model=model)
    return {"ok": True}


@app.get("/api/setting")
def api_get_setting(user: dict = Depends(require_user)):
    key = user["api_key"]
    model = user["model"]
    _, provider = claude_client.resolve_provider(key, model)
    masked = ""
    if key:
        masked = key[:8] + "…" + key[-4:] if len(key) > 16 else "已配置"
    return {
        "has_key": bool(key),
        "key_masked": masked,
        "model": model,
        "provider": provider,
    }


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
