"""Pure, network-free extraction of job postings from HTML pages.

This module deliberately does not fetch URLs, persist data, or mark any field as
verified. Callers are responsible for safely obtaining and validating page HTML.
"""

from __future__ import annotations

from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
import json
import re
from typing import Any
from urllib.parse import urljoin, urlparse


MAX_HTML_BYTES = 2 * 1024 * 1024
MAX_DESCRIPTION_CHARS = 50_000
MAX_FIELD_CHARS = 2_000
MAX_APPLICATION_URLS = 20
MAX_JSON_LD_SCRIPTS = 50

_APPLICATION_TERMS = re.compile(
    r"\b(candidatar(?:-se)?|candidatura|candidate-se|inscreva-se|"
    r"inscrever-se|aplicar|apply(?:\s+now)?|submit\s+application|"
    r"application\s+form|enviar\s+curr[ií]culo|envie\s+seu\s+curr[ií]culo|"
    r"quero\s me\s candidatar|register\s+interest)\b",
    re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]*>")
_SPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class ExtractedJob:
    title: str | None
    company: str | None
    company_hint: str | None
    description: str | None
    requirements: str | None
    location: str | None
    mode: str
    employment_type: str | None
    date_posted: str | None
    valid_through: str | None
    application_urls: tuple[str, ...]
    page_url: str
    extraction_method: str
    structured_data_found: bool
    missing_fields: tuple[str, ...]
    warnings: tuple[str, ...]


