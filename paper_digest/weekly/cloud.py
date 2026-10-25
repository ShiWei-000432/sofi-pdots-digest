"""Private GitHub runner with encrypted, durable checkpoints (no API billing).

Only reviewed workflow_dispatch/schedule jobs may invoke this entry point. A private
release stores authenticated ciphertext; credentials never enter Git history.
"""

import base64
import gzip
import hashlib
import io
import json
import os
import signal
import sqlite3
import sys
import tempfile
import time
import uuid
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from cryptography.fernet import Fernet

from .cli import due, load
from .delivery import send
from .provider import CodexCLIProvider
from .report import write_report
from .service import run
from .state import Store

TAG = "private-runtime-state-v1"
MAX_BYTES = 100 * 1024 * 1024


class CloudError(RuntimeError):
    pass


class Vault:
    def __init__(self, repo, token, key):
        self.repo = repo
        self.base = "https://api.github.com/repos/" + repo
        self.session = requests.Session()
        self.session.headers.update(
            Authorization="Bearer " + token,
            Accept="application/vnd.github+json",
            **{"X-GitHub-Api-Version": "2022-11-28"},
        )
        self.cipher = Fernet(key.encode())
        self.release = None
        self.pointer = None
        # Fail closed even if a workflow's YAML guard was accidentally removed.
        if self.api("GET", "").get("private") is not True:
            raise CloudError("Private repository required; no credentials restored")

    def api(self, method, path, payload=None, raw=None):
        url = self.base + path
        if raw is not None:
            url = "https://uploads.github.com/repos/" + self.repo + path
        try:
            response = self.session.request(
                method,
                url,
                json=payload if raw is None else None,
                data=raw,
                headers={"Content-Type": "application/octet-stream"} if raw else {},
                timeout=(15, 120),
                allow_redirects=False,
            )
            if not response.ok:
                raise CloudError(f"GitHub state API HTTP {response.status_code}")
            return response.json() if response.content else None
        except requests.RequestException:
            raise CloudError("GitHub state transport failure") from None

    def locate(self, create=False):
        for page in range(1, 11):
            rows = self.api("GET", f"/releases?per_page=100&page={page}")
            found = [r for r in rows if r["tag_name"] == TAG]
            if found:
                self.release = found[0]
                self.pointer = json.loads(self.release.get("body") or "{}").get(
                    "latest"
                )
                return
            if len(rows) < 100:
                break
        if not create:
            raise CloudError("Persistent state missing; explicit bootstrap required")
        self.release = self.api(
            "POST",
            "/releases",
            {
                "tag_name": TAG,
                "name": "Private encrypted runtime checkpoints",
                "draft": True,
                "body": "{}",
                "target_commitish": "main",
            },
        )

    def download(self, asset):
        # Never forward the GitHub token to a signed storage redirect.
        response = self.session.get(
            self.base + f"/releases/assets/{asset}",
            headers={"Accept": "application/octet-stream"},
            allow_redirects=False,
            timeout=(15, 120),
        )
        if response.status_code == 302:
            location = response.headers["Location"]
            if not location.startswith("https://"):
                raise CloudError("Unsafe storage redirect")
            response = requests.get(location, timeout=(15, 120))
        if response.status_code != 200 or len(response.content) > MAX_BYTES:
            raise CloudError("State download failed or oversized")
        return response.content

    def restore(self, root):
        if not self.pointer:
            raise CloudError("No committed checkpoint; explicit bootstrap required")
        blob = self.download(self.pointer["asset_id"])
        if hashlib.sha256(blob).hexdigest() != self.pointer["sha256"]:
            raise CloudError("Checkpoint digest mismatch")
        unpack(self.cipher.decrypt(blob), root)

    def save(self, root, databases):
        blob = self.cipher.encrypt(pack(root, databases))
        if len(blob) > MAX_BYTES:
            raise CloudError("Checkpoint exceeds 100 MiB; maintenance required")
        rid = self.release["id"]
        current = self.api("GET", f"/releases/{rid}")
        if json.loads(current.get("body") or "{}").get("latest") != self.pointer:
            raise CloudError("Concurrent state writer detected; refusing to overwrite")
        name = "state-" + str(time.time_ns()) + "-" + uuid.uuid4().hex + ".enc"
        asset = self.api("POST", f"/releases/{rid}/assets?name={name}", raw=blob)
        pointer = {"asset_id": asset["id"], "sha256": hashlib.sha256(blob).hexdigest()}
        # Verify uploaded bytes before committing the manifest and allowing SMTP DATA.
        if hashlib.sha256(self.download(asset["id"])).hexdigest() != pointer["sha256"]:
            raise CloudError("Checkpoint read-back verification failed")
        self.api("PATCH", f"/releases/{rid}", {"body": json.dumps({"latest": pointer})})
        observed = self.api("GET", f"/releases/{rid}")
        if json.loads(observed["body"]).get("latest") != pointer:
            raise CloudError("Checkpoint manifest verification failed")
        self.pointer = pointer
        # Keep the latest 8 complete encrypted snapshots; current pointer always survives.
        assets = self.api("GET", f"/releases/{rid}/assets?per_page=100")
        eligible = sorted(
            (
                a
                for a in assets
                if a["name"].startswith("state-") and a["state"] == "uploaded"
            ),
            key=lambda a: a["id"],
            reverse=True,
        )
        for old in eligible[8:]:
            if old["id"] != pointer["asset_id"]:
                try:
                    self.api("DELETE", f"/releases/assets/{old['id']}")
                except CloudError:
                    pass  # Cleanup failure cannot invalidate a committed checkpoint.


