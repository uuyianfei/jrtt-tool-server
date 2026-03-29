import argparse
import os
import signal
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DEFAULT_PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"


def stream_output(prefix: str, pipe):
    try:
        for line in iter(pipe.readline, ""):
            if not line:
                break
            text = f"[{prefix}] {line.rstrip()}"
            try:
                print(text)
            except UnicodeEncodeError:
                fallback = text.encode("gbk", errors="replace").decode("gbk", errors="replace")
                print(fallback)
    finally:
        pipe.close()


def start_articles_worker(python_bin: str):
    env = os.environ.copy()
    env["WORKER_ROLE"] = "author-articles-local"
    env["AUTHOR_COLLECT_JOB_ENABLED"] = "false"
    env["AUTHOR_ARTICLES_JOB_ENABLED"] = "true"
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
    parser = argparse.ArgumentParser(description="Start local articles-only crawler worker")
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

    proc = start_articles_worker(python_bin)
    print(f"Started articles-only worker pid={proc.pid}")
    print("Press Ctrl+C to stop.")

    try:
        stream_output("articles", proc.stdout)
        code = proc.wait()
        print(f"Worker exited code={code}")
    except KeyboardInterrupt:
        print("\nStopping worker...")
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
        print("Worker stopped.")


if __name__ == "__main__":
    main()
