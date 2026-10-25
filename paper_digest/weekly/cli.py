"""Local weekly CLI, secure interactive setup, and launchd catch-up guard."""

import argparse
import fcntl
import getpass
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tomllib
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .delivery import email_config, send
from .service import run
from .state import Store


def load(path):
    cfg = tomllib.loads(path.read_text())
    ZoneInfo(cfg["timezone"])
    if not __import__("re").fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", cfg["recipient"]):
        raise ValueError("收件邮箱格式无效")
    for name in ["backfill_days", "overlap_days", "weekly_days", "max_papers"]:
        if not isinstance(cfg[name], int) or cfg[name] < 1:
            raise ValueError(name + " must be positive")
    if not 1 <= cfg["max_papers"] <= 10:
        raise ValueError("max_papers must be 1..10")
    for name in ["page_size", "max_pages", "timeout", "retries"]:
        if cfg["search"][name] < 1:
            raise ValueError("search." + name + " must be positive")
    if cfg["search"]["page_size"] > 200:
        raise ValueError("page_size must be <=200")
    if not (
        0 <= cfg["schedule"]["weekday"] <= 6
        and 0 <= cfg["schedule"]["hour"] <= 23
        and 0 <= cfg["schedule"]["minute"] <= 59
    ):
        raise ValueError("Invalid schedule")
    for field in ["max_candidates", "max_input_chars", "timeout"]:
        if not isinstance(cfg["analysis"][field], int) or cfg["analysis"][field] <= 0:
            raise ValueError("analysis." + field + " must be positive")
    for field in ["max_fulltexts", "retries"]:
        if not isinstance(cfg["analysis"][field], int) or cfg["analysis"][field] < 0:
            raise ValueError("analysis." + field + " must be non-negative")
    if cfg["analysis"]["max_input_chars"] < 6000:
        raise ValueError("max_input_chars must be >=6000")
    if not cfg["sources"] or set(cfg["sources"]) - {
        "crossref",
        "pubmed",
        "openalex",
        "arxiv",
        "europepmc",
        "biorxiv",
        "semantic_scholar",
    }:
        raise ValueError("sources contains unsupported source or is empty")
    if cfg["analysis"]["provider"] != "codex_cli":
        raise ValueError(
            "Weekly only supports codex_cli; paid API remains opt-in upstream"
        )
    return cfg


def env_file():
    return Path.home() / ".config" / "sofi-pdots" / "smtp.json"


def load_env():
    path = env_file()
    if path.exists():
        if path.stat().st_mode & 0o077:
            raise ValueError("SMTP 凭据文件权限过宽，请 chmod 600 " + str(path))
        for key, value in json.loads(path.read_text()).items():
            if key.startswith("SMTP_"):
                os.environ.setdefault(key, value)


def setup_qq():
    address = input("请输入完整 QQ 发件邮箱（例如 123456@qq.com）：").strip()
    if not __import__("re").fullmatch(
        r"[^\s@]+@(?:qq|foxmail)\.com", address, __import__("re").I
    ):
        raise ValueError("请输入完整的 QQ/foxmail 邮箱地址")
    password = getpass.getpass(
        "请粘贴 QQ 邮箱 SMTP 授权码（输入不会显示，不是 QQ 密码）："
    ).strip()
    if not password:
        raise ValueError("授权码为空，未保存")
    path = env_file()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = dict(
        SMTP_HOST="smtp.qq.com",
        SMTP_PORT="465",
        SMTP_SECURITY="ssl",
        SMTP_USERNAME=address,
        SMTP_FROM=address,
        SMTP_PASSWORD=password,
    )
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    print("已保存本机配置（仅当前用户可读），未发送邮件。")


def due(now, cfg, last):
    s = cfg["schedule"]
    target = now.replace(
        hour=s["hour"], minute=s["minute"], second=0, microsecond=0
    ) - timedelta(days=(now.weekday() - s["weekday"]) % 7)
    if target > now:
        target -= timedelta(days=7)
    return not last or datetime.fromisoformat(last) < target


