"""Leitor HTTPS seguro e respeitoso para páginas públicas de vagas.

Este módulo não extrai nem persiste vagas. Ele apenas obtém HTML após
validar URL, DNS, redirecionamentos, robots.txt, limites e intervalo por domínio.

A validação DNS/IP e a conexão com IP fixado reutilizam as primitivas já
implementadas em app.company_discovery, evitando uma segunda implementação
independente dessas proteções.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Condition, Lock
from time import monotonic, sleep
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx

from app.company_discovery import (
    USER_AGENT as BASE_USER_AGENT,
    _canonical_hostname,
    _remaining_time,
    _send_to_validated_ips,
    _validate_public_dns,
    _validate_public_https_url,
)


USER_AGENT = BASE_USER_AGENT
ROBOTS_USER_AGENT = "BuscaVagasBot"
REQUEST_TIMEOUT_SECONDS = 10.0
MAX_TOTAL_SECONDS = 30.0
MAX_HTML_BYTES = 1_000_000
MAX_ROBOTS_BYTES = 256_000
MAX_REDIRECTS = 5
MAX_URL_LENGTH = 2048
MIN_DOMAIN_INTERVAL_SECONDS = 1.0
ROBOTS_CACHE_TTL_SECONDS = 6 * 60 * 60
MAX_ROBOTS_CACHE_ENTRIES = 2048
RATE_LIMIT_CLEANUP_EVERY = 64
MAX_RATE_LIMIT_ENTRIES = 4096
REDIRECT_STATUSES = {301, 302, 303, 307, 308}


class JobPageFetchError(RuntimeError):
    """Falha segura e classificada ao obter uma página de vaga."""

    def __init__(self, message: str, *, code: str = "fetch_error"):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class FetchedJobPage:
    """HTML obtido com sucesso e metadados da requisição."""

    requested_url: str
    final_url: str
    status_code: int
    content_type: str
    html: str
    fetched_at: datetime
    redirects: tuple[str, ...]
    robots_checked: bool


# Limitador cooperativo em memória. Cada processo mantém seu próprio controle;
# uma limitação distribuída entre máquinas exigirá armazenamento compartilhado.
_rate_lock = Lock()
_next_domain_request: dict[str, float] = {}
_rate_limit_calls = 0

# Cache compartilhado entre chamadas do processo. O lock/condition protege
# tanto a validade das entradas quanto a carga única por hostname.
_robots_condition = Condition(Lock())
_robots_cache: OrderedDict[str, tuple[float, RobotFileParser | None]] = OrderedDict()
_robots_loading: set[str] = set()


def _check_url(url: str):
    if not isinstance(url, str) or not url.strip():
        raise JobPageFetchError("Informe uma URL de página de vaga.", code="invalid_url")
    if len(url) > MAX_URL_LENGTH:
        raise JobPageFetchError("URL muito longa.", code="invalid_url")
    try:
        return _validate_public_https_url(url)
    except (TypeError, ValueError) as exc:
        raise JobPageFetchError(str(exc), code="unsafe_url") from exc


def _host_key(url: str) -> str:
    parsed = _check_url(url)
    return _canonical_hostname(parsed.hostname)


def _wait_domain_slot(
    host: str,
    deadline: float,
    minimum_interval: float = MIN_DOMAIN_INTERVAL_SECONDS,
) -> None:
    """Reserva um intervalo por hostname sem manter o lock durante a espera."""
    now = monotonic()
    _remaining_time(deadline)
    interval = max(MIN_DOMAIN_INTERVAL_SECONDS, minimum_interval)
    global _rate_limit_calls
    with _rate_lock:
        _rate_limit_calls += 1
        if (
            _rate_limit_calls % RATE_LIMIT_CLEANUP_EVERY == 0
            or len(_next_domain_request) >= MAX_RATE_LIMIT_ENTRIES
        ):
            # Entradas vencidas não precisam permanecer no dicionário. Mantém
            # reservas futuras, inclusive as feitas por chamadas concorrentes.
            for old_host, next_at in tuple(_next_domain_request.items()):
                if next_at <= now:
                    _next_domain_request.pop(old_host, None)
        scheduled = max(now, _next_domain_request.get(host, now))
        _next_domain_request[host] = scheduled + interval
    wait_seconds = max(0.0, scheduled - now)
    if wait_seconds:
        if wait_seconds >= deadline - monotonic():
            raise JobPageFetchError(
                "O limite total foi excedido aguardando o intervalo do domínio.",
                code="deadline_exceeded",
            )
        sleep(wait_seconds)
    _remaining_time(deadline)


def _read_limited_body(response: httpx.Response, limit: int, deadline: float) -> bytes:
    header_length = response.headers.get("content-length")
    if header_length:
        try:
            if int(header_length) > limit:
                raise JobPageFetchError("Resposta excede o tamanho permitido.", code="response_too_large")
        except ValueError:
            # Um Content-Length inválido não é confiável; o limite real abaixo prevalece.
            pass

    chunks: list[bytes] = []
    total = 0
    try:
        for chunk in response.iter_bytes():
            _remaining_time(deadline)
            total += len(chunk)
            if total > limit:
                raise JobPageFetchError(
                    "Resposta excede o tamanho permitido.",
                    code="response_too_large",
                )
            chunks.append(chunk)
    except httpx.HTTPError as exc:
        raise JobPageFetchError("Falha ao receber o conteúdo da página.", code="network_error") from exc
    return b"".join(chunks)


def _request_once(
    client: httpx.Client,
    url: str,
    deadline: float,
    *,
    max_bytes: int,
    minimum_interval: float = MIN_DOMAIN_INTERVAL_SECONDS,
) -> tuple[int, dict[str, str], bytes, str]:
    """Faz uma única requisição sem seguir redirects automaticamente."""
    parsed = _check_url(url)
    host = _canonical_hostname(parsed.hostname)
    _wait_domain_slot(host, deadline, minimum_interval)

    try:
        ips = _validate_public_dns(host, deadline)
        response = _send_to_validated_ips(client, url, host, ips, deadline)
    except (ValueError, TimeoutError) as exc:
        raise JobPageFetchError(str(exc), code="dns_or_connection_validation") from exc
    except httpx.HTTPError as exc:
        raise JobPageFetchError("Não foi possível conectar ao domínio validado.", code="network_error") from exc

    try:
        status = response.status_code
        headers = {key.lower(): value for key, value in response.headers.items()}
        body = b""
        if status not in REDIRECT_STATUSES and status not in (404, 410, 401, 403):
            body = _read_limited_body(response, max_bytes, deadline)
        return status, headers, body, str(response.url)
    finally:
        response.close()


def _robots_url(url: str) -> str:
    parsed = _check_url(url)
    host = _canonical_hostname(parsed.hostname)
    if ":" in host:
        netloc = f"[{host}]"
    else:
        netloc = host
    return urlunsplit(("https", netloc, "/robots.txt", "", ""))


def _load_robots_uncached(
    client: httpx.Client,
    page_url: str,
    deadline: float,
) -> RobotFileParser | None:
    robots_url = _robots_url(page_url)
    status, headers, body, _ = _request_once(
        client,
        robots_url,
        deadline,
        max_bytes=MAX_ROBOTS_BYTES,
    )

    if status in (404, 410):
        return None
    if status in (401, 403):
        raise JobPageFetchError(
            "O robots.txt impede o acesso automatizado a este domínio.",
            code="robots_disallowed",
        )
    if status in REDIRECT_STATUSES:
        raise JobPageFetchError(
            "Redirecionamento de robots.txt não será seguido.",
            code="robots_redirect",
        )
    if status < 200 or status >= 300:
        raise JobPageFetchError(
            f"Não foi possível consultar robots.txt (HTTP {status}).",
            code="robots_unavailable",
        )

    content_type = headers.get("content-type", "").lower()
    if content_type and not any(
        marker in content_type for marker in ("text/plain", "application/octet-stream")
    ):
        raise JobPageFetchError(
            "robots.txt retornou um tipo de conteúdo inesperado.",
            code="robots_invalid_content_type",
        )

    text = body.decode("utf-8", errors="replace")
    parser = RobotFileParser(robots_url)
    parser.parse(text.splitlines())
    return parser


def _load_robots(
    client: httpx.Client,
    page_url: str,
    deadline: float,
    cache: dict[str, RobotFileParser | None] | None = None,
) -> RobotFileParser | None:
    """Carrega robots.txt com cache por host e evita cargas concorrentes duplicadas.

    O argumento ``cache`` mantém compatibilidade com consumidores/testes que
    desejam um cache local. Sem ele, usa o cache compartilhado do processo.
    """
    host = _host_key(page_url)
    if cache is not None:
        if host not in cache:
            cache[host] = _load_robots_uncached(client, page_url, deadline)
        return cache[host]

    while True:
        _remaining_time(deadline)
        with _robots_condition:
            cached = _robots_cache.get(host)
            now = monotonic()
            if cached is not None:
                expires_at, parser = cached
                if expires_at > now:
                    _robots_cache.move_to_end(host)
                    return parser
                _robots_cache.pop(host, None)

            if host not in _robots_loading:
                _robots_loading.add(host)
                break

            remaining = deadline - monotonic()
            if remaining <= 0:
                raise JobPageFetchError(
                    "O limite total foi excedido aguardando robots.txt.",
                    code="deadline_exceeded",
                )
            _robots_condition.wait(timeout=remaining)

    try:
        parser = _load_robots_uncached(client, page_url, deadline)
    except Exception:
        with _robots_condition:
            _robots_loading.discard(host)
            _robots_condition.notify_all()
        raise

    with _robots_condition:
        _robots_cache[host] = (monotonic() + ROBOTS_CACHE_TTL_SECONDS, parser)
        _robots_cache.move_to_end(host)
        while len(_robots_cache) > MAX_ROBOTS_CACHE_ENTRIES:
            _robots_cache.popitem(last=False)
        _robots_loading.discard(host)
        _robots_condition.notify_all()
    return parser


def _robots_allows(
    client: httpx.Client,
    url: str,
    deadline: float,
    cache: dict[str, RobotFileParser | None] | None = None,
) -> tuple[bool, float]:
    parser = _load_robots(client, url, deadline, cache)
    if parser is None:
        return True, MIN_DOMAIN_INTERVAL_SECONDS

    crawl_delay = parser.crawl_delay(ROBOTS_USER_AGENT) or 0.0
    request_rate = parser.request_rate(ROBOTS_USER_AGENT)
    if request_rate and request_rate.requests > 0:
        crawl_delay = max(
            crawl_delay,
            request_rate.seconds / request_rate.requests,
        )

    return (
        parser.can_fetch(ROBOTS_USER_AGENT, url),
        max(MIN_DOMAIN_INTERVAL_SECONDS, float(crawl_delay)),
    )


def _fetch_html(
    client: httpx.Client,
    requested_url: str,
    deadline: float,
) -> tuple[str, str, int, str, tuple[str, ...]]:
    current_url = requested_url
    redirects: list[str] = []
    for redirect_number in range(MAX_REDIRECTS + 1):
        _remaining_time(deadline)
        _check_url(current_url)

        allowed, crawl_interval = _robots_allows(
            client, current_url, deadline
        )
        if not allowed:
            raise JobPageFetchError(
                "A regra de robots.txt não permite acessar esta página.",
                code="robots_disallowed",
            )

        status, headers, body, _ = _request_once(
            client,
            current_url,
            deadline,
            max_bytes=MAX_HTML_BYTES,
            minimum_interval=crawl_interval,
        )

        if status in REDIRECT_STATUSES:
            if redirect_number >= MAX_REDIRECTS:
                raise JobPageFetchError(
                    "Limite de redirecionamentos excedido.",
                    code="too_many_redirects",
                )
            location = headers.get("location")
            if not location:
                raise JobPageFetchError(
                    "Redirecionamento sem endereço de destino.",
                    code="invalid_redirect",
                )
            next_url = urljoin(current_url, location)
            _check_url(next_url)
            redirects.append(next_url)
            current_url = next_url
            continue

        if status < 200 or status >= 300:
            raise JobPageFetchError(
                f"A página retornou HTTP {status}.",
                code="http_status",
            )

        content_type = headers.get("content-type", "").lower()
        if "text/html" not in content_type and "application/xhtml+xml" not in content_type:
            raise JobPageFetchError(
                "O endereço não retornou conteúdo HTML.",
                code="invalid_content_type",
            )

        charset = "utf-8"
        for part in content_type.split(";")[1:]:
            key, separator, value = part.strip().partition("=")
            if separator and key.strip().lower() == "charset":
                candidate = value.strip().strip('"\'')
                if candidate and len(candidate) <= 40:
                    charset = candidate
                break
        try:
            html = body.decode(charset, errors="replace")
        except LookupError:
            html = body.decode("utf-8", errors="replace")

        _remaining_time(deadline)
        return html, current_url, status, content_type, tuple(redirects)

    raise JobPageFetchError("Não foi possível obter a página.", code="fetch_error")


def fetch_job_page(url: str) -> FetchedJobPage:
    """Obtém uma página de vaga pública via HTTPS, respeitando robots.txt.

    A função não interpreta o HTML, não grava dados e não executa candidaturas.
    Redirecionamentos para outros domínios são permitidos somente após validar
    HTTPS, DNS público e robots.txt do domínio de destino.
    """
    _check_url(url)
    deadline = monotonic() + MAX_TOTAL_SECONDS
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "close",
    }

    try:
        with httpx.Client(
            timeout=REQUEST_TIMEOUT_SECONDS,
            follow_redirects=False,
            headers=headers,
            trust_env=False,
            verify=True,
        ) as client:
            html, final_url, status, content_type, redirects = _fetch_html(
                client,
                url,
                deadline,
            )
    except JobPageFetchError:
        raise
    except (httpx.HTTPError, TimeoutError) as exc:
        raise JobPageFetchError(
            "A operação de leitura da página falhou ou excedeu o prazo.",
            code="network_or_timeout",
        ) from exc

    return FetchedJobPage(
        requested_url=url,
        final_url=final_url,
        status_code=status,
        content_type=content_type,
        html=html,
        fetched_at=datetime.now(timezone.utc),
        redirects=redirects,
        robots_checked=True,
    )


__all__ = [
    "FetchedJobPage",
    "JobPageFetchError",
    "fetch_job_page",
]
