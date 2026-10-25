"""Evidence records, conservative identifier union, and domain ranking."""

import hashlib
import re
from urllib.parse import unquote


def doi(value):
    return re.sub(
        r"^(?:https?://(?:dx\.)?doi.org/|doi:\s*)",
        "",
        unquote(value or "").strip(),
        flags=re.I,
    ).lower()


def date_parts(parts):
    if not parts:
        return None, "unknown"
    return "-".join(
        str(v).zfill(4 if i == 0 else 2) for i, v in enumerate(parts[:3])
    ), ("year", "month", "day")[min(len(parts), 3) - 1]


def record(
    paper,
    published=None,
    precision="unknown",
    indexed=None,
    venue="",
    pmid=None,
    pmcid=None,
):
    # Do NOT copy upstream published_at when date_label is Entered/Indexed.
    if published is None and paper.date_label == "Published":
        published, precision = paper.published_at.date().isoformat(), "day"
    ids = []
    if paper.doi:
        ids.append("doi:" + doi(paper.doi))
    if pmid:
        ids.append("pmid:" + str(pmid))
    if paper.arxiv_id:
        ids.append("arxiv:" + re.sub(r"v\d+$", "", paper.arxiv_id))
    if not ids:
        ids.append(paper.paper_id)
    return dict(
        id=ids[0],
        ids=ids,
        title=paper.title,
        abstract=paper.summary,
        authors=paper.authors,
        venue=venue,
        published_at=published,
        publication_precision=precision,
        indexed_at=indexed,
        first_seen_at=None,
        doi=doi(paper.doi),
        pmid=pmid,
        pmcid=pmcid,
        url=paper.abstract_url,
        sources=[paper.source],
        source_urls=paper.source_urls,
        evidence=[],
        related_ids=[],
        evidence_scope="仅有摘要" if paper.summary else "仅有元数据",
    )


def merge(records):
    groups = []
    for item in records:
        hits = [g for g in groups if set(g["ids"]) & set(item["ids"])]
        if not hits:
            groups.append(item.copy())
            continue
        target = hits[0]
        for other in [item, *hits[1:]]:
            for field in ["ids", "sources", "evidence", "related_ids"]:
                target[field] = list(dict.fromkeys(target[field] + other[field]))
            target["source_urls"].update(other["source_urls"])
            if len(other["abstract"]) > len(target["abstract"]):
                target["abstract"] = other["abstract"]
                target["evidence_scope"] = other["evidence_scope"]
            for field in ["pmid", "pmcid", "venue", "doi"]:
                target[field] = target.get(field) or other.get(field)
            # Preserve each raw source as evidence if dates disagree.
            if other["published_at"] and (
                not target["published_at"]
                or other["published_at"] < target["published_at"]
            ):
                target["published_at"] = other["published_at"]
                target["publication_precision"] = other["publication_precision"]
        for other in hits[1:]:
            groups.remove(other)
    for g in groups:
        g["id"] = next((i for i in g["ids"] if i.startswith("doi:")), g["ids"][0])
    return groups


def rank(p, extra_terms=()):
    text = re.sub(r"[-–]", " ", (p["title"] + " " + p["abstract"]).lower())
    title = p["title"].lower()
    if (
        re.search(r"\bsofi\b", title)
        and any(
            w in title for w in ["financial", "banking", "stock", "earnings", "fintech"]
        )
        and not any(w in title for w in ["microscop", "fluorescen", "optical"])
    ):
        return 0, "不相关", ["金融同名排除"]
    if "supplementary document" in title or "data and analysis scripts" in title:
        return 0, "支撑资源", ["补充资料不单列为研究论文"]
    optical = any(w in text for w in ["fluorescen", "microscop", "optical", "imaging"])
    sofi = "optical fluctuation imaging" in text or bool(
        re.search(r"\bsofi\b", text) and optical
    )
    pdots = (
        bool(
            re.search(
                r"\bpdots?\b|polymer dots?|conjugated polymer nanoparticles", text
            )
        )
        and optical
    )
    if any(
        w in text
        for w in [
            "carbonized polymer dot",
            "carbonised polymer dot",
            "carbon dot",
            "carbon quantum dot",
        ]
    ) and not any(w in text for w in ["semiconducting polymer", "conjugated polymer"]):
        pdots = False
    method = any(
        w in text
        for w in [
            "fluorescence fluctuation",
            "super resolution microscop",
            "single molecule localization",
            "computational microscop",
        ]
    )
    priorities = [
        "artifact",
        "fidelity",
        "uncertainty",
        "confidence",
        "self supervised",
        "physics informed",
        "cumulant",
        "non stationary",
        "blinking",
        "polarization",
        "background correction",
        "bleaching",
        "few frame",
        *extra_terms,
    ]
    matches = [w for w in priorities if w in text]
    if not (sofi or pdots or (method and matches)):
        return 0, "不相关", []
    category = "直接相关" if sofi else "探针与实验支撑" if pdots else "方法可迁移"
    score = (65 if sofi else 48 if pdots else 35) + min(25, 5 * len(matches))
    reasons = [category, *matches]
    if p["abstract"]:
        score += 5
        reasons.append("有摘要可核查")
    return score, category, reasons


def recent(p, cutoff, end):
    value = p.get("published_at")
    if not value:
        return False
    # Partial dates qualify only if the entire precision interval is within range.
    if len(value) == 4:
        lo, hi = value + "-01-01", value + "-12-31"
    elif len(value) == 7:
        import calendar

        y, m = map(int, value.split("-"))
        lo, hi = value + "-01", value + f"-{calendar.monthrange(y, m)[1]:02}"
    else:
        lo = hi = value[:10]
    return lo >= cutoff and hi <= end


def fingerprint(value):
    import json

    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
