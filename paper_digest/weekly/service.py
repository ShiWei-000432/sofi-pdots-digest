"""Fetch -> persist -> screen -> summarize -> archive -> explicit send."""

import json
from datetime import timedelta
from pathlib import Path

from .evidence import check_links, enrich
from .model import fingerprint, merge, rank, recent
from .provider import CodexCLIProvider, ProviderError
from .report import write_report
from .sources import Collector


def run(cfg, store, now, root, reuse=None, fetch_only=False):
    stamp = now.strftime("%Y%m%d-%H%M%S")
    out = root / "runs" / stamp
    out.mkdir(parents=True, exist_ok=True)
    collector = Collector(cfg, out, now)
    earliest = (now - timedelta(days=cfg["backfill_days"])).date().isoformat()
    end = now.date().isoformat()
    if reuse:
        source = Path(reuse)
        records = json.loads((source / "candidates.json").read_text())
        receipts = json.loads((source / "sources.json").read_text())
        # Keep evidence paths resolvable without duplicating entire archives.
        for p in records:
            p["evidence"] = [
                str((source / e).resolve()) if not Path(e).is_absolute() else e
                for e in p["evidence"]
            ]
        fetched = sum(r["count"] for r in receipts)
    else:
        watermarks = store.get_meta("source_watermarks", {})
        starts = {
            s: max(
                earliest,
                (
                    __import__("datetime").datetime.fromisoformat(d)
                    - timedelta(days=cfg["overlap_days"])
                )
                .date()
                .isoformat(),
            )
            for s, d in watermarks.items()
        }
        records = collector.collect(earliest, end, starts)
        receipts = collector.receipts
        fetched = len(records)
    unique = merge(records)
    for p in unique:
        p["score"], p["category"], p["score_reasons"] = rank(
            p, cfg["search"].get("extra_priority_terms", [])
        )
        store.fetched(p, now.isoformat())
    (out / "candidates.json").write_text(
        json.dumps(unique, ensure_ascii=False, indent=2)
    )
    (out / "sources.json").write_text(
        json.dumps(receipts, ensure_ascii=False, indent=2)
    )
    watermarks = store.get_meta("source_watermarks", {})
    if not reuse:
        for s in cfg["sources"]:
            rows = [r for r in receipts if r["source"] == s]
            if rows and all(r["status"] == "ok" for r in rows):
                watermarks[s] = now.isoformat()
        store.meta("source_watermarks", watermarks)
        if receipts and all(r["status"] == "ok" for r in receipts):
            store.meta("last_successful_search", now.isoformat())
    store.meta("last_archive", str(out))
    if fetch_only:
        return None, out
    candidates = [
        p
        for p in store.unsent()
        if recent(p, earliest, end) and p["score"] >= cfg["min_score"]
    ]
    candidates.sort(
        key=lambda p: (
            recent(
                p, (now - timedelta(days=cfg["weekly_days"])).date().isoformat(), end
            ),
            p["score"],
        ),
        reverse=True,
    )
    provider = CodexCLIProvider(cfg["analysis"])
    selected = []
    failures = []
    screened = []
    shortlist = candidates[: cfg["analysis"]["max_candidates"]]
    keys = {
        p["id"]: fingerprint(
            ["screen-v2", cfg["analysis"].get("model"), p["title"], p["abstract"]]
        )
        for p in shortlist
    }
    missing = [p for p in shortlist if store.cached(keys[p["id"]]) is None]
    if missing:
        try:
            print(f"批量相关性判断：{len(missing)} 篇", flush=True)
            decisions = provider.screen_many(missing)
            for p in missing:
                store.cache(keys[p["id"]], decisions[p["id"]])
        except ProviderError as exc:
            failures.append({"stage": "screen", "error": str(exc)})
    for p in shortlist:
        screen = store.cached(keys[p["id"]])
        if screen is None:
            continue
        p["screen"] = screen
        if screen["relevant"] and screen["value_score"] >= cfg["min_score"]:
            p["rule_score"] = p["score"]
            p["score"] = round((p["score"] + screen["value_score"]) / 2)
            screened.append(p)
    screened.sort(key=lambda p: p["score"], reverse=True)
    for idx, p in enumerate(screened[: cfg["max_papers"]]):
        if idx < cfg["analysis"]["max_fulltexts"]:
            enrich(p, collector)
        check_links(p, collector)
        key = fingerprint(
            [
                "summary-v1",
                cfg["analysis"].get("model"),
                p["title"],
                p["abstract"],
                p.get("fulltext"),
                cfg["analysis"]["max_input_chars"],
            ]
        )
        try:
            analysis = store.cached(key)
            if analysis is None:
                print("详细摘要：" + p["title"], flush=True)
                analysis = provider.summarize(p)
                store.cache(key, analysis, p["id"], now.isoformat())
            p["analysis"] = analysis
            p["section"] = (
                "本周新成果"
                if recent(
                    p,
                    (now - timedelta(days=cfg["weekly_days"])).date().isoformat(),
                    end,
                )
                else "近期补充"
            )
            # Explicit source relations only; do not fuzzy-merge preprint/journal titles.
            for related in p.get("related_ids", []):
                row = store.db.execute(
                    "SELECT p.sent_at FROM papers p JOIN aliases a ON a.id=p.id WHERE a.alias=?",
                    (related,),
                ).fetchone()
                if row and row[0]:
                    p["section"] = "版本更新"
            p.pop(
                "fulltext", None
            )  # full source retained separately, not copied into email JSON
            selected.append(p)
        except ProviderError as exc:
            failures.append({"id": p["id"], "stage": "summary", "error": str(exc)})
            break
    report = dict(
        date=end,
        generated_at=now.isoformat(),
        papers=selected,
        sources=receipts,
        fetched_count=fetched,
        unique_count=len(unique),
        eligible_count=len(candidates),
        analysis_failures=failures,
        complete=bool(receipts) and all(r["status"] == "ok" for r in receipts),
        first_run=not store.get_meta("last_sent_at"),
    )
    write_report(report, out, cfg["recipient"])
    store.meta("last_report", str(out))
    return report, out
