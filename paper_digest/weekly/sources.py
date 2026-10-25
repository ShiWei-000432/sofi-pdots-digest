"""Paged official APIs with per-query coverage receipts and raw evidence."""

import gzip
import json
import os
import re
import xml.etree.ElementTree as ET
from collections import deque
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode
from urllib.request import Request

from paper_digest.arxiv_client import Paper, parse_feed
from paper_digest.crossref_client import parse_crossref_item
from paper_digest.network import fetch_bytes_with_retry
from paper_digest.openalex_client import parse_openalex_work
from paper_digest.pubmed_client import _MONTH_NUMBERS, parse_pubmed_article

from .model import date_parts, doi, rank, record

EP = "https://www.ebi.ac.uk/europepmc/webservices/rest"
NCBI = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"

# Independent bounds: a small generic limit must not starve later channels.
FREE_LIMITS = {
    "crossref": {"max_pages": 3, "max_requests": 180},
    "openalex": {"max_pages": 30, "max_requests": 90},
    "biorxiv": {"max_pages": 800, "max_requests": 800},
}


class RequestBudgetExceeded(RuntimeError):
    pass


def clean(text):
    import html

    return " ".join(html.unescape(re.sub("<[^>]*>", " ", text or "")).split())


def queries(cfg, source):
    s = cfg["search"]
    context = ["fluorescence", "blinking", "microscopy", "imaging"]

    def group(terms):
        if source == "pubmed":
            return "(" + " OR ".join(f'"{t}"[Title/Abstract]' for t in terms) + ")"
        if source == "arxiv":
            return "(" + " OR ".join(f'all:"{t}"' for t in terms) + ")"
        return "(" + " OR ".join(f'"{t}"' for t in terms) + ")"

    if source in ("crossref", "semantic_scholar"):
        # Ranked lexical search, not Boolean. Each concept is an independent request.
        return (
            [("A", q) for q in s["sofi"][:-1]]
            + [("A", "SOFI fluorescence microscopy")]
            + [("B", q + " fluorescence") for q in s["pdots"]]
            + [("C", q + " " + w) for q in s["methods"] for w in s["qualifiers"]]
        )
    return [
        (
            "A",
            group(s["sofi"][:-1])
            + " OR ("
            + group(["SOFI"])
            + " AND "
            + group(context)
            + ")",
        ),
        ("B", group(s["pdots"]) + " AND " + group(context)),
        ("C", group(s["methods"]) + " AND " + group(s["qualifiers"])),
    ]


