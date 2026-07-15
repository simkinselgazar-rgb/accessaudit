"""FastAPI web application for WCAG Trusted Tester."""
from __future__ import annotations

import asyncio
import json
import sys

# Windows needs ProactorEventLoop for subprocess support (Playwright)
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from config import (
    AI_API_BASE_URL,
    AI_BACKEND,
    AI_JUDGE_API_KEY,
    AI_JUDGE_API_URL,
    AI_JUDGE_MODEL,
    AI_MAX_RETRIES,
    AI_MAX_TOKENS,
    AI_MODEL,
    AI_TIMEOUT,
    AI_VISION_API_URL,
    AI_VISION_MODEL,
    COMPANY_LOGO_PATH,
    COMPANY_NAME,
    COVERAGE_LEVEL,
    PROJECT_DIR,
    REPORT_FORMAT,
    REVIEWS_DIR,
    STATIC_DIR,
    TEMPLATES_DIR,
    WCAG_VERSION,
    WEB_HOST,
    WEB_PORT,
)
from models import CaptureData, ConformanceLevel, ReviewMeta, ReviewStatus
from functions.security import (
    InvalidReviewIdError,
    UnsafeURLError,
    UploadTooLargeError,
    save_user_upload,
    validate_public_url,
    validate_review_id,
)
from storage.review_store import (
    create_review,
    delete_all_reviews,
    delete_review,
    list_reviews,
    load_all_test_results,
    load_meta,
    load_test_result,
    load_user_decisions,
    save_meta,
    save_user_decision,
)

logging.basicConfig(
    level=logging.INFO,
    # name = dotted module path (e.g. capture.v2.orchestrator) — shows the
    # logger hierarchy. filename:lineno = the exact source location of the
    # log call — click-to-jump in IDEs / terminals. Both are useful: name
    # is stable across moves; file:line points at the literal emit site.
    format="%(asctime)s [%(levelname)s] %(name)s (%(filename)s:%(lineno)d): %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(
            Path(__file__).resolve().parent.parent / "wcag_tester.log",
            encoding="utf-8",
        ),
    ],
)
logger = logging.getLogger(__name__)
# Reduce noise from third-party libraries
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

app = FastAPI(title="WCAG Trusted Tester", version="6.0.0")


from app import queue as _queue_module
from app.cancellation import (
    _cancelled_reviews,
    _cancelled_reviews_lock,
)
from app.queue import _review_queue, queue_worker
from app.websocket_manager import (
    _active_websockets,
    _active_websockets_lock,
    broadcast,
)


_REVIEW_ID_PATH_PREFIXES = (
    "/review/",
    "/api/review/",
    "/ws/",
)


@app.middleware("http")
async def _validate_review_id_middleware(request: Request, call_next):
    """Reject any request whose path embeds a malformed review_id.

    All review_id path params route through here so traversal attempts
    ('/review/..%2Fother/progress', '/api/review/../x/status') are
    blocked before any handler sees them.
    """
    path = request.url.path
    for prefix in _REVIEW_ID_PATH_PREFIXES:
        if path.startswith(prefix):
            rest = path[len(prefix):]
            candidate = rest.split("/", 1)[0]
            if candidate:
                try:
                    validate_review_id(candidate)
                except InvalidReviewIdError as exc:
                    return JSONResponse(
                        {"error": f"Invalid review id: {exc}"},
                        status_code=400,
                    )
            break
    return await call_next(request)


# Mount static files
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Templates
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# ─── Routes ──────────────────────────────────────────────────────────

_shutting_down = False


@app.on_event("startup")
async def startup():
    _queue_module._queue_worker_task = asyncio.create_task(queue_worker())
    logger.info("Queue worker started")
    # Probe every AI I/O service up front so a dead/misconfigured endpoint
    # (the embeddings-fleet class of failure) is visible before any review.
    try:
        from functions.preflight import log_preflight
        await log_preflight()
    except Exception:
        logger.warning("Startup AI preflight failed to run", exc_info=True)


@app.on_event("shutdown")
async def shutdown():
    global _shutting_down
    _shutting_down = True
    logger.info("Shutdown requested — cleaning up...")

    # Cancel the queue worker
    task = _queue_module._queue_worker_task
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        logger.info("Queue worker stopped")

    # Mark any in-progress reviews as interrupted (resumable, not error)
    active_statuses = {"capturing", "testing", "generating_report", "crawling", "aggregating",
                       "selecting", "authenticating", "testing_documents"}
    try:
        reviews_dir = REVIEWS_DIR
        if reviews_dir.exists():
            for review_dir in reviews_dir.iterdir():
                if review_dir.is_dir():
                    try:
                        meta = load_meta(review_dir)
                        if meta.status in active_statuses:
                            meta.status = "interrupted"
                            meta.error = "Server shutdown during processing — can be resumed"
                            save_meta(review_dir, meta)
                            logger.info("Marked review %s as interrupted", meta.review_id)
                    except Exception:
                        logger.warning(
                            "Failed to mark review %s as interrupted on shutdown",
                            review_dir.name, exc_info=True,
                        )
    except Exception:
        logger.exception("Failed to scan reviews dir during shutdown")

    # Close all WebSocket connections
    for review_id, clients in _active_websockets.items():
        for ws in clients:
            try:
                await ws.close(code=1001, reason="Server shutting down")
            except Exception:
                logger.debug("WebSocket close on shutdown failed for review %s", review_id)
    _active_websockets.clear()

    logger.info("Shutdown complete")


# ---------------------------------------------------------------------------
# Settings page
# ---------------------------------------------------------------------------

_SETTINGS_FILE = PROJECT_DIR / "settings.json"


