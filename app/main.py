import os
import time
import asyncio
import requests
import uuid
from urllib.parse import quote
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple, List, Dict, Any

from fastapi import FastAPI, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from app.leonardo_service import ASPECT_TO_SIZE, LeonardoPoolService
from app.store import Store, hash_password
from app.telegram_bot import TelegramBotRunner


BASE_DIR = Path(__file__).resolve().parent.parent
store = Store(db_path=str(BASE_DIR / "data" / "app.db"))
store.bootstrap_defaults(
    model_file=str(BASE_DIR / "model_id.txt"),
)
service = LeonardoPoolService(store=store)
telegram_bot = TelegramBotRunner(store=store, service=service)

app = FastAPI(title="LeoAPI OpenAI Compatible", version="1.0.0")
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("ADMIN_SESSION_SECRET", "change-this-session-secret"),
)

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "app" / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))

STUDIO_BATCH_JOBS: Dict[str, Dict[str, Any]] = {}
STUDIO_BATCH_JOBS_LOCK = asyncio.Lock()
STUDIO_BATCH_MAX_JOBS = 200


class OpenAIImageRequest(BaseModel):
    prompt: str
    model: Optional[str] = None
    n: int = Field(default=1, ge=1, le=4)
    size: Optional[str] = None
    aspect_ratio: Optional[str] = None
    image_url: Optional[str] = None
    image_urls: Optional[list[str]] = None


class StudioBatchRequest(BaseModel):
    prompts: List[str] = Field(default_factory=list)
    model: Optional[str] = None
    n: int = Field(default=1, ge=1, le=4)
    size: Optional[str] = None
    aspect_ratio: Optional[str] = None
    image_url: Optional[str] = None
    image_urls: Optional[list[str]] = None
    concurrency: int = Field(default=3, ge=1, le=8)


def is_logged_in(request: Request) -> bool:
    return bool(request.session.get("admin_logged_in"))


def is_studio_logged_in(request: Request) -> bool:
    return bool(request.session.get("studio_logged_in"))


def redirect_to_login() -> RedirectResponse:
    return RedirectResponse(url="/admin/login", status_code=302)


def redirect_to_studio_login() -> RedirectResponse:
    return RedirectResponse(url="/studio/login", status_code=302)


def refresh_cookie_profiles_if_stale(max_age_seconds: int = 120) -> None:
    now = int(time.time())
    active = [c for c in store.list_cookies() if int(c.get("is_active", 0) or 0) == 1]
    if not active:
        return

    stale = any((now - int(c.get("last_checked_at", 0) or 0)) > max_age_seconds for c in active)
    if not stale:
        return

    try:
        service.refresh_cookie_profiles()
    except Exception:
        # Keep dashboard responsive even if provider check fails.
        return


def format_ts(ts: int) -> str:
    if not ts:
        return "-"
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "-"


def _is_studio_enabled() -> bool:
    return store.get_setting("studio_enabled", "0") == "1"


def _studio_username() -> str:
    return (store.get_setting("studio_auth_username", "studio") or "studio").strip() or "studio"


def _studio_password_hash() -> str:
    return (store.get_setting("studio_auth_password_hash", hash_password("studio123")) or "").strip()


def _verify_studio_login(username: str, password: str) -> bool:
    user_ok = (username or "").strip() == _studio_username()
    pass_ok = hash_password(password or "") == _studio_password_hash()
    return user_ok and pass_ok


