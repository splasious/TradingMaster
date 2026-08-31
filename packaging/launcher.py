"""Desktop launcher: starts the backend exe and the bundled Next.js
standalone server as child processes, waits for both to report healthy,
opens the default browser, and tears both down cleanly on exit.

Expects this layout next to the launcher exe (see packaging/README.md for
how it's built):
    tradingmaster-backend.exe
    node/node.exe
    frontend/server.js, frontend/.next/, frontend/public/, frontend/node_modules/
"""
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser

BACKEND_PORT = 8000
FRONTEND_PORT = 3000
HEALTH_TIMEOUT_SECONDS = 60


def _base_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _wait_healthy(url: str, timeout: float, label: str) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    print(f"{label} is up.")
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(1)
    print(f"{label} did not respond within {timeout:.0f}s.")
    return False


def main() -> None:
    base = _base_dir()
    backend_exe = os.path.join(base, "tradingmaster-backend.exe")
    node_exe = os.path.join(base, "node", "node.exe")
    frontend_server = os.path.join(base, "frontend", "server.js")

    for path, label in [(backend_exe, "tradingmaster-backend.exe"), (node_exe, "node/node.exe"), (frontend_server, "frontend/server.js")]:
        if not os.path.exists(path):
            print(f"Missing {label} at {path} -- packaging is incomplete.")
            input("Press Enter to exit...")
            sys.exit(1)

    print("Starting TradingMaster backend...")
    backend_proc = subprocess.Popen([backend_exe], cwd=base)

    print("Starting TradingMaster frontend...")
    frontend_env = dict(os.environ, PORT=str(FRONTEND_PORT), HOSTNAME="localhost")
    frontend_proc = subprocess.Popen([node_exe, frontend_server], cwd=os.path.join(base, "frontend"), env=frontend_env)

    try:
        backend_ok = _wait_healthy(f"http://localhost:{BACKEND_PORT}/api/v1/system/health", HEALTH_TIMEOUT_SECONDS, "Backend")
        frontend_ok = _wait_healthy(f"http://localhost:{FRONTEND_PORT}", HEALTH_TIMEOUT_SECONDS, "Frontend")

        if backend_ok and frontend_ok:
            webbrowser.open(f"http://localhost:{FRONTEND_PORT}")
            print("\nTradingMaster is running. Close this window to stop it.")
        else:
            print("\nTradingMaster failed to start cleanly -- check the messages above.")

        while True:
            if backend_proc.poll() is not None:
                print("Backend process exited -- stopping.")
                break
            if frontend_proc.poll() is not None:
                print("Frontend process exited -- stopping.")
                break
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping TradingMaster...")
    finally:
        for proc, name in [(frontend_proc, "frontend"), (backend_proc, "backend")]:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()


if __name__ == "__main__":
    main()
