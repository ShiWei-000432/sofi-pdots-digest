"""Command-line interface for Paper Digest."""

from __future__ import annotations

import sys
from argparse import ArgumentParser
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import cast

from . import __version__
from .analysis import AnalysisError
from .archive_site import ArchiveSiteError, build_archive_site
from .arxiv_client import ArxivClientError
from .config import ConfigError, load_config
from .crossref_client import CrossrefClientError
from .delivery import DeliveryError, send_configured_deliveries
from .digest import summarize_digest, write_outputs
from .feedback import (
    FeedbackMergeStrategy,
    FeedbackStateDiff,
    clear_feedback_action,
    clear_feedback_due_date,
    clear_feedback_note,
    clear_feedback_review_interval_days,
    clear_feedback_snoozed_until,
    clear_feedback_status,
    diff_feedback_states,
    list_…10762 tokens truncated…)
                server.starttls()
                server.ehlo()
            _authenticate_and_send(server, message, config.username, password)
    except (OSError, smtplib.SMTPException) as exc:
        raise EmailDeliveryError(
            "failed to send digest email via "
            f"{config.smtp_host}:{config.smtp_port}: {exc}"
        ) from exc


def send_digest_email(config: EmailConfig, digest: DigestRun) -> None:
    """Backward-compatible wrapper for sending the full digest by email."""

    send_email_message(
        config,
        subject=_build_subject(config, digest),
        body=render_markdown(digest),
    )


def _authenticate_and_send(
    server: smtplib.SMTP,
    message: EmailMessage,
    username: str | None,
    password: str | None,
) -> None:
    if username is not None and password is not None:
        server.login(username, password)
    server.send_message(message)


def _resolve_password(config: EmailConfig) -> str | None:
    if config.password_env is None:
        return None

    password = os.getenv(config.password_env)
    if not password:
        raise EmailDeliveryError(
            f"email password environment variable {config.password_env!r} is not set"
        )
    return password


def _build_subject(config: EmailConfig, digest: DigestRun) -> str:
    summary = summarize_digest(digest)
    date_label = digest.generated_at.strftime("%Y-%m-%d")
    prefix = config.subject_prefix.strip()
    return f"{prefix} {date_label} | {summary}".strip()


def send_weekly_message(
    config: EmailConfig,
    message: EmailMessage,
    on_data: Callable[[], None],
    on_accepted: Callable[[], None],
) -> None:
    """SMTP transaction milestones for the weekly outbox.

    A transport error after on_data is ambiguous; callers must not auto-resend.
    Persist acceptance before QUIT, whose failure cannot undo SMTP acceptance.
    """
    import ssl

    password = _resolve_password(config)
    server = None
    try:
        if config.use_tls:
            server = smtplib.SMTP_SSL(
                config.smtp_host,
                config.smtp_port,
                timeout=30,
                context=ssl.create_default_context(),
            )
        else:
            server = smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=30)
            server.ehlo()
            if config.use_starttls:
                server.starttls(context=ssl.create_default_context())
                server.ehlo()
        if config.username and password:
            server.login(config.username, password)
        on_data()
        refused = server.send_message(message)
        if refused:
            raise smtplib.SMTPRecipientsRefused(refused)
        on_accepted()
    finally:
        if server is not None:
            try:
                server.quit()
            except (OSError, smtplib.SMTPException):
                server.close()