def _resolve_generation_request(payload: OpenAIImageRequest) -> Tuple[str, str, List[str]]:
    model_id = payload.model or store.get_default_model_id()
    if not model_id:
        raise ValueError("No model configured")

    aspect_ratio = payload.aspect_ratio or store.get_setting("default_aspect_ratio", "1:1")

    if payload.size:
        size_map = {
            "1344x768": "16:9",
            "768x1344": "9:16",
            "2752x1536": "16:9",
            "1536x2752": "9:16",
            "1024x1024": "1:1",
            "1536x1536": "1:1",
            "1152x896": "4:3",
            "2048x1536": "4:3",
        }
        aspect_ratio = size_map.get(payload.size, aspect_ratio)

    if aspect_ratio not in ASPECT_TO_SIZE:
        raise ValueError("aspect_ratio must be one of 16:9, 9:16, 1:1, 4:3")

    reference_image_urls: list[str] = []
    if payload.image_url:
        reference_image_urls.append(payload.image_url)
    if payload.image_urls:
        reference_image_urls.extend([u for u in payload.image_urls if u])

    return model_id, aspect_ratio, reference_image_urls


async def _studio_prune_jobs() -> None:
    if len(STUDIO_BATCH_JOBS) <= STUDIO_BATCH_MAX_JOBS:
        return
    # Remove oldest jobs first when over capacity.
    sorted_ids = sorted(STUDIO_BATCH_JOBS.keys(), key=lambda k: int(STUDIO_BATCH_JOBS[k].get("created", 0)))
    overflow = len(sorted_ids) - STUDIO_BATCH_MAX_JOBS
    for job_id in sorted_ids[:overflow]:
        STUDIO_BATCH_JOBS.pop(job_id, None)


async def _studio_run_batch_job(job_id: str, payload: StudioBatchRequest, prompts: List[str]) -> None:
    sem = asyncio.Semaphore(max(1, min(8, int(payload.concurrency))))
    proxy_base = (store.get_setting("studio_proxy_base_url", "") or "").strip().rstrip("/")

    async with STUDIO_BATCH_JOBS_LOCK:
        job = STUDIO_BATCH_JOBS.get(job_id)
        if not job:
            return
        job["status"] = "running"
        job["started"] = int(time.time())

    async def update_result(i: int, data: Dict[str, Any]) -> None:
        async with STUDIO_BATCH_JOBS_LOCK:
            job = STUDIO_BATCH_JOBS.get(job_id)
            if not job:
                return
            job_results = job.get("results", [])
            if 0 <= i < len(job_results):
                job_results[i] = data

            completed = sum(1 for item in job_results if item.get("status") in {"done", "failed"})
            success = sum(1 for item in job_results if item.get("ok") is True)
            failed = sum(1 for item in job_results if item.get("ok") is False)
            count = max(1, int(job.get("count", 1)))

            job["completed"] = completed
            job["success"] = success
            job["failed"] = failed
            job["progress"] = int((completed / count) * 100)

    async def run_one(i: int, prompt: str) -> None:
        req_payload = OpenAIImageRequest(
            prompt=prompt,
            model=payload.model,
            n=payload.n,
            size=payload.size,
            aspect_ratio=payload.aspect_ratio,
            image_url=payload.image_url,
            image_urls=payload.image_urls,
        )

        await update_result(i, {"prompt": prompt, "status": "running", "ok": None, "images": []})

        async with sem:
            try:
                if proxy_base:
                    def _forward() -> requests.Response:
                        return requests.post(
                            f"{proxy_base}/v1/images/generations",
                            json=req_payload.model_dump(),
                            timeout=600,
                        )

                    response = await run_in_threadpool(_forward)
                    data = response.json() if response.ok else {"detail": response.text[:400]}
                    if response.status_code >= 400:
                        raise Exception((data.get("error") or {}).get("message") or data.get("detail") or "Proxy generation failed")
                    urls = [item.get("url") for item in data.get("data", []) if item.get("url")]
                    await update_result(
                        i,
                        {
                            "prompt": prompt,
                            "status": "done",
                            "ok": True,
                            "images": urls,
                            "provider": data.get("provider", {}),
                        },
                    )
                    return

                model_id, aspect_ratio, reference_image_urls = _resolve_generation_request(req_payload)
                out = await run_in_threadpool(
                    service.generate_images,
                    req_payload.prompt,
                    req_payload.n,
                    model_id,
                    aspect_ratio,
                    reference_image_urls,
                )
                urls = [item.get("url") for item in out.get("data", []) if item.get("url")]
                await update_result(
                    i,
                    {
                        "prompt": prompt,
                        "status": "done",
                        "ok": True,
                        "images": urls,
                        "provider": out.get("provider", {}),
                    },
                )
            except Exception as exc:
                await update_result(
                    i,
                    {
                        "prompt": prompt,
                        "status": "failed",
                        "ok": False,
                        "images": [],
                        "error": str(exc),
                    },
                )

    await asyncio.gather(*(run_one(i, p) for i, p in enumerate(prompts)))

    async with STUDIO_BATCH_JOBS_LOCK:
        job = STUDIO_BATCH_JOBS.get(job_id)
        if not job:
            return
        job["status"] = "completed"
        job["progress"] = 100
        job["finished"] = int(time.time())


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/v1/images/generations")
async def openai_compatible_images(payload: OpenAIImageRequest) -> JSONResponse:
    try:
        model_id, aspect_ratio, reference_image_urls = _resolve_generation_request(payload)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": {"message": str(exc)}})

    result = await run_in_threadpool(
        service.generate_images,
        payload.prompt,
        payload.n,
        model_id,
        aspect_ratio,
        reference_image_urls,
    )
    return JSONResponse(content=result)


