"""Menjalankan Source dengan HTTPS. Konfigurasi dari LOGLAB_* (lihat kontrak)."""
import uvicorn

from loglab.config import Settings
from loglab.source_app import create_app


def main() -> None:
    settings = Settings.from_env("source")
    app = create_app(settings)
    uvicorn.run(
        app,
        host=settings.bind_host,
        port=settings.port,
        ssl_certfile=str(settings.certfile),
        ssl_keyfile=str(settings.keyfile),
    )


if __name__ == "__main__":
    main()
