"""Single frozen-exe entrypoint for the packaged desktop build.

Two responsibilities live in one exe on purpose: PyInstaller's onefile
build can only reasonably ship one Python interpreter, and
app/services/strategy/sandbox.py normally isolates each strategy
execution by spawning `sys.executable -m app.services.strategy.sandbox_worker`
as a fresh subprocess -- which doesn't work once frozen, since the exe is
no longer a `python.exe` that understands `-m`. Instead the frozen exe
re-invokes *itself* with `--sandbox-worker`, and this entrypoint dispatches
to the worker's main() instead of starting the server. Dev mode (running
from source, not frozen) is untouched -- sandbox.py only takes this path
when `sys.frozen` is set.
"""
import sys


def main() -> None:
    if "--sandbox-worker" in sys.argv:
        from app.services.strategy.sandbox_worker import main as worker_main
        worker_main()
        return

    import logging

    import uvicorn
    from alembic import command
    from alembic.config import Config

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logger = logging.getLogger("packaging.backend_entry")

    logger.info("Applying database migrations...")
    alembic_cfg = Config(_resource_path("alembic.ini"))
    alembic_cfg.set_main_option("script_location", _resource_path("alembic"))
    command.upgrade(alembic_cfg, "head")
    logger.info("Migrations up to date.")

    import asyncio

    from app.seed import seed

    logger.info("Seeding roles/admin account (idempotent)...")
    asyncio.run(seed())

    from app.main import app

    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")


def _resource_path(relative: str) -> str:
    import os

    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative)


if __name__ == "__main__":
    main()