@app.get("/studio", response_class=HTMLResponse)
def studio_landing(request: Request):
    if not _is_studio_enabled():
        return RedirectResponse(url="/admin/settings", status_code=302)
    if not is_studio_logged_in(request):
        return redirect_to_studio_login()
    return RedirectResponse(url="/studio/dashboard", status_code=302)


@app.get("/studio/dashboard", response_class=HTMLResponse)
def studio_dashboard(request: Request):
    if not _is_studio_enabled():
        return RedirectResponse(url="/admin/settings", status_code=302)
    if not is_studio_logged_in(request):
        return redirect_to_studio_login()

    models = store.list_models()
    default_model_id = store.get_default_model_id() or ""
    return templates.TemplateResponse(
        request,
        "studio_dashboard.html",
        {
            "models": models,
            "default_model_id": default_model_id,
            "default_aspect_ratio": store.get_setting("default_aspect_ratio", "1:1"),
            "studio_proxy_base_url": store.get_setting("studio_proxy_base_url", ""),
            "studio_proxy_enabled": bool((store.get_setting("studio_proxy_base_url", "") or "").strip()),
            "studio_batch_api_path": "/studio/api/images/batch",
            "studio_username": _studio_username(),
        },
    )


@app.get("/studio/login", response_class=HTMLResponse)
def studio_login_page(request: Request):
    if not _is_studio_enabled():
        return RedirectResponse(url="/admin/settings", status_code=302)
    if is_studio_logged_in(request):
        return RedirectResponse(url="/studio/dashboard", status_code=302)
    return templates.TemplateResponse(request, "studio_login.html", {"error": ""})


@app.post("/studio/login", response_class=HTMLResponse)
def studio_login(request: Request, username: str = Form(...), password: str = Form(...)):
    if not _is_studio_enabled():
        return RedirectResponse(url="/admin/settings", status_code=302)
    if _verify_studio_login(username, password):
        request.session["studio_logged_in"] = True
        return RedirectResponse(url="/studio/dashboard", status_code=302)
    return templates.TemplateResponse(
        request,
        "studio_login.html",
        {"error": "Invalid Studio credentials"},
        status_code=401,
    )


@app.post("/studio/logout")
def studio_logout(request: Request):
    request.session.pop("studio_logged_in", None)
    return RedirectResponse(url="/studio/login", status_code=302)