class Collector:
    def __init__(self, cfg, out, now):
        self.cfg, self.out, self.now = cfg, out, now
        self.receipts = []
        self.counter = 0
        self.source_requests = {}
        (out / "evidence").mkdir(parents=True, exist_ok=True)

    def limit(self, source, name):
        fallback = self.cfg["search"].get(
            "max_requests_per_source" if name == "max_requests" else name, 40
        )
        return (
            self.cfg["search"]
            .get("source_limits", {})
            .get(source, {})
            .get(name, FREE_LIMITS.get(source, {}).get(name, fallback))
        )

    def get(self, base, params=None, source="lookup", headers=None):
        if self.source_requests.get(source, 0) >= self.limit(source, "max_requests"):
            raise RequestBudgetExceeded(
                "configured request budget exhausted; coverage incomplete"
            )
        self.source_requests[source] = self.source_requests.get(source, 0) + 1
        params = params or {}
        public = {k: v for k, v in params.items() if k not in ("api_key", "key")}
        url = base + ("?" + urlencode(params) if params else "")
        raw = fetch_bytes_with_retry(
            Request(
                url,
                headers={
                    "User-Agent": "paper-digest-sofi/1.0 (https://github.com/X-PG13/paper-digest)",
                    **(headers or {}),
                },
            ),
            timeout_seconds=self.cfg["search"]["timeout"],
            request_delay_seconds=(
                3.1
                if source == "arxiv"
                else 1.05
                if source in {"crossref", "openalex"}
                else 0.36
            ),
            retry_attempts=self.cfg["search"]["retries"],
            retry_backoff_seconds=2,
            max_retry_delay_seconds=60,
            error_factory=RuntimeError,
            operation_description=f"{source} request failed",
        )
        self.counter += 1
        name = f"evidence/{source}-{self.counter:05d}"
        (self.out / (name + ".raw.gz")).write_bytes(gzip.compress(raw, mtime=0))
        (self.out / (name + ".json")).write_text(
            json.dumps(
                {"url": base, "params": public, "retrieved_at": self.now.isoformat()},
                ensure_ascii=False,
                indent=2,
            )
        )
        return raw, name + ".raw.gz"

    def collect(self, start, end, source_starts=None):
        records = []
        for source in self.cfg["sources"]:
            # OpenAlex filters publication time here, not index time. Recheck the
            # backfill window so late-indexed older publications remain discoverable.
            source_start = (
                start
                if source == "openalex"
                else (source_starts or {}).get(source, start)
            )
            qs = (
                [("ABC", "all categories; local domain filter")]
                if source == "biorxiv"
                else queries(self.cfg, source)
            )
            pending = deque()
            for channel, query in qs:
                receipt = dict(
                    source=source,
                    channel=channel,
                    query=query,
                    start=source_start,
                    end=end,
                    status="pending",
                    count=0,
                    retained_count=0,
                    pages=0,
                    truncated=False,
                )
                if source == "biorxiv":
                    receipt["limitation"] = (
                        "全分类分页扫描；仅领域规则命中进入候选，完整原始响应以 gzip 归档"
                    )
                self.receipts.append(receipt)
                pending.append(
                    (receipt, iter(self.pages(source, query, source_start, end)))
                )
            # Round robin: every independent query gets its first page before any
            # query consumes a second page. Never silently drop later channels.
            while pending:
                receipt, iterator = pending.popleft()
                try:
                    batch, more = next(iterator)
                    receipt["count"] += len(batch)
                    retained = (
                        [
                            p
                            for p in batch
                            if rank(
                                p, self.cfg["search"].get("extra_priority_terms", [])
                            )[0]
                            > 0
                        ]
                        if source == "biorxiv"
                        else batch
                    )
                    records.extend(retained)
                    receipt["retained_count"] += len(retained)
                    receipt["pages"] += 1
                    receipt["truncated"] = more
                    receipt["status"] = "partial" if more else "ok"
                    if more:
                        pending.append((receipt, iterator))
                    if source == "biorxiv" and receipt["pages"] % 50 == 0:
                        print(
                            f"biorxiv: {receipt['pages']} pages, {receipt['count']} records scanned",
                            flush=True,
                        )
                except StopIteration:
                    receipt["status"] = "partial" if receipt["truncated"] else "ok"
                    if receipt["truncated"]:
                        receipt["limitation"] = (
                            receipt.get("limitation", "")
                            + "; configured page limit reached"
                        )
                except RequestBudgetExceeded as exc:
                    for row, _ in [(receipt, iterator), *pending]:
                        row.update(
                            status="partial", truncated=True, limitation=str(exc)
                        )
                    pending.clear()
                except Exception as exc:
                    receipt.update(
                        status="failed",
                        error=type(exc).__name__
                        + ": "
                        + re.sub(r"(api_key|key)=[^&\s]+", r"\1=REDACTED", str(exc))[
                            :400
                        ],
                    )
                    receipt["remaining_queries_skipped"] = len(pending)
                    for row, _ in pending:
                        row.update(
                            status="partial",
                            truncated=True,
                            limitation="source circuit breaker: later pages/queries skipped",
                        )
                    pending.clear()
                (self.out / "sources.progress.json").write_text(
                    json.dumps(self.receipts, ensure_ascii=False, indent=2)
                )
            for row in self.receipts:
                if row["source"] == source:
                    print(
                        f"{source}/{row['channel']}: {row['status']}, {row['count']}, pages={row['pages']}",
                        flush=True,
                    )
        # A highly specific supplemental title is a useful lead, not a paper itself.
        supplemental = list(
            dict.fromkeys(
                p["title"]
                for p in records
                if p["title"].lower().startswith("supplementary document for ")
                and "fluctuation" in p["title"].lower()
            )
        )[:3]
        for title in supplemental:
            target = re.sub(
                r" - [^ ]+\.pdf$",
                "",
                title[len("Supplementary document for ") :],
                flags=re.I,
            )
            receipt = dict(
                source="crossref",
                channel="A-followup",
                query=target,
                start=start,
                end=end,
                status="ok",
                count=0,
                truncated=False,
            )
            self.receipts.append(receipt)
            try:
                raw, ev = self.get(
                    "https://api.crossref.org/works",
                    {"query.title": target, "rows": 5},
                    "crossref-followup",
                )
                for x in json.loads(raw)["message"]["items"]:
                    if (
                        clean(x.get("title", [""])[0]).casefold()
                        == clean(target).casefold()
                    ):
                        records.extend(with_evidence([crossref_record(x)], ev))
                        receipt["count"] += 1
            except Exception as exc:
                receipt.update(status="failed", error=type(exc).__name__)
        return records

    def pages(self, source, query, start, end):
        size = self.cfg["search"]["page_size"]
        cursor = "*"
        for page in range(self.limit(source, "max_pages")):
            batch = []
            if source == "europepmc":
                raw, ev = self.get(
                    EP + "/search",
                    {
                        "query": f"({query}) AND (FIRST_PDATE:[{start} TO {end}] OR FIRST_IDATE:[{start} TO {end}])",
                        "format": "json",
                        "resultType": "core",
                        "pageSize": size,
                        "cursorMark": cursor,
                    },
                    source,
                )
                data = json.loads(raw)
                for x in data["resultList"]["result"]:
                    batch.append(ep_record(x))
                nxt = data.get("nextCursorMark")
                more = bool(
                    nxt and nxt != cursor and (page + 1) * size < data["hitCount"]
                )
                cursor = nxt
            elif source == "crossref":
                raw, ev = self.get(
                    "https://api.crossref.org/works",
                    {
                        "query.bibliographic": query,
                        "filter": f"from-index-date:{start},until-index-date:{end},from-pub-date:{(self.now - timedelta(days=self.cfg['backfill_days'])).date().isoformat()},until-pub-date:{end}",
                        "rows": size,
                        "cursor": cursor,
                    },
                    source,
                )
                data = json.loads(raw)["message"]
                for x in data["items"]:
                    batch.append(crossref_record(x))
                cursor = data.get("next-cursor")
                more = bool(
                    cursor
                    and len(data["items"]) == size
                    and (page + 1) * size < data["total-results"]
                )
            elif source == "pubmed":
                params = dict(
                    db="pubmed",
                    term=query,
                    datetype="crdt",
                    mindate=start,
                    maxdate=end,
                    retmode="json",
                    retstart=page * size,
                    retmax=size,
                )
                if os.getenv("NCBI_API_KEY"):
                    params["api_key"] = os.environ["NCBI_API_KEY"]
                raw, _ = self.get(NCBI + "esearch.fcgi", params, source)
                data = json.loads(raw)["esearchresult"]
                if data.get("ERROR") or data.get("errorlist"):
                    raise ValueError(
                        "PubMed rejected query: "
                        + str(data.get("errorlist", data.get("ERROR")))
                    )
                ids = data["idlist"]
                more = (page + 1) * size < min(int(data["count"]), 9999)
                if not ids:
                    return
                params.update(id=",".join(ids), retmode="xml")
                params = {
                    k: v
                    for k, v in params.items()
                    if k in ["db", "id", "retmode", "api_key"]
                }
                raw, ev = self.get(NCBI + "efetch.fcgi", params, source)
                for node in ET.fromstring(raw).findall("PubmedArticle"):
                    batch.append(pubmed_record(node))
            elif source == "openalex":
                size = min(size, 100)
                params = {
                    "search": query,
                    "filter": f"from_publication_date:{start},to_publication_date:{end}",
                    "per-page": size,
                }
                key = os.getenv("OPENALEX_API_KEY")
                if key:
                    params.update(api_key=key, cursor=cursor)
                else:
                    params["page"] = page + 1
                raw, ev = self.get("https://api.openalex.org/works", params, source)
                data = json.loads(raw)
                for x in data["results"]:
                    p = parse_openalex_work(x)
                    if p:
                        r = record(
                            p,
                            indexed=x.get("created_date"),
                            venue=(
                                (x.get("primary_location") or {}).get("source") or {}
                            ).get("display_name", ""),
                        )
                        r["pmid"] = (x.get("ids", {}).get("pmid") or "").rstrip(
                            "/"
                        ).rsplit("/", 1)[-1] or None
                        if r["pmid"]:
                            r["ids"].append("pmid:" + r["pmid"])
                        batch.append(r)
                more = (page + 1) * size < data["meta"]["count"]
                cursor = data["meta"].get("next_cursor")
                if not key and (page + 2) * size > 10000:
                    yield with_evidence(batch, ev), more
                    return  # official basic paging depth; a free key enables cursor paging
            elif source == "arxiv":
                raw, ev = self.get(
                    "https://export.arxiv.org/api/query",
                    {
                        "search_query": f"({query}) AND submittedDate:[{start.replace('-', '')}0000 TO {end.replace('-', '')}2359]",
                        "start": page * size,
                        "max_results": size,
                        "sortBy": "submittedDate",
                        "sortOrder": "descending",
                    },
                    source,
                )
                root = ET.fromstring(raw)
                total = int(
                    root.findtext(
                        "{http://a9.com/-/spec/opensearch/1.1/}totalResults", "0"
                    )
                )
                batch = [record(p, venue="arXiv") for p in parse_feed(raw)]
                more = (page + 1) * size < total
            elif source == "biorxiv":
                raw, ev = self.get(
                    f"https://api.biorxiv.org/details/biorxiv/{start}/{end}/{page * 30}",
                    source=source,
                )
                data = json.loads(raw)
                if data.get("messages", [{}])[0].get("status") != "ok":
                    raise ValueError("bioRxiv did not return ok")
                for x in data.get("collection", []):
                    r = basic_record(
                        x["title"],
                        x.get("abstract", ""),
                        x["doi"],
                        x["date"],
                        source,
                        x.get("authors", "").split(";"),
                        "bioRxiv",
                    )
                    if x.get("published") and x["published"] != "NA":
                        r["related_ids"].append("doi:" + doi(x["published"]))
                    r["version"] = x.get("version")
                    batch.append(r)
                more = (page + 1) * 30 < int(data["messages"][0].get("total", 0))
            elif source == "semantic_scholar":
                headers = (
                    {"x-api-key": os.environ["SEMANTIC_SCHOLAR_API_KEY"]}
                    if os.getenv("SEMANTIC_SCHOLAR_API_KEY")
                    else {}
                )
                raw, ev = self.get(
                    "https://api.semanticscholar.org/graph/v1/paper/search",
                    {
                        "query": query,
                        "publicationDateOrYear": f"{start}:{end}",
                        "limit": min(size, 100),
                        "offset": page * min(size, 100),
                        "fields": "title,abstract,authors,venue,publicationDate,year,externalIds,url",
                    },
                    source,
                    headers,
                )
                data = json.loads(raw)
                for x in data.get("data", []):
                    ids = x.get("externalIds") or {}
                    r = basic_record(
                        x["title"],
                        x.get("abstract") or "",
                        ids.get("DOI") or "",
                        x.get("publicationDate") or str(x.get("year") or ""),
                        source,
                        [a["name"] for a in x.get("authors", [])],
                        x.get("venue", ""),
                        x.get("url"),
                    )
                    if ids.get("PubMed"):
                        r["ids"].append("pmid:" + ids["PubMed"])
                    batch.append(r)
                more = "next" in data
            else:
                raise ValueError("Unsupported source " + source)
            yield with_evidence(batch, ev), more
            if not more:
                return