def _load_settings() -> dict:
    """Load saved settings from settings.json, with defaults."""
    from config import AI_MAX_CONCURRENT, AI_RPM
    defaults = {
        "ai_backend": AI_BACKEND if 'AI_BACKEND' in dir() else "vllm",
        "api_key": "",
        "api_base_url": AI_API_BASE_URL,
        "ai_model": AI_MODEL,
        "ai_vision_model": AI_VISION_MODEL,
        "ai_vision_api_url": AI_VISION_API_URL,
        "ai_judge_model": AI_JUDGE_MODEL,
        "ai_judge_api_url": AI_JUDGE_API_URL,
        "ai_judge_api_key": "",
        "ai_max_tokens": AI_MAX_TOKENS,
        "ai_timeout": AI_TIMEOUT,
        "ai_max_retries": AI_MAX_RETRIES,
        "ai_max_concurrent": AI_MAX_CONCURRENT,
        "ai_rpm": AI_RPM,
    }
    if _SETTINGS_FILE.exists():
        try:
            saved = json.loads(_SETTINGS_FILE.read_text())
            defaults.update(saved)
        except Exception:
            logger.exception("Failed to parse %s; showing built-in defaults", _SETTINGS_FILE)
    def _mask(k: str) -> str:
        v = defaults.get(k, "")
        if v and len(v) > 8:
            return v[:4] + "*" * (len(v) - 8) + v[-4:]
        return ""

    defaults["api_key_masked"] = _mask("api_key")
    defaults["ai_judge_api_key_masked"] = _mask("ai_judge_api_key")
    defaults["ai_reviewer_api_key_masked"] = _mask("ai_reviewer_api_key")
    defaults["ai_video_api_key_masked"] = _mask("ai_video_api_key")
    return defaults


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    settings = _load_settings()
    return templates.TemplateResponse(request, "settings.html", context={
        "settings": settings,
    })


@app.post("/api/settings")
async def save_settings(request: Request):
    try:
        data = await request.json()
        current = _load_settings()
        # Preserve existing secrets when the form submits the masked
        # placeholder ("****" pattern) instead of a real new value.
        for key in ("api_key", "ai_judge_api_key", "ai_reviewer_api_key", "ai_video_api_key"):
            value = data.get(key, "")
            if not value or "*" in value:
                data[key] = current.get(key, "")
        # Strip per-role keys that just duplicate the master api_key, so
        # the file stays the single source of truth. config.py cascades
        # ai_judge_api_key / ai_reviewer_api_key / ai_video_api_key down
        # to api_key automatically when they're empty.
        master = data.get("api_key", "")
        for key in ("ai_judge_api_key", "ai_reviewer_api_key", "ai_video_api_key"):
            if data.get(key, "") == master:
                data.pop(key, None)
        _SETTINGS_FILE.write_text(json.dumps(data, indent=2))
        return JSONResponse({"status": "ok"})
    except Exception as e:
        logger.exception("save_settings failed")
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    reviews = list_reviews()
    # Support ?rerun=<review_id> to pre-fill form
    rerun_id = request.query_params.get("rerun", "")
    rerun_data = {}
    if rerun_id:
        try:
            review_dir = REVIEWS_DIR / rerun_id
            if review_dir.exists():
                meta = load_meta(review_dir)
                rerun_data = meta.to_dict()
        except Exception:
            logger.exception("Failed to load rerun source review %s", rerun_id)
    return templates.TemplateResponse(request, "index.html", context={
        "reviews": [r.to_dict() for r in reviews],
        "rerun": rerun_data,
    })