@app.post("/studio/api/images/generations")
async def studio_generate_images(request: Request, payload: OpenAIImageRequest) -> JSONResponse:
    if not _is_studio_enabled():
        return JSONResponse(status_code=403, content={"error": {"message": "Studio is disabled"}})
    if not is_studio_logged_in(request):
        return JSONResponse(status_code=401, content={"error": {"message": "Studio login required"}})

    proxy_base = (store.get_setting("studio_proxy_base_url", "") or "").strip().rstrip("/")
    if proxy_base:
        try:
            def _forward() -> requests.Response:
                return requests.post(
                    f"{proxy_base}/v1/images/generations",
                    json=payload.model_dump(),
                    timeout=600,
                )

            response = await run_in_threadpool(
                _forward,
            )
        except Exception as exc:
            return JSONResponse(status_code=502, content={"error": {"message": f"Proxy request failed: {exc}"}})

        try:
            body = response.json()
        except Exception:
            body = {"error": {"message": response.text[:400]}}
        return JSONResponse(status_code=response.status_code, content=body)

    try:
        model_id, aspect_ratio, reference_image_urls = _resolve_generation_request(payload)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": {"message": str(exc)}})

    result = await run_in_threadpool(
        service.generate_images,
        payload.prompt,
        payload.n,
        model_id,
        aspect_ratio,
        reference_image_urls,
    )
    return JSONResponse(content=result)


@app.post("/studio/api/images/batch")
async def studio_generate_batch(request: Request, payload: StudioBatchRequest) -> JSONResponse:
    if not _is_studio_enabled():
        return JSONResponse(status_code=403, content={"error": {"message": "Studio is disabled"}})
    if not is_studio_logged_in(request):
        return JSONResponse(status_code=401, content={"error": {"message": "Studio login required"}})

    prompts = [p.strip() for p in payload.prompts if (p or "").strip()]
    if not prompts:
        return JSONResponse(status_code=400, content={"error": {"message": "No prompts provided"}})

    job_id = uuid.uuid4().hex
    now = int(time.time())
    initial_results = [
        {"prompt": prompt, "status": "queued", "ok": None, "images": []}
        for prompt in prompts
    ]

    async with STUDIO_BATCH_JOBS_LOCK:
        await _studio_prune_jobs()
        STUDIO_BATCH_JOBS[job_id] = {
            "job_id": job_id,
            "status": "queued",
            "created": now,
            "count": len(prompts),
            "completed": 0,
            "success": 0,
            "failed": 0,
            "progress": 0,
            "results": initial_results,
        }

    asyncio.create_task(_studio_run_batch_job(job_id, payload, prompts))
    return JSONResponse(
        content={
            "job_id": job_id,
            "status": "queued",
            "created": now,
            "count": len(prompts),
            "poll_url": f"/studio/api/images/batch/{job_id}",
        }
    )


@app.get("/studio/api/images/batch/{job_id}")
async def studio_batch_progress(request: Request, job_id: str) -> JSONResponse:
    if not _is_studio_enabled():
        return JSONResponse(status_code=403, content={"error": {"message": "Studio is disabled"}})
    if not is_studio_logged_in(request):
        return JSONResponse(status_code=401, content={"error": {"message": "Studio login required"}})

    async with STUDIO_BATCH_JOBS_LOCK:
        job = STUDIO_BATCH_JOBS.get(job_id)
        if not job:
            return JSONResponse(status_code=404, content={"error": {"message": "Job not found"}})
        payload = dict(job)

    return JSONResponse(content=payload)


@app.on_event("startup")
def startup_event() -> None:
    telegram_bot.refresh_from_settings()


@app.on_event("shutdown")
def shutdown_event() -> None:
    telegram_bot.stop()


@app.get("/admin", response_class=HTMLResponse)
def admin_home(request: Request):
    if not is_logged_in(request):
        return redirect_to_login()
    return RedirectResponse(url="/admin/dashboard", status_code=302)


@app.get("/admin/login", response_class=HTMLResponse)
def admin_login_page(request: Request):
    if is_logged_in(request):
        return RedirectResponse(url="/admin/dashboard", status_code=302)
    return templates.TemplateResponse(request, "login.html", {"error": ""})


