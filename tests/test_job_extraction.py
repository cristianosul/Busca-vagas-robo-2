import pytest

from app.job_extraction import (
    MAX_HTML_BYTES,
    MAX_DESCRIPTION_CHARS,
    extract_jobs_from_html,
)


PAGE_URL = "https://empresa.example/vagas/desenvolvedor"


def test_extracts_basic_jobposting_jsonld():
    html = '''
    <html><head><script type="application/ld+json">
    {
      "@context": "https://schema.org",
      "@type": "JobPosting",
      "title": "Desenvolvedor Python",
      "description": "<p>Desenvolver APIs.</p><ul><li>Python</li></ul>",
      "hiringOrganization": {"@type": "Organization", "name": "Empresa Exemplo"},
      "datePosted": "2026-11-01",
      "validThrough": "2026-12-01",
      "employmentType": "FULL_TIME",
      "jobLocation": {"address": {"addressLocality": "Caxias do Sul", "addressRegion": "RS", "addressCountry": "BR"}}
    }
    </script></head></html>
    '''
    jobs = extract_jobs_from_html(html, PAGE_URL)
    assert len(jobs) == 1
    job = jobs[0]
    assert job.title == "Desenvolvedor Python"
    assert job.company == "Empresa Exemplo"
    assert "Desenvolver APIs." in job.description
    assert "Python" in job.description
    assert job.location == "Caxias do Sul, RS, BR"
    assert job.employment_type == "FULL_TIME"
    assert job.date_posted == "2026-11-01"
    assert job.valid_through == "2026-12-01"
    assert job.extraction_method == "json-ld"
    assert job.structured_data_found is True


def test_supports_graph_and_type_as_list():
    html = '''<script type="application/ld+json">
    {"@graph":[{"@type":["Thing","https://schema.org/JobPosting"],
    "title":"Analista de Dados","hiringOrganization":{"name":"Dados SA"}}]}
    </script>'''
    jobs = extract_jobs_from_html(html, PAGE_URL)
    assert len(jobs) == 1
    assert jobs[0].title == "Analista de Dados"
    assert jobs[0].company == "Dados SA"


def test_supports_top_level_jsonld_array():
    html = '''<script type="application/ld+json">[
      {"@type":"JobPosting","title":"Vaga A"},
      {"@type":"JobPosting","title":"Vaga B"}
    ]</script>'''
    jobs = extract_jobs_from_html(html, PAGE_URL)
    assert [job.title for job in jobs] == ["Vaga A", "Vaga B"]


def test_extracts_nested_and_multiple_locations():
    html = '''<script type="application/ld+json">{
      "@type":"JobPosting","title":"Suporte",
      "jobLocation":[
        {"address":{"addressLocality":"Porto Alegre","addressRegion":"RS"}},
        {"address":{"addressLocality":"Canoas","addressRegion":"RS"}}
      ]
    }</script>'''
    job = extract_jobs_from_html(html, PAGE_URL)[0]
    assert "Porto Alegre, RS" in job.location
    assert "Canoas, RS" in job.location


def test_telecommute_means_remote():
    html = '''<script type="application/ld+json">{
      "@type":"JobPosting","title":"Engenheiro",
      "jobLocationType":"TELECOMMUTE"
    }</script>'''
    assert extract_jobs_from_html(html, PAGE_URL)[0].mode == "Remoto"


def test_infers_hybrid_from_text_and_employment_type_list():
    html = '''<script type="application/ld+json">{
      "@type":"JobPosting","title":"Analista híbrido",
      "employmentType":["FULL_TIME","CONTRACTOR"]
    }</script>'''
    job = extract_jobs_from_html(html, PAGE_URL)[0]
    assert job.mode == "Híbrido"
    assert job.employment_type == "FULL_TIME, CONTRACTOR"


