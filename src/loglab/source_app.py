"""Form dan route login Source (FastAPI). Keputusan auth ada di SourceService.

Route mengikuti kontrak 1.0.0: GET /login, POST /login (form),
POST /api/v1/login (JSON untuk alat uji), GET /health.
Form dan JSON memakai SourceService yang sama, jadi format event-nya sama.
"""
import html
import json
from contextlib import AbstractAsyncContextManager
from typing import Any, Callable, Optional

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from loglab.config import Settings
from loglab.source_service import SourceService

MAX_USERNAME = 64
MAX_PASSWORD = 128
MAX_JSON_BODY = 4096
SERVICE_NAME = "loglab-source"

LOGIN_FORM = (
    '<form method="post" action="/login">'
    '<p><label>Username <input name="username" maxlength="64" '
    'autocomplete="username"></label></p>'
    '<p><label>Password <input name="password" type="password" maxlength="128" '
    'autocomplete="current-password"></label></p>'
    '<p><button type="submit">Login</button></p>'
    "</form>"
)


def _page(title: str, body: str) -> str:
    safe_title = html.escape(title)
    return (
        '<!doctype html><html lang="id"><head><meta charset="utf-8">'
        f"<title>{safe_title}</title></head><body><h1>{safe_title}</h1>"
        f"{body}</body></html>"
    )


def _valid(username: Any, password: Any) -> bool:
    return (
        isinstance(username, str)
        and isinstance(password, str)
        and 0 < len(username) <= MAX_USERNAME
        and 0 < len(password) <= MAX_PASSWORD
    )


def _client_id(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def create_app(
    settings: Settings,
    service: Optional[SourceService] = None,
    lifespan: Optional[Callable[[FastAPI], AbstractAsyncContextManager]] = None,
) -> FastAPI:
    service = service or SourceService(settings)
    app = FastAPI(
        title="LogLab Source",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.get("/")
    def root() -> RedirectResponse:
        return RedirectResponse("/login", status_code=303)

    @app.get("/login", response_class=HTMLResponse)
    def login_page() -> str:
        return _page("Login uji LogLab", LOGIN_FORM)

    @app.get("/health")
    def health() -> dict:
        # liveness saja: tidak membuktikan outbox selesai atau Monitor tersedia
        return {
            "service": SERVICE_NAME,
            "state": "ready",
            "contract_version": settings.contract_version,
        }

    @app.post("/login", response_class=HTMLResponse)
    def login_form(
        request: Request,
        username: str = Form(""),
        password: str = Form(""),
    ) -> HTMLResponse:
        if not _valid(username, password):
            service.count_request_rejected()
            return HTMLResponse(
                _page(
                    "Permintaan tidak valid",
                    "<p>Username dan password wajib diisi.</p>" + LOGIN_FORM,
                ),
                status_code=400,
            )
        is_success, _event = service.login(username, password, _client_id(request))
        if is_success:
            return HTMLResponse(
                _page("Login berhasil", f"<p>Selamat datang, {html.escape(username)}.</p>")
            )
        return HTMLResponse(
            _page("Login gagal", "<p>Username atau password salah.</p>" + LOGIN_FORM),
            status_code=401,
        )

    @app.post("/api/v1/login")
    async def login_api(request: Request) -> JSONResponse:
        username: Any = None
        password: Any = None
        declared = request.headers.get("content-length", "")
        too_big = declared.isdigit() and int(declared) > MAX_JSON_BODY
        if not too_big:
            body = await request.body()
            if len(body) <= MAX_JSON_BODY:
                try:
                    data = json.loads(body)
                except ValueError:
                    data = None
                if isinstance(data, dict):
                    username = data.get("username")
                    password = data.get("password")
        if not _valid(username, password):
            await run_in_threadpool(service.count_request_rejected)
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        is_success, _event = await run_in_threadpool(
            service.login, username, password, _client_id(request)
        )
        if is_success:
            return JSONResponse({"result": "success"})
        return JSONResponse({"result": "failure"}, status_code=401)

    return app
