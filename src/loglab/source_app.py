"""Form dan route login Source (FastAPI). Keputusan auth ada di SourceService."""
import html
from typing import Optional

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse

from loglab.config import Settings
from loglab.source_service import SourceService

MAX_USERNAME = 64
MAX_PASSWORD = 128

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


def create_app(settings: Settings, service: Optional[SourceService] = None) -> FastAPI:
    service = service or SourceService(settings)
    app = FastAPI(
        title="LogLab Source", docs_url=None, redoc_url=None, openapi_url=None
    )

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return _page("Login uji LogLab", LOGIN_FORM)

    @app.get("/healthz")
    def healthz() -> dict:
        # hanya status proses; detail lain tidak dibuka
        return {"status": "ok"}

    @app.post("/login", response_class=HTMLResponse)
    def login(
        request: Request,
        username: str = Form(""),
        password: str = Form(""),
    ) -> HTMLResponse:
        if (
            not username
            or not password
            or len(username) > MAX_USERNAME
            or len(password) > MAX_PASSWORD
        ):
            service.count_request_rejected()
            return HTMLResponse(
                _page(
                    "Permintaan tidak valid",
                    "<p>Username dan password wajib diisi.</p>" + LOGIN_FORM,
                ),
                status_code=400,
            )
        client_id = request.client.host if request.client else "unknown"
        is_success, _event = service.login(username, password, client_id)
        if is_success:
            return HTMLResponse(
                _page("Login berhasil", f"<p>Selamat datang, {html.escape(username)}.</p>")
            )
        return HTMLResponse(
            _page("Login gagal", "<p>Username atau password salah.</p>" + LOGIN_FORM),
            status_code=401,
        )

    return app