def crossref_record(x):
    dates = [
        date_parts((x.get(k) or {}).get("date-parts", [[]])[0])
        for k in ["published-online", "published-print", "published", "issued"]
        if (x.get(k) or {}).get("date-parts", [[]])[0]
    ]
    published, precision = (
        min(dates, key=lambda t: t[0]) if dates else (None, "unknown")
    )
    r = record(
        parse_crossref_item(x),
        published,
        precision,
        (x.get("indexed") or {}).get("date-time"),
        "; ".join(x.get("container-title", [])),
    )
    for relation in ["is-preprint-of", "has-preprint", "is-version-of", "has-version"]:
        r["related_ids"] += [
            "doi:" + doi(i["id"])
            for i in x.get("relation", {}).get(relation, [])
            if i.get("id-type") == "doi"
        ]
    return r


def with_evidence(batch, ev):
    for r in batch:
        r["evidence"].append(ev)
    return batch


def basic_record(
    title, abstract, identifier, published, source, authors, venue, url=None
):
    precision = {4: "year", 7: "month", 10: "day"}.get(len(published or ""), "unknown")
    dt = datetime(1900, 1, 1, tzinfo=UTC)
    p = Paper(
        title,
        clean(abstract),
        authors,
        [],
        identifier or url or title,
        url or "https://doi.org/" + identifier,
        None,
        dt,
        dt,
        source=source,
        date_label="Unknown",
        doi=identifier or None,
    )
    return record(p, published or None, precision, venue=venue)