def test_resolves_jsonld_application_url_but_does_not_mark_verified():
    html = '''<script type="application/ld+json">{
      "@type":"JobPosting","title":"Dev","applicationUrl":"/candidatar"
    }</script>'''
    job = extract_jobs_from_html(html, PAGE_URL)[0]
    assert job.application_urls == ("https://empresa.example/candidatar",)
    assert not hasattr(job, "application_verified")


def test_anchor_application_links_are_added_to_structured_job():
    html = '''
    <script type="application/ld+json">{"@type":"JobPosting","title":"Dev"}</script>
    <a href="/apply">Apply now</a>
    <a href="https://portal.example/candidatura">Candidate-se</a>
    '''
    job = extract_jobs_from_html(html, PAGE_URL)[0]
    assert "https://empresa.example/apply" in job.application_urls
    assert "https://portal.example/candidatura" in job.application_urls


def test_html_fallback_uses_metadata_and_h1():
    html = '''<html><head>
      <meta property="og:title" content="Assistente Administrativo">
      <meta name="description" content="Atividades de escritório">
      <meta name="company" content="Organização Exemplo">
    </head><body><h1>Outro título</h1></body></html>'''
    job = extract_jobs_from_html(html, PAGE_URL)[0]
    assert job.title == "Assistente Administrativo"
    assert job.description == "Atividades de escritório"
    assert job.company == "Organização Exemplo"
    assert job.extraction_method == "html-fallback"
    assert job.structured_data_found is False


def test_html_fallback_uses_h1_when_no_title_metadata():
    job = extract_jobs_from_html("<h1>Vendedor</h1>", PAGE_URL)[0]
    assert job.title == "Vendedor"


def test_company_hint_is_not_promoted_to_company():
    job = extract_jobs_from_html("<h1>Auxiliar</h1>", PAGE_URL, company_hint="Empresa sugerida")[0]
    assert job.company is None
    assert job.company_hint == "Empresa sugerida"
    assert "company" in job.missing_fields
    assert any("company_hint" in warning for warning in job.warnings)


def test_malformed_jsonld_falls_back_and_reports_warning():
    html = '''<script type="application/ld+json">{not valid json}</script><h1>Recepcionista</h1>'''
    job = extract_jobs_from_html(html, PAGE_URL)[0]
    assert job.title == "Recepcionista"
    assert job.extraction_method == "html-fallback"
    assert any("malformados" in warning for warning in job.warnings)


def test_ignores_script_content_in_fallback_description():
    html = '''<html><body><script>SECRET_SCRIPT_TEXT</script>
    <h1>Operador</h1><p>Atendimento ao cliente.</p></body></html>'''
    job = extract_jobs_from_html(html, PAGE_URL)[0]
    assert "Atendimento ao cliente" in job.description
    assert "SECRET_SCRIPT_TEXT" not in job.description


def test_rejects_non_string_html_and_empty_page_url():
    with pytest.raises(TypeError):
        extract_jobs_from_html(None, PAGE_URL)
    with pytest.raises(ValueError):
        extract_jobs_from_html("<h1>Vaga</h1>", " ")


def test_rejects_html_over_size_limit():
    html = "x" * (MAX_HTML_BYTES + 1)
    with pytest.raises(ValueError, match="excede o limite"):
        extract_jobs_from_html(html, PAGE_URL)


def test_description_is_capped():
    long_description = "A" * (MAX_DESCRIPTION_CHARS + 500)
    html = '<script type="application/ld+json">' + __import__("json").dumps({
        "@type": "JobPosting", "title": "Vaga", "description": long_description
    }) + "</script>"
    job = extract_jobs_from_html(html, PAGE_URL)[0]
    assert len(job.description) == MAX_DESCRIPTION_CHARS


def test_rejects_candidate_url_with_credentials():
    html = '''<script type="application/ld+json">{
      "@type":"JobPosting","title":"Vaga",
      "applicationUrl":"https://user:pass@portal.example/apply"
    }</script>'''
    job = extract_jobs_from_html(html, PAGE_URL)[0]
    assert job.application_urls == ()