def pack(root, databases):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in root.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            rel = path.relative_to(root).as_posix()
            if rel.startswith("codex/") and rel not in {
                "codex/auth.json",
                "codex/config.toml",
            }:
                continue
            if rel.endswith(
                (".sqlite3", ".sqlite3-wal", ".sqlite3-shm", ".lock", ".log")
            ):
                continue
            if "/backups/" in rel or "/sessions/" in rel:
                continue
            archive.write(path, rel)
        for rel, db in databases.items():
            with tempfile.TemporaryDirectory() as temp:
                target = Path(temp) / "snapshot.sqlite3"
                with sqlite3.connect(target) as dest:
                    db.backup(dest)
                archive.write(target, rel)
    with zipfile.ZipFile(io.BytesIO(output.getvalue())) as check:
        if sum(i.file_size for i in check.infolist()) > MAX_BYTES:
            raise CloudError(
                "Expanded checkpoint exceeds 100 MiB; maintenance required"
            )
    return output.getvalue()


def unpack(blob, root):
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        if sum(i.file_size for i in archive.infolist()) > MAX_BYTES:
            raise CloudError("Expanded checkpoint too large")
        for info in archive.infolist():
            path = Path(info.filename)
            if path.is_absolute() or ".." in path.parts or info.is_dir():
                raise CloudError("Unsafe checkpoint member")
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            target.write_bytes(archive.read(info))
            target.chmod(0o600)


def validate_auth(root):
    auth = json.loads((root / "codex/auth.json").read_text())
    if auth.get("auth_mode") != "chatgpt" or not auth.get("tokens", {}).get(
        "refresh_token"
    ):
        raise CloudError("Cloud-specific managed ChatGPT login required")


def initialize(root, seed):
    (root / "codex").mkdir(parents=True, mode=0o700)
    (root / "codex/auth.json").write_text(json.dumps(seed["auth"]))
    (root / "codex/auth.json").chmod(0o600)
    (root / "weekly.toml").write_text(seed["config"])
    (root / "smtp.json").write_text(json.dumps(seed["smtp"]))
    (root / "test-evidence.json").write_text(json.dumps(seed["test_report"]))
    validate_auth(root)
    output = root / "weekly-output"
    output.mkdir()
    store = Store(output / "state.sqlite3")
    for table in ("papers", "aliases", "meta", "outbox"):
        for row in seed["state"][table]:
            placeholders = ",".join("?" for _ in row)
            store.db.execute(f"INSERT INTO {table} VALUES ({placeholders})", row)
    store.db.commit()
    return store


def cloud_test(cfg, store, root):
    if store.get_meta("cloud_test_passed"):
        print("Cloud test already accepted; no duplicate test email")
        return
    report = json.loads((root / "test-evidence.json").read_text())
    report["papers"] = report["papers"][:1]
    paper = report["papers"][0]
    paper["analysis"] = CodexCLIProvider(cfg["analysis"]).summarize(paper)
    report["test"] = True
    out = root / "weekly-output/cloud-test"
    write_report(report, out, cfg["recipient"])
    # Separate outbox keeps this explicitly requested migration test from changing
    # production watermarks or being blocked by the previous local test's paper IDs.
    test_store = cfg["_test_store"]
    send(cfg, test_store, report, out)
    store.meta("cloud_test_passed", True)
    cfg["_checkpoint"]()
    print("Cloud Codex summary validated; SMTP accepted migration test")


