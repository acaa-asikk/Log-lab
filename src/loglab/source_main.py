"""Entrypoint Source: loglab.source_main:app (kontrak 1.0.0).

Dijalankan oleh systemd lewat Uvicorn; bind, port, dan path TLS dibaca dari env
oleh launcher. Aplikasi login dan satu sender berjalan di proses yang sama;
kunci sender_lock menjamin hanya satu sender aktif.
"""
import contextlib
import logging
import threading
from typing import Callable, Optional

from fastapi import FastAPI

from loglab.config import Settings
from loglab.sender import HttpTransport, Sender, SenderAlreadyRunning
from loglab.source_app import create_app

logger = logging.getLogger("loglab.sender")


def _run_sender(sender: Sender, stop: threading.Event, poll_interval: float) -> None:
    while not stop.is_set():
        try:
            sender.run_forever(stop, poll_interval=poll_interval)
        except Exception:  # sender tidak boleh mati diam-diam
            logger.exception("sender error; mencoba lagi")
            stop.wait(2.0)


def make_lifespan(settings: Settings, transport: Callable, poll_interval: float = 0.5):
    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        stop = threading.Event()
        thread: Optional[threading.Thread] = None
        sender: Optional[Sender] = Sender(settings, transport)
        try:
            sender.__enter__()
        except SenderAlreadyRunning:
            logger.warning("sender lain sudah aktif; aplikasi jalan tanpa sender")
            sender = None
        if sender is not None:
            thread = threading.Thread(
                target=_run_sender,
                args=(sender, stop, poll_interval),
                name="loglab-sender",
                daemon=True,
            )
            thread.start()
        try:
            yield
        finally:
            stop.set()
            if thread is not None:
                thread.join(timeout=10)
            if sender is not None:
                sender.__exit__(None, None, None)

    return lifespan


def build_app(
    settings: Optional[Settings] = None,
    transport: Optional[Callable] = None,
    poll_interval: float = 0.5,
) -> FastAPI:
    # from_env memvalidasi env terhadap kontrak (key tak dikenal, nilai tetap,
    # secret placeholder, LOGLAB_ROLE salah) dan gagal sebelum startup.
    settings = settings or Settings.from_env("source")
    transport = transport or HttpTransport(
        base_url=settings.monitor_url, ca_bundle=settings.ca_bundle
    )
    return create_app(
        settings, lifespan=make_lifespan(settings, transport, poll_interval)
    )


_app: Optional[FastAPI] = None


def __getattr__(name: str):
    # `app` dibangun saat pertama diminta (Uvicorn), bukan saat modul di-import,
    # supaya tes dan alat lain bisa meng-import modul tanpa env produksi.
    global _app
    if name == "app":
        if _app is None:
            _app = build_app()
        return _app
    raise AttributeError(name)
