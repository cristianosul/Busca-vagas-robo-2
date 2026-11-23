from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import (
    parse_qsl,
    urlencode,
    urljoin,
    urlsplit,
    urlunsplit,
)
from contextlib import closing
import ipaddress
import time
import threading
from urllib.robotparser import RobotFileParser

import dns.exception
import dns.resolver
import httpx


USER_AGENT = "BuscaVagasBot/0.1 (+public job source discovery)"
REQUEST_TIMEOUT = 15.0
MAX_DISCOVERY_SECONDS = 30.0
MAX_HTML_BYTES = 1_000_000
MAX_REDIRECTS = 5
MAX_URL_LENGTH = 2048
MAX_COMPANY_NAME_LENGTH = 150
MAX_LINKS = 500
MAX_LINK_TEXT_LENGTH = 1000
HTML_PARSE_CHUNK_SIZE = 8192
MIN_DOMAIN_REQUEST_INTERVAL = 1.0

_DOMAIN_RATE_LOCK = threading.Lock()
_DOMAIN_LAST_REQUEST = {}

TRACKING_PARAMETERS = {
    "gclid",
    "fbclid",
    "dclid",
    "msclkid",
    "igshid",
    "mc_cid",
    "mc_eid",
    "ref_src",
    "ref_url",
}

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


def _remaining_time(deadline: float) -> float:
    """Retorna o tempo restante da operação ou interrompe a execução."""

    remaining = deadline - time.monotonic()

    if remaining <= 0:
        raise TimeoutError(
            "O tempo limite total da descoberta foi excedido."
        )

    return remaining


def _wait_for_domain_rate_limit(host: str, deadline: float) -> None:
    """Serializa requisições por domínio sem reservar horários futuros."""

    host = _canonical_hostname(host)

    # O lock permanece durante a espera para que duas threads não iniciem
    # requisições simultâneas ao mesmo domínio.
    with _DOMAIN_RATE_LOCK:
        now = time.monotonic()
        last_request = _DOMAIN_LAST_REQUEST.get(host)

        delay = 0.0
        if last_request is not None:
            delay = max(
                0.0,
                MIN_DOMAIN_REQUEST_INTERVAL - (now - last_request),
            )

        if delay > 0:
            if delay >= _remaining_time(deadline):
                raise TimeoutError(
                    "O limite de tempo foi excedido aguardando o intervalo "
                    "de segurança do domínio."
                )
            time.sleep(delay)

        _remaining_time(deadline)
        # Registra somente quando a requisição está prestes a começar.
        # Uma espera cancelada/expirada não deixa reserva pendente.
        _DOMAIN_LAST_REQUEST[host] = time.monotonic()


def _fetch_robots_policy(
    client: httpx.Client,
    official_url: str,
    official_host: str,
    deadline: float,
    dns_cache: dict[str, list[str]],
) -> RobotFileParser:
    """Obtém e interpreta robots.txt; falhas não-404 bloqueiam a coleta."""

    parsed = urlsplit(official_url)
    robots_url = urlunsplit((
        parsed.scheme,
        parsed.netloc,
        "/robots.txt",
        "",
        "",
    ))

    # Argumentos posicionais mantêm compatibilidade com mocks simples
    # usados nos testes, sem alterar a política aplicada à requisição.
    robots_text, _ = _fetch_checked_html(
        client,
        robots_url,
        official_host,
        deadline,
        None,
        True,
        dns_cache,
    )

    parser = RobotFileParser()
    parser.set_url(robots_url)
    parser.parse(robots_text.splitlines())
    return parser


@dataclass(frozen=True)
class CareerPageCandidate:
    company_name: str
    page_url: str
    link_text: str


