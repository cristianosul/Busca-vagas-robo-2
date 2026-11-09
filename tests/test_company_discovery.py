
import httpx
import pytest

from app import company_discovery as discovery


def install_mock_site(monkeypatch, handler):
    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    def fake_dns(host, port, *args, **kwargs):
        return [
            (
                discovery.socket.AF_INET,
                discovery.socket.SOCK_STREAM,
                6,
                "",
                ("93.184.216.34", 443),
            )
        ]

    monkeypatch.setattr(
        discovery.httpx,
        "Client",
        fake_client,
    )

    monkeypatch.setattr(
        discovery.socket,
        "getaddrinfo",
        fake_dns,
    )


def test_discovers_career_pages_and_ignores_external_links(
    monkeypatch,
):
    html = """
    <html>
      <body>
        <a href="/carreiras">Trabalhe Conosco</a>
        <a href="/carreiras#vagas">Carreiras duplicadas</a>
        <a href="https://jobs.example.com/jobs">Jobs</a>
        <a href="https://other-company.com/jobs">Vagas externas</a>
        <a href="http://example.com/careers">Link sem HTTPS</a>
        <a href="/sobre">Sobre a empresa</a>
      </body>
    </html>
    """

    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text=html,
            request=request,
        )

    install_mock_site(monkeypatch, handler)

    results = discovery.discover_career_pages(
        "Empresa Exemplo",
        "https://example.com",
    )

    urls = [item.page_url for item in results]

    assert len(results) == 2
    assert "https://example.com/carreiras" in urls
    assert "https://jobs.example.com/jobs" in urls

    assert all(
        item.company_name == "Empresa Exemplo"
        for item in results
    )


def test_rejects_http_official_site():
    with pytest.raises(ValueError):
        discovery.discover_career_pages(
            "Empresa Exemplo",
            "http://example.com",
        )


def test_rejects_private_ip_address():
    with pytest.raises(ValueError):
        discovery.discover_career_pages(
            "Empresa Exemplo",
            "https://127.0.0.1",
        )


def test_rejects_empty_company_name():
    with pytest.raises(ValueError):
        discovery.discover_career_pages(
            " ",
            "https://example.com",
        )


def test_rejects_redirect_to_another_domain(monkeypatch):
    requested_hosts = []

    def handler(request):
        requested_hosts.append(request.headers.get("host"))

        if request.headers.get("host") == "example.com":
            return httpx.Response(
                302,
                headers={
                    "location": "https://other-company.com/jobs"
                },
                request=request,
            )

        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<html></html>",
            request=request,
        )

    install_mock_site(monkeypatch, handler)

    with pytest.raises(ValueError, match="outro domínio"):
        discovery.discover_career_pages(
            "Empresa Exemplo",
            "https://example.com",
        )

    assert requested_hosts == ["example.com"]


def test_rejects_non_html_response(monkeypatch):
    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "application/pdf"},
            content=b"arquivo",
            request=request,
        )

    install_mock_site(monkeypatch, handler)

    with pytest.raises(ValueError, match="HTML"):
        discovery.discover_career_pages(
            "Empresa Exemplo",
            "https://example.com",
        )


def test_rejects_redirect_to_private_ip(monkeypatch):
    requested_hosts = []

    def handler(request):
        requested_hosts.append(request.url.host)

        return httpx.Response(
            302,
            headers={
                "location": "https://127.0.0.1/admin"
            },
            request=request,
        )

    install_mock_site(monkeypatch, handler)

    with pytest.raises(ValueError, match="IP privado"):
        discovery.discover_career_pages(
            "Empresa Exemplo",
            "https://example.com",
        )

    assert requested_hosts == ["example.com"]


def test_rejects_domain_resolving_to_private_ip(monkeypatch):
    requested_hosts = []

    def handler(request):
        requested_hosts.append(request.url.host)

        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<html></html>",
            request=request,
        )

    install_mock_site(monkeypatch, handler)

    def private_dns(host, port, *args, **kwargs):
        return [
            (
                discovery.socket.AF_INET,
                discovery.socket.SOCK_STREAM,
                6,
                "",
                ("10.0.0.5", 443),
            )
        ]

    monkeypatch.setattr(
        discovery.socket,
        "getaddrinfo",
        private_dns,
    )

    with pytest.raises(ValueError, match="privado"):
        discovery.discover_career_pages(
            "Empresa Exemplo",
            "https://example.com",
        )

    assert requested_hosts == []
