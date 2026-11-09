from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit
import ipaddress
import socket

import httpx


USER_AGENT = "BuscaVagasBot/0.1 (+public job source discovery)"
REQUEST_TIMEOUT = 15.0
MAX_HTML_BYTES = 1_000_000
MAX_REDIRECTS = 5

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

    try:
        port = parsed.port
    except ValueError:
        raise ValueError("Porta do endereço inválida.")

    if port not in (None, 443):
        raise ValueError("Somente a porta HTTPS padrão é permitida.")

    host = parsed.hostname.lower().rstrip(".")

    if host == "localhost" or host.endswith(".localhost"):
        raise ValueError("Endereço local não permitido.")

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None

    if ip is not None and not ip.is_global:
        raise ValueError("Endereço IP privado não permitido.")

    return parsed


def _validate_public_dns(host: str) -> list[str]:
    """Resolve o domínio e retorna somente IPs públicos validados."""

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None

    if ip is not None:
        if not ip.is_global:
            raise ValueError("Endereço IP privado não permitido.")
        return [ip.compressed]

    try:
        results = socket.getaddrinfo(
            host,
            443,
            type=socket.SOCK_STREAM,
        )
    except OSError as exc:
        raise ValueError(
            "Não foi possível validar o endereço DNS."
        ) from exc

    addresses = {
        result[4][0].split("%", 1)[0]
        for result in results
    }

    if not addresses:
        raise ValueError("O domínio não possui endereço DNS válido.")

    validated = []

    for address in addresses:
        try:
            resolved_ip = ipaddress.ip_address(address)
        except ValueError as exc:
            raise ValueError(
                "O DNS retornou um endereço IP inválido."
            ) from exc

        if not resolved_ip.is_global:
            raise ValueError(
                "O domínio aponta para um endereço IP "
                "privado ou não público."
            )

        validated.append(resolved_ip.compressed)

    return sorted(validated)


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


def _allowed_redirect_host(
    candidate_host: str,
    official_host: str,
) -> bool:
    """Permite apenas o domínio oficial e sua versão com www."""

    base_host = official_host.removeprefix("www.")

    return candidate_host in {
        base_host,
        f"www.{base_host}",
    }


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


def _fetch_checked_html(
    client: httpx.Client,
    initial_url: str,
    official_host: str,
) -> tuple[str, str]:
    """
    Busca HTML seguindo redirecionamentos manualmente.

    Valida o domínio e o DNS antes de cada requisição.
    """

    current_url = initial_url

    for redirect_count in range(MAX_REDIRECTS + 1):
        parsed = _validate_public_https_url(current_url)

        host = parsed.hostname.lower().rstrip(".")

        if not _allowed_redirect_host(host, official_host):
            raise ValueError(
                "Redirecionamento para domínio não autorizado."
            )

        _validate_public_dns(host)

        with client.stream("GET", current_url) as response:

            if response.is_redirect:
                if redirect_count >= MAX_REDIRECTS:
                    raise ValueError(
                        "Limite de redirecionamentos excedido."
                    )

                location = response.headers.get("location")

                if not location:
                    raise ValueError(
                        "Redirecionamento sem endereço de destino."
                    )

                next_url = urljoin(current_url, location)

                next_parsed = _validate_public_https_url(next_url)

                next_host = (
                    next_parsed.hostname.lower().rstrip(".")
                )

                if not _allowed_redirect_host(
                    next_host,
                    official_host,
                ):
                    raise ValueError(
                        "O site tentou redirecionar para "
                        "outro domínio. Acesso bloqueado."
                    )

                current_url = next_url
                continue

            response.raise_for_status()

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

            return html, current_url

    raise ValueError(
        "Não foi possível acessar a página com segurança."
    )


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
        follow_redirects=False,
        headers=headers,
    ) as client:

        html, final_url = _fetch_checked_html(
            client,
            official_site_url,
            official_host,
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
