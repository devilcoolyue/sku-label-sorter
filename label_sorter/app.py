"""面单排序合并 Web 服务。

运行:
  uvicorn app:app --host 0.0.0.0 --port 8000
或:
  python app.py
"""

import json
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

import fitz
from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse

import auth as auth_mod
import sorter

BASE_DIR = Path(__file__).parent
WORK_DIR = Path(os.environ.get("SORTER_WORK_DIR", "/tmp/label_sorter_jobs"))
WORK_DIR.mkdir(parents=True, exist_ok=True)
HISTORY_FILE = WORK_DIR / "history.json"

app = FastAPI(title="面单SKU排序合并")

AUTH = auth_mod.Auth(WORK_DIR)
LOGIN_PAGE = BASE_DIR / "static" / "login.html"
INDEX_PAGE = BASE_DIR / "static" / "index.html"
# 只有登录接口是敞开的，其余一律要 Cookie
PUBLIC_PATHS = {"/api/login"}
# 明文 HTTP 下不能带 Secure，否则浏览器根本不存这个 Cookie；上了 HTTPS 记得开
COOKIE_SECURE = os.environ.get("SORTER_COOKIE_SECURE", "0") == "1"

JOBS = {}  # job_id -> dict
JOBS_LOCK = threading.Lock()
HISTORY_LOCK = threading.Lock()
MAX_LOG_LINES = 500
MAX_HISTORY = 50
JOB_ID_RE = re.compile(r"^[0-9a-f]{12}$")
OUTPUT_NAME = "sorted_merged.pdf"
PAGES_NAME = "pages.json"  # 逐页识别结果，复查比对的基线


def locate(files_meta, idx):
    """全局页序 -> (文件名, 文件内页码)。"""
    for f in files_meta:
        if idx < f["pages"]:
            return f["name"], idx + 1
        idx -= f["pages"]
    return "?", idx + 1


def diff_pages(base, cur, files_meta):
    """逐页比对两次识别结果，返回有出入的页。"""
    out = []
    for i in range(min(len(base), len(cur))):
        a, b = base[i] or [], cur[i] or []
        if a != b:
            name, page = locate(files_meta, i)
            out.append({"file": name, "page": page, "before": a, "after": b})
    return out


def load_history():
    if not HISTORY_FILE.exists():
        return []
    try:
        return json.loads(HISTORY_FILE.read_text("utf-8"))
    except (json.JSONDecodeError, OSError):
        return []  # 存档损坏不该拖垮服务


def append_history(entry):
    """写入一条记录，并按上限清掉最旧的任务目录。"""
    with HISTORY_LOCK:
        items = [x for x in load_history() if x.get("id") != entry["id"]]
        items.insert(0, entry)
        for old in items[MAX_HISTORY:]:
            shutil.rmtree(WORK_DIR / old["id"], ignore_errors=True)
        items = items[:MAX_HISTORY]
        tmp = HISTORY_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(items, ensure_ascii=False), "utf-8")
        tmp.replace(HISTORY_FILE)  # 原子替换，避免读到写一半的文件


def _run_job(job_id):
    job = JOBS[job_id]
    job["status"] = "running"
    job["started_at"] = time.time()
    lock = threading.Lock()  # progress 由多个识别线程并发回调

    def progress(done, total, info):
        idx = info["index"]
        with lock:
            if info.get("phase") == "start":
                job["page_status"][idx] = "running"
                return
            if info.get("phase") == "recheck":
                job["page_status"][idx] = "fixed" if info["changed"] else "ok"
                job["page_skus"][idx] = info["skus"]
                tail = (f"复查修正 {info['before']} → {info['skus'][0]}"
                        if info["changed"] else
                        f"复查确认 {info['before']}")
                job["log"].append(
                    f"{info['file']} 第{info['page']}页 [recheck] {tail}")
                return
            job["page_status"][idx] = (
                "error" if info.get("error") else
                "ok" if info["skus"] else "none"
            )
            job["page_skus"][idx] = info["skus"]
            job["done"] = done
            job["total"] = total
            if info.get("error"):
                body = info["error"].split("\n")[0]
            else:
                body = "、".join(info["skus"]) or "未识别"
            job["log"].append(f"{info['file']} 第{info['page']}页 "
                              f"[{info['source']}] {body}")
            if len(job["log"]) > MAX_LOG_LINES:
                del job["log"][: len(job["log"]) - MAX_LOG_LINES]

    try:
        stats = sorter.sort_and_merge(
            input_paths=job["inputs"],
            output_path=job["output"],
            prefixes=job["config"]["prefixes"],
            fallback=job["config"]["fallback"],
            model=job["config"]["model"],
            api_key=job["config"]["api_key"] or None,
            progress=progress,
            provider=job["config"]["provider"],
            base_url=job["config"]["base_url"] or None,
            threads=job["config"]["threads"],
            recheck=job["config"]["recheck"],
        )
        job["stats"] = stats
        job["status"] = "done"
    except Exception as e:  # noqa: BLE001 — 前端需要看到任意失败原因
        job["status"] = "error"
        job["error"] = f"{type(e).__name__}: {e}"
    finally:
        job["finished_at"] = time.time()
        cfg = job["config"]
        try:  # 逐页结果落盘，之后复查要拿它当基线
            (Path(job["output"]).parent / PAGES_NAME).write_text(
                json.dumps(job["page_skus"], ensure_ascii=False), "utf-8")
        except OSError:
            pass
        if job.get("baseline") is not None:
            job["diff"] = diff_pages(job["baseline"], job["page_skus"],
                                     job["files"])
        append_history({
            "id": job_id,
            "created_at": job["created_at"],
            "finished_at": job["finished_at"],
            "files": job["files"],
            "total": job["total"] or sum(f["pages"] for f in job["files"]),
            "status": job["status"],
            "error": job["error"],
            "stats": job["stats"],
            # 只留可复现的配置，api_key 不落盘
            "config": {k: cfg[k] for k in
                       ("prefixes", "fallback", "provider", "model",
                        "base_url", "threads", "recheck")},
            "has_output": os.path.exists(job["output"]),
            "verify_of": job.get("verify_of"),
            "diff": job.get("diff"),
        })


