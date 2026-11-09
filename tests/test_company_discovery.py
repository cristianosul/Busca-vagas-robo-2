
import httpx
import pytest
import shutil
import ssl
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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
        requested_hosts.append(request.headers.get("host"))

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


def test_connects_to_validated_ip_and_preserves_hostname(monkeypatch):
    observed = {}

    def handler(request):
        observed["ip"] = request.url.host
        observed["host"] = request.headers.get("host")
        observed["sni"] = request.extensions.get("sni_hostname")
        observed["connection"] = request.headers.get("connection")

        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<html></html>",
            request=request,
        )

    install_mock_site(monkeypatch, handler)

    discovery.discover_career_pages(
        "Empresa Exemplo",
        "https://example.com",
    )

    assert observed["ip"] == "93.184.216.34"
    assert observed["host"] == "example.com"
    assert observed["sni"] == "example.com"
    assert observed["connection"] == "close"


def test_rejects_mixed_public_and_private_dns_results(monkeypatch):
    requested_hosts = []

    def handler(request):
        requested_hosts.append(request.headers.get("host"))
        return httpx.Response(
            200,
            text="<html><body>Empresa</body></html>",
            headers={"content-type": "text/html"},
        )

    install_mock_site(monkeypatch, handler)

    def mixed_dns(host, port, *args, **kwargs):
        return [
            (discovery.socket.AF_INET, discovery.socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (discovery.socket.AF_INET, discovery.socket.SOCK_STREAM, 6, "", ("10.0.0.5", 443)),
        ]

    monkeypatch.setattr(discovery.socket, "getaddrinfo", mixed_dns)

    with pytest.raises(ValueError, match="privado"):
        discovery.discover_career_pages(
            "Empresa Exemplo",
            "https://example.com",
        )

    assert requested_hosts == []


def test_prevents_dns_rebinding_by_pinning_validated_ip(monkeypatch):
    dns_calls = []
    requests_seen = []

    def handler(request):
        requests_seen.append({
            "ip": request.url.host,
            "host": request.headers.get("host"),
            "sni": request.extensions.get("sni_hostname"),
        })

        return httpx.Response(
            200,
            text="<html><a href='/carreiras'>Carreiras</a></html>",
            headers={"content-type": "text/html"},
        )

    install_mock_site(monkeypatch, handler)

    def rebinding_dns(host, port, *args, **kwargs):
        dns_calls.append(host)

        ip = (
            "93.184.216.34"
            if len(dns_calls) == 1
            else "10.0.0.5"
        )

        return [
            (
                discovery.socket.AF_INET,
                discovery.socket.SOCK_STREAM,
                6,
                "",
                (ip, 443),
            )
        ]

    monkeypatch.setattr(
        discovery.socket,
        "getaddrinfo",
        rebinding_dns,
    )

    discovery.discover_career_pages(
        "Empresa Exemplo",
        "https://example.com",
    )

    assert len(dns_calls) == 1
    assert requests_seen

    assert requests_seen[0]["ip"] == "93.184.216.34"
    assert requests_seen[0]["host"] == "example.com"
    assert requests_seen[0]["sni"] == "example.com"


def test_rejects_html_response_over_maximum_size(monkeypatch):
    def handler(request):
        large_html = (
            b"<html><body>"
            + b"A" * (discovery.MAX_HTML_BYTES + 1)
            + b"</body></html>"
        )

        return httpx.Response(
            200,
            content=large_html,
            headers={"content-type": "text/html"},
        )

    install_mock_site(monkeypatch, handler)

    with pytest.raises(ValueError):
        discovery.discover_career_pages(
            "Empresa Exemplo",
            "https://example.com",
        )


def test_handles_connection_failure(monkeypatch):
    def handler(request):
        raise httpx.ConnectError(
            "Falha simulada de conexão",
            request=request,
        )

    install_mock_site(monkeypatch, handler)

    with pytest.raises(httpx.ConnectError):
        discovery.discover_career_pages(
            "Empresa Exemplo",
            "https://example.com",
        )


def test_closes_response_when_html_reading_fails(monkeypatch):
    stream_closed = []

    class FailingStream(httpx.SyncByteStream):
        def __iter__(self):
            yield b"<html>"
            raise httpx.ReadError(
                "Falha simulada durante a leitura"
            )

        def close(self):
            stream_closed.append(True)

    def handler(request):
        return httpx.Response(
            200,
            stream=FailingStream(),
            headers={"content-type": "text/html"},
            request=request,
        )

    install_mock_site(monkeypatch, handler)

    with pytest.raises(httpx.ReadError):
        discovery.discover_career_pages(
            "Empresa Exemplo",
            "https://example.com",
        )

    assert stream_closed


def test_enables_tls_certificate_verification(monkeypatch):
    captured_config = {}

    def handler(request):
        return httpx.Response(
            200,
            text="<html><body>Empresa</body></html>",
            headers={"content-type": "text/html"},
        )

    install_mock_site(monkeypatch, handler)

    original_client = httpx.Client

    def capture_client(*args, **kwargs):
        captured_config.update(kwargs)
        return original_client(*args, **kwargs)

    monkeypatch.setattr(
        httpx,
        "Client",
        capture_client,
    )

    discovery.discover_career_pages(
        "Empresa Exemplo",
        "https://example.com",
    )

    assert captured_config["verify"] is True
    assert captured_config["trust_env"] is False


def test_rejects_self_signed_tls_certificate(tmp_path):
    openssl = shutil.which("openssl")

    if not openssl:
        pytest.skip("OpenSSL não está disponível")

    cert_file = tmp_path / "cert.pem"
    key_file = tmp_path / "key.pem"

    subprocess.run(
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-keyout",
            str(key_file),
            "-out",
            str(cert_file),
            "-days",
            "1",
            "-nodes",
            "-subj",
            "/CN=127.0.0.1",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    class TestHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        TestHandler,
    )

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(
        certfile=str(cert_file),
        keyfile=str(key_file),
    )

    server.socket = context.wrap_socket(
        server.socket,
        server_side=True,
    )

    thread = threading.Thread(
        target=server.serve_forever,
        daemon=True,
    )

    thread.start()

    try:
        url = f"https://127.0.0.1:{server.server_port}/"

        with pytest.raises(httpx.ConnectError):
            httpx.get(
                url,
                verify=True,
                trust_env=False,
                timeout=5,
            )

    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

def test_ip_official_site_rejects_fake_subdomains():
    assert not discovery._same_company_domain(
        "empresa.93.184.216.34",
        "93.184.216.34",
    )

    assert not discovery._allowed_redirect_host(
        "empresa.93.184.216.34",
        "93.184.216.34",
    )

    assert discovery._same_company_domain(
        "93.184.216.34",
        "93.184.216.34",
    )

    assert discovery._allowed_redirect_host(
        "93.184.216.34",
        "93.184.216.34",
    )
    
