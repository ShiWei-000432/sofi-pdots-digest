"""Durable, idempotent SMTP outbox. Unknown DATA outcomes need reconciliation."""

import json
import os
import smtplib
from datetime import UTC, datetime
from email import policy
from email.parser import BytesParser
from email.utils import format_datetime, make_msgid

from paper_digest.config import EmailConfig
from paper_digest.email_delivery import send_weekly_message

from .model import fingerprint


def email_config(cfg):
    e = cfg["email"]
    required = ["host", "port", "username", "password", "from", "security"]
    missing = [e[k + "_env"] for k in required if not os.getenv(e[k + "_env"])]
    if missing:
        raise ValueError("缺少本地环境变量：" + ", ".join(missing))
    security = os.environ[e["security_env"]]
    if security not in ("ssl", "starttls"):
        raise ValueError("SMTP_SECURITY 必须为 ssl 或 starttls")
    return EmailConfig(
        smtp_host=os.environ[e["host_env"]],
        smtp_port=int(os.environ[e["port_env"]]),
        username=os.environ[e["username_env"]],
        password_env=e["password_env"],
        from_address=os.environ[e["from_env"]],
        to_addresses=[cfg["recipient"]],
        use_tls=security == "ssl",
        use_starttls=security == "starttls",
        subject_prefix="",
        skip_if_empty=False,
    )


def send(cfg, store, report, out):
    config = email_config(
        cfg
    )  # Known missing settings are safe retryable preflight errors.
    key = fingerprint(
        {
            "recipient": cfg["recipient"],
            "date": report["date"],
            "papers": sorted(p["id"] for p in report["papers"]),
        }
    )
    row = store.db.execute(
        "SELECT message_id,status,payload FROM outbox WHERE id=?", (key,)
    ).fetchone()
    if row and row[1] == "accepted":
        return "already_accepted"
    if row and row[1] in ("uncertain", "sending"):
        raise RuntimeError(
            "SMTP结果不确定，请核对发件箱和 Message-ID；禁止自动重发：" + row[0]
        )
    for paper in report["papers"]:
        previous = store.db.execute(
            "SELECT sent_at FROM papers WHERE id=?", (paper["id"],)
        ).fetchone()
        if previous and previous[0]:
            raise RuntimeError(
                "归档包含其他已接受邮件中的论文，请重新生成周报；未发送重复内容"
            )
    message_id = (
        row[0] if row else make_msgid(domain=config.from_address.split("@")[-1])
    )
    message = BytesParser(policy=policy.SMTP).parsebytes(
        (out / "preview.eml").read_bytes()
    )
    message["From"] = config.from_address
    message["Message-ID"] = message_id
    message["Date"] = format_datetime(datetime.now(UTC))
    payload = {"paper_ids": [p["id"] for p in report["papers"]], "archive": str(out)}
    store.db.execute(
        "INSERT OR REPLACE INTO outbox VALUES (?,?,?,?,NULL)",
        (key, message_id, "prepared", json.dumps(payload)),
    )
    store.db.commit()

    def data():
        store.db.execute("UPDATE outbox SET status='sending' WHERE id=?", (key,))
        store.db.commit()

    def accepted():
        now = datetime.now(UTC).isoformat()
        with store.db:
            store.db.execute(
                "UPDATE outbox SET status='accepted',error=NULL WHERE id=?", (key,)
            )
            for pid in payload["paper_ids"]:
                store.db.execute("UPDATE papers SET sent_at=? WHERE id=?", (now, pid))
            store.db.execute(
                "INSERT OR REPLACE INTO meta VALUES (?,?)",
                ("last_sent_at", json.dumps(now)),
            )

    try:
        send_weekly_message(config, message, data, accepted)
    except Exception as exc:
        status = store.db.execute(
            "SELECT status FROM outbox WHERE id=?", (key,)
        ).fetchone()[0]
        if status == "accepted":
            return "accepted"
        # Explicit negative SMTP reply means not accepted; disconnect during DATA is ambiguous.
        definitive = isinstance(
            exc,
            (
                smtplib.SMTPAuthenticationError,
                smtplib.SMTPRecipientsRefused,
                smtplib.SMTPSenderRefused,
                smtplib.SMTPDataError,
            ),
        )
        outcome = "retryable" if status == "prepared" or definitive else "uncertain"
        store.db.execute(
            "UPDATE outbox SET status=?,error=? WHERE id=?",
            (outcome, type(exc).__name__, key),
        )
        store.db.commit()
        raise RuntimeError(
            f"SMTP {outcome}: {type(exc).__name__}; Message-ID {message_id}"
        ) from None
    if report.get("test"):
        store.meta("test_send_passed", True)
    return "accepted"
