"""Pure, bounded and network-free extraction of job postings from HTML.

This module does not fetch URLs, persist records, or verify companies,
application links, dates, or any other criterion. URL safety beyond basic
syntax validation belongs to the dedicated page fetcher/verifier.
"""

from __future__ import annotations

from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
import json
import re
from typing import Any, Iterator
from urllib.parse import urljoin, urlparse, urlunparse

MAX_HTML_BYTES = 2 * 1024 * 1024
MAX_DESCRIPTION_CHARS = 50_000
MAX_FIELD_CHARS = 2_000
MAX_APPLICATION_URLS = 20
MAX_JSON_LD_SCRIPTS = 50
MAX_EXTRACTED_JOBS = 50
MAX_JSON_NODES = 10_000
MAX_JSON_DEPTH = 40
MAX_ANCHORS = 500

_APPLICATION_TERMS = re.compile(
    r"\b(candidatar(?:-se)?|candidatura|candidate-se|inscreva-se|"
    r"inscrever-se|aplicar|apply(?:\s+now)?|submit\s+application|"
    r"application\s+form|enviar\s+curr[ií]culo|envie\s+seu\s+curr[ií]culo|"
    r"quero\s+me\s+candidatar|register\s+interest)\b",
    re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]*>")
_SPACE_RE = re.compile(r"\s+")
_BLOCK_TAG_RE = re.compile(r"(?i)<\s*(?:br\s*/?|/p|/div|/li|/h[1-6]|/section|/article)\b[^>]*>")


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
    canonical_url: str | None = None
    identifier: str | None = None
    job_url: str | None = None


