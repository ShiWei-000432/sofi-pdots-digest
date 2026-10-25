import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet, InvalidToken

from paper_digest.weekly.cli import load
from paper_digest.weekly.cloud import CloudError, Vault, manual_request, pack, unpack
from paper_digest.weekly.delivery import send
from paper_digest.weekly.state import Store
from tests.test_weekly import ROOT


class CloudTests(unittest.TestCase):
    def test_manual_run_requires_dispatch_and_stable_run_id(self):
        with patch.dict(
            os.environ, {"GITHUB_EVENT_NAME": "schedule", "GITHUB_RUN_ID": "123"}
        ):
            with self.assertRaises(CloudError):
                manual_request("manual")
        with patch.dict(
            os.environ,
            {"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_RUN_ID": "123"},
        ):
            self.assertEqual(manual_request("manual"), "cloud_manual_123")
            self.assertIsNone(manual_request("check"))

    def test_unpublished_draft_tag_can_be_replaced_by_github(self):
        with patch.object(Vault, "api", return_value={"private": True}):
            vault = Vault("owner/private", "test", Fernet.generate_key().decode())
        release = {
            "id": 42,
            "tag_name": "untagged-example",
            "draft": True,
            "name": "Private encrypted runtime checkpoints",
            "body": json.dumps({"latest": {"asset_id": 7}}),
        }
        with patch.object(Vault, "api", return_value=[release]):
            vault.locate()
        self.assertEqual(vault.release["id"], 42)
        self.assertEqual(vault.pointer["asset_id"], 7)

    def test_public_repository_rejected_before_credentials(self):
        with patch.object(Vault, "api", return_value={"private": False}):
            with self.assertRaises(CloudError):
                Vault("owner/public", "test-token", Fernet.generate_key().decode())

    def test_checkpoint_captures_committed_wal_and_auth_not_logs(self):
        with (
            tempfile.TemporaryDirectory() as folder,
            tempfile.TemporaryDirectory() as restored,
        ):
            root = Path(folder)
            (root / "codex").mkdir()
            (root / "codex/auth.json").write_text('{"test":true}')
            (root / "codex/private.log").write_text("not archived")
            store = Store(root / "state.sqlite3")
            store.meta("committed", True)
            cipher = Fernet(Fernet.generate_key())
            blob = cipher.encrypt(pack(root, {"state.sqlite3": store.db}))
            self.assertNotIn(b'"test"', blob)
            unpack(cipher.decrypt(blob), Path(restored))
            other = Store(Path(restored) / "state.sqlite3")
            self.assertTrue(other.get_meta("committed"))
            self.assertFalse((Path(restored) / "codex/private.log").exists())
            other.close()
            store.close()
            with self.assertRaises(InvalidToken):
                Fernet(Fernet.generate_key()).decrypt(blob)

    def test_zip_traversal_is_rejected(self):
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as z:
            z.writestr("../escape", "bad")
        with tempfile.TemporaryDirectory() as folder, self.assertRaises(CloudError):
            unpack(payload.getvalue(), Path(folder))

    def test_checkpoint_failure_stops_smtp_data(self):
        cfg = load(ROOT / "weekly.example.toml")
        env = dict(
            SMTP_HOST="smtp.example.com",
            SMTP_PORT="465",
            SMTP_SECURITY="ssl",
            SMTP_USERNAME="test@example.com",
            SMTP_FROM="test@example.com",
            SMTP_PASSWORD="test",
        )
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, env):
            root = Path(folder)
            (root / "preview.eml").write_text(
                "Subject: test\nTo: reader@example.com\n\nbody"
            )
            store = Store(root / "state.sqlite3")

            def fail():
                raise CloudError("checkpoint unavailable")

            cfg["_checkpoint"] = fail
            stages = []

            def smtp(_cfg, _msg, before_data, accepted):
                before_data()
                stages.append("SMTP DATA")
                accepted()

            with patch(
                "paper_digest.weekly.delivery.send_weekly_message", side_effect=smtp
            ):
                with self.assertRaises(RuntimeError):
                    send(cfg, store, {"date": "2026-10-25", "papers": []}, root)
            self.assertEqual(stages, [])
            self.assertIsNone(store.get_meta("last_sent_at"))
            self.assertEqual(
                store.db.execute("SELECT status FROM outbox").fetchone()[0], "uncertain"
            )
            store.close()

    def test_manifest_conflict_aborts_before_upload(self):
        with patch.object(Vault, "api", return_value={"private": True}):
            vault = Vault("owner/private", "test", Fernet.generate_key().decode())
        vault.release = {"id": 1}
        vault.pointer = {"asset_id": 1}
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(
                vault,
                "api",
                return_value={"body": json.dumps({"latest": {"asset_id": 2}})},
            ) as api,
        ):
            with self.assertRaises(CloudError):
                vault.save(Path(folder), {})
            self.assertEqual(api.call_count, 1)
