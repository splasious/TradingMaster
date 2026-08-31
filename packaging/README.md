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

## Building the full installer

```
# 1. Backend exe
cd backend
.venv/Scripts/python.exe -m PyInstaller ../packaging/tradingmaster-backend.spec --distpath ../packaging/dist --workpath ../packaging/build

# 2. Launcher exe
.venv/Scripts/python.exe -m PyInstaller --name tradingmaster-launcher --onefile --console --distpath ../packaging/dist --workpath ../packaging/build --specpath ../packaging ../packaging/launcher.py

# 3. Frontend production bundle (next.config.ts has output: "standalone")
cd ../frontend
npm run build
mkdir -p ../packaging/dist/frontend
cp -r .next/standalone/. ../packaging/dist/frontend/
cp -r .next/static ../packaging/dist/frontend/.next/static
cp -r public ../packaging/dist/frontend/public

# 4. Portable Node runtime (just the exe -- Node ships as one self-contained binary)
mkdir -p ../packaging/dist/node
cp "/c/Program Files/nodejs/node.exe" ../packaging/dist/node/node.exe

# 5. Installer (Inno Setup)
cd ../packaging
"C:\Users\<you>\AppData\Local\Programs\Inno Setup 6\ISCC.exe" installer.iss
# -> packaging/installer_output/TradingMasterSetup.exe
```

`launcher.py` starts the backend exe and `node/node.exe frontend/server.js`
as child processes on `localhost` (not `127.0.0.1` -- the backend's CORS
config only allows `http://localhost:3000`, and browsers treat those as
different origins), waits for both health checks, opens the default
browser, and tears both down together on exit or Ctrl+C.

## Status

Fully verified end to end, including running the *actual compiled
installer* (`TradingMasterSetup.exe /VERYSILENT`) in an isolated directory
with no dev servers running, then logging in through the real browser
flow against the installed app:

- [x] Backend freezes to a single exe; migrations, seeding, login, and the
      sandboxed Python-strategy subprocess all verified working.
- [x] Frontend production bundle (Next.js `output: "standalone"`) + portable
      Node runtime -- 23MB standalone build vs. 588MB full node_modules.
- [x] Launcher starts both, waits for health, opens the browser, cleans up
      on exit.
- [x] Inno Setup installer (~83MB) -- installs, auto-launches, real login
      flow confirmed working from a clean install directory.

Known gaps for a wider release (not blocking for personal local use):
- PostgreSQL is assumed already installed and running locally, per the
  scoping decision for this pass -- the app falls back to a bundled SQLite
  file if no `DATABASE_URL` is configured via `.env` next to the exe.
- No code signing -- Windows SmartScreen will warn on first run.
- Default JWT/encryption secrets in `.env.example` are dev-only; anyone
  using this beyond local trial use should generate real ones (see that
  file's comments).
