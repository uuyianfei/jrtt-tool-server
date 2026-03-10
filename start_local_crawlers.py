import argparse
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DEFAULT_PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"


def stream_output(prefix: str, pipe):
    try:
        for line in iter(pipe.readline, ""):
            if not line:
                break
            print(f"[{prefix}] {line.rstrip()}")
    finally:
        pipe.close()


def start_worker(python_bin: str, role: str, collect_enabled: bool, articles_enabled: bool):
    env = os.environ.copy()
    env["WORKER_ROLE"] = role
    env["AUTHOR_COLLECT_JOB_ENABLED"] = "true" if collect_enabled else "false"
    env["AUTHOR_ARTICLES_JOB_ENABLED"] = "true" if articles_enabled else "false"
    env["CRAWL_JOB_ENABLED"] = "false"
    env["PYTHONUNBUFFERED"] = "1"

    cmd = [python_bin, "-u", "run_crawler.py"]
    return subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def main():
    parser = argparse.ArgumentParser(description="Start local dual crawler workers")
    parser.add_argument(
        "--python",
        default=str(DEFAULT_PYTHON),
        help="Python executable path (default: .venv/Scripts/python.exe)",
    )
    args = parser.parse_args()

    python_bin = args.python
    if not Path(python_bin).exists():
        print(f"Python not found: {python_bin}")
        print("Use --python to specify a valid interpreter path.")
        sys.exit(1)

    collect_proc = start_worker(
        python_bin=python_bin,
        role="author-collect-local",
        collect_enabled=True,
        articles_enabled=False,
    )
    articles_proc = start_worker(
        python_bin=python_bin,
        role="author-articles-local",
        collect_enabled=False,
        articles_enabled=True,
    )

    threads = [
        threading.Thread(target=stream_output, args=("collect", collect_proc.stdout), daemon=True),
        threading.Thread(target=stream_output, args=("articles", articles_proc.stdout), daemon=True),
    ]
    for t in threads:
        t.start()

    print("Started local crawler workers:")
    print(f"  collect  pid={collect_proc.pid}")
    print(f"  articles pid={articles_proc.pid}")
    print("Press Ctrl+C to stop both workers.")

    try:
        while True:
            collect_code = collect_proc.poll()
            articles_code = articles_proc.poll()
            if collect_code is not None or articles_code is not None:
                print(f"Worker exited: collect={collect_code}, articles={articles_code}")
                break
            signal.pause() if hasattr(signal, "pause") else threading.Event().wait(1.0)
    except KeyboardInterrupt:
        print("\nStopping workers...")
    finally:
        for proc in (collect_proc, articles_proc):
            if proc.poll() is None:
                proc.terminate()
        for proc in (collect_proc, articles_proc):
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
        print("Workers stopped.")


if __name__ == "__main__":
    main()
