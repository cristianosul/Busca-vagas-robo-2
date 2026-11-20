"""Testes unitários do leitor seguro de páginas de vagas.

Toda comunicação HTTP e resolução DNS é simulada. Nenhum teste acessa a rede.
"""

from __future__ import annotations

import httpx
import pytest

import app.job_page_fetcher as fetcher


PUBLIC_IPS = ["93.184.216.34"]


def make_response(url: str, status: int = 200, *, headers=None, body=b""):
    request = httpx.Request("GET", url)
    return httpx.Response(status, headers=headers or {}, content=body, request=request)


@pytest.fixture(autouse=True)
def isolate_rate_limiter(monkeypatch):
    """Evita esperas e estado compartilhado entre testes."""
    monkeypatch.setattr(fetcher, "_wait_domain_slot", lambda *args, **kwargs: None)
    with fetcher._rate_lock:
        fetcher._next_domain_request.clear()


def install_fake_network(monkeypatch, responder):
    calls = []

    def fake_dns(host, deadline):
        return PUBLIC_IPS

    def fake_send(client, url, host, ips, deadline):
        calls.append(url)
        return responder(url, len(calls))

    monkeypatch.setattr(fetcher, "_validate_public_dns", fake_dns)
    monkeypatch.setattr(fetcher, "_send_to_validated_ips", fake_send)
    return calls


def test_rejects_non_https_and_credential_urls():
    for url in (
        "http://example.com/vaga",
        "https://user:password@example.com/vaga",
        "file:///etc/passwd",
        "",
    ):
        with pytest.raises(fetcher.JobPageFetchError):
            fetcher._check_url(url)


def test_rejects_overlong_url():
    with pytest.raises(fetcher.JobPageFetchError) as exc:
        fetcher._check_url("https://example.com/" + "a" * fetcher.MAX_URL_LENGTH)
    assert exc.value.code == "invalid_url"


def test_fetches_html_after_robots_allows(monkeypatch):
    def responder(url, call_number):
        if url.endswith("/robots.txt"):
            return make_response(
                url,
                headers={"content-type": "text/plain"},
                body=b"User-agent: *\nAllow: /\n",
            )
        return make_response(
            url,
            headers={"content-type": "text/html; charset=utf-8"},
            body="<html><h1>Vaga</h1></html>".encode(),
        )

    calls = install_fake_network(monkeypatch, responder)
    result = fetcher.fetch_job_page("https://example.com/jobs/1")

    assert result.html == "<html><h1>Vaga</h1></html>"
    assert result.status_code == 200
    assert result.final_url == "https://example.com/jobs/1"
    assert result.robots_checked is True
    assert len(calls) == 2
    assert calls[0] == "https://example.com/robots.txt"


def test_robots_disallow_blocks_page_request(monkeypatch):
    def responder(url, call_number):
        return make_response(
            url,
            headers={"content-type": "text/plain"},
            body=b"User-agent: *\nDisallow: /jobs\n",
        )

    calls = install_fake_network(monkeypatch, responder)
    with pytest.raises(fetcher.JobPageFetchError) as exc:
        fetcher.fetch_job_page("https://example.com/jobs/blocked")

    assert exc.value.code == "robots_disallowed"
    assert len(calls) == 1


def test_robots_403_blocks_access(monkeypatch):
    calls = install_fake_network(
        monkeypatch,
        lambda url, number: make_response(url, 403),
    )
    with pytest.raises(fetcher.JobPageFetchError) as exc:
        fetcher.fetch_job_page("https://example.com/job")
    assert exc.value.code == "robots_disallowed"
    assert len(calls) == 1


def test_robots_404_allows_fetch(monkeypatch):
    def responder(url, number):
        if url.endswith("robots.txt"):
            return make_response(url, 404)
        return make_response(url, headers={"content-type": "text/html"}, body=b"<p>ok</p>")

    install_fake_network(monkeypatch, responder)
    result = fetcher.fetch_job_page("https://example.com/job")
    assert result.html == "<p>ok</p>"


def test_rejects_redirect_to_unsafe_url(monkeypatch):
    def responder(url, number):
        if url.endswith("robots.txt"):
            return make_response(url, 404)
        return make_response(url, 302, headers={"location": "http://127.0.0.1/admin"})

    calls = install_fake_network(monkeypatch, responder)
    with pytest.raises(fetcher.JobPageFetchError):
        fetcher.fetch_job_page("https://example.com/job")
    assert len(calls) == 2


