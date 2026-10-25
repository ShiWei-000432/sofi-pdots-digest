import json
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from paper_digest.weekly.cli import load
from paper_digest.weekly.state import Store
from paper_digest.weekly.supervisor import backup, inspect_state, supervise

ROOT = Path(__file__).resolve().parents[1]


class SupervisorTests(unittest.TestCase):
    def test_readonly_health_does_not_mutate_active_smtp(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            store = Store(root / "state.sqlite3")
            store.db.execute(
                "INSERT INTO outbox VALUES (?,?,?,?,?)",
                ("k", "m", "sending", "{}", None),
            )
            store.db.commit()
            now = datetime.now(ZoneInfo("Asia/Shanghai"))
            info = inspect_state(root, load(ROOT / "weekly.example.toml"), now)
            self.assertEqual(info["uncertain_deliveries"], 1)
            self.assertEqual(
                store.db.execute("SELECT status FROM outbox").fetchone()[0], "sending"
            )
            store.close()

    def test_backup_is_readable_and_daily(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            store = Store(root / "state.sqlite3")
            store.meta("sentinel", "retained")
            now = datetime.now(ZoneInfo("Asia/Shanghai"))
            backup(root, now)
            backup(root, now)
            files = list((root / "backups").glob("*.sqlite3"))
            self.assertEqual(len(files), 1)
            restored = Store(files[0])
            self.assertEqual(restored.get_meta("sentinel"), "retained")
            restored.close()
            store.close()

    def test_timeout_marks_failure_and_terminates_process_group(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cfg = root / "weekly.toml"
            cfg.write_text((ROOT / "weekly.example.toml").read_text())
            proc = Mock(pid=123)
            proc.wait.side_effect = [subprocess.TimeoutExpired("worker", 1), 0]
            with (
                patch(
                    "paper_digest.weekly.supervisor.subprocess.Popen", return_value=proc
                ),
                patch("paper_digest.weekly.supervisor.os.killpg") as kill,
            ):
                self.assertEqual(supervise(cfg), 124)
                kill.assert_called_once()
            health = json.loads((root / "weekly-output/health.json").read_text())
            self.assertEqual(health["status"], "failed")
            self.assertEqual(health["error"], "runtime_limit_exceeded")

    def test_all_inherited_jobs_are_guarded(self):
        import re

        for path in (ROOT / ".github/workflows").glob("*.yml"):
            if path.name == "weekly-ci.yml":
                continue
            jobs = path.read_text().split("jobs:\n", 1)[1]
            for block in re.split(r"\n(?=  [\w-]+:\n)", jobs):
                self.assertIn(
                    "github.repository == 'X-PG13/paper-digest'", block, path.name
                )
