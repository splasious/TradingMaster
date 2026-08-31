# Desktop packaging

Turns the backend into a standalone Windows exe so it can run without a
Python install. Build-time only -- `pyinstaller` is not a runtime
dependency of the app, so it isn't in `backend/requirements.txt`; install
it into the backend venv just to build:

```
cd backend
.venv/Scripts/python.exe -m pip install pyinstaller
.venv/Scripts/python.exe -m PyInstaller ../packaging/tradingmaster-backend.spec --distpath ../packaging/dist --workpath ../packaging/build
```

The exe lands at `packaging/dist/tradingmaster-backend.exe` (gitignored --
rebuild it, don't commit it). Run it from an empty directory; on first
launch it applies Alembic migrations and seeds roles/admin account into
whatever `DATABASE_URL` it finds (a `.env` file next to the exe, same as
the dev server -- defaults to a local SQLite file if none is present),
then serves the API on `127.0.0.1:8000`.

## Why one exe, not two

`app/services/strategy/sandbox.py` isolates every strategy execution by
spawning a **fresh subprocess** of the Python interpreter running
`-m app.services.strategy.sandbox_worker`. A frozen exe's `sys.executable`
*is* the exe itself, not a `python.exe` that understands `-m` -- so
`backend_entry.py` re-invokes the same exe with `--sandbox-worker` instead,
and `sandbox.py` detects `sys.frozen` to pick the right invocation. This
is the piece that was actually verified working (frozen exe spawning
itself, running RestrictedPython, returning a real signal) before the rest
of this packaging effort was worth pursuing.

## Status

- [x] Backend freezes to a single exe; migrations, seeding, login, and the
      sandboxed Python-strategy subprocess all verified working.
- [ ] Frontend production bundle + portable Node runtime.
- [ ] Launcher that starts both and opens the browser.
- [ ] Inno Setup installer.