def test_follows_valid_redirect_and_checks_destination_robots(monkeypatch):
    def responder(url, number):
        if url == "https://example.com/robots.txt":
            return make_response(url, 404)
        if url == "https://example.com/job":
            return make_response(url, 302, headers={"location": "https://jobs.example.net/opening/1"})
        if url == "https://jobs.example.net/robots.txt":
            return make_response(url, 404)
        return make_response(url, headers={"content-type": "text/html"}, body=b"<h1>Nova vaga</h1>")

    install_fake_network(monkeypatch, responder)
    result = fetcher.fetch_job_page("https://example.com/job")
    assert result.final_url == "https://jobs.example.net/opening/1"
    assert result.redirects == ("https://jobs.example.net/opening/1",)


def test_rejects_too_many_redirects(monkeypatch):
    def responder(url, number):
        if url.endswith("robots.txt"):
            return make_response(url, 404)
        return make_response(url, 302, headers={"location": f"https://example.com/job/{number}"})

    install_fake_network(monkeypatch, responder)
    with pytest.raises(fetcher.JobPageFetchError) as exc:
        fetcher.fetch_job_page("https://example.com/job")
    assert exc.value.code == "too_many_redirects"


def test_rejects_html_response_over_limit(monkeypatch):
    def responder(url, number):
        if url.endswith("robots.txt"):
            return make_response(url, 404)
        return make_response(
            url,
            headers={"content-type": "text/html", "content-length": str(fetcher.MAX_HTML_BYTES + 1)},
            body=b"x",
        )

    install_fake_network(monkeypatch, responder)
    with pytest.raises(fetcher.JobPageFetchError) as exc:
        fetcher.fetch_job_page("https://example.com/job")
    assert exc.value.code == "response_too_large"


def test_rejects_unexpected_content_type(monkeypatch):
    def responder(url, number):
        if url.endswith("robots.txt"):
            return make_response(url, 404)
        return make_response(url, headers={"content-type": "application/pdf"}, body=b"pdf")

    install_fake_network(monkeypatch, responder)
    with pytest.raises(fetcher.JobPageFetchError) as exc:
        fetcher.fetch_job_page("https://example.com/job")
    assert exc.value.code == "invalid_content_type"


def test_http_error_status_is_reported(monkeypatch):
    def responder(url, number):
        if url.endswith("robots.txt"):
            return make_response(url, 404)
        return make_response(url, 503)

    install_fake_network(monkeypatch, responder)
    with pytest.raises(fetcher.JobPageFetchError) as exc:
        fetcher.fetch_job_page("https://example.com/job")
    assert exc.value.code == "http_status"


def test_rejects_oversized_robots_file(monkeypatch):
    def responder(url, number):
        return make_response(
            url,
            headers={
                "content-type": "text/plain",
                "content-length": str(fetcher.MAX_ROBOTS_BYTES + 1),
            },
            body=b"x",
        )

    install_fake_network(monkeypatch, responder)
    with pytest.raises(fetcher.JobPageFetchError) as exc:
        fetcher.fetch_job_page("https://example.com/job")
    assert exc.value.code == "response_too_large"


def test_rejects_unexpected_robots_content_type(monkeypatch):
    def responder(url, number):
        return make_response(url, headers={"content-type": "text/html"}, body=b"<html/>")

    install_fake_network(monkeypatch, responder)
    with pytest.raises(fetcher.JobPageFetchError) as exc:
        fetcher.fetch_job_page("https://example.com/job")
    assert exc.value.code == "robots_invalid_content_type"


def test_non_utf8_charset_falls_back_to_utf8(monkeypatch):
    def responder(url, number):
        if url.endswith("robots.txt"):
            return make_response(url, 404)
        return make_response(
            url,
            headers={"content-type": "text/html; charset=unknown-charset"},
            body=b"<p>vaga</p>",
        )

    install_fake_network(monkeypatch, responder)
    result = fetcher.fetch_job_page("https://example.com/job")
    assert result.html == "<p>vaga</p>"


def test_dns_validation_failure_is_wrapped(monkeypatch):
    monkeypatch.setattr(fetcher, "_validate_public_dns", lambda *args: (_ for _ in ()).throw(ValueError("private IP")))
    with pytest.raises(fetcher.JobPageFetchError) as exc:
        fetcher._request_once(
            httpx.Client(),
            "https://example.com/job",
            fetcher.monotonic() + 10,
            max_bytes=100,
        )
    assert exc.value.code == "dns_or_connection_validation"


def test_request_failure_is_wrapped(monkeypatch):
    monkeypatch.setattr(fetcher, "_validate_public_dns", lambda *args: PUBLIC_IPS)
    monkeypatch.setattr(
        fetcher,
        "_send_to_validated_ips",
        lambda *args: (_ for _ in ()).throw(httpx.ConnectError("offline")),
    )
    with pytest.raises(fetcher.JobPageFetchError) as exc:
        fetcher._request_once(
            httpx.Client(),
            "https://example.com/job",
            fetcher.monotonic() + 10,
            max_bytes=100,
        )
    assert exc.value.code == "network_error"
