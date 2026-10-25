"""Chinese Markdown, escaped HTML, JSON, and multipart email preview."""

import html
import json
from email.message import EmailMessage
from email.policy import SMTP

import bleach
import markdown


def render(report):
    papers = report["papers"]
    lines = [
        f"# SOFI × Pdots 文献周报｜{report['date']}",
        "",
        "## 本周最值得关注的进展",
        "",
    ]
    if report.get("test"):
        lines.insert(
            2,
            "本邮件为部署测试周报；使用真实检索数据。首次运行回溯近90天，近期补充与本周新成果分开列出。",
        )
    weekly = [p for p in papers if p["section"] == "本周新成果"]
    if not papers:
        lines.append(
            "本周未发现重要新成果。"
            if report["complete"] and not report["analysis_failures"]
            else "检索或分析未完整完成，不能据此判断本周没有重要成果。"
        )
    elif not weekly:
        lines.append("本期入选均为近期补充；本周新成果覆盖情况请结合下方来源报告判断。")
    for priority, p in enumerate(papers[:3], 1):
        lines.append(
            f"- **优先 {priority}｜{p['title']}**：{p['analysis']['why_care']}"
        )
    for section in ["本周新成果", "近期补充", "版本更新"]:
        selected = [p for p in papers if p["section"] == section]
        if not selected:
            continue
        lines += ["", f"## {section}", ""]
        for p in selected:
            a = p["analysis"]
            lines += [
                f"### {p['title']}",
                f"**{a['title_zh']}**",
                "",
                f"作者：{'; '.join(p['authors']) or '尚未核实'}  ",
                f"期刊／平台：{p['venue'] or '尚未核实'}  ",
                f"发表日期：{p['published_at'] or '尚未核实'}（精度：{p['publication_precision']}）；索引／入库：{p['indexed_at'] or '来源未提供'}；首次发现：{p['first_seen_at']}  ",
                f"DOI：{p['doi'] or '未提供'}；[原文]({p['url']})；来源：{', '.join(p['sources'])}  ",
                f"分类：{p['category']}；排序分：{p['score']}（非真实性概率）  ",
                f"评分理由：{'；'.join(p['score_reasons'])}；{p['screen']['reason']}",
                "",
                f"**关注理由**：{a['why_care']}",
                f"**研究问题**：{a['problem']}",
                f"**核心方法**：{a['method']}",
                f"**创新点**：{a['innovation']}",
                "**论文报告的实验与定量结果**："
                + (
                    "；".join(c["text"] for c in a["reported_results"])
                    or "摘要未说明／尚未核实"
                ),
                "**作者明确报告的局限**："
                + (
                    "；".join(c["text"] for c in a["author_limitations"])
                    or "摘要未说明／尚未核实"
                ),
                f"**对 SOFI＋Pdots、200–500 帧数据的迁移建议（分析判断）**：{a['transfer_advice']}",
                f"**可能局限（分析判断）**：{a['analyst_caveats']}",
                f"**精读／复现**：{a['recommendation']}；{a['recommendation_reason']}",
                "**代码／数据**："
                + (
                    "；".join(
                        f"[{r['url']}]({r['url']})（{r['status']}）"
                        for r in p.get("resource_links", [])
                    )
                    or "未找到公开链接（当前已获取证据范围内）"
                ),
                f"**证据范围**：{a['evidence_scope']}。{p.get('fulltext_status', '未尝试获取全文')}。",
                "",
            ]
    lines += ["## 建议精读／复现", ""]
    for p in papers[:3]:
        lines.append(
            f"- {p['title']}：{p['analysis']['recommendation']}；{p['analysis']['recommendation_reason']}"
        )
    lines += [
        "",
        "## 检索与运行情况",
        "",
        f"检索时间：{report['generated_at']}；候选数：{report['fetched_count']}；跨源去重后：{report['unique_count']}；入选：{len(papers)}。",
        "覆盖多个数据库，不承诺全网覆盖或绝对不漏检。分页截断、失败或额度限制不能解释为零成果。",
        "",
    ]
    for source in dict.fromkeys(r["source"] for r in report["sources"]):
        rows = [r for r in report["sources"] if r["source"] == source]
        status = "成功" if all(r["status"] == "ok" for r in rows) else "受限／失败"
        lines.append(
            f"- {source}：{status}；返回 {sum(r['count'] for r in rows)} 条；截断查询 {sum(r['truncated'] for r in rows)} 个。"
        )
        notes = set()
        for r in rows:
            if r.get("limitation"):
                notes.add(r["limitation"])
            if r.get("remaining_queries_skipped"):
                notes.add(f"故障后跳过 {r['remaining_queries_skipped']} 个后续查询")
        for note in sorted(notes):
            lines.append("  限制：" + note)
        for r in rows:
            if r.get("error"):
                lines.append("  " + r["error"])
    if report["analysis_failures"]:
        lines += [
            "",
            "分析待重试："
            + json.dumps(report["analysis_failures"], ensure_ascii=False),
        ]
    lines += ["", "邮件状态以本地 outbox 为准：SMTP 接受不等于确认进入收件箱。"]
    return "\n\n".join(lines)


def write_report(report, out, recipient):
    md = render(report)
    # Escape ALL raw evidence before Markdown processing; disable image embedding.
    safe = html.escape(md, quote=False)
    page = (
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>SOFI × Pdots 文献周报</title><style>body{max-width:900px;margin:40px auto;padding:0 24px;font:16px/1.7 system-ui;color:#183040}h1,h2{color:#075e69}h3{margin-top:2em}a{color:#08789a}</style><body>'
        + bleach.clean(
            markdown.markdown(safe),
            tags={
                "p",
                "h1",
                "h2",
                "h3",
                "h4",
                "ul",
                "ol",
                "li",
                "strong",
                "em",
                "a",
                "br",
                "code",
                "pre",
                "blockquote",
            },
            attributes={"a": ["href", "title"]},
            protocols={"http", "https", "mailto"},
            strip=True,
        )
        + "</body></html>"
    )
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.md").write_text(md)
    (out / "report.html").write_text(page)
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    message = EmailMessage(policy=SMTP)
    message["Subject"] = (
        f"【SOFI × Pdots 文献周报】{report['date']}｜本周重点 {len(report['papers'])} 篇"
    )
    if report.get("test"):
        message.replace_header("Subject", "【测试】" + str(message["Subject"]))
    message["To"] = recipient
    message.set_content(md)
    message.add_alternative(page, subtype="html")
    (out / "preview.eml").write_bytes(message.as_bytes())
    return message