def ep_record(x):
    published = x.get("firstPublicationDate") or x.get("pubYear")
    venue = (
        (x.get("journalInfo") or {}).get("journal", {}).get("title")
        or x.get("bookOrReportDetails", {}).get("publisher")
        or x.get("source", "")
    )
    r = basic_record(
        x.get("title", ""),
        x.get("abstractText", ""),
        x.get("doi", ""),
        published,
        "europepmc",
        [a.get("fullName", "") for a in x.get("authorList", {}).get("author", [])],
        venue,
        f"https://europepmc.org/article/{x['source']}/{x['id']}",
    )
    r["indexed_at"] = x.get("firstIndexDate")
    r["pmid"], r["pmcid"] = (
        x.get("pmid") or (x["id"] if x["source"] == "MED" else None),
        x.get("pmcid"),
    )
    if r["pmid"]:
        r["ids"].append("pmid:" + r["pmid"])
    return r


def pubmed_record(node):
    p = parse_pubmed_article(node)
    candidates = []
    for path in [
        "./MedlineCitation/Article/ArticleDate",
        "./MedlineCitation/Article/Journal/JournalIssue/PubDate",
    ]:
        for n in node.findall(path):
            y, m, d = n.findtext("Year"), n.findtext("Month"), n.findtext("Day")
            if y:
                parts = [int(y)]
                if (
                    m
                    and m.lower() in _MONTH_NUMBERS
                    and m.lower()
                    not in ["spring", "summer", "autumn", "fall", "winter"]
                ):
                    parts.append(_MONTH_NUMBERS[m.lower()])
                    if d:
                        parts.append(int(d))
                candidates.append(date_parts(parts))
            elif n.findtext("MedlineDate"):
                match = re.search(r"\b(\d{4})\b", n.findtext("MedlineDate"))
                if match:
                    candidates.append((match[1], "year"))
    published, precision = (
        min(candidates, key=lambda x: x[0]) if candidates else (None, "unknown")
    )
    return record(
        p,
        published,
        precision,
        p.updated_at.isoformat()
        if node.find('./PubmedData/History/PubMedPubDate[@PubStatus="entrez"]')
        is not None
        or node.find('./PubmedData/History/PubMedPubDate[@PubStatus="pubmed"]')
        is not None
        or node.find('./PubmedData/History/PubMedPubDate[@PubStatus="medline"]')
        is not None
        else None,
        node.findtext("./MedlineCitation/Article/Journal/Title", ""),
        node.findtext("./MedlineCitation/PMID"),
        node.findtext('./PubmedData/ArticleIdList/ArticleId[@IdType="pmc"]'),
    )