@app.post("/review/start")
async def start_review(
    request: Request,
    url: str = Form(default=""),
    pdf_file: UploadFile | None = File(default=None),
    report_format: str = Form(default=REPORT_FORMAT),
    coverage_level: str = Form(default=COVERAGE_LEVEL),
    wcag_version: str = Form(default=WCAG_VERSION),
    company_name: str = Form(default=""),
    product_name: str = Form(default=""),
    product_description: str = Form(default=""),
    contact_name: str = Form(default=""),
    contact_email: str = Form(default=""),
    notes: str = Form(default=""),
    company_logo: UploadFile | None = File(default=None),
    ctx_page_purpose: str = Form(default=""),
    ctx_time_limits: str = Form(default=""),
    ctx_alt_version_url: str = Form(default=""),
    ctx_expected_behaviors: str = Form(default=""),
    ctx_additional_notes: str = Form(default=""),
):
    user_context = {}
    if ctx_page_purpose:
        user_context["page_purpose"] = ctx_page_purpose
    if ctx_time_limits:
        user_context["time_limits"] = ctx_time_limits
    if ctx_alt_version_url:
        user_context["alt_version_url"] = ctx_alt_version_url
    if ctx_expected_behaviors:
        user_context["expected_behaviors"] = ctx_expected_behaviors
    if ctx_additional_notes:
        user_context["additional_notes"] = ctx_additional_notes

    meta = ReviewMeta(
        review_id=ReviewMeta.generate_id(),
        created_at=datetime.now(timezone.utc).isoformat(),
        model_used=AI_MODEL,
        report_format=report_format,
        coverage_level=coverage_level,
        wcag_version=wcag_version,
        company_name=company_name,
        product_name=product_name,
        product_description=product_description,
        contact_name=contact_name,
        contact_email=contact_email,
        notes=notes,
        review_type="single",
        user_context=user_context,
    )

    if pdf_file and pdf_file.filename:
        review_dir = create_review(meta)
        try:
            saved_path = await save_user_upload(
                pdf_file, review_dir / "captures", label="uploaded document",
            )
        except (UploadTooLargeError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        meta.source_file = str(saved_path)
        meta.file_type = saved_path.suffix.lower().lstrip(".")
        save_meta(review_dir, meta)
    elif url:
        try:
            meta.source_url = validate_public_url(url)
        except UnsafeURLError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        create_review(meta)
    else:
        return JSONResponse({"error": "URL or file required"}, status_code=400)

    if company_logo and company_logo.filename:
        review_dir = REVIEWS_DIR / meta.review_id
        try:
            logo_path = await save_user_upload(
                company_logo, review_dir / "captures", label="logo",
            )
        except (UploadTooLargeError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        meta.company_logo_path = str(logo_path)
        save_meta(review_dir, meta)
    elif Path(COMPANY_LOGO_PATH).exists():
        meta.company_logo_path = COMPANY_LOGO_PATH
        save_meta(REVIEWS_DIR / meta.review_id, meta)

    # Default company name if not provided
    if not meta.company_name:
        meta.company_name = COMPANY_NAME
        save_meta(REVIEWS_DIR / meta.review_id, meta)

    await _review_queue.put(meta.review_id)
    return JSONResponse({
        "review_id": meta.review_id,
        "redirect": f"/review/{meta.review_id}/progress",
    })


@app.post("/review/start-site")
async def start_site_review(
    request: Request,
    url: str = Form(default=""),
    report_format: str = Form(default=REPORT_FORMAT),
    coverage_level: str = Form(default=COVERAGE_LEVEL),
    wcag_version: str = Form(default=WCAG_VERSION),
    # max_pages kept only as an optional discovery-ceiling safety net so a
    # runaway crawl on a 50,000-page site can't chew forever. The AI page
    # selector decides how many pages to actually test; operator-facing
    # form no longer exposes this.
    max_pages: int = Form(default=200),
    company_name: str = Form(default=""),
    product_name: str = Form(default=""),
    product_description: str = Form(default=""),
    contact_name: str = Form(default=""),
    contact_email: str = Form(default=""),
    notes: str = Form(default=""),
    page_rationale: str = Form(default=""),
    company_logo: UploadFile | None = File(default=None),
    ctx_page_purpose: str = Form(default=""),
    ctx_time_limits: str = Form(default=""),
    ctx_alt_version_url: str = Form(default=""),
    ctx_expected_behaviors: str = Form(default=""),
    ctx_additional_notes: str = Form(default=""),
):
    if not url:
        return JSONResponse({"error": "URL required for site crawl"}, status_code=400)
    try:
        url = validate_public_url(url)
    except UnsafeURLError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    user_context = {}
    if ctx_page_purpose:
        user_context["page_purpose"] = ctx_page_purpose
    if ctx_time_limits:
        user_context["time_limits"] = ctx_time_limits
    if ctx_alt_version_url:
        user_context["alt_version_url"] = ctx_alt_version_url
    if ctx_expected_behaviors:
        user_context["expected_behaviors"] = ctx_expected_behaviors
    if ctx_additional_notes:
        user_context["additional_notes"] = ctx_additional_notes

    meta = ReviewMeta(
        review_id=ReviewMeta.generate_id(),
        source_url=url,
        created_at=datetime.now(timezone.utc).isoformat(),
        model_used=AI_MODEL,
        report_format=report_format,
        coverage_level=coverage_level,
        wcag_version=wcag_version,
        company_name=company_name,
        product_name=product_name,
        product_description=product_description,
        contact_name=contact_name,
        contact_email=contact_email,
        notes=notes,
        page_rationale=page_rationale,
        review_type="site",
        max_pages=min(max(max_pages, 2), 500),
        user_context=user_context,
    )

    create_review(meta)

    if company_logo and company_logo.filename:
        review_dir = REVIEWS_DIR / meta.review_id
        try:
            logo_path = await save_user_upload(
                company_logo, review_dir / "captures", label="logo",
            )
        except (UploadTooLargeError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        meta.company_logo_path = str(logo_path)
        save_meta(review_dir, meta)
    elif Path(COMPANY_LOGO_PATH).exists():
        meta.company_logo_path = COMPANY_LOGO_PATH
        save_meta(REVIEWS_DIR / meta.review_id, meta)

    if not meta.company_name:
        meta.company_name = COMPANY_NAME
        save_meta(REVIEWS_DIR / meta.review_id, meta)

    await _review_queue.put(meta.review_id)
    return JSONResponse({
        "review_id": meta.review_id,
        "redirect": f"/review/{meta.review_id}/progress",
    })


@app.post("/review/start-multi")
async def start_multi_review(
    request: Request,
    multi_urls: str = Form(default=""),
    report_format: str = Form(default=REPORT_FORMAT),
    coverage_level: str = Form(default=COVERAGE_LEVEL),
    wcag_version: str = Form(default=WCAG_VERSION),
    company_name: str = Form(default=""),
    product_name: str = Form(default=""),
    product_description: str = Form(default=""),
    contact_name: str = Form(default=""),
    contact_email: str = Form(default=""),
    notes: str = Form(default=""),
    page_rationale: str = Form(default=""),
    company_logo: UploadFile | None = File(default=None),
    ctx_page_purpose: str = Form(default=""),
    ctx_time_limits: str = Form(default=""),
    ctx_alt_version_url: str = Form(default=""),
    ctx_expected_behaviors: str = Form(default=""),
    ctx_additional_notes: str = Form(default=""),
):
    """Start a multi-page review with user-specified URLs.

    Works like a site crawl but skips the crawler — goes straight to
    testing the URLs the user provided. Results are aggregated into
    one ACR with cross-page consistency checks.
    """
    raw_urls = [u.strip() for u in multi_urls.split("\n") if u.strip()]
    urls: list[str] = []
    for u in raw_urls:
        try:
            urls.append(validate_public_url(u))
        except UnsafeURLError as exc:
            return JSONResponse({"error": f"Rejected URL '{u}': {exc}"}, status_code=400)

    if len(urls) < 2:
        return JSONResponse({"error": "Multi-page mode requires at least 2 valid URLs"}, status_code=400)

    user_context = {}
    if ctx_page_purpose:
        user_context["page_purpose"] = ctx_page_purpose
    if ctx_time_limits:
        user_context["time_limits"] = ctx_time_limits
    if ctx_alt_version_url:
        user_context["alt_version_url"] = ctx_alt_version_url
    if ctx_expected_behaviors:
        user_context["expected_behaviors"] = ctx_expected_behaviors
    if ctx_additional_notes:
        user_context["additional_notes"] = ctx_additional_notes

    meta = ReviewMeta(
        review_id=ReviewMeta.generate_id(),
        source_url=urls[0],  # Primary URL for display
        created_at=datetime.now(timezone.utc).isoformat(),
        model_used=AI_MODEL,
        report_format=report_format,
        coverage_level=coverage_level,
        wcag_version=wcag_version,
        company_name=company_name,
        product_name=product_name,
        product_description=product_description,
        contact_name=contact_name,
        contact_email=contact_email,
        notes=notes,
        page_rationale=page_rationale,
        review_type="multi",
        max_pages=len(urls),
        user_context={**user_context, "multi_urls": urls},
    )

    create_review(meta)

    if company_logo and company_logo.filename:
        review_dir = REVIEWS_DIR / meta.review_id
        try:
            logo_path = await save_user_upload(
                company_logo, review_dir / "captures", label="logo",
            )
        except (UploadTooLargeError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        meta.company_logo_path = str(logo_path)
        save_meta(REVIEWS_DIR / meta.review_id, meta)
    elif Path(COMPANY_LOGO_PATH).exists():
        meta.company_logo_path = COMPANY_LOGO_PATH
        save_meta(REVIEWS_DIR / meta.review_id, meta)

    if not meta.company_name:
        meta.company_name = COMPANY_NAME
        save_meta(REVIEWS_DIR / meta.review_id, meta)

    await _review_queue.put(meta.review_id)
    return JSONResponse({
        "review_id": meta.review_id,
        "redirect": f"/review/{meta.review_id}/progress",
    })


@app.get("/review/{review_id}/progress", response_class=HTMLResponse)
async def progress_page(request: Request, review_id: str):
    review_dir = REVIEWS_DIR / review_id
    if not review_dir.exists():
        return HTMLResponse("<h1>Review not found</h1>", status_code=404)
    meta = load_meta(review_dir)
    return templates.TemplateResponse(request, "progress.html", context={
        "review_id": review_id,
        "meta": meta.to_dict(),
    })


@app.websocket("/ws/{review_id}")
async def websocket_endpoint(websocket: WebSocket, review_id: str):
    await websocket.accept()
    with _active_websockets_lock:
        if review_id not in _active_websockets:
            _active_websockets[review_id] = []
        _active_websockets[review_id].append(websocket)

    try:
        while True:
            data = await websocket.receive_text()
            is_ping = False
            if data == "ping":
                is_ping = True
            else:
                try:
                    msg = json.loads(data)
                    if isinstance(msg, dict) and msg.get("type") == "ping":
                        is_ping = True
                except (json.JSONDecodeError, TypeError):
                    pass
            if is_ping:
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    finally:
        with _active_websockets_lock:
            clients = _active_websockets.get(review_id)
            if clients is not None:
                try:
                    clients.remove(websocket)
                except ValueError:
                    pass


@app.get("/review/{review_id}/report", response_class=HTMLResponse)
async def report_page(request: Request, review_id: str):
    review_dir = REVIEWS_DIR / review_id
    if not review_dir.exists():
        return HTMLResponse("<h1>Review not found</h1>", status_code=404)

    meta = load_meta(review_dir)
    report_html = review_dir / "report" / "acr_report.html"

    # Persist the audit + reviewer reports onto the report page so the
    # operator sees quality signals every time they visit, not just
    # transiently while the progress page is open.
    audit_data = None
    audit_p = review_dir / "audit.json"
    if audit_p.exists():
        try:
            audit_data = json.loads(audit_p.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("Failed to parse %s", audit_p)
            audit_data = None
    reviewer_data = None
    reviewer_p = review_dir / "reviewer_report.json"
    if reviewer_p.exists():
        try:
            reviewer_data = json.loads(reviewer_p.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("Failed to parse %s", reviewer_p)
            reviewer_data = None

    common_ctx = {
        "review_id": review_id,
        "meta": meta.to_dict(),
        "audit_data": audit_data,
        "reviewer_data": reviewer_data,
    }

    if report_html.exists():
        acr_html = report_html.read_text(encoding="utf-8", errors="replace")
        results = load_all_test_results(review_dir)
        return templates.TemplateResponse(request, "report_wrapper.html", context={
            **common_ctx,
            "acr_html": acr_html,
            "results": results,
        })

    # Fallback page
    results = load_all_test_results(review_dir)
    return templates.TemplateResponse(request, "report.html", context={
        **common_ctx,
        "results": results,
    })


@app.get("/review/{review_id}/report/json")
async def report_json(review_id: str):
    review_dir = REVIEWS_DIR / review_id
    report_json_path = review_dir / "report" / "acr_report.json"
    if report_json_path.exists():
        return JSONResponse(json.loads(report_json_path.read_text()))
    results = load_all_test_results(review_dir)
    return JSONResponse(results)


@app.get("/review/{review_id}/test/{criterion_id}", response_class=HTMLResponse)
async def test_detail(request: Request, review_id: str, criterion_id: str):
    review_dir = REVIEWS_DIR / review_id
    if not review_dir.exists():
        return HTMLResponse("<h1>Review not found</h1>", status_code=404)

    result = load_test_result(review_dir, criterion_id)
    if result is None:
        return HTMLResponse("<h1>Test not found</h1>", status_code=404)

    decisions = load_user_decisions(review_dir, criterion_id)

    # Load programmatic data and all AI prompts/responses
    criterion_dir = review_dir / "tests" / criterion_id.replace(".", "_")
    prog_data = None
    ai_prompt = ""
    ai_response = ""
    code_ai_prompt = ""
    code_ai_response = ""

    prog_path = criterion_dir / "programmatic_data.json"
    if prog_path.exists():
        prog_data = json.loads(prog_path.read_text(encoding="utf-8", errors="replace"))

    # Visual AI prompt and response
    prompt_path = criterion_dir / "prompt.txt"
    if prompt_path.exists():
        ai_prompt = prompt_path.read_text(encoding="utf-8", errors="replace")

    for resp_name in ("visual_ai_response.json", "ai_response.txt"):
        resp_path = criterion_dir / resp_name
        if resp_path.exists():
            ai_response = resp_path.read_text(encoding="utf-8", errors="replace")
            break

    # Code AI prompt and response
    code_prompt_path = criterion_dir / "code_ai_prompt.txt"
    if code_prompt_path.exists():
        code_ai_prompt = code_prompt_path.read_text(encoding="utf-8", errors="replace")

    code_resp_path = criterion_dir / "code_ai_response.json"
    if code_resp_path.exists():
        code_ai_response = code_resp_path.read_text(encoding="utf-8", errors="replace")

    # Judge AI response
    judge_response = ""
    judge_path = criterion_dir / "judge_response.json"
    if judge_path.exists():
        judge_response = judge_path.read_text(encoding="utf-8", errors="replace")

    # Judge DOM context — the deterministic ground-truth block the judge
    # received. Critical for forensic review when an operator needs to
    # see exactly what the judge "knew" before reaching a verdict.
    judge_dom_context = ""
    judge_ctx_path = criterion_dir / "judge_dom_context.txt"
    if judge_ctx_path.exists():
        judge_dom_context = judge_ctx_path.read_text(encoding="utf-8", errors="replace")

    # Merge user decisions into findings so the template shows saved state
    if decisions and result.get("findings"):
        for finding in result["findings"]:
            fid = finding.get("id", "")
            if fid in decisions:
                finding["decision"] = decisions[fid].get("status", "undecided")
                finding["decision_reason"] = decisions[fid].get("reason", "")

    # Load normative text from the check class
    normative_text = ""
    try:
        from checks.registry import get_check_by_id
        check = get_check_by_id(criterion_id)
        if check:
            normative_text = check.normative_text
    except Exception:
        logger.exception("Failed to load normative text for %s", criterion_id)

    # AT Simulation data — extract from result since it's not saved separately
    at_sim_data = ""
    at_sim_conf = result.get("at_sim_conformance", "")
    at_sim_confidence = result.get("at_sim_confidence", 0)
    at_sim_findings = [f for f in result.get("findings", []) if f.get("source") == "at_sim"]
    if at_sim_conf or at_sim_findings:
        at_sim_data = json.dumps({
            "conformance": at_sim_conf,
            "confidence": at_sim_confidence,
            "findings": at_sim_findings,
        }, indent=2)

    # Capture-evidence index for this criterion. Surfaces every supporting
    # artifact relevant to this SC so operators can verify findings
    # against the actual captured ground truth without rummaging through
    # the filesystem. Each entry has {label, kind, path} where path is
    # relative to /review/{id}/captures/file/.
    captures_dir = review_dir / "captures"
    evidence: list[dict] = []
    def _add_evidence(label: str, kind: str, rel_path: str) -> None:
        full = captures_dir / rel_path
        if full.exists() and full.is_file():
            evidence.append({"label": label, "kind": kind, "path": rel_path})

    # Always-relevant page-wide artifacts
    _add_evidence("Full page screenshot", "image", "full_page.png")
    _add_evidence("Viewport screenshot", "image", "viewport.png")
    _add_evidence("Page DOM (rendered HTML)", "html", "dom.html")
    _add_evidence("Accessibility tree", "json", "a11y_tree.json")

    # SC-specific evidence — only attach when relevant
    sc_prefix = ".".join(criterion_id.split(".")[:2]) if "." in criterion_id else criterion_id
    if sc_prefix in ("1.4",) or criterion_id == "1.4.10":
        _add_evidence("320px viewport (reflow)", "image", "viewport_320px.png")
        _add_evidence("200% zoom screenshot", "image", "full_page_200pct.png")
    if criterion_id == "1.4.12":
        _add_evidence("Text-spacing override", "image", "text_spacing_override.png")
    if sc_prefix == "2.1" or criterion_id in ("2.4.3", "2.4.7", "2.4.11"):
        _add_evidence("Tab walk (keyboard order)", "json", "tab_walk.json")
        _add_evidence("Keyboard traps", "json", "keyboard_traps.json")
        # Keyboard walkthrough video (first .webm in the directory)
        kw_dir = captures_dir / "keyboard_walkthrough"
        if kw_dir.is_dir():
            for vf in sorted(kw_dir.iterdir()):
                if vf.suffix.lower() in (".webm", ".mp4"):
                    _add_evidence(f"Keyboard walkthrough video: {vf.name}", "video", f"keyboard_walkthrough/{vf.name}")
                    break
    if criterion_id in ("3.3.1", "3.3.2", "3.3.3", "3.3.4"):
        fe_dir = captures_dir / "form_error_captures"
        if fe_dir.is_dir():
            for img in sorted(fe_dir.iterdir()):
                if img.suffix.lower() == ".png":
                    _add_evidence(f"Form error: {img.name}", "image", f"form_error_captures/{img.name}")
    if criterion_id in ("1.2.2", "1.2.3", "1.2.4", "1.2.5"):
        tr_dir = captures_dir / "transcripts"
        if tr_dir.is_dir():
            for tf in sorted(tr_dir.iterdir()):
                if tf.is_file():
                    _add_evidence(f"Transcript: {tf.name}", "text", f"transcripts/{tf.name}")
    if sc_prefix == "2.3" or criterion_id == "2.3.1":
        ff_dir = captures_dir / "flash_frames"
        if ff_dir.is_dir():
            for img in sorted(ff_dir.iterdir()):
                if img.suffix.lower() == ".png":
                    _add_evidence(f"Flash frame: {img.name}", "image", f"flash_frames/{img.name}")

    return templates.TemplateResponse(request, "test_detail.html", context={
        "review_id": review_id,
        "criterion_id": criterion_id,
        "result": result,
        "decisions": decisions,
        "programmatic_data": prog_data,
        "ai_prompt": ai_prompt,
        "ai_response": ai_response,
        "code_ai_prompt": code_ai_prompt,
        "code_ai_response": code_ai_response,
        "at_sim_data": at_sim_data,
        "judge_response": judge_response,
        "judge_dom_context": judge_dom_context,
        "evidence": evidence,
        "normative_text": normative_text,
    })


@app.post("/api/review/{review_id}/test/{criterion_id}/finding/{finding_id}/decision")
async def update_finding_decision(
    review_id: str,
    criterion_id: str,
    finding_id: str,
    request: Request,
):
    body = await request.json()
    status = body.get("status", "undecided")
    reason = body.get("reason", "")

    review_dir = REVIEWS_DIR / review_id
    if not review_dir.exists():
        return JSONResponse({"error": "Review not found"}, status_code=404)

    save_user_decision(review_dir, criterion_id, finding_id, status, reason)
    return JSONResponse({"ok": True})


@app.get("/api/reviews")
async def api_list_reviews():
    reviews = list_reviews()
    return JSONResponse([r.to_dict() for r in reviews])


@app.delete("/api/review/{review_id}")
async def api_delete_review(review_id: str):
    success = delete_review(review_id)
    if not success:
        return JSONResponse({"error": "Cannot delete (running or not found)"}, status_code=400)
    return JSONResponse({"ok": True})


@app.delete("/api/reviews")
async def api_delete_all_reviews():
    deleted, skipped = delete_all_reviews()
    return JSONResponse({"deleted": deleted, "skipped": skipped})


@app.get("/api/queue/status")
async def api_queue_status():
    running = []
    queued_list = []
    for review in list_reviews():
        if review.status in ("capturing", "testing", "generating_report", "crawling", "aggregating"):
            running.append(review.review_id)
        elif review.status == "queued":
            queued_list.append(review.review_id)
    return JSONResponse({
        "running": running,
        "queued": queued_list,
        "queue_size": _review_queue.qsize(),
    })


@app.get("/api/review/{review_id}/status")
async def api_review_status(review_id: str):
    """Return current review status for progress page polling."""
    review_dir = REVIEWS_DIR / review_id
    if not review_dir.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        meta = load_meta(review_dir)
        return JSONResponse({
            "status": meta.status,
            "total_criteria": getattr(meta, "total_criteria", 0),
            "supports": getattr(meta, "supports", 0),
            "partially_supports": getattr(meta, "partially_supports", 0),
            "does_not_support": getattr(meta, "does_not_support", 0),
            "not_applicable": getattr(meta, "not_applicable", 0),
            "not_evaluated": getattr(meta, "not_evaluated", 0),
            "error": getattr(meta, "error", ""),
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/review/{review_id}/cancel")
async def api_cancel_review(review_id: str):
    """Cancel a running review. Takes effect between check iterations."""
    review_dir = REVIEWS_DIR / review_id
    if not review_dir.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    with _cancelled_reviews_lock:
        _cancelled_reviews.add(review_id)
    logger.info("Review %s marked for cancellation", review_id)
    await broadcast(review_id, {"type": "cancelling", "message": "Cancelling review..."})
    return JSONResponse({"ok": True, "message": "Review will cancel after the current check finishes"})


@app.post("/api/review/{review_id}/resume")
async def api_resume_review(review_id: str):
    """Resume an interrupted or cancelled review from where it left off."""
    review_dir = REVIEWS_DIR / review_id
    if not review_dir.exists():
        return JSONResponse({"error": "not found"}, status_code=404)

    meta = load_meta(review_dir)
    if meta.status not in ("interrupted", "error", "cancelled"):
        return JSONResponse(
            {"error": f"Cannot resume review in status '{meta.status}'"},
            status_code=400,
        )

    # Count how many tests already completed
    tests_dir = review_dir / "tests"
    completed = 0
    if tests_dir.exists():
        for sc_dir in tests_dir.iterdir():
            if sc_dir.is_dir() and (sc_dir / "result.json").exists():
                completed += 1

    meta.status = "queued"
    meta.error = None
    save_meta(review_dir, meta)

    # Re-queue the review — process_review will skip completed tests
    await _review_queue.put(meta.review_id)

    logger.info("Review %s resumed (%d tests already completed)", review_id, completed)
    return JSONResponse({
        "ok": True,
        "message": f"Review resumed — {completed} tests already completed, continuing from where it left off",
        "completed": completed,
        "total": meta.total_criteria,
    })


@app.patch("/api/review/{review_id}/meta")
async def api_update_review_meta(review_id: str, request: Request):
    """Update editable meta fields (page_rationale, notes, etc.)."""
    review_dir = REVIEWS_DIR / review_id
    if not review_dir.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        body = await request.json()
        meta = load_meta(review_dir)
        editable = {"page_rationale", "notes", "product_description",
                     "product_name", "contact_name", "contact_email",
                     "evaluation_methods"}
        changed = False
        for key, value in body.items():
            if key in editable and isinstance(value, str):
                setattr(meta, key, value)
                changed = True
        if changed:
            save_meta(review_dir, meta)
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/review/{review_id}/results")
async def api_review_results(review_id: str):
    """Return all completed test results for backfilling the progress page."""
    review_dir = REVIEWS_DIR / review_id
    if not review_dir.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    results = load_all_test_results(review_dir)
    meta = load_meta(review_dir)
    return JSONResponse({
        "status": meta.status,
        "total_criteria": getattr(meta, "total_criteria", 0),
        "results": [
            {
                "criterion_id": r.get("criterion_id", ""),
                "criterion_name": r.get("criterion_name", ""),
                "conformance_level": r.get("conformance_level", "Not Evaluated"),
                "confidence": r.get("confidence", 0),
                "findings_count": len(r.get("findings", [])),
                "level": r.get("level", ""),
            }
            for r in results
        ],
    })


@app.get("/api/health")
async def api_health(deep: bool = False):
    """Health probe. By default does a light text-LLM check (fast, for the UI
    poller). ``?deep=true`` probes every AI I/O service (text, embeddings,
    vision, whisper) so a dead/misconfigured endpoint -- the embeddings-fleet
    class of failure -- is surfaced on demand."""
    if deep:
        try:
            from functions.preflight import preflight_ai_services
            pf = await preflight_ai_services()
            return JSONResponse({
                "status": "ok",
                "ai_api": "connected" if pf["services"]["text_llm"]["ok"] else "disconnected",
                "all_ok": pf["all_ok"],
                "services": pf["services"],
            })
        except Exception:
            logger.warning("Deep AI health check failed", exc_info=True)
            return JSONResponse({"status": "ok", "ai_api": "disconnected", "all_ok": False})

    ai_status = "disconnected"
    try:
        from functions.llm import LLMClient

        health = await LLMClient().check_health()
        if health.get("status") == "ok":
            ai_status = "connected"
    except Exception:
        logger.warning("AI health check failed", exc_info=True)
    return JSONResponse({"status": "ok", "ai_api": ai_status})


@app.get("/review/{review_id}/export/pdf")
async def export_pdf(review_id: str):
    review_dir = REVIEWS_DIR / review_id
    html_path = review_dir / "report" / "acr_report.html"
    pdf_path = review_dir / "report" / "acr_report.pdf"

    if not pdf_path.exists() and html_path.exists():
        try:
            from report.pdf_exporter import export_pdf as do_export
            await do_export(str(html_path), str(pdf_path))
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    if pdf_path.exists():
        return FileResponse(str(pdf_path), media_type="application/pdf",
                          filename=f"acr_report_{review_id}.pdf")
    return JSONResponse({"error": "Report not available"}, status_code=404)


@app.get("/review/{review_id}/export/xlsx")
async def export_xlsx(review_id: str):
    review_dir = REVIEWS_DIR / review_id
    xlsx_path = review_dir / "report" / "acr_report.xlsx"

    if not xlsx_path.exists():
        try:
            from report.xlsx_exporter import export_xlsx as do_export
            results = load_all_test_results(review_dir)
            meta = load_meta(review_dir)
            do_export(results, meta, str(xlsx_path))
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    if xlsx_path.exists():
        return FileResponse(str(xlsx_path),
                          media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                          filename=f"acr_report_{review_id}.xlsx")
    return JSONResponse({"error": "Report not available"}, status_code=404)


@app.get("/review/{review_id}/export/docx")
async def export_docx(review_id: str):
    review_dir = REVIEWS_DIR / review_id
    docx_path = review_dir / "report" / "acr_report.docx"

    if not docx_path.exists():
        try:
            from report.docx_exporter import export_docx as do_export
            results = load_all_test_results(review_dir)
            meta = load_meta(review_dir)
            do_export(results, meta, str(docx_path))
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    if docx_path.exists():
        return FileResponse(str(docx_path),
                          media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                          filename=f"acr_report_{review_id}.docx")
    return JSONResponse({"error": "Report not available"}, status_code=404)


@app.get("/review/{review_id}/export/pdf/client")
async def export_pdf_client(review_id: str):
    """Client-facing PDF export — no confidence, AI, or source info."""
    review_dir = REVIEWS_DIR / review_id
    html_path = review_dir / "report" / "acr_report_client.html"
    pdf_path = review_dir / "report" / "acr_report_client.pdf"

    if not pdf_path.exists() and html_path.exists():
        try:
            from report.pdf_exporter import export_pdf as do_export
            await do_export(str(html_path), str(pdf_path))
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    if pdf_path.exists():
        return FileResponse(str(pdf_path), media_type="application/pdf",
                          filename=f"acr_report_{review_id}_client.pdf")
    return JSONResponse({"error": "Client report not available"}, status_code=404)


@app.get("/review/{review_id}/export/xlsx/client")
async def export_xlsx_client(review_id: str):
    """Client-facing XLSX export — no confidence, AI, or source info."""
    review_dir = REVIEWS_DIR / review_id
    xlsx_path = review_dir / "report" / "acr_report_client.xlsx"

    if not xlsx_path.exists():
        try:
            from report.xlsx_exporter import export_xlsx as do_export
            results = load_all_test_results(review_dir)
            meta = load_meta(review_dir)
            do_export(results, meta, str(xlsx_path), client_mode=True)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    if xlsx_path.exists():
        return FileResponse(str(xlsx_path),
                          media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                          filename=f"acr_report_{review_id}_client.xlsx")
    return JSONResponse({"error": "Client report not available"}, status_code=404)


@app.get("/review/{review_id}/export/docx/client")
async def export_docx_client(review_id: str):
    """Client-facing DOCX export — no confidence, AI, or source info."""
    review_dir = REVIEWS_DIR / review_id
    docx_path = review_dir / "report" / "acr_report_client.docx"

    if not docx_path.exists():
        try:
            from report.docx_exporter import export_docx as do_export
            results = load_all_test_results(review_dir)
            meta = load_meta(review_dir)
            do_export(results, meta, str(docx_path), client_mode=True)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    if docx_path.exists():
        return FileResponse(str(docx_path),
                          media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                          filename=f"acr_report_{review_id}_client.docx")
    return JSONResponse({"error": "Client report not available"}, status_code=404)


@app.get("/review/{review_id}/export/internal")
async def export_internal_summary(review_id: str):
    """Export internal-use summary with remediation guidance.

    This is for the evaluating organization's internal use only — NOT
    delivered to clients.  Includes detailed remediation suggestions, technical
    notes, and prioritized action items.
    """
    review_dir = REVIEWS_DIR / review_id
    if not review_dir.exists():
        return JSONResponse({"error": "Review not found"}, status_code=404)

    meta = load_meta(review_dir)
    results = load_all_test_results(review_dir)

    # Load synthesis data if available
    synthesis = {}
    synthesis_path = review_dir / "synthesis.json"
    if synthesis_path.exists():
        synthesis = json.loads(synthesis_path.read_text())

    # Build internal summary
    internal = {
        "header": "INTERNAL USE ONLY",
        "product": meta.product_name or meta.source_url or "Unknown",
        "company": meta.company_name or "",
        "date": meta.created_at,
        "review_id": review_id,
        "executive_summary": synthesis.get("executive_summary", ""),
        "systemic_issues": synthesis.get("systemic_issues", []),
        "priority_order": synthesis.get("priority_order", []),
        "criteria": [],
    }

    for r in sorted(results, key=lambda x: x.get("criterion_id", "") if isinstance(x, dict) else x.criterion_id):
        if isinstance(r, dict):
            cid = r.get("criterion_id", "")
            cname = r.get("criterion_name", "")
            conf = r.get("conformance_level", "Not Evaluated")
            findings_data = r.get("findings", [])
            summary = r.get("summary", "")
        else:
            cid = r.criterion_id
            cname = r.criterion_name
            conf = r.conformance_level.value if hasattr(r.conformance_level, 'value') else str(r.conformance_level)
            findings_data = [f.to_dict() if hasattr(f, 'to_dict') else f for f in r.findings]
            summary = r.summary

        criterion_entry = {
            "criterion_id": cid,
            "criterion_name": cname,
            "conformance_level": conf,
            "vpat_remark": summary,
            "findings_with_remediation": [],
        }

        for f in findings_data:
            if isinstance(f, dict):
                criterion_entry["findings_with_remediation"].append({
                    "element": f.get("element", ""),
                    "issue": f.get("issue", ""),
                    "impact": f.get("impact", ""),
                    "severity": f.get("severity", ""),
                    "remediation_note": f.get("recommendation", ""),
                })
            else:
                criterion_entry["findings_with_remediation"].append({
                    "element": f.element if hasattr(f, 'element') else "",
                    "issue": f.issue if hasattr(f, 'issue') else "",
                    "impact": f.impact if hasattr(f, 'impact') else "",
                    "severity": f.severity.value if hasattr(f.severity, 'value') else str(f.severity) if hasattr(f, 'severity') else "",
                    "remediation_note": f.recommendation if hasattr(f, 'recommendation') else "",
                })

        internal["criteria"].append(criterion_entry)

    return JSONResponse(internal)


@app.get("/review/{review_id}/download/evidence.zip")
async def download_evidence_zip(review_id: str):
    """Stream all captured evidence + reports for a review as a single ZIP.

    The package is what an auditor would deliver to a client: the
    rendered ACR (HTML/JSON/DOCX/XLSX/PDF if generated), the reviewer
    and audit reports, and every screenshot, video, transcript, and
    capture artifact under captures/. Big payload — generated on demand,
    streamed back, never stored.
    """
    import io
    import zipfile

    review_dir = REVIEWS_DIR / review_id
    if not review_dir.exists():
        return JSONResponse({"error": "review not found"}, status_code=404)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        # Walk the entire review directory and add every file with its
        # path relative to the review root. Skip the in-flight log file
        # if present at top level — that's server-wide noise.
        for root, _dirs, files in os.walk(review_dir):
            for fname in files:
                src = Path(root) / fname
                try:
                    arcname = src.relative_to(review_dir).as_posix()
                except ValueError:
                    continue
                # Top-level transcripts directory is included implicitly.
                try:
                    zf.write(src, arcname=f"{review_id}/{arcname}")
                except Exception as exc:
                    logger.warning("Skipping %s in evidence zip: %s", src, exc)
    buf.seek(0)
    headers = {
        "Content-Disposition": f'attachment; filename="evidence_{review_id}.zip"',
    }
    return Response(content=buf.getvalue(), media_type="application/zip", headers=headers)


@app.get("/review/{review_id}/captures", response_class=HTMLResponse)
async def captures_page(request: Request, review_id: str):
    """Browse all captured screenshots, videos, and images."""
    review_dir = REVIEWS_DIR / review_id
    if not review_dir.exists():
        return HTMLResponse("<h1>Review not found</h1>", status_code=404)

    captures_dir = review_dir / "captures"
    if not captures_dir.exists():
        return HTMLResponse("<h1>No captures found</h1>", status_code=404)

    # Collect all media files organized by category
    categories: dict[str, list[dict]] = {}
    for root, dirs, files in os.walk(str(captures_dir)):
        rel_root = Path(root).relative_to(captures_dir)
        category = str(rel_root).replace("\\", "/")
        if category == ".":
            category = "screenshots"
        for f in sorted(files):
            ext = Path(f).suffix.lower()
            if ext not in (".png", ".jpg", ".jpeg", ".webm", ".mp4", ".json", ".html", ".txt"):
                continue
            if category not in categories:
                categories[category] = []
            media_type = "image" if ext in (".png", ".jpg", ".jpeg") else "video" if ext in (".webm", ".mp4") else "text"
            categories[category].append({
                "name": f,
                "path": f"/review/{review_id}/captures/file/{category}/{f}",
                "type": media_type,
                "size": os.path.getsize(os.path.join(root, f)),
            })

    meta = load_meta(review_dir)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Captures - {review_id}</title>
<link rel="stylesheet" href="/static/style.css">
<style>
.captures-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 16px; }}
.capture-card {{ border: 1px solid #e0e0e0; border-radius: 8px; overflow: hidden; background: #fff; }}
.capture-card img {{ width: 100%; height: auto; display: block; cursor: pointer; }}
.capture-card video {{ width: 100%; height: auto; display: block; }}
.capture-card .info {{ padding: 8px 12px; font-size: 0.85rem; color: #616161; }}
.capture-card .info a {{ color: #1565c0; text-decoration: none; }}
.category {{ margin: 24px 0 12px; font-size: 1.1rem; color: #283593; border-bottom: 2px solid #c5cae9; padding-bottom: 4px; }}
</style>
</head>
<body>
<header class="app-header"><div class="container"><h1>WCAG Trusted Tester</h1></div></header>
<main class="container" id="main-content">
<nav class="breadcrumb" aria-label="Breadcrumb">
  <a href="/">Home</a> &rsaquo; <a href="/review/{review_id}/report">Report</a> &rsaquo; <span>Captures</span>
</nav>
<h2>Captured Evidence — {meta.source_url or review_id}</h2>
<p style="color:#616161;font-size:0.9rem;">All screenshots, videos, and data captured during the accessibility review. Click images to view full size. Videos can be played inline.</p>
"""

    for cat_name in sorted(categories.keys()):
        items = categories[cat_name]
        html += f'<h3 class="category">{cat_name} ({len(items)} files)</h3>\n'
        html += '<div class="captures-grid">\n'
        for item in items:
            size_kb = item["size"] / 1024
            size_str = f"{size_kb:.0f} KB" if size_kb < 1024 else f"{size_kb/1024:.1f} MB"
            if item["type"] == "image":
                html += f'''<div class="capture-card">
  <a href="{item['path']}" target="_blank"><img src="{item['path']}" alt="{item['name']}" loading="lazy"></a>
  <div class="info"><a href="{item['path']}" download>{item['name']}</a> ({size_str})</div>
</div>\n'''
            elif item["type"] == "video":
                html += f'''<div class="capture-card">
  <video controls preload="metadata"><source src="{item['path']}"></video>
  <div class="info"><a href="{item['path']}" download>{item['name']}</a> ({size_str})</div>
</div>\n'''
            else:
                html += f'''<div class="capture-card">
  <div class="info" style="padding:16px;"><a href="{item['path']}" target="_blank">{item['name']}</a> ({size_str})</div>
</div>\n'''
        html += '</div>\n'

    html += """
<div class="mt-2 mb-3"><a href="/review/""" + review_id + """/report">&larr; Back to Report</a></div>
</main>
<footer class="app-footer"><div class="container">WCAG Trusted Tester v2.0.0</div></footer>
</body></html>"""

    return HTMLResponse(html)


@app.get("/review/{review_id}/captures/file/{path:path}")
async def serve_capture_file(review_id: str, path: str):
    """Serve a captured file (image, video, JSON, etc.)."""
    review_dir = REVIEWS_DIR / review_id
    captures_dir = (review_dir / "captures").resolve()
    file_path = (review_dir / "captures" / path).resolve()

    if not file_path.exists() or not file_path.is_relative_to(captures_dir):
        return JSONResponse({"error": "File not found"}, status_code=404)

    ext = file_path.suffix.lower()
    mime_map = {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".webm": "video/webm", ".mp4": "video/mp4",
        ".json": "application/json", ".html": "text/html", ".txt": "text/plain",
    }
    media_type = mime_map.get(ext, "application/octet-stream")
    return FileResponse(str(file_path), media_type=media_type)