class _PageParser(HTMLParser):
    """Collect only bounded, relevant page metadata and visible text/links."""

    _IGNORED = {"script", "style", "noscript", "svg", "iframe", "template"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.h1_parts: list[str] = []
        self.meta: dict[str, str] = {}
        self.anchors: list[tuple[str, str]] = []
        self._ignored_depth = 0
        self._in_title = False
        self._h1_depth = 0
        self._anchor_depth = 0
        self._anchor_href = ""
        self._anchor_parts: list[str] = []
        self._current_json_ld: list[str] | None = None
        self.json_ld: list[str] = []
        self._json_depth = 0
        self._visible_parts: list[str] = []
        self._visible_chars = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {k.lower(): (v or "") for k, v in attrs}
        tag = tag.lower()
        if tag in self._IGNORED:
            self._ignored_depth += 1
            if tag == "script" and attr.get("type", "").lower().split(";")[0].strip() == "application/ld+json":
                if len(self.json_ld) < MAX_JSON_LD_SCRIPTS:
                    self._current_json_ld = []
            return
        if self._ignored_depth:
            return
        if tag == "meta":
            key = (attr.get("property") or attr.get("name") or attr.get("itemprop") or "").lower()
            value = _clean_text(attr.get("content", ""), MAX_FIELD_CHARS)
            if key and value and key not in self.meta:
                self.meta[key] = value
        elif tag == "title":
            self._in_title = True
        elif tag == "h1":
            self._h1_depth += 1
        elif tag == "a":
            if self._anchor_depth == 0:
                self._anchor_href = attr.get("href", "")
                self._anchor_parts = []
            self._anchor_depth += 1

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._IGNORED:
            if self._ignored_depth:
                self._ignored_depth -= 1
            if tag == "script" and self._current_json_ld is not None:
                self.json_ld.append("".join(self._current_json_ld))
                self._current_json_ld = None
            return
        if self._ignored_depth:
            return
        if tag == "title":
            self._in_title = False
        elif tag == "h1" and self._h1_depth:
            self._h1_depth -= 1
        elif tag == "a" and self._anchor_depth:
            self._anchor_depth -= 1
            if self._anchor_depth == 0:
                text = _clean_text(" ".join(self._anchor_parts), 500)
                if self._anchor_href and text and len(self.anchors) < 500:
                    self.anchors.append((self._anchor_href, text))
                self._anchor_href = ""
                self._anchor_parts = []

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            if self._current_json_ld is not None:
                self._current_json_ld.append(data)
            return
        if self._in_title and len(self.title_parts) < 100:
            self.title_parts.append(data)
        if self._h1_depth and len(self.h1_parts) < 100:
            self.h1_parts.append(data)
        if self._anchor_depth and len(self._anchor_parts) < 100:
            self._anchor_parts.append(data)
        if self._visible_chars < MAX_DESCRIPTION_CHARS:
            piece = data[: MAX_DESCRIPTION_CHARS - self._visible_chars]
            self._visible_parts.append(piece)
            self._visible_chars += len(piece)


def _clean_text(value: Any, limit: int = MAX_FIELD_CHARS) -> str | None:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return None
    text = unescape(_TAG_RE.sub(" ", str(value)))
    text = _SPACE_RE.sub(" ", text).strip()
    return text[:limit] or None


def _description_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    # Convert common block boundaries before removing remaining markup.
    text = re.sub(r"(?i)<\s*(br|/p|/div|/li|/h[1-6])\b[^>]*>", "\n", value)
    text = unescape(_TAG_RE.sub(" ", text))
    lines = [_SPACE_RE.sub(" ", line).strip() for line in text.splitlines()]
    result = "\n".join(line for line in lines if line)
    return result[:MAX_DESCRIPTION_CHARS] or None


def _as_items(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def _types(value: Any) -> set[str]:
    return {str(v).rsplit("/", 1)[-1].lower() for v in _as_items(value) if isinstance(v, str)}


def _iter_job_postings(value: Any):
    if isinstance(value, list):
        for item in value:
            yield from _iter_job_postings(item)
    elif isinstance(value, dict):
        if "jobposting" in _types(value.get("@type")):
            yield value
        graph = value.get("@graph")
        if graph is not None:
            yield from _iter_job_postings(graph)
        # Some publishers wrap JSON-LD objects in arbitrary arrays/properties.
        for key, child in value.items():
            if key not in {"@graph", "@context", "@type"} and isinstance(child, (dict, list)):
                yield from _iter_job_postings(child)


def _first_text(*values: Any, limit: int = MAX_FIELD_CHARS) -> str | None:
    for value in values:
        if isinstance(value, str):
            cleaned = _clean_text(value, limit)
            if cleaned:
                return cleaned
        elif isinstance(value, dict):
            cleaned = _clean_text(value.get("name") or value.get("value"), limit)
            if cleaned:
                return cleaned
    return None


def _postal_location(value: Any) -> str | None:
    parts: list[str] = []
    for item in _as_items(value):
        if not isinstance(item, dict):
            continue
        address = item.get("address", item)
        if isinstance(address, str):
            candidate = _clean_text(address)
            if candidate:
                parts.append(candidate)
            continue
        if not isinstance(address, dict):
            continue
        fields = [address.get(k) for k in ("addressLocality", "addressRegion", "postalCode", "addressCountry")]
        # Country may itself be a schema object.
        if isinstance(fields[-1], dict):
            fields[-1] = fields[-1].get("name")
        text = ", ".join(v for v in (_clean_text(x, 300) for x in fields) if v)
        if text and text not in parts:
            parts.append(text)
    return _clean_text("; ".join(parts), MAX_FIELD_CHARS) if parts else None


def _resolve_candidate_url(href: str, page_url: str) -> str | None:
    try:
        parsed_page = urlparse(page_url)
        if parsed_page.scheme.lower() not in {"http", "https"} or not parsed_page.hostname:
            return None
        resolved = urljoin(page_url, href.strip())
        parsed = urlparse(resolved)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return None
        if parsed.username or parsed.password:
            return None
        return resolved[:2_000]
    except (TypeError, ValueError):
        return None


def _mode_from_job(job: dict[str, Any], description: str | None) -> str:
    location_type = str(job.get("jobLocationType", "")).upper()
    if "TELECOMMUTE" in location_type:
        return "Remoto"
    combined = " ".join(str(x or "") for x in (job.get("title"), description, job.get("jobLocationType"))).lower()
    if re.search(r"\b(h[ií]brid[oa]|hybrid)\b", combined):
        return "Híbrido"
    if re.search(r"\b(remoto|remote|work from home|home office)\b", combined):
        return "Remoto"
    if job.get("jobLocation"):
        return "Presencial"
    return "Não informado"


def _job_from_jsonld(job: dict[str, Any], page_url: str, company_hint: str | None) -> ExtractedJob:
    organization = job.get("hiringOrganization")
    company = _first_text(organization.get("name") if isinstance(organization, dict) else organization)
    description = _description_text(job.get("description"))
    location = _postal_location(job.get("jobLocation"))
    if not location:
        location = _first_text(job.get("applicantLocationRequirements"))
    employment = job.get("employmentType")
    if isinstance(employment, list):
        employment_type = _clean_text(", ".join(str(x) for x in employment), 300)
    else:
        employment_type = _clean_text(employment, 300)
    apply_urls: list[str] = []
    for key in ("directApply", "applicationUrl", "applyUrl"):
        val = job.get(key)
        if isinstance(val, str) and val.lower().startswith(("http://", "https://", "/")):
            resolved = _resolve_candidate_url(val, page_url)
            if resolved and resolved not in apply_urls:
                apply_urls.append(resolved)
    title = _first_text(job.get("title"), job.get("name"))
    missing = tuple(name for name, val in (("title", title), ("company", company), ("description", description), ("location", location), ("employment_type", employment_type), ("date_posted", _clean_text(job.get("datePosted"))), ("valid_through", _clean_text(job.get("validThrough")))) if not val)
    warnings = ("Empresa não identificada nos dados estruturados.",) if not company else ()
    return ExtractedJob(
        title=title, company=company, company_hint=company_hint,
        description=description, requirements=None, location=location,
        mode=_mode_from_job(job, description), employment_type=employment_type,
        date_posted=_clean_text(job.get("datePosted"), 100),
        valid_through=_clean_text(job.get("validThrough"), 100),
        application_urls=tuple(apply_urls[:MAX_APPLICATION_URLS]), page_url=page_url,
        extraction_method="json-ld", structured_data_found=True,
        missing_fields=missing, warnings=warnings,
    )


def extract_jobs_from_html(
    html: str,
    page_url: str,
    company_hint: str | None = None,
) -> list[ExtractedJob]:
    """Extract JobPosting records from supplied HTML without making network calls.

    `page_url` is retained as the source page and used only to resolve relative
    candidate links. It is not treated as a verified application URL.
    """
    if not isinstance(html, str):
        raise TypeError("html deve ser uma string")
    if not isinstance(page_url, str) or not page_url.strip():
        raise ValueError("page_url deve ser uma URL não vazia")
    if len(html.encode("utf-8", errors="replace")) > MAX_HTML_BYTES:
        raise ValueError(f"HTML excede o limite de {MAX_HTML_BYTES} bytes")

    parser = _PageParser()
    parser.feed(html)
    parser.close()
    hint = _clean_text(company_hint, 200)

    structured: list[ExtractedJob] = []
    malformed_json = False
    for raw in parser.json_ld:
        try:
            payload = json.loads(raw.strip())
        except (json.JSONDecodeError, TypeError, ValueError):
            malformed_json = True
            continue
        for job in _iter_job_postings(payload):
            structured.append(_job_from_jsonld(job, page_url, hint))

    if structured:
        # Add application links discovered in page anchors to each structured job;
        # they remain candidates, not verified application destinations.
        discovered_links = []
        for href, text in parser.anchors:
            if _APPLICATION_TERMS.search(text):
                url = _resolve_candidate_url(href, page_url)
                if url and url not in discovered_links:
                    discovered_links.append(url)
        if discovered_links:
            structured = [
                ExtractedJob(**{**job.__dict__, "application_urls": tuple(dict.fromkeys((*job.application_urls, *discovered_links)))[:MAX_APPLICATION_URLS]})
                for job in structured
            ]
        return structured

    meta = parser.meta
    title = _first_text(meta.get("og:title"), meta.get("twitter:title"), " ".join(parser.h1_parts), " ".join(parser.title_parts))
    description = _first_text(meta.get("og:description"), meta.get("description"), meta.get("twitter:description"), limit=MAX_DESCRIPTION_CHARS)
    if not description:
        description = _clean_text(" ".join(parser._visible_parts), MAX_DESCRIPTION_CHARS)
    company = _first_text(meta.get("hiringorganization"), meta.get("company"), limit=200)

    application_urls: list[str] = []
    for href, text in parser.anchors:
        if _APPLICATION_TERMS.search(text):
            url = _resolve_candidate_url(href, page_url)
            if url and url not in application_urls:
                application_urls.append(url)
            if len(application_urls) >= MAX_APPLICATION_URLS:
                break

    visible = " ".join((title or "", description or "")).lower()
    if re.search(r"\b(h[ií]brid[oa]|hybrid)\b", visible):
        mode = "Híbrido"
    elif re.search(r"\b(remoto|remote|home office|work from home)\b", visible):
        mode = "Remoto"
    elif re.search(r"\b(presencial|on[- ]site|in[- ]person)\b", visible):
        mode = "Presencial"
    else:
        mode = "Não informado"

    missing = tuple(name for name, val in (("title", title), ("company", company), ("description", description), ("location", None), ("employment_type", None), ("date_posted", None), ("valid_through", None)) if not val)
    warnings = ["Dados extraídos por fallback HTML; podem estar incompletos."]
    if malformed_json:
        warnings.append("Um ou mais blocos JSON-LD estavam malformados.")
    if not company:
        warnings.append("Empresa não identificada; company_hint não foi promovido a empresa.")
    return [ExtractedJob(
        title=title, company=company, company_hint=hint,
        description=description, requirements=None, location=None, mode=mode,
        employment_type=None, date_posted=None, valid_through=None,
        application_urls=tuple(application_urls), page_url=page_url,
        extraction_method="html-fallback", structured_data_found=False,
        missing_fields=missing, warnings=tuple(warnings),
    )]