@app.post("/admin/login", response_class=HTMLResponse)
def admin_login(request: Request, username: str = Form(...), password: str = Form(...)):
    if store.verify_admin(username, password):
        request.session["admin_logged_in"] = True
        return RedirectResponse(url="/admin/dashboard", status_code=302)
    return templates.TemplateResponse(
        request,
        "login.html",
        {"error": "Username/password salah"},
        status_code=401,
    )


@app.post("/admin/logout")
def admin_logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/admin/login", status_code=302)


@app.get("/admin/dashboard", response_class=HTMLResponse)
def admin_dashboard(request: Request):
    if not is_logged_in(request):
        return redirect_to_login()

    refresh_cookie_profiles_if_stale(max_age_seconds=120)

    stats = store.stats()
    cookies = store.list_cookies()
    active_total = 0
    active_nonzero = 0
    active_zero = 0
    total_balance_active = 0
    total_nonzero = 0
    snapshot = []

    for c in cookies:
        balance = int(c.get("last_balance", 0) or 0)
        is_active = int(c.get("is_active", 0) or 0) == 1
        last_checked = int(c.get("last_checked_at", 0) or 0)
        status = "READY" if is_active and balance > 0 else ("DEPLETED" if is_active else "DISABLED")

        snapshot.append(
            {
                "id": int(c.get("id", 0) or 0),
                "balance": balance,
                "status": status,
                "last_checked_human": format_ts(last_checked),
            }
        )

        if is_active:
            active_total += 1
            total_balance_active += max(0, balance)

        if balance > 0:
            total_nonzero += 1
            if is_active:
                active_nonzero += 1
        else:
            if is_active:
                active_zero += 1

    health = {
        "active_total": active_total,
        "ready_accounts": active_nonzero,
        "depleted_accounts": active_zero,
        "active_total_balance": total_balance_active,
        "active_nonzero": active_nonzero,
        "active_zero": active_zero,
        "total_nonzero": total_nonzero,
        "total_accounts": len(cookies),
        "last_sync": max((int(c.get("last_checked_at", 0) or 0) for c in cookies), default=0),
        "last_sync_human": format_ts(max((int(c.get("last_checked_at", 0) or 0) for c in cookies), default=0)),
    }
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "stats": stats,
            "cookie_health": health,
            "cookie_snapshot": snapshot[:8],
            "default_aspect_ratio": store.get_setting("default_aspect_ratio", "1:1"),
        },
    )


@app.get("/admin/cookies", response_class=HTMLResponse)
def admin_cookies(request: Request):
    if not is_logged_in(request):
        return redirect_to_login()

    status = request.query_params.get("status", "")
    message = request.query_params.get("message", "")

    cookies = []
    for c in store.list_cookies():
        cookies.append(
            {
                "id": c.get("id", 0),
                "value": c.get("value", ""),
                "is_active": int(c.get("is_active", 0) or 0),
                "last_error": c.get("last_error", "") or "",
                "last_used_at": int(c.get("last_used_at", 0) or 0),
                "last_balance": int(c.get("last_balance", 0) or 0),
                "last_checked_at": int(c.get("last_checked_at", 0) or 0),
            }
        )

    return templates.TemplateResponse(
        request,
        "cookies.html",
        {
            "cookies": cookies,
            "status": status,
            "message": message,
        },
    )


@app.post("/admin/cookies/add")
def admin_add_cookie(request: Request, cookie_value: str = Form(...)):
    if not is_logged_in(request):
        return redirect_to_login()
    try:
        result = service.add_cookie_validated(cookie_value)
        msg = quote(f"Auth valid ditambahkan. Balance: {result['balance']}")
        return RedirectResponse(
            url=f"/admin/cookies?status=ok&message={msg}",
            status_code=302,
        )
    except Exception as exc:
        safe = quote(str(exc))
        return RedirectResponse(url=f"/admin/cookies?status=error&message={safe}", status_code=302)


