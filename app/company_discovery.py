
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit
import ipaddress

import httpx


USER_AGENT = "BuscaVagasBot/0.1 (+public job source discovery)"
REQUEST_TIMEOUT = 15.0
MAX_HTML_BYTES = 1_000_000

CAREER_TERMS = (
    "carreira",
    "carreiras",
    "trabalhe-conosco",
    "trabalhe conosco",
    "oportunidades",
    "vagas",
    "career",
    "careers",
    "jobs",
    "work-with-us",
    "join-us",
)


@dataclass(frozen=True)
class CareerPageCandidate:
    company_name: str
    page_url: str
    link_text: str


class _LinkParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.current_href = None
        self.current_text = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a":
            attributes = dict(attrs)
            self.current_href = attributes.get("href")
            self.current_text = []

    def handle_data(self, data):
        if self.current_href is not None:
            self.current_text.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "a" and self.current_href is not None:
            text = " ".join(" ".join(self.current_text).split())

            self.links.append((self.current_href, text))

            self.current_href = None
            self.current_text = []


def _validate_public_https_url(url: str):
    parsed = urlsplit(url.strip())

    if parsed.scheme.lower() != "https":
        raise ValueError("O site precisa utilizar HTTPS.")

    if not parsed.hostname:
        raise ValueError("Endereço do site inválido.")

    if parsed.username or parsed.password:
        raise ValueError("A URL não pode conter credenciais.")

    host = parsed.hostname.lower().rstrip(".")

    if host == "localhost" or host.endswith(".localhost"):
        raise ValueError("Endereço local não permitido.")

    try:
        ip = ipaddress.ip_address(host)
        if not ip.is_global:
            raise ValueError("Endereço IP privado não permitido.")
    except ValueError as exc:
        if "não permitido" in str(exc):
            raise

    return parsed


def _same_company_domain(
    candidate_host: str,
    official_host: str,
) -> bool:
    base_host = official_host.removeprefix("www.")

    return (
        candidate_host == base_host
        or candidate_host == f"www.{base_host}"
        or candidate_host.endswith(f".{base_host}")
    )


def _normalize_page_url(url: str) -> str:
    parsed = urlsplit(url)

    return urlunsplit((
        "https",
        parsed.netloc.lower(),
        parsed.path or "/",
        parsed.query,
        "",
    ))


def _is_career_link(url: str, link_text: str) -> bool:
    searchable = f"{url} {link_text}".lower()

    return any(term in searchable for term in CAREER_TERMS)


def discover_career_pages(
    company_name: str,
    official_site_url: str,
) -> list[CareerPageCandidate]:
    """
    Procura possíveis páginas de carreiras dentro do domínio
    do site oficial informado.

    Não cadastra fontes no banco e não publica oportunidades.
    """

    company_name = company_name.strip()

    if len(company_name) < 2:
        raise ValueError("Informe um nome de empresa válido.")

    official = _validate_public_https_url(official_site_url)

    official_host = official.hostname.lower().rstrip(".")

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html",
    }

    with httpx.Client(
        timeout=REQUEST_TIMEOUT,
        follow_redirects=True,
        headers=headers,
    ) as client:
        with client.stream(
            "GET",
            official_site_url,
        ) as response:
            response.raise_for_status()

            final_url = str(response.url)
            final = _validate_public_https_url(final_url)

            if not _same_company_domain(
                final.hostname.lower().rstrip("."),
                official_host,
            ):
                raise ValueError(
                    "O site redirecionou para outro domínio. "
                    "A origem precisa ser conferida."
                )

            content_type = response.headers.get(
                "content-type",
                "",
            ).lower()

            if "text/html" not in content_type:
                raise ValueError(
                    "O endereço não retornou uma página HTML."
                )

            chunks = []
            total_bytes = 0

            for chunk in response.iter_bytes():
                total_bytes += len(chunk)

                if total_bytes > MAX_HTML_BYTES:
                    raise ValueError(
                        "Página muito grande para análise."
                    )

                chunks.append(chunk)

    html = b"".join(chunks).decode(
        "utf-8",
        errors="replace",
    )

    parser = _LinkParser()
    parser.feed(html)

    candidates = []
    seen_urls = set()

    for href, link_text in parser.links:
        if not href:
            continue

        absolute_url = urljoin(final_url, href)

        try:
            parsed = _validate_public_https_url(absolute_url)
        except ValueError:
            continue

        host = parsed.hostname.lower().rstrip(".")

        if not _same_company_domain(host, official_host):
            continue

        normalized_url = _normalize_page_url(absolute_url)

        if normalized_url in seen_urls:
            continue

        if not _is_career_link(normalized_url, link_text):
            continue

        seen_urls.add(normalized_url)

        candidates.append(
            CareerPageCandidate(
                company_name=company_name,
                page_url=normalized_url,
                link_text=link_text,
            )
        )

    return candidates