def main():
    os.umask(0o077)
    operation = os.getenv("CLOUD_OPERATION", "check")
    if operation not in {"bootstrap", "test", "activate", "pause", "check", "weekly"}:
        raise CloudError("Unsupported operation")
    root = Path(os.environ["RUNNER_TEMP"]) / "sofi-private"
    root.mkdir(mode=0o700, exist_ok=True)
    vault = Vault(
        os.environ["GITHUB_REPOSITORY"],
        os.environ["GITHUB_TOKEN"],
        os.environ["CLOUD_STATE_KEY"],
    )
    vault.locate(create=operation == "bootstrap")
    if operation == "bootstrap" and vault.pointer:
        raise CloudError("Already initialized; will not overwrite refreshed auth/state")
    if operation == "bootstrap":
        seed = json.loads(
            gzip.decompress(base64.b64decode(os.environ["CLOUD_BOOTSTRAP"]))
        )
        store = initialize(root, seed)
    else:
        vault.restore(root)
        store = Store(root / "weekly-output/state.sqlite3")
    validate_auth(root)
    os.environ["CODEX_HOME"] = str(root / "codex")
    # Load only known SMTP names. Secrets never appear on command lines or in output.
    for key, value in json.loads((root / "smtp.json").read_text()).items():
        if key.startswith("SMTP_"):
            os.environ[key] = str(value)
    cfg = load(root / "weekly.toml")
    cfg["analysis"]["executable"] = "codex"
    test_store = Store(root / "weekly-output/cloud-test.sqlite3")
    databases = {
        "weekly-output/state.sqlite3": store.db,
        "weekly-output/cloud-test.sqlite3": test_store.db,
    }

    def checkpoint():
        return vault.save(root, databases)

    cfg["_checkpoint"] = checkpoint
    cfg["_test_store"] = test_store
    cfg["analysis"]["_checkpoint"] = checkpoint
    now = datetime.now(ZoneInfo(cfg["timezone"]))

    # 40 min work + bounded state write-back fits within the workflow's 50 min cap.
    def deadline(*_):
        raise CloudError("Cloud work deadline exceeded")

    signal.signal(signal.SIGALRM, deadline)
    signal.alarm(2400)
    try:
        if operation == "bootstrap":
            store.meta("cloud_active", False)
            checkpoint()
            print("Private encrypted state initialized; schedule remains inactive")
        elif operation == "pause":
            store.meta("cloud_active", False)
        elif operation == "test":
            cloud_test(cfg, store, root)
        elif operation == "activate":
            if not store.get_meta("cloud_test_passed"):
                raise CloudError("Cloud test must pass before activation")
            store.meta("cloud_active", True)
        elif store.get_meta("cloud_active"):
            if store.db.execute(
                "SELECT 1 FROM outbox WHERE status='uncertain'"
            ).fetchone():
                raise CloudError(
                    "Uncertain SMTP outcome; manual reconciliation required"
                )
            if due(now, cfg, store.get_meta("last_sent_at")):
                week = (
                    (now - timedelta(days=(now.weekday() - 4) % 7)).date().isoformat()
                )
                attempts = store.get_meta("cloud_attempts", {})
                if attempts.get("week") != week:
                    attempts = {"week": week, "count": 0}
                if attempts["count"] >= 3:
                    raise CloudError("Weekly retry budget exhausted; review required")
                attempts["count"] += 1
                store.meta("cloud_attempts", attempts)
                checkpoint()
                report, out = run(cfg, store, now, root / "weekly-output")
                if report["analysis_failures"]:
                    raise CloudError("Analysis incomplete; candidates retained")
                if not report["complete"] and not any(
                    s["count"] > 0 and s["status"] in ("ok", "partial")
                    for s in report["sources"]
                ):
                    raise CloudError(
                        "Sources unavailable; not recording an empty success"
                    )
                checkpoint()
                send(cfg, store, report, out)
                print("Weekly report accepted by SMTP; inbox delivery unverified")
            else:
                print("No delivery due; cloud checkpoint and state healthy")
        else:
            print("Cloud sending paused; no message sent")
    finally:
        signal.alarm(0)
        try:
            checkpoint()
        finally:
            test_store.close()
            store.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # No exception bodies/HTTP bodies: authentication material can occur there.
        reason = str(exc) if isinstance(exc, CloudError) else type(exc).__name__
        print("Cloud run failed: " + reason, file=sys.stderr)
        raise SystemExit(1) from None