def scheduler(action, cfg, path, root, store):
    if sys.platform != "darwin":
        raise ValueError("此部署为 macOS，仅实现 launchd；未安装其他平台任务")
    label = "org.local.sofi-pdots.weekly"
    dest = Path.home() / "Library" / "LaunchAgents" / (label + ".plist")
    domain = f"gui/{os.getuid()}"
    if action in ["pause", "remove"]:
        subprocess.run(
            ["launchctl", "bootout", domain + "/" + label], capture_output=True
        )
        if action == "remove":
            dest.unlink(missing_ok=True)
        print("已暂停" if action == "pause" else "已取消并移除任务")
        return
    if not store.get_meta("test_send_passed"):
        raise ValueError("尚无成功 SMTP 测试记录，未启用定时任务")
    email_config(cfg)
    codex = shutil.which("codex")
    if not codex:
        raise ValueError("Codex CLI 不在 PATH")
    # Hourly lightweight tick honors Asia/Shanghai even if macOS timezone changes.
    # It also catches up on login/wake after a missed Friday run.
    args = [sys.executable, "-m", "paper_digest.weekly.supervisor", str(path)]
    payload = dict(
        Label=label,
        ProgramArguments=args,
        WorkingDirectory=str(path.parent),
        StartInterval=3600,
        RunAtLoad=True,
        StandardOutPath=str(root / "scheduler.log"),
        StandardErrorPath=str(root / "scheduler-error.log"),
        EnvironmentVariables={
            "PATH": str(Path(codex).parent) + ":/usr/bin:/bin:/usr/sbin:/sbin"
        },
        StartCalendarInterval={
            "Weekday": (cfg["schedule"]["weekday"] + 1) % 7,
            "Hour": cfg["schedule"]["hour"],
            "Minute": cfg["schedule"]["minute"],
        },
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(plistlib.dumps(payload))
    subprocess.run(["launchctl", "bootout", domain + "/" + label], capture_output=True)
    subprocess.run(["launchctl", "bootstrap", domain, str(dest)], check=True)
    print("已启用 launchd：每周目标时间＋每小时补跑检查；电脑关闭或离线不能准时发送。")


def main(argv=None):
    parser = argparse.ArgumentParser(description="SOFI × Pdots 周报")
    parser.add_argument("--config", default="weekly.local.toml")
    parser.add_argument(
        "command",
        choices=[
            "run",
            "fetch",
            "send",
            "status",
            "doctor",
            "setup-qq",
            "tick",
            "enable",
            "pause",
            "resume",
            "remove",
            "reconcile",
        ],
    )
    parser.add_argument("--reuse", help="复用已检索 candidates.json/sources.json 目录")
    parser.add_argument(
        "--test", action="store_true", help="将本次发送标为部署测试周报"
    )
    parser.add_argument("--archive", help="发送指定周报目录；默认上次生成目录")
    parser.add_argument("--message-id")
    parser.add_argument("--outcome", choices=["accepted", "not-sent"])
    args = parser.parse_args(argv)
    if args.command == "setup-qq":
        setup_qq()
        return 0
    path = Path(args.config).resolve()
    cfg = load(path)
    load_env()
    root = (path.parent / cfg["output_dir"]).resolve()
    root.mkdir(parents=True, exist_ok=True)
    now = datetime.now(ZoneInfo(cfg["timezone"]))
    with (root / "run.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("已有任务运行，跳过重复启动")
            return 0
        store = Store(root / "state.sqlite3")
        try:
            if args.command == "status":
                print(
                    json.dumps(
                        {
                            "last_report": store.get_meta("last_report"),
                            "watermarks": store.get_meta("source_watermarks", {}),
                            "outbox": [
                                dict(
                                    zip(
                                        ["message_id", "status", "error"],
                                        r,
                                        strict=True,
                                    )
                                )
                                for r in store.db.execute(
                                    "SELECT message_id,status,error FROM outbox"
                                )
                            ],
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            elif args.command == "doctor":
                result = subprocess.run(
                    ["codex", "login", "status"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                print(
                    "Codex 登录检查：" + ("通过" if result.returncode == 0 else "失败")
                )
                email_config(cfg)
                print("SMTP 配置齐全（尚未发送／未验证收件箱）")
            elif args.command in ["enable", "resume", "pause", "remove"]:
                scheduler(args.command, cfg, path, root, store)
            elif args.command == "reconcile":
                if not args.message_id or not args.outcome:
                    raise ValueError(
                        "需 --message-id 和 --outcome；仅在人工核对服务器结果后使用"
                    )
                row = store.db.execute(
                    "SELECT id,payload,status FROM outbox WHERE message_id=?",
                    (args.message_id,),
                ).fetchone()
                if not row or row[2] != "uncertain":
                    raise ValueError("未找到不确定投递")
                with store.db:
                    store.db.execute(
                        "UPDATE outbox SET status=? WHERE id=?",
                        (
                            "accepted" if args.outcome == "accepted" else "retryable",
                            row[0],
                        ),
                    )
                    if args.outcome == "accepted":
                        for pid in json.loads(row[1])["paper_ids"]:
                            store.db.execute(
                                "UPDATE papers SET sent_at=? WHERE id=?",
                                (now.isoformat(), pid),
                            )
                print("投递状态已按人工核查结果更新")
            elif args.command == "send":
                out = Path(args.archive or store.get_meta("last_report") or "")
                report = json.loads((out / "report.json").read_text())
                if report["analysis_failures"]:
                    raise ValueError("分析未完成，请先重试生成；未发送")
                if args.test:
                    from .report import write_report

                    report["test"] = True
                    write_report(report, out, cfg["recipient"])
                print(
                    "SMTP：" + send(cfg, store, report, out) + "（不代表收件箱已确认）"
                )
            else:
                if args.command == "tick":
                    if not due(now, cfg, store.get_meta("last_sent_at")):
                        return 0
                    if store.db.execute(
                        "SELECT 1 FROM outbox WHERE status='uncertain'"
                    ).fetchone():
                        raise ValueError("存在不确定投递，需要人工核查；未自动重发")
                    last = store.get_meta("last_attempt")
                    if last and now - datetime.fromisoformat(last) < timedelta(hours=6):
                        return 0
                    store.meta("last_attempt", now.isoformat())
                report, out = run(
                    cfg, store, now, root, args.reuse, args.command == "fetch"
                )
                print("归档：" + str(out))
                if (
                    args.command == "tick"
                    and report
                    and not report["analysis_failures"]
                ):
                    if (
                        not any(
                            r["count"] > 0 and r["status"] in ("ok", "partial")
                            for r in report["sources"]
                        )
                        and not report["complete"]
                    ):
                        raise ValueError(
                            "检索不可用，未将故障空周报标记为本周完成；待重试"
                        )
                    print("SMTP：" + send(cfg, store, report, out))
                if report and report["analysis_failures"]:
                    return 2
        finally:
            store.close()
    return 0