@app.post("/admin/cookies/{cookie_id}/delete")
def admin_delete_cookie(request: Request, cookie_id: int):
    if not is_logged_in(request):
        return redirect_to_login()
    store.delete_cookie(cookie_id)
    return RedirectResponse(url="/admin/cookies", status_code=302)


@app.post("/admin/cookies/{cookie_id}/toggle")
def admin_toggle_cookie(request: Request, cookie_id: int, enabled: str = Form(...)):
    if not is_logged_in(request):
        return redirect_to_login()
    store.toggle_cookie(cookie_id, enabled == "1")
    return RedirectResponse(url="/admin/cookies", status_code=302)


@app.post("/admin/cookies/refresh")
def admin_refresh_cookies(request: Request):
    if not is_logged_in(request):
        return redirect_to_login()
    result = service.refresh_cookie_profiles()
    msg = quote(f"Refresh balance selesai: {result['ok']}/{result['checked']} berhasil")
    return RedirectResponse(url=f"/admin/cookies?status=ok&message={msg}", status_code=302)


@app.post("/admin/cookies/refresh-session")
def admin_refresh_cookie_sessions(request: Request):
    if not is_logged_in(request):
        return redirect_to_login()
    result = service.refresh_cookie_sessions()
    msg = quote(f"Refresh session selesai: {result['ok']}/{result['checked']} berhasil")
    return RedirectResponse(url=f"/admin/cookies?status=ok&message={msg}", status_code=302)


@app.get("/admin/models", response_class=HTMLResponse)
def admin_models(request: Request):
    if not is_logged_in(request):
        return redirect_to_login()
    return templates.TemplateResponse(
        request,
        "models.html",
        {
            "models": store.list_models(),
        },
    )


@app.post("/admin/models/add")
def admin_add_model(request: Request, name: str = Form(""), model_id: str = Form(...)):
    if not is_logged_in(request):
        return redirect_to_login()
    store.add_model(name=name, model_id=model_id)
    return RedirectResponse(url="/admin/models", status_code=302)


@app.post("/admin/models/{model_db_id}/delete")
def admin_delete_model(request: Request, model_db_id: int):
    if not is_logged_in(request):
        return redirect_to_login()
    store.delete_model(model_db_id)
    return RedirectResponse(url="/admin/models", status_code=302)


@app.post("/admin/models/{model_db_id}/default")
def admin_set_default_model(request: Request, model_db_id: int):
    if not is_logged_in(request):
        return redirect_to_login()
    store.set_default_model(model_db_id)
    return RedirectResponse(url="/admin/models", status_code=302)


@app.get("/admin/settings", response_class=HTMLResponse)
def admin_settings(request: Request):
    if not is_logged_in(request):
        return redirect_to_login()
    status = request.query_params.get("status", "")
    message = request.query_params.get("message", "")
    bot_status = telegram_bot.status()
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "current_aspect_ratio": store.get_setting("default_aspect_ratio", "1:1"),
            "auto_save_images": store.get_setting("auto_save_images", "0") == "1",
            "save_images_dir": store.get_setting("save_images_dir", "data/generated"),
            "telegram_enabled": store.get_setting("telegram_enabled", "0") == "1",
            "telegram_bot_token": store.get_setting("telegram_bot_token", ""),
            "telegram_allowed_chat_ids": store.get_setting("telegram_allowed_chat_ids", ""),
            "telegram_running": bot_status.get("running", False),
            "studio_enabled": _is_studio_enabled(),
            "studio_proxy_base_url": store.get_setting("studio_proxy_base_url", ""),
            "studio_auth_username": _studio_username(),
            "status": status,
            "message": message,
            "supported": ["16:9", "9:16", "1:1", "4:3"],
        },
    )