@app.post("/api/jobs")
async def create_job(
    files: list[UploadFile] = File(...),
    prefixes: str = Form(",".join(sorter.DEFAULT_PREFIXES)),
    fallback: str = Form("ocr"),
    provider: str = Form("anthropic"),
    model: str = Form("claude-opus-5"),
    api_key: str = Form(""),
    base_url: str = Form(""),
    threads: int = Form(sorter.DEFAULT_THREADS),
    recheck: str = Form("1"),
):
    if fallback not in ("none", "ocr", "llm", "ocr+llm"):
        raise HTTPException(400, "fallback 参数无效")
    if provider not in ("anthropic", "openai"):
        raise HTTPException(400, "provider 参数无效")
    if not 1 <= threads <= sorter.MAX_THREADS:
        raise HTTPException(400, f"线程数需在 1~{sorter.MAX_THREADS} 之间")
    if "llm" in fallback and not model.strip():
        raise HTTPException(400, "使用大模型时必须填写模型名")
    prefix_list = [p.strip().upper() for p in prefixes.split(",") if p.strip()]
    if not prefix_list:
        raise HTTPException(400, "SKU 前缀不能为空")

    job_id = uuid.uuid4().hex[:12]
    job_dir = WORK_DIR / job_id
    job_dir.mkdir()

    inputs = []
    for f in files:
        if not f.filename.lower().endswith(".pdf"):
            shutil.rmtree(job_dir, ignore_errors=True)
            raise HTTPException(400, f"仅支持PDF文件: {f.filename}")
        # 只保留文件名部分，防止路径穿越
        name = os.path.basename(f.filename)
        dest = job_dir / name
        with dest.open("wb") as w:
            shutil.copyfileobj(f.file, w)
        inputs.append(str(dest))
    if not inputs:
        raise HTTPException(400, "未上传任何文件")

    # 先点清页数，前端才能立刻把格子铺出来
    files_meta = []
    for path in inputs:
        try:
            d = fitz.open(path)
            files_meta.append({"name": os.path.basename(path), "pages": len(d)})
            d.close()
        except Exception as e:  # noqa: BLE001
            shutil.rmtree(job_dir, ignore_errors=True)
            raise HTTPException(400, f"无法读取 {os.path.basename(path)}: {e}")
    total_pages = sum(f["pages"] for f in files_meta)

    job = {
        "id": job_id,
        "status": "pending",
        "inputs": inputs,
        "files": files_meta,
        "page_status": ["pending"] * total_pages,
        "page_skus": [None] * total_pages,
        "output": str(job_dir / OUTPUT_NAME),
        "config": {
            "prefixes": prefix_list,
            "fallback": fallback,
            "provider": provider,
            "model": model.strip(),
            "api_key": api_key,
            "base_url": base_url.strip(),
            "threads": threads,
            "recheck": recheck not in ("0", "false", ""),
        },
        "done": 0,
        "total": total_pages,
        "log": [],
        "stats": None,
        "error": None,
        "created_at": time.time(),
    }
    with JOBS_LOCK:
        JOBS[job_id] = job
    threading.Thread(target=_run_job, args=(job_id,), daemon=True).start()
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
async def job_status(job_id: str, log_from: int = 0):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    return JSONResponse({
        "id": job["id"],
        "status": job["status"],
        "done": job["done"],
        "total": job["total"],
        "files": job["files"],
        "page_status": job["page_status"],
        "log": job["log"][log_from:],
        "log_next": len(job["log"]),
        "stats": job["stats"],
        "error": job["error"],
        "verify_of": job.get("verify_of"),
        "diff": job.get("diff"),
    })


@app.get("/api/jobs/{job_id}/download")
async def job_download(job_id: str):
    # 不查内存字典 —— 服务重启后历史任务的 PDF 仍应可下载
    if not JOB_ID_RE.match(job_id):
        raise HTTPException(404, "任务不存在")
    path = WORK_DIR / job_id / OUTPUT_NAME
    if not path.exists():
        raise HTTPException(404, "文件已不存在（可能已被历史记录上限清理）")
    return FileResponse(path, media_type="application/pdf",
                        filename=OUTPUT_NAME)


