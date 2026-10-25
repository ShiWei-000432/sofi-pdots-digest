"""Bounded launchd entry point with durable health and consistent SQLite backups."""

import fcntl
import json
import os
import signal
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .cli import due, load


def write_health(root, payload):
    tmp = root / "health.json.tmp"
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    tmp.replace(root / "health.json")


def backup(root, now):
    path = root / "state.sqlite3"
    if not path.exists():
        return
    folder = root / "backups"
    folder.mkdir(exist_ok=True)
    target = folder / (now.strftime("%Y-%m-%d") + ".sqlite3")
    if target.exists():
        return
    with sqlite3.connect(path) as source, sqlite3.connect(target) as dest:
        source.backup(dest)
    for old in sorted(folder.glob("*.sqlite3"))[:-7]:
        old.unlink()


def inspect_state(root, cfg, now):
    path = root / "state.sqlite3"
    if not path.exists():
        return {"delivery_due": True, "uncertain_deliveries": 0}
    # Read-only inspection must never change sending to uncertain while a job runs.
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        row = db.execute("SELECT value FROM meta WHERE key='last_sent_at'").fetchone()
        last = json.loads(row[0]) if row else None
        pending = db.execute(
            "SELECT count(*) FROM outbox WHERE status IN ('uncertain','sending')"
        ).fetchone()[0]
    return {
        "last_smtp_accepted_at": last,
        "delivery_due": due(now, cfg, last),
        "uncertain_deliveries": pending,
    }


def supervise(config_path):
    config_path = Path(config_path).resolve()
    cfg = load(config_path)
    root = (config_path.parent / cfg["output_dir"]).resolve()
    root.mkdir(parents=True, exist_ok=True)
    now = datetime.now(ZoneInfo(cfg["timezone"]))
    with (root / "supervisor.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        health = {
            "started_at": now.isoformat(),
            "status": "running",
            **inspect_state(root, cfg, now),
        }
        write_health(root, health)
        code = 1
        try:
            backup(root, now)
            cmd = [
                sys.executable,
                "-m",
                "paper_digest.weekly",
                "--config",
                str(config_path),
                "tick",
            ]
            with (root / "worker.log").open("a") as log:
                proc = subprocess.Popen(
                    cmd,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    cwd=config_path.parent,
                    start_new_session=True,
                )
                try:
                    code = proc.wait(
                        timeout=cfg.get("schedule", {}).get("max_runtime_seconds", 5400)
                    )
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGTERM)
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(proc.pid, signal.SIGKILL)
                        proc.wait()
                    code = 124
                    health["error"] = "runtime_limit_exceeded"
        except Exception as exc:
            health["error"] = type(exc).__name__
        finally:
            end = datetime.now(ZoneInfo(cfg["timezone"]))
            health.update(inspect_state(root, cfg, end))
            health.update(finished_at=end.isoformat(), exit_code=code)
            health["status"] = (
                "failed"
                if code
                else "deferred"
                if health["delivery_due"]
                else "healthy"
            )
            write_health(root, health)
        return code


if __name__ == "__main__":
    raise SystemExit(supervise(sys.argv[1]))
