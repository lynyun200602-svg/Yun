"""发面馒头 —— FastAPI 主应用。

第一版范围：
- 不用 Chroma / embedding；qa_cache.query_embedding 保留但不用
- 缓存命中 = 字符串完全相等
- 上传 pdf/docx 只存磁盘，提问时读全文做上下文
- 数据库存「问题-解答」问答对，可复用
"""
import json
import os
from pathlib import Path
from typing import List

from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from dotenv import load_dotenv, set_key

import db
import document
import claude_client

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
ENV_PATH = BASE_DIR / ".env"

app = FastAPI(title="发面馒头")

db.init_db()
# 旧版平铺在根目录的文件迁入默认简历集合
_default_cid = db.get_current_collection()
if _default_cid:
    document.migrate_legacy_files(_default_cid)


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


# ================= 页面 =================
@app.get("/")
@app.get("/index")
def page_index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/cache-manage")
def page_cache():
    return FileResponse(STATIC_DIR / "cache.html")


@app.get("/setting")
def page_setting():
    return FileResponse(STATIC_DIR / "setting.html")


# ================= 上传 =================
@app.post("/api/upload")
async def api_upload(files: List[UploadFile] = File(...)):
    cid = db.get_current_collection()
    saved = []
    for f in files:
        ext = Path(f.filename).suffix.lower()
        if ext not in (".pdf", ".docx"):
            continue
        dest = Path(document.upload_dir(cid)) / f.filename
        dest.write_bytes(await f.read())
        saved.append(f.filename)
    return {"saved": saved}


@app.get("/api/files")
def api_files():
    cid = db.get_current_collection()
    return {"files": document.list_files(cid)}


@app.delete("/api/files/{name}")
def api_delete_file(name: str):
    cid = db.get_current_collection()
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
def api_clear_files():
    cid = db.get_current_collection()
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
def api_collections():
    cid = db.get_current_collection()
    items = db.list_collections()
    for it in items:
        it["file_count"] = len(document.list_files(it["id"]))
        it["current"] = (it["id"] == cid)
    return {"collections": items, "current_id": cid}


@app.post("/api/collections")
def api_create_collection(req: CollectionCreate):
    name = req.name.strip()
    if not name:
        raise HTTPException(400, "名称不能为空")
    new_id = db.create_collection(name)
    db.set_current_collection(new_id)
    return {"ok": True, "id": new_id}


@app.post("/api/collections/switch")
def api_switch_collection(req: CollectionSwitch):
    db.set_current_collection(req.id)
    return {"ok": True}


@app.delete("/api/collections/{cid}")
def api_delete_collection(cid: int):
    if len(db.list_collections()) <= 1:
        raise HTTPException(400, "至少保留一个简历")
    document.delete_collection_files(cid)
    db.delete_collection(cid)
    if db.get_current_collection() == cid:
        db.set_current_collection(db.list_collections()[0]["id"])
    return {"ok": True}


# ================= 问答核心 =================
@app.post("/api/chat")
def api_chat(req: ChatRequest):
    question = req.question.strip()
    if not question:
        raise HTTPException(400, "问题不能为空")

    cid = db.get_current_collection()

    # 1. 缓存精确匹配（完全相等，按当前简历分支）
    hit = db.find_by_query(question, cid)
    if hit:
        return {"answer": hit["ai_answer"], "from_cache": True, "id": hit["id"]}

    # 2. 读取当前简历集合的全部文档作为上下文
    context = document.read_documents(cid)
    if not context:
        raise HTTPException(400, "当前简历尚未上传资料，请先上传 pdf/docx 文件")

    # 3. 调用模型
    answer, err = claude_client.answer_question(question, context)
    if err:
        raise HTTPException(500, err)

    # 4. 写入缓存（归属当前简历分支）
    qa_id = db.save_qa(question, answer, cid)

    return {"answer": answer, "from_cache": False, "id": qa_id}


# ================= 问答核心（流式） =================
def _sse(payload):
    """把一个 dict 包装成 SSE 事件文本。"""
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


@app.post("/api/chat/stream")
def api_chat_stream(req: ChatRequest):
    question = req.question.strip()
    if not question:
        raise HTTPException(400, "问题不能为空")

    cid = db.get_current_collection()

    # 1. 缓存精确匹配（按当前简历分支）→ 直接流式返回历史答案
    hit = db.find_by_query(question, cid)
    if hit:
        def gen_cache():
            yield _sse({"type": "delta", "text": hit["ai_answer"]})
            yield _sse({"type": "done", "from_cache": True, "id": hit["id"]})
        return StreamingResponse(gen_cache(), media_type="text/event-stream")

    # 2. 读取当前简历集合的全部文档作为上下文
    context = document.read_documents(cid)
    if not context:
        raise HTTPException(400, "当前简历尚未上传资料，请先上传 pdf/docx 文件")

    # 3. 流式调用模型，逐字返回
    def gen():
        parts = []
        for kind, text in claude_client.stream_answer(question, context):
            if kind == "thinking":
                yield _sse({"type": "thinking"})
            elif kind == "delta":
                parts.append(text)
                yield _sse({"type": "delta", "text": text})
            elif kind == "error":
                yield _sse({"type": "error", "text": text})
                return
            elif kind == "done":
                break
        answer = "".join(parts).strip()
        if answer:
            qa_id = db.save_qa(question, answer, cid)
            yield _sse({"type": "done", "id": qa_id})
        else:
            yield _sse({"type": "done"})

    return StreamingResponse(gen(), media_type="text/event-stream")


# ================= 缓存管理 =================
@app.get("/api/cache")
def api_cache():
    return {"items": db.list_qa()}


@app.put("/api/cache/{qa_id}")
def api_cache_update(qa_id: int, req: UpdateRequest):
    db.update_qa(qa_id, req.ai_answer)
    return {"ok": True}


@app.delete("/api/cache/{qa_id}")
def api_cache_delete(qa_id: int):
    db.delete_qa(qa_id)
    return {"ok": True}


@app.get("/api/cache/export")
def api_cache_export():
    items = db.list_qa()
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


# ================= 设置 =================
@app.post("/api/setting")
def api_save_setting(req: SettingRequest):
    key = req.api_key.strip()
    if key:
        set_key(str(ENV_PATH), "ANTHROPIC_API_KEY", key)
        os.environ["ANTHROPIC_API_KEY"] = key
    model = req.model.strip() or os.getenv("CLAUDE_MODEL", "deepseek-v4-pro")
    set_key(str(ENV_PATH), "CLAUDE_MODEL", model)
    os.environ["CLAUDE_MODEL"] = model
    return {"ok": True}


@app.get("/api/setting")
def api_get_setting():
    key = os.getenv("ANTHROPIC_API_KEY", "")
    model = os.getenv("CLAUDE_MODEL", "deepseek-v4-pro")
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