@app.post("/api/jobs/{job_id}/verify")
async def verify_job(job_id: str, api_key: str = Form("")):
    """用原任务的配置把同一批文件重跑一遍，逐页比对两次结果。

    api_key 不落盘，所以由前端重新带上；其余配置从历史记录里取。
    """
    if not JOB_ID_RE.match(job_id):
        raise HTTPException(404, "任务不存在")
    src_dir = WORK_DIR / job_id
    pages_file = src_dir / PAGES_NAME
    if not pages_file.exists():
        raise HTTPException(404, "原任务的逐页结果已不存在，无法比对")
    hist = next((x for x in load_history() if x["id"] == job_id), None)
    if not hist:
        raise HTTPException(404, "原任务记录已不存在")

    baseline = json.loads(pages_file.read_text("utf-8"))
    cfg = dict(hist["config"])
    inputs = []
    for f in hist["files"]:
        p = src_dir / f["name"]
        if not p.exists():
            raise HTTPException(404, f"原文件已不存在: {f['name']}")
        inputs.append(str(p))

    new_id = uuid.uuid4().hex[:12]
    new_dir = WORK_DIR / new_id
    new_dir.mkdir()
    job = {
        "id": new_id,
        "status": "pending",
        "inputs": inputs,          # 直接复用原目录里的 PDF，不重复占空间
        "files": hist["files"],
        "page_status": ["pending"] * hist["total"],
        "page_skus": [None] * hist["total"],
        "baseline": baseline,
        "verify_of": job_id,
        "output": str(new_dir / OUTPUT_NAME),
        "config": {**cfg, "api_key": api_key},
        "done": 0,
        "total": hist["total"],
        "log": [],
        "stats": None,
        "error": None,
        "diff": None,
        "created_at": time.time(),
    }
    with JOBS_LOCK:
        JOBS[new_id] = job
    threading.Thread(target=_run_job, args=(new_id,), daemon=True).start()
    return {"job_id": new_id, "baseline_of": job_id}


@app.delete("/api/history/{job_id}")
async def delete_history(job_id: str):
    if not JOB_ID_RE.match(job_id):
        raise HTTPException(404, "任务不存在")
    with HISTORY_LOCK:
        items = load_history()
        kept = [x for x in items if x.get("id") != job_id]
        if len(kept) == len(items):
            raise HTTPException(404, "记录不存在")
        tmp = HISTORY_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(kept, ensure_ascii=False), "utf-8")
        tmp.replace(HISTORY_FILE)
    shutil.rmtree(WORK_DIR / job_id, ignore_errors=True)
    with JOBS_LOCK:
        JOBS.pop(job_id, None)
    return {"ok": True}


@app.get("/api/history")
async def history():
    items = load_history()
    for it in items:  # 目录可能已被清理，前端据此禁用下载按钮
        it["has_output"] = (WORK_DIR / it["id"] / OUTPUT_NAME).exists()
    return {"items": items}


@app.middleware("http")
async def require_login(request: Request, call_next):
    """全局闸门：没有有效 Cookie 时，接口返回 401，页面返回登录页。

    页面必须在服务端拦掉 —— 只靠前端 JS 跳转的话 HTML 早就发出去了。
    """
    if request.url.path in PUBLIC_PATHS or AUTH.verify_token(
            request.cookies.get(auth_mod.COOKIE)):
        return await call_next(request)
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "未登录或登录已过期"}, status_code=401)
    return FileResponse(LOGIN_PAGE)


@app.post("/api/login")
async def login(request: Request):
    ip = request.client.host if request.client else "?"
    left = AUTH.locked_for(ip)
    if left:
        raise HTTPException(429, f"失败次数过多，请 {left // 60 + 1} 分钟后再试")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "请求格式错误")
    if not AUTH.check(ip, body.get("username", ""), body.get("password", "")):
        raise HTTPException(401, "用户名或密码错误")

    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        auth_mod.COOKIE, AUTH.make_token(auth_mod.USER),
        max_age=int(auth_mod.SESSION_HOURS * 3600),
        httponly=True, samesite="strict", secure=COOKIE_SECURE, path="/",
    )
    return resp


@app.post("/api/logout")
async def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth_mod.COOKIE, path="/")
    return resp


@app.get("/{full_path:path}")
async def page(full_path: str):
    """走到这里说明已登录（未登录的在中间件就被换成登录页了）。"""
    if full_path.startswith("api/"):  # 没匹配上的接口不该回 HTML
        raise HTTPException(404, "接口不存在")
    return FileResponse(INDEX_PAGE)


if __name__ == "__main__":
    import uvicorn
    if AUTH.initial_password:
        print("=" * 60, flush=True)
        print("  未设置 SORTER_PASSWORD，已生成随机口令：", flush=True)
        print(f"  用户名 {auth_mod.USER}   密码 {AUTH.initial_password}",
              flush=True)
        print(f"  （已存于 {WORK_DIR / '.password'}）", flush=True)
        print("=" * 60, flush=True)
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
