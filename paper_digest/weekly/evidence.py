"""Lawful Europe PMC OA full text and public repository link checks."""

import json
import re
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit

from .sources import EP, ep_record


def enrich(p, collector):
    if not p.get("pmcid") and p.get("doi"):
        try:
            raw, ev = collector.get(
                EP + "/search",
                {
                    "query": 'DOI:"' + p["doi"] + '"',
                    "format": "json",
                    "resultType": "core",
                },
                "oa-lookup",
            )
            items = json.loads(raw)["resultList"]["result"]
            if items:
                other = ep_record(items[0])
                p["pmcid"] = other.get("pmcid")
                if len(other["abstract"]) > len(p["abstract"]):
                    p["abstract"] = other["abstract"]
                p["evidence"].append(ev)
        except Exception:
            p["fulltext_status"] = "Europe PMC metadata lookup failed"
    if p.get("pmcid"):
        try:
            raw, ev = collector.get(
                EP + "/" + p["pmcid"] + "/fullTextXML", source="oa-fulltext"
            )
            root = ET.fromstring(raw)
            body = root.find("body")
            if body is None:
                raise ValueError("No article body")
            p["fulltext"] = " ".join(root.itertext())
            p["fulltext_status"] = "合法开放全文已获取（Europe PMC XML）"
            p["evidence"].append(ev)
            p["fulltext_url"] = EP + "/" + p["pmcid"] + "/fullTextXML"
        except Exception:
            p["fulltext_status"] = "未获取合法开放全文；仅使用摘要或元数据"
    else:
        p["fulltext_status"] = "Europe PMC 未提供可取全文的 PMCID"


def check_links(p, collector):
    # Only extract existing evidence URLs, never let the model invent links.
    text = p.get("fulltext", "") + " " + p["abstract"]
    urls = list(dict.fromkeys(re.findall(r'https?://[^\s<>"\)]+', text)))
    allowed = {
        "github.com",
        "gitlab.com",
        "zenodo.org",
        "doi.org",
        "figshare.com",
        "osf.io",
        "datadryad.org",
    }
    p["resource_links"] = []
    for url in urls:
        host = urlsplit(url).hostname
        if host not in allowed or (
            host == "doi.org"
            and not any(x in url.lower() for x in ["zenodo", "figshare", "dryad"])
        ):
            continue
        status = "尚未核实"
        try:
            _, ev = collector.get(url, source="resource-check")
            status = "链接可访问；与论文关联来自证据，未运行代码或检查数据完整性"
            p["evidence"].append(ev)
        except Exception:
            status = "访问失败，尚未核实"
        p["resource_links"].append({"url": url, "status": status})
        if len(p["resource_links"]) >= 5:
            break