class _LinkParser(HTMLParser):
    """Extrai links sem acumular uma pilha ilimitada de âncoras."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.current_anchor = None

    def _save_current_anchor(self):
        anchor = self.current_anchor
        self.current_anchor = None

        if anchor is None or len(self.links) >= MAX_LINKS:
            return

        href = anchor["href"]
        if href is None:
            return

        text = " ".join(" ".join(anchor["text"]).split())
        self.links.append((href, text))

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "a":
            return

        # Âncoras HTML aninhadas são inválidas. Fecha a anterior para
        # impedir crescimento ilimitado da pilha em HTML malformado.
        self._save_current_anchor()

        if len(self.links) >= MAX_LINKS:
            return

        attributes = dict(attrs)
        href = attributes.get("href")
        if href is None:
            return

        self.current_anchor = {
            "href": href,
            "text": [],
            "text_length": 0,
        }

    def handle_data(self, data):
        anchor = self.current_anchor
        if anchor is None:
            return

        remaining = MAX_LINK_TEXT_LENGTH - anchor["text_length"]
        if remaining <= 0:
            return

        fragment = data[:remaining]
        if fragment:
            anchor["text"].append(fragment)
            anchor["text_length"] += len(fragment)

    def handle_endtag(self, tag):
        if tag.lower() == "a":
            self._save_current_anchor()

    def close(self):
        super().close()
        # Aproveita links cujo fechamento não veio no HTML.
        self._save_current_anchor()


def _parse_html_links(html: str, deadline: float) -> list[tuple[str, str]]:
    """Analisa HTML em blocos, verificando o prazo entre cada bloco."""

    parser = _LinkParser()

    for offset in range(0, len(html), HTML_PARSE_CHUNK_SIZE):
        _remaining_time(deadline)
        parser.feed(html[offset:offset + HTML_PARSE_CHUNK_SIZE])

    _remaining_time(deadline)
    parser.close()
    _remaining_time(deadline)

    return parser.links


def _canonical_hostname(host: str) -> str:
    """Normaliza e valida um hostname, incluindo domínios internacionais."""

    host = host.lower().rstrip(".")

    if not host:
        raise ValueError("Endereço do site inválido.")

    try:
        ip = ipaddress.ip_address(host)
        return ip.compressed
    except ValueError:
        pass

    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("Nome de domínio inválido.") from exc

    if len(ascii_host) > 253:
        raise ValueError("Nome de domínio muito longo.")

    labels = ascii_host.split(".")

    for label in labels:
        if (
            not label
            or len(label) > 63
            or not label[0].isalnum()
            or not label[-1].isalnum()
            or any(
                not (character.isalnum() or character == "-")
                for character in label
            )
        ):
            raise ValueError("Nome de domínio inválido.")

    return ascii_host


def _validate_public_https_url(url: str):
    """Valida HTTPS, hostname, porta e ausência de credenciais."""

    if not isinstance(url, str):
        raise ValueError("O endereço do site precisa ser um texto.")

    if len(url) > MAX_URL_LENGTH:
        raise ValueError("Endereço do site muito longo.")

    if any(
        character.isspace() or ord(character) < 32
        for character in url
    ):
        raise ValueError("Endereço do site inválido.")

    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise ValueError("Endereço do site inválido.") from exc

    if parsed.scheme.lower() != "https":
        raise ValueError("O site precisa utilizar HTTPS.")

    if not parsed.hostname:
        raise ValueError("Endereço do site inválido.")

    if parsed.username or parsed.password:
        raise ValueError("A URL não pode conter credenciais.")

    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Porta do endereço inválida.") from exc

    if port not in (None, 443):
        raise ValueError("Somente a porta HTTPS padrão é permitida.")

    host = _canonical_hostname(parsed.hostname)

    if host == "localhost" or host.endswith(".localhost"):
        raise ValueError("Endereço local não permitido.")

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None

    if ip is not None and not ip.is_global:
        raise ValueError("Endereço IP privado não permitido.")

    return parsed


def _validate_public_dns(
    host: str,
    deadline: float,
) -> list[str]:
    """Resolve A e AAAA com timeout e retorna somente IPs públicos."""

    host = _canonical_hostname(host)

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None

    if ip is not None:
        if not ip.is_global:
            raise ValueError("Endereço IP privado não permitido.")
        return [ip.compressed]

    resolver = dns.resolver.Resolver(configure=True)
    remaining = _remaining_time(deadline)

    resolver.timeout = min(3.0, remaining)
    resolver.lifetime = min(3.0, remaining)

    addresses = set()
    successful_responses = 0

    for record_type in ("A", "AAAA"):
        remaining = _remaining_time(deadline)

        try:
            answers = resolver.resolve(
                host,
                record_type,
                lifetime=min(3.0, remaining),
                raise_on_no_answer=False,
            )

        except dns.resolver.NXDOMAIN as exc:
            raise ValueError(
                "O domínio não possui endereço DNS válido."
            ) from exc

        except dns.resolver.NoNameservers as exc:
            raise ValueError(
                "Não foi possível validar o endereço DNS."
            ) from exc

        except dns.exception.Timeout as exc:
            raise ValueError(
                "A consulta DNS excedeu o tempo limite."
            ) from exc

        except dns.resolver.NoAnswer:
            successful_responses += 1
            continue

        except Exception as exc:
            raise ValueError(
                "Ocorreu uma falha inesperada durante a consulta DNS."
            ) from exc

        if answers.rrset is None:
            continue

        successful_responses += 1

        for answer in answers:
            addresses.add(str(answer))

    if successful_responses == 0 or not addresses:
        raise ValueError(
            "O domínio não possui endereço DNS válido."
        )

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


def _url_with_pinned_ip(url: str, ip: str) -> str:
    """Monta a URL com o IP validado, preservando caminho e consulta."""

    parsed = urlsplit(url)
    address = ipaddress.ip_address(ip)

    if address.version == 6:
        netloc = f"[{address.compressed}]"
    else:
        netloc = address.compressed

    return urlunsplit((
        parsed.scheme,
        netloc,
        parsed.path or "/",
        parsed.query,
        "",
    ))


def _same_company_domain(
    candidate_host: str,
    official_host: str,
) -> bool:
    try:
        official_host = _canonical_hostname(official_host)
        candidate_host = _canonical_hostname(candidate_host)
    except ValueError:
        return False

    try:
        official_ip = ipaddress.ip_address(official_host)
    except ValueError:
        official_ip = None

    if official_ip is not None:
        try:
            candidate_ip = ipaddress.ip_address(candidate_host)
        except ValueError:
            return False

        return candidate_ip == official_ip

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

    try:
        official_host = _canonical_hostname(official_host)
        candidate_host = _canonical_hostname(candidate_host)
    except ValueError:
        return False

    try:
        official_ip = ipaddress.ip_address(official_host)
    except ValueError:
        official_ip = None

    if official_ip is not None:
        try:
            candidate_ip = ipaddress.ip_address(candidate_host)
        except ValueError:
            return False

        return candidate_ip == official_ip

    base_host = official_host.removeprefix("www.")

    return candidate_host in {
        base_host,
        f"www.{base_host}",
    }


def _normalize_page_url(url: str) -> str:
    """Normaliza a URL e remove fragmentos e parâmetros de rastreamento."""

    parsed = urlsplit(url)
    host = _canonical_hostname(parsed.hostname)

    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Porta do endereço inválida.") from exc

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None

    if ip is not None and ip.version == 6:
        netloc = f"[{host}]"
    else:
        netloc = host

    if port not in (None, 443):
        netloc = f"{netloc}:{port}"

    query_items = parse_qsl(
        parsed.query,
        keep_blank_values=True,
    )

    filtered_query = []

    for key, value in query_items:
        lowered_key = key.lower()

        if lowered_key.startswith("utm_"):
            continue

        if lowered_key in TRACKING_PARAMETERS:
            continue

        filtered_query.append((key, value))

    normalized_query = urlencode(sorted(filtered_query))

    return urlunsplit((
        "https",
        netloc,
        parsed.path or "/",
        normalized_query,
        "",
    ))


def _is_career_link(url: str, link_text: str) -> bool:
    searchable = f"{url} {link_text}".lower()

    return any(term in searchable for term in CAREER_TERMS)


def _send_to_validated_ips(
    client: httpx.Client,
    url: str,
    host: str,
    validated_ips: list[str],
    deadline: float,
):
    """Tenta os IPs públicos previamente validados."""

    # O intervalo é aplicado uma vez por requisição lógica, não uma vez
    # para cada IP de fallback. Assim, falhas de conexão não geram espera
    # artificial entre tentativas da mesma requisição.
    _wait_for_domain_rate_limit(host, deadline)

    last_error = None

    for ip in validated_ips:
        remaining = _remaining_time(deadline)

        pinned_url = _url_with_pinned_ip(url, ip)

        try:
            ip_literal = ipaddress.ip_address(host)
            host_header = (
                f"[{host}]" if ip_literal.version == 6 else host
            )
        except ValueError:
            host_header = host

        request = client.build_request(
            "GET",
            pinned_url,
            headers={
                "Host": host_header,
                "Connection": "close",
            },
            extensions={
                "sni_hostname": host,
            },
            timeout=min(REQUEST_TIMEOUT, remaining),
        )

        try:
            return client.send(request, stream=True)

        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            last_error = exc

    if last_error is not None:
        raise last_error

    raise ValueError("Nenhum endereço IP público foi validado.")


def _fetch_checked_html(
    client: httpx.Client,
    initial_url: str,
    official_host: str,
    deadline: float,
    expected_content_type: str | None = "text/html",
    allow_missing: bool = False,
    dns_cache: dict[str, list[str]] | None = None,
    robots_policy: RobotFileParser | None = None,
) -> tuple[str, str]:
    """
    Busca HTML seguindo redirecionamentos manualmente.

    Valida o domínio e o DNS antes de cada requisição.
    Conecta usando IPs públicos previamente validados,
    preservando o Host e o SNI do domínio original.
    """

    current_url = initial_url

    for redirect_count in range(MAX_REDIRECTS + 1):
        _remaining_time(deadline)

        parsed = _validate_public_https_url(current_url)
        host = _canonical_hostname(parsed.hostname)

        if (
            robots_policy is not None
            and not robots_policy.can_fetch(USER_AGENT, current_url)
        ):
            raise ValueError(
                "O robots.txt não permite acessar este endereço."
            )

        if not _allowed_redirect_host(host, official_host):
            raise ValueError(
                "Redirecionamento para domínio não autorizado."
            )

        if dns_cache is not None and host in dns_cache:
            validated_ips = dns_cache[host]
        else:
            validated_ips = _validate_public_dns(host, deadline)
            if dns_cache is not None:
                dns_cache[host] = validated_ips

        with closing(
            _send_to_validated_ips(
                client,
                current_url,
                host,
                validated_ips,
                deadline,
            )
        ) as response:

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

                next_host = _canonical_hostname(
                    next_parsed.hostname
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

            if allow_missing and response.status_code in (404, 410):
                return "", current_url

            response.raise_for_status()

            _remaining_time(deadline)

            content_type = response.headers.get(
                "content-type",
                "",
            ).lower()

            if (
                expected_content_type is not None
                and expected_content_type not in content_type
            ):
                raise ValueError(
                    "O endereço não retornou uma página HTML."
                )

            chunks = []
            total_bytes = 0

            for chunk in response.iter_bytes():
                _remaining_time(deadline)

                total_bytes += len(chunk)

                if total_bytes > MAX_HTML_BYTES:
                    raise ValueError(
                        "Página muito grande para análise."
                    )

                chunks.append(chunk)

            _remaining_time(deadline)

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

    if not isinstance(company_name, str):
        raise ValueError("O nome da empresa precisa ser um texto.")

    company_name = company_name.strip()

    if len(company_name) < 2:
        raise ValueError("Informe um nome de empresa válido.")

    if len(company_name) > MAX_COMPANY_NAME_LENGTH:
        raise ValueError("Nome da empresa muito longo.")

    deadline = time.monotonic() + MAX_DISCOVERY_SECONDS

    _remaining_time(deadline)

    if not isinstance(official_site_url, str):
        raise ValueError(
            "O endereço do site oficial precisa ser um texto."
        )

    official = _validate_public_https_url(official_site_url)

    official_host = _canonical_hostname(official.hostname)

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html",
    }

    dns_cache: dict[str, list[str]] = {}

    with httpx.Client(
        timeout=REQUEST_TIMEOUT,
        follow_redirects=False,
        headers=headers,
        trust_env=False,
        verify=True,
    ) as client:

        robots = _fetch_robots_policy(
            client,
            official_site_url,
            official_host,
            deadline,
            dns_cache,
        )

        if not robots.can_fetch(USER_AGENT, official_site_url):
            return []

        html, final_url = _fetch_checked_html(
            client,
            official_site_url,
            official_host,
            deadline,
            dns_cache=dns_cache,
            robots_policy=robots,
        )

    _remaining_time(deadline)

    parsed_links = _parse_html_links(html, deadline)

    candidates = []
    seen_urls = set()

    for href, link_text in parsed_links:
        _remaining_time(deadline)

        if not href:
            continue

        absolute_url = urljoin(final_url, href)

        try:
            parsed = _validate_public_https_url(absolute_url)
        except ValueError:
            continue

        host = _canonical_hostname(parsed.hostname)

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

    _remaining_time(deadline)

    return candidates