class _PageParser(HTMLParser):
    """Collect bounded metadata, structured data and likely main-content text."""

    _IGNORED = {"script", "style", "noscript", "svg", "iframe", "template"}
    _NON_CONTENT = {"nav", "footer", "header", "aside"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.h1_parts: list[str] = []
        self.meta: dict[str, str] = {}
        self.anchors: list[tuple[str, str]] = []
        self.json_ld: list[str] = []
        self.canonical_urls: list[str] = []
        self._ignored_stack: list[str] = []
        self._non_content_depth = 0
        self._in_title = False
        self._h1_depth = 0
        self._anchor_depth = 0
        self._anchor_href = ""
        self._anchor_parts: list[str] = []
        self._current_json_ld: list[str] | None = None
        self._visible_parts: list[str] = []
        self._visible_chars = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attr = {key.lower(): (value or "") for key, value in attrs}
        if tag in self._IGNORED:
            self._ignored_stack.append(tag)
            if tag == "script" and attr.get("type", "").lower().split(";")[0].strip() == "application/ld+json":
                if len(self.json_ld) < MAX_JSON_LD_SCRIPTS:
                    self._current_json_ld = []
            return
        if self._ignored_stack:
            return
        if tag in self._NON_CONTENT:
            self._non_content_depth += 1
        if tag == "meta":
            key = (attr.get("property") or attr.get("name") or attr.get("itemprop") or "").lower()
            value = _clean_text(attr.get("content", ""), MAX_FIELD_CHARS)
            if key and value and key not in self.meta:
                self.meta[key] = value
        elif tag == "link" and "canonical" in attr.get("rel", "").lower().split():
            if attr.get("href") and len(self.canonical_urls) < 5:
                self.canonical_urls.append(attr["href"])
        elif tag == "title":
            self._in_title = True
        elif tag == "h1":
            self._h1_depth += 1
        elif tag == "a" and self._anchor_depth == 0:
            self._anchor_href = attr.get("href", "")
            self._anchor_parts = []
            self._anchor_depth = 1
        elif tag == "a" and self._anchor_depth:
            self._anchor_depth += 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._IGNORED:
            # HTMLParser is tolerant of malformed markup; close only the most
            # recent matching ignored element to avoid corrupting nested state.
            if tag in self._ignored_stack:
                index = len(self._ignored_stack) - 1 - self._ignored_stack[::-1].index(tag)
                del self._ignored_stack[index:]
            if tag == "script" and self._current_json_ld is not None:
                self.json_ld.append("".join(self._current_json_ld))
                self._current_json_ld = None
            return
        if self._ignored_stack:
            return
        if tag in self._NON_CONTENT and self._non_content_depth:
            self._non_content_depth -= 1
        if tag == "title":
            self._in_title = False
        elif tag == "h1" and self._h1_depth:
            self._h1_depth -= 1
        elif tag == "a" and self._anchor_depth:
            self._anchor_depth -= 1
            if self._anchor_depth == 0:
                text = _clean_text(" ".join(self._anchor_parts), 500)
                if self._anchor_href and text and len(self.anchors) < MAX_ANCHORS:
                    self.anchors.append((self._anchor_href, text))
                self._anchor_href = ""
                self._anchor_parts = []

    def handle_data(self, data: str) -> None:
        if self._ignored_stack:
            if self._current_json_ld is not None:
                self._current_json_ld.append(data)
            return
        if self._in_title and len(self.title_parts) < 100:
            self.title_parts.append(data)
        if self._h1_depth and len(self.h1_parts) < 100:
            self.h1_parts.append(data)
        if self._anchor_depth and len(self._anchor_parts) < 100:
            self._anchor_parts.append(data)
        if not self._non_content_depth and self._visible_chars < MAX_DESCRIPTION_CHARS:
            piece = data[: MAX_DESCRIPTION_CHARS - self._visible_chars]
            if piece.strip():
                self._visible_parts.append(piece)
                self._visible_chars += len(piece)


def _clean_text(value: Any, limit: int = MAX_FIELD_CHARS) -> str | None:
    if value is None or isinstance(value, (dict, list, tuple)):
        return None
    text = unescape(_TAG_RE.sub(" ", str(value)))
    text = _SPACE_RE.sub(" ", text).strip()
    return text[:limit] or None


def _description_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = _BLOCK_TAG_RE.sub("\n", value)
    text = unescape(_TAG_RE.sub(" ", text))
    lines = [_SPACE_RE.sub(" ", line).strip() for line in text.splitlines()]
    result = "\n".join(line for line in lines if line)
    return result[:MAX_DESCRIPTION_CHARS] or None


def _as_items(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def _types(value: Any) -> set[str]:
    return {item.rsplit("/", 1)[-1].rsplit("#", 1)[-1].lower() for item in _as_items(value) if isinstance(item, str)}


def _iter_job_postings(value: Any) -> Iterator[dict[str, Any]]:
    """Iterate JobPosting nodes without unbounded recursive traversal."""
    stack: list[tuple[Any, int]] = [(value, 0)]
    visited_nodes = 0
    yielded = 0
    while stack and visited_nodes < MAX_JSON_NODES and yielded < MAX_EXTRACTED_JOBS:
        node, depth = stack.pop()
        visited_nodes += 1
        if depth > MAX_JSON_DEPTH:
            continue
        if isinstance(node, list):
            for child in reversed(node[:MAX_JSON_NODES]):
                stack.append((child, depth + 1))
        elif isinstance(node, dict):
            if "jobposting" in _types(node.get("@type")):
                yield node
                yielded += 1
            # Reverse insertion order so traversal follows source order.
            for key, child in reversed(list(node.items())):
                if key not in {"@context", "@type"} and isinstance(child, (dict, list)):
                    stack.append((child, depth + 1))


def _first_text(*values: Any, limit: int = MAX_FIELD_CHARS) -> str | None:
    for value in values:
        if isinstance(value, str):
            cleaned = _clean_text(value, limit)
            if cleaned:
                return cleaned
        elif isinstance(value, dict):
            cleaned = _clean_text(value.get("name") or value.get("value") or value.get("@id"), limit)
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
            if candidate and candidate not in parts:
                parts.append(candidate)
            continue
        if not isinstance(address, dict):
            continue
        fields = [address.get(key) for key in ("addressLocality", "addressRegion", "postalCode", "addressCountry")]
        if isinstance(fields[-1], dict):
            fields[-1] = fields[-1].get("name") or fields[-1].get("@id")
        text = ", ".join(value for value in (_clean_text(part, 300) for part in fields) if value)
        if text and text not in parts:
            parts.append(text)
    return _clean_text("; ".join(parts), MAX_FIELD_CHARS) if parts else None


def _resolve_candidate_url(href: str, page_url: str) -> str | None:
    try:
        base = urlparse(page_url)
        if base.scheme.lower() not in {"http", "https"} or not base.hostname:
            return None
        if base.username or base.password:
            return None
        raw = href.strip()
        if not raw or raw.startswith(("#", "javascript:", "data:", "mailto:", "tel:")):
            return None
        resolved = urljoin(page_url, raw)
        parsed = urlparse(resolved)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return None
        if parsed.username or parsed.password:
            return None
        # Fragments do not identify a different application endpoint.
        return urlunparse(parsed._replace(fragment=""))[:2_000]
    except (TypeError, ValueError):
        return None


def _requirements_from_description(description: str | None) -> str | None:
    if not description:
        return None
    # Preserve a requirements subsection when the publisher labels one.
    match = re.search(
        r"(?is)(?:^|\n)\s*(?:requisitos|qualifica[cç][õo]es|requirements|qualifications)\s*:?\s*\n?(.*?)(?=\n\s*(?:responsabilidades|benef[ií]cios|sobre a empresa|about us|responsibilities|benefits)\s*:|$)",
        description,
    )
    if match:
        return match.group(1).strip()[:MAX_DESCRIPTION_CHARS] or None
    return None


def _requirements_from_job(job: dict[str, Any], description: str | None) -> str | None:
    values: list[str] = []
    for key in ("qualifications", "skills", "educationRequirements", "experienceRequirements", "experienceInPlaceOfEducation"):
        for item in _as_items(job.get(key)):
            if isinstance(item, dict):
                item = item.get("name") or item.get("description") or item.get("value")
            text = _clean_text(item, 2_000)
            if text and text not in values:
                values.append(text)
    if values:
        return "\n".join(values)[:MAX_DESCRIPTION_CHARS]
    return _requirements_from_description(description)


def _mode_from_job(job: dict[str, Any], description: str | None) -> str:
    location_type = " ".join(str(value or "") for value in _as_items(job.get("jobLocationType"))).upper()
    if "TELECOMMUTE" in location_type:
        return "Remoto"
    text = " ".join(str(value or "") for value in (job.get("title"), description, job.get("jobLocationType"))).lower()
    if re.search(r"\b(h[ií]brid[oa]|hybrid)\b", text):
        return "Híbrido"
    if re.search(r"\b(remoto|remote|work from home|home office)\b", text):
        return "Remoto"
    if job.get("jobLocation"):
        return "Presencial"
    return "Não informado"


def _identifier_value(value: Any) -> str | None:
    """Return a stable identifier, preferring PropertyValue.value over its label."""
    candidates = value if isinstance(value, list) else [value]
    for candidate in candidates:
        if isinstance(candidate, dict):
            # Schema.org PropertyValue commonly uses name as the field label
            # (for example, "Código da vaga") and value as the actual ID.
            for key in ("value", "@id", "name"):
                raw = candidate.get(key)
                if isinstance(raw, dict):
                    cleaned = _first_text(raw, limit=500)
                else:
                    cleaned = _clean_text(raw, 500)
                if cleaned:
                    return cleaned
        else:
            cleaned = _clean_text(candidate, 500)
            if cleaned:
                return cleaned
    return None


def _canonical_value(value: Any, page_url: str) -> str | None:
    if isinstance(value, dict):
        value = value.get("url") or value.get("@id") or value.get("name")
    if not isinstance(value, str):
        return None
    return _resolve_candidate_url(value, page_url)


def _job_from_jsonld(job: dict[str, Any], page_url: str, company_hint: str | None, canonical_url: str | None) -> ExtractedJob:
    organization = job.get("hiringOrganization")
    company = None
    for organization_item in _as_items(organization):
        company = _first_text(organization_item.get("name") if isinstance(organization_item, dict) else organization_item)
        if company:
            break
    description = _description_text(job.get("description"))
    location = _postal_location(job.get("jobLocation"))
    if not location:
        location = _first_text(job.get("applicantLocationRequirements"))
    employment = job.get("employmentType")
    if isinstance(employment, list):
        employment_type = _clean_text(", ".join(str(item) for item in employment), 300)
    else:
        employment_type = _clean_text(employment, 300)

    application_urls: list[str] = []
    for key in ("applicationUrl", "applyUrl"):
        for item in _as_items(job.get(key)):
            if isinstance(item, str):
                resolved = _resolve_candidate_url(item, page_url)
                if resolved and resolved not in application_urls:
                    application_urls.append(resolved)
    direct_apply = job.get("directApply")
    if isinstance(direct_apply, str):
        resolved = _resolve_candidate_url(direct_apply, page_url)
        if resolved and resolved not in application_urls:
            application_urls.append(resolved)

    title = _first_text(job.get("title"), job.get("name"))
    date_posted = _clean_text(job.get("datePosted"), 100)
    valid_through = _clean_text(job.get("validThrough"), 100)
    identifier = _identifier_value(job.get("identifier"))
    job_url = _canonical_value(job.get("url") or job.get("mainEntityOfPage"), page_url)
    missing = tuple(name for name, value in (
        ("title", title), ("company", company), ("description", description),
        ("location", location), ("employment_type", employment_type),
        ("date_posted", date_posted), ("valid_through", valid_through),
    ) if not value)
    warnings = []
    if not company:
        warnings.append("Empresa não identificada nos dados estruturados.")
    if not description:
        warnings.append("Descrição da vaga ausente nos dados estruturados.")
    return ExtractedJob(
        title=title, company=company, company_hint=company_hint,
        description=description, requirements=_requirements_from_job(job, description),
        location=location, mode=_mode_from_job(job, description),
        employment_type=employment_type, date_posted=date_posted,
        valid_through=valid_through,
        application_urls=tuple(application_urls[:MAX_APPLICATION_URLS]),
        page_url=page_url, extraction_method="json-ld", structured_data_found=True,
        missing_fields=missing, warnings=tuple(warnings),
        canonical_url=canonical_url, identifier=identifier, job_url=job_url,
    )


def _deduplicate_jobs(jobs: list[ExtractedJob]) -> list[ExtractedJob]:
    result: list[ExtractedJob] = []
    seen: set[tuple[Any, ...]] = set()
    for job in jobs:
        stable_id = job.identifier or job.job_url
        if stable_id:
            key = ("stable", job.title, job.company, stable_id)
        else:
            # Without a publisher ID or job-specific URL, use the available
            # vacancy content as a fingerprint. Title/company alone are not
            # sufficient: employers often publish several roles with the
            # same title in different locations or with different descriptions.
            key = (
                "content",
                job.title,
                job.company,
                job.description,
                job.requirements,
                job.location,
                job.mode,
                job.employment_type,
                job.date_posted,
                job.valid_through,
                job.application_urls,
            )
        if key not in seen:
            seen.add(key)
            result.append(job)
        if len(result) >= MAX_EXTRACTED_JOBS:
            break
    return result


def extract_jobs_from_html(html: str, page_url: str, company_hint: str | None = None) -> list[ExtractedJob]:
    """Extract JobPosting records from supplied HTML without making network calls.

    `page_url` is retained as the fetched page URL. Canonical and JSON-LD job
    URLs are stored separately and are never treated as verified destinations.
    """
    if not isinstance(html, str):
        raise TypeError("html deve ser uma string")
    if not isinstance(page_url, str) or not page_url.strip():
        raise ValueError("page_url deve ser uma URL não vazia")
    try:
        parsed_page = urlparse(page_url)
        if parsed_page.scheme.lower() not in {"http", "https"} or not parsed_page.hostname:
            raise ValueError
        if parsed_page.username or parsed_page.password:
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError("page_url deve ser uma URL HTTP(S) válida") from None
    if len(html.encode("utf-8", errors="replace")) > MAX_HTML_BYTES:
        raise ValueError(f"HTML excede o limite de {MAX_HTML_BYTES} bytes")

    parser = _PageParser()
    parser.feed(html)
    parser.close()
    hint = _clean_text(company_hint, 200)
    canonical_url = next((_resolve_candidate_url(url, page_url) for url in parser.canonical_urls if _resolve_candidate_url(url, page_url)), None)

    structured: list[ExtractedJob] = []
    malformed_json = False
    for raw in parser.json_ld:
        try:
            payload = json.loads(raw.strip())
        except (json.JSONDecodeError, TypeError, ValueError):
            malformed_json = True
            continue
        for job in _iter_job_postings(payload):
            structured.append(_job_from_jsonld(job, page_url, hint, canonical_url))
            if len(structured) >= MAX_EXTRACTED_JOBS:
                break
        if len(structured) >= MAX_EXTRACTED_JOBS:
            break

    if structured:
        # Page-level anchor links are unambiguously assignable only when the
        # page contains one structured posting. With multiple postings, do not
        # cross-assign generic links to every job.
        if len(structured) == 1:
            job = structured[0]
            links = list(job.application_urls)
            for href, anchor_text in parser.anchors:
                if _APPLICATION_TERMS.search(anchor_text):
                    url = _resolve_candidate_url(href, page_url)
                    if url and url not in links:
                        links.append(url)
                    if len(links) >= MAX_APPLICATION_URLS:
                        break
            structured[0] = ExtractedJob(**{**job.__dict__, "application_urls": tuple(links[:MAX_APPLICATION_URLS])})
        elif any(_APPLICATION_TERMS.search(anchor_text) for _, anchor_text in parser.anchors):
            structured = [ExtractedJob(**{
                **job.__dict__,
                "warnings": tuple((*job.warnings, "Links de candidatura encontrados na página não foram associados porque há várias vagas estruturadas.")),
            }) for job in structured]
        return _deduplicate_jobs(structured)

    meta = parser.meta
    title = _first_text(meta.get("og:title"), meta.get("twitter:title"), " ".join(parser.h1_parts), " ".join(parser.title_parts))
    description = _first_text(meta.get("og:description"), meta.get("description"), meta.get("twitter:description"), limit=MAX_DESCRIPTION_CHARS)
    if not description:
        description = _description_text(" ".join(parser._visible_parts))
    company = _first_text(meta.get("hiringorganization"), meta.get("company"), limit=200)

    application_urls: list[str] = []
    for href, anchor_text in parser.anchors:
        if _APPLICATION_TERMS.search(anchor_text):
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

    missing = tuple(name for name, value in (
        ("title", title), ("company", company), ("description", description),
        ("location", None), ("employment_type", None), ("date_posted", None), ("valid_through", None),
    ) if not value)
    warnings = ["Dados extraídos por fallback HTML; podem estar incompletos."]
    if malformed_json:
        warnings.append("Um ou mais blocos JSON-LD estavam malformados.")
    if not company:
        warnings.append("Empresa não identificada; company_hint não foi promovido a empresa.")
    if not description:
        warnings.append("Descrição não encontrada; o texto genérico da página não foi presumido como descrição da vaga.")
    return [ExtractedJob(
        title=title, company=company, company_hint=hint, description=description,
        requirements=_requirements_from_description(description), location=None, mode=mode, employment_type=None,
        date_posted=None, valid_through=None, application_urls=tuple(application_urls),
        page_url=page_url, extraction_method="html-fallback", structured_data_found=False,
        missing_fields=missing, warnings=tuple(warnings), canonical_url=canonical_url,
    )]