@app.post("/admin/settings/aspect-ratio")
def admin_set_aspect_ratio(request: Request, aspect_ratio: str = Form(...)):
    if not is_logged_in(request):
        return redirect_to_login()
    if aspect_ratio in ASPECT_TO_SIZE:
        store.set_setting("default_aspect_ratio", aspect_ratio)
    return RedirectResponse(url="/admin/settings", status_code=302)


@app.post("/admin/settings/auto-save-images")
def admin_set_auto_save_images(
    request: Request,
    auto_save_images: Optional[str] = Form(None),
    save_images_dir: str = Form("data/generated"),
):
    if not is_logged_in(request):
        return redirect_to_login()

    enabled = auto_save_images == "1"
    save_dir = (save_images_dir or "data/generated").strip() or "data/generated"
    store.set_setting("auto_save_images", "1" if enabled else "0")
    store.set_setting("save_images_dir", save_dir)
    return RedirectResponse(url="/admin/settings", status_code=302)


@app.post("/admin/settings/telegram")
def admin_set_telegram_settings(
    request: Request,
    telegram_enabled: Optional[str] = Form(None),
    telegram_bot_token: str = Form(""),
    telegram_allowed_chat_ids: str = Form(""),
):
    if not is_logged_in(request):
        return redirect_to_login()

    store.set_setting("telegram_enabled", "1" if telegram_enabled == "1" else "0")
    store.set_setting("telegram_bot_token", (telegram_bot_token or "").strip())
    store.set_setting("telegram_allowed_chat_ids", (telegram_allowed_chat_ids or "").strip())
    telegram_bot.refresh_from_settings()
    return RedirectResponse(url="/admin/settings", status_code=302)


@app.post("/admin/settings/studio")
def admin_set_studio_settings(
    request: Request,
    studio_enabled: Optional[str] = Form(None),
    studio_proxy_base_url: str = Form(""),
    studio_auth_username: str = Form("studio"),
    studio_auth_password: str = Form(""),
):
    if not is_logged_in(request):
        return redirect_to_login()

    store.set_setting("studio_enabled", "1" if studio_enabled == "1" else "0")
    store.set_setting("studio_proxy_base_url", (studio_proxy_base_url or "").strip())
    username = (studio_auth_username or "studio").strip() or "studio"
    store.set_setting("studio_auth_username", username)
    if (studio_auth_password or "").strip():
        store.set_setting("studio_auth_password_hash", hash_password(studio_auth_password.strip()))
    msg = quote("Studio settings updated")
    return RedirectResponse(url=f"/admin/settings?status=ok&message={msg}", status_code=302)


@app.post("/admin/settings/telegram/test")
def admin_test_telegram_settings(
    request: Request,
    test_chat_id: str = Form(""),
):
    if not is_logged_in(request):
        return redirect_to_login()

    parsed_chat_id: Optional[int] = None
    raw_chat_id = (test_chat_id or "").strip()
    if raw_chat_id:
        try:
            parsed_chat_id = int(raw_chat_id)
        except Exception:
            msg = quote("Chat ID test tidak valid. Gunakan angka, contoh: 123456789 atau -1001234567890")
            return RedirectResponse(url=f"/admin/settings?status=error&message={msg}", status_code=302)

    try:
        result = telegram_bot.test_connection(parsed_chat_id)
        username = result.get("username", "")
        if result.get("chat_id_tested") is not None:
            msg_text = f"Koneksi bot OK (@{username}). Pesan test terkirim ke chat ID {result['chat_id_tested']}"
        else:
            msg_text = f"Koneksi bot OK (@{username}). Token valid."
        msg = quote(msg_text)
        return RedirectResponse(url=f"/admin/settings?status=ok&message={msg}", status_code=302)
    except Exception as exc:
        msg = quote(f"Test Telegram gagal: {exc}")
        return RedirectResponse(url=f"/admin/settings?status=error&message={msg}", status_code=302)
