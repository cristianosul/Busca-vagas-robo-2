
from datetime import datetime, timezone

import pytest

import app.repository as repository
from app.models import (
    CandidateEvidence,
    CandidateIn,
    EvidenceRecord,
    SourceCreate,
)



@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(repository, "DATABASE_URL", "")
    monkeypatch.setattr(
        repository,
        "DB_PATH",
        tmp_path / "test_repository.db",
    )
    repository.init_db()


def make_candidate(url="https://example.com/jobs/123"):
    return CandidateIn(
        title="Auxiliar administrativo",
        company="Empresa de Teste",
        location="Brasil",
        mode="Não informado",
        source_name="Teste interno",
        source_url="https://example.com/careers",
        original_job_url=url,
        application_url=None,
    )


def make_source(source_id="empresa-teste"):
    return SourceCreate(
        source_id=source_id,
        name="Empresa de Teste",
        source_type="company",
        base_url="https://example.com",
        authorization="official_public_page",
        enabled=False,
        notes="Fonte fictícia para testes",
    )


def make_complete_evidence():
    criteria = (
        "company_verified",
        "original_page_verified",
        "application_link_verified",
        "job_content_verified",
        "date_verified",
    )

    records = [
        EvidenceRecord(
            criterion=criterion,
            evidence_url="https://example.com/evidence",
            method="manual_review",
            checked_at=datetime.now(timezone.utc),
            result="verified",
        )
        for criterion in criteria
    ]

    return CandidateEvidence(
        company_verified=True,
        original_page_verified=True,
        application_link_verified=True,
        job_content_verified=True,
        date_verified=True,
        evidence_records=records,
    )


def test_normalize_job_url_removes_tracking_parameters():
    result = repository.normalize_job_url(
        "HTTPS://Example.com/jobs/123/?utm_source=test&fbclid=abc"
    )

    assert result == "https://example.com/jobs/123"


def test_normalize_job_url_preserves_meaningful_parameters():
    result = repository.normalize_job_url(
        "https://example.com/jobs/123?id=456"
    )

    assert result == "https://example.com/jobs/123?id=456"


def test_normalize_job_url_rejects_invalid_scheme():
    with pytest.raises(ValueError):
        repository.normalize_job_url(
            "ftp://example.com/jobs/123"
        )


def test_save_candidate_inserts_candidate(isolated_db):
    result = repository.save_candidate(make_candidate())

    assert result["id"] is not None
    assert result["status"] == "review"
    assert result["score"] == 0


def test_save_candidate_blocks_duplicate(isolated_db):
    candidate = make_candidate()

    first = repository.save_candidate(candidate)
    second = repository.save_candidate(candidate)

    assert first["id"] == second["id"]
    assert second["status"] == "duplicate"
    assert second["existing_status"] == "review"

    with repository.connect() as conn:
        row = repository.execute(
            conn,
            "SELECT COUNT(*) AS total FROM candidates",
        ).fetchone()

    assert row["total"] == 1


def test_save_candidate_uses_evidence_score(isolated_db):
    evidence = make_complete_evidence()

    result = repository.save_candidate(
        make_candidate(),
        evidence,
    )

    assert result["score"] == 100
    assert result["status"] == "approved"


def test_list_candidates_returns_saved_candidate(isolated_db):
    repository.save_candidate(make_candidate())

    results = repository.list_candidates()

    assert len(results) == 1
    assert results[0]["title"] == "Auxiliar administrativo"
    assert results[0]["company"] == "Empresa de Teste"


def test_list_candidates_filters_by_status(isolated_db):
    repository.save_candidate(make_candidate())

    results = repository.list_candidates(status="review")

    assert len(results) == 1
    assert results[0]["status"] == "review"

    approved = repository.list_candidates(status="approved")

    assert approved == []


def test_save_source_inserts_source(isolated_db):
    result = repository.save_source(make_source())

    assert result["source_id"] == "empresa-teste"
    assert result["enabled"] is False


def test_save_source_rejects_duplicate(isolated_db):
    source = make_source()

    repository.save_source(source)

    with pytest.raises(ValueError):
        repository.save_source(source)


def test_get_source_record_returns_source(isolated_db):
    repository.save_source(make_source())

    result = repository.get_source_record("empresa-teste")

    assert result is not None
    assert result["name"] == "Empresa de Teste"


def test_get_source_record_returns_none_for_missing_source(isolated_db):
    result = repository.get_source_record("inexistente")

    assert result is None


def test_list_sources_returns_registered_sources(isolated_db):
    repository.save_source(make_source())

    results = repository.list_sources()

    assert len(results) == 1
    assert results[0]["source_id"] == "empresa-teste"


def test_update_source_status_changes_enabled(isolated_db):
    repository.save_source(make_source())

    result = repository.update_source_status(
        "empresa-teste",
        True,
    )

    assert result is not None
    assert result["enabled"] is True


def test_update_source_status_returns_none_for_missing_source(
    isolated_db,
):
    result = repository.update_source_status(
        "inexistente",
        True,
    )

    assert result is None


def test_list_candidates_preserves_all_evidence(isolated_db):
    evidence = make_complete_evidence()

    repository.save_candidate(make_candidate(), evidence)

    results = repository.list_candidates()

    assert len(results) == 1
    assert results[0]["score"] == 100
    assert results[0]["status"] == "approved"

    saved_evidence = results[0]["evidence"]

    assert saved_evidence["company_verified"] is True
    assert saved_evidence["original_page_verified"] is True
    assert saved_evidence["application_link_verified"] is True
    assert saved_evidence["job_content_verified"] is True
    assert saved_evidence["date_verified"] is True
    assert len(saved_evidence["evidence_records"]) == 5
    

def test_list_candidates_preserves_company_relationships(isolated_db):
    data = make_candidate().model_dump()
    data.update({
        "advertiser_company": "Agência Anunciante",
        "hiring_company": "Empresa Contratante",
        "intermediary_name": "Consultoria Intermediária",
    })

    candidate = CandidateIn(**data)

    repository.save_candidate(candidate)

    result = repository.list_candidates()[0]

    assert result["advertiser_company"] == "Agência Anunciante"
    assert result["hiring_company"] == "Empresa Contratante"
    assert result["intermediary_name"] == "Consultoria Intermediária"


def test_list_sources_filters_enabled_sources(isolated_db):
    repository.save_source(make_source("empresa-1"))
    repository.save_source(make_source("empresa-2"))

    repository.update_source_status("empresa-2", True)

    enabled = repository.list_sources(enabled_only=True)

    assert len(enabled) == 1
    assert enabled[0]["source_id"] == "empresa-2"
    assert enabled[0]["enabled"] is True


def test_candidate_insert_failure_rolls_back(isolated_db, monkeypatch):
    original_execute = repository.execute

    def failing_execute(conn, sql, params=()):
        if "INSERT INTO candidates" in sql:
            raise RuntimeError("Falha simulada na gravação")

        return original_execute(conn, sql, params)

    monkeypatch.setattr(repository, "execute", failing_execute)

    with pytest.raises(RuntimeError, match="Falha simulada"):
        repository.save_candidate(make_candidate())

    monkeypatch.setattr(repository, "execute", original_execute)

    with repository.connect() as conn:
        row = repository.execute(
            conn,
            "SELECT COUNT(*) AS total FROM candidates",
        ).fetchone()

    assert row["total"] == 0


def test_init_db_migrates_legacy_database(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy.db"

    monkeypatch.setattr(repository, "DATABASE_URL", "")
    monkeypatch.setattr(repository, "DB_PATH", db_path)

    import sqlite3

    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                company TEXT NOT NULL,
                location TEXT,
                mode TEXT NOT NULL,
                source_name TEXT NOT NULL,
                source_url TEXT NOT NULL,
                original_job_url TEXT NOT NULL,
                application_url TEXT,
                score INTEGER NOT NULL,
                status TEXT NOT NULL,
                reasons_json TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE sources (
                source_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                source_type TEXT NOT NULL,
                base_url TEXT NOT NULL,
                "authorization" TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 0,
                notes TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

        conn.execute("""
            INSERT INTO candidates (
                title, company, location, mode, source_name,
                source_url, original_job_url, application_url,
                score, status, reasons_json, payload_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            "Vaga antiga",
            "Empresa antiga",
            "Brasil",
            "Não informado",
            "Fonte antiga",
            "https://example.com/careers",
            "https://example.com/jobs/123",
            None,
            0,
            "review",
            "[]",
            "{}",
            "2026-01-01T00:00:00+00:00",
        ))

    repository.init_db()

    with repository.connect() as conn:
        candidate_columns = repository._get_table_columns(
            conn, "candidates"
        )
        source_columns = repository._get_table_columns(
            conn, "sources"
        )

        row = repository.execute(
            conn,
            "SELECT normalized_url FROM candidates WHERE id = ?",
            (1,),
        ).fetchone()

    assert "normalized_url" in candidate_columns
    assert "evidence_json" in candidate_columns
    assert "official_site_url" in source_columns
    assert "authorization_evidence_url" in source_columns
    assert row["normalized_url"] == "https://example.com/jobs/123"


def test_init_db_stops_on_duplicate_normalized_urls(
    tmp_path,
    monkeypatch,
):
    import sqlite3

    db_path = tmp_path / "duplicate_legacy.db"

    monkeypatch.setattr(repository, "DATABASE_URL", "")
    monkeypatch.setattr(repository, "DB_PATH", db_path)

    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                company TEXT NOT NULL,
                location TEXT,
                mode TEXT NOT NULL,
                source_name TEXT NOT NULL,
                source_url TEXT NOT NULL,
                original_job_url TEXT NOT NULL,
                application_url TEXT,
                score INTEGER NOT NULL,
                status TEXT NOT NULL,
                reasons_json TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE sources (
                source_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                source_type TEXT NOT NULL,
                base_url TEXT NOT NULL,
                "authorization" TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 0,
                notes TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

        for url in (
            "https://example.com/jobs/123?utm_source=google",
            "https://example.com/jobs/123",
        ):
            conn.execute("""
                INSERT INTO candidates (
                    title, company, location, mode, source_name,
                    source_url, original_job_url, application_url,
                    score, status, reasons_json, payload_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                "Vaga antiga",
                "Empresa antiga",
                "Brasil",
                "Não informado",
                "Fonte antiga",
                "https://example.com/careers",
                url,
                None,
                0,
                "review",
                "[]",
                "{}",
                "2026-01-01T00:00:00+00:00",
            ))

    with pytest.raises(RuntimeError, match="URLs normalizadas duplicadas"):
        repository.init_db()

    with sqlite3.connect(db_path) as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM candidates"
        ).fetchone()[0]

    assert total == 2


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/job with spaces",
        "https://example.com/jobs/\n123",
        "https://example.com\\jobs\\123",
        "https://-example.com/jobs/123",
        "https://example..com/jobs/123",
        "https://example.com:99999/jobs/123",
        "https://user:password@example.com/jobs/123",
    ],
)
def test_normalize_job_url_rejects_invalid_values(url):
    with pytest.raises(ValueError):
        repository.normalize_job_url(url)


def test_normalize_job_url_accepts_outer_whitespace():
    result = repository.normalize_job_url(
        "  https://example.com/jobs/123  "
    )

    assert result == "https://example.com/jobs/123"


def test_normalize_job_url_converts_international_domain():
    result = repository.normalize_job_url(
        "https://exämple.com/jobs/123"
    )

    assert result == "https://xn--exmple-cua.com/jobs/123"


def test_normalize_job_url_removes_default_port_and_fragment():
    result = repository.normalize_job_url(
        "https://example.com:443/jobs/123#details"
    )

    assert result == "https://example.com/jobs/123"


def test_normalize_job_url_sorts_meaningful_parameters():
    result = repository.normalize_job_url(
        "https://example.com/jobs/123?z=2&a=1"
    )

    assert result == "https://example.com/jobs/123?a=1&z=2"


def test_normalize_job_url_rejects_control_character():
    with pytest.raises(ValueError):
        repository.normalize_job_url(
            "https://example.com/jobs/\x00"
        )


def test_init_db_reports_invalid_legacy_url_and_preserves_candidate(
    tmp_path,
    monkeypatch,
):
    import sqlite3

    db_path = tmp_path / "invalid_legacy.db"

    monkeypatch.setattr(repository, "DATABASE_URL", "")
    monkeypatch.setattr(repository, "DB_PATH", db_path)

    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                company TEXT NOT NULL,
                location TEXT,
                mode TEXT NOT NULL,
                source_name TEXT NOT NULL,
                source_url TEXT NOT NULL,
                original_job_url TEXT NOT NULL,
                application_url TEXT,
                score INTEGER NOT NULL,
                status TEXT NOT NULL,
                reasons_json TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE sources (
                source_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                source_type TEXT NOT NULL,
                base_url TEXT NOT NULL,
                "authorization" TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 0,
                notes TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

        conn.execute("""
            INSERT INTO candidates (
                id, title, company, location, mode, source_name,
                source_url, original_job_url, application_url,
                score, status, reasons_json, payload_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            77,
            "Vaga antiga",
            "Empresa antiga",
            "Brasil",
            "Não informado",
            "Fonte antiga",
            "https://example.com/careers",
            "endereco-invalido",
            None,
            0,
            "review",
            "[]",
            "{}",
            "2026-01-01T00:00:00+00:00",
        ))

    with pytest.raises(
        RuntimeError,
        match=r"oportunidade ID 77",
    ):
        repository.init_db()

    with sqlite3.connect(db_path) as conn:
        row = conn.execute("""
            SELECT id, original_job_url
            FROM candidates
            WHERE id = 77
        """).fetchone()

        total = conn.execute(
            "SELECT COUNT(*) FROM candidates"
        ).fetchone()[0]

    assert total == 1
    assert row == (77, "endereco-invalido")


# ============================================================
# Testes adicionais: SQL, PostgreSQL e transações SQLite
# ============================================================


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        (
            "SELECT ?",
            "SELECT %s",
        ),
        (
            "SELECT ?, ?",
            "SELECT %s, %s",
        ),
        (
            "SELECT '?' AS literal, ? AS value",
            "SELECT '?' AS literal, %s AS value",
        ),
        (
            "SELECT 'it''s ?' AS literal, ? AS value",
            "SELECT 'it''s ?' AS literal, %s AS value",
        ),
        (
            'SELECT "?" AS identifier, ? AS value',
            'SELECT "?" AS identifier, %s AS value',
        ),
        (
            'SELECT "a""?b", ?',
            'SELECT "a""?b", %s',
        ),
        (
            "SELECT ? -- comentário com ?\n, ?",
            "SELECT %s -- comentário com ?\n, %s",
        ),
        (
            "SELECT ? /* comentário com ? */, ?",
            "SELECT %s /* comentário com ? */, %s",
        ),
        (
            "SELECT ? /* externo /* interno ? */ final ? */, ?",
            "SELECT %s /* externo /* interno ? */ final ? */, %s",
        ),
        (
            "SELECT $$ conteúdo ? $$, ?",
            "SELECT $$ conteúdo ? $$, %s",
        ),
        (
            "SELECT $tag$ conteúdo ? $tag$, ?",
            "SELECT $tag$ conteúdo ? $tag$, %s",
        ),
        (
            "SELECT $body$ texto $other$ ? $body$, ?",
            "SELECT $body$ texto $other$ ? $body$, %s",
        ),
        (
            "SELECT '?' AS literal, ? -- final ?",
            "SELECT '?' AS literal, %s -- final ?",
        ),
    ],
)
def test_convert_sql_placeholders_preserves_sql_literals(
    sql,
    expected,
):
    result = repository._convert_sql_placeholders(sql)

    assert result == expected


def test_convert_sql_placeholders_preserves_sql_without_placeholders():
    sql = """
        SELECT id, title
        FROM candidates
        WHERE status = 'review'
    """

    assert repository._convert_sql_placeholders(sql) == sql


def test_convert_sql_placeholders_handles_empty_sql():
    assert repository._convert_sql_placeholders("") == ""


def test_convert_sql_placeholders_does_not_modify_question_marks_in_comments():
    sql = """
        SELECT ?
        -- comentário ?
        /* outro comentário ? */
    """

    expected = """
        SELECT %s
        -- comentário ?
        /* outro comentário ? */
    """

    assert repository._convert_sql_placeholders(sql) == expected


def test_convert_sql_placeholders_handles_multiple_dollar_quoted_blocks():
    sql = (
        "SELECT $$ primeiro ? $$, "
        "$body$ segundo ? $body$, ?"
    )

    expected = (
        "SELECT $$ primeiro ? $$, "
        "$body$ segundo ? $body$, %s"
    )

    assert repository._convert_sql_placeholders(sql) == expected


def test_execute_converts_placeholders_for_postgresql(monkeypatch):
    class FakeConnection:
        def __init__(self):
            self.sql = None
            self.params = None
            self.result = object()

        def execute(self, sql, params=()):
            self.sql = sql
            self.params = params
            return self.result

    connection = FakeConnection()

    monkeypatch.setattr(
        repository,
        "DATABASE_URL",
        "postgresql://user:password@example.com/database",
    )

    result = repository.execute(
        connection,
        "SELECT '?' AS literal, ? AS value",
        ("teste",),
    )

    assert result is connection.result
    assert connection.sql == (
        "SELECT '?' AS literal, %s AS value"
    )
    assert connection.params == ("teste",)


def test_execute_preserves_parameters_for_postgresql(monkeypatch):
    class FakeConnection:
        def __init__(self):
            self.sql = None
            self.params = None

        def execute(self, sql, params=()):
            self.sql = sql
            self.params = params
            return "resultado"

    connection = FakeConnection()

    monkeypatch.setattr(
        repository,
        "DATABASE_URL",
        "postgresql://user:password@example.com/database",
    )

    params = ("Empresa", 123, None)

    repository.execute(
        connection,
        "SELECT * FROM candidates WHERE company = ? AND id = ? AND location = ?",
        params,
    )

    assert connection.sql == (
        "SELECT * FROM candidates WHERE company = %s AND id = %s AND location = %s"
    )
    assert connection.params == params


# ============================================================
# Testes adicionais: transações SQLite
# ============================================================


def test_db_session_immediate_starts_write_transaction(isolated_db):
    with repository.db_session(immediate=True) as conn:
        assert conn.in_transaction is True


def test_db_session_immediate_commits_successful_transaction(
    isolated_db,
):
    with repository.db_session() as conn:
        repository.execute(
            conn,
            """
            CREATE TABLE transaction_probe (
                value TEXT NOT NULL
            )
            """,
        )

    with repository.db_session(immediate=True) as conn:
        repository.execute(
            conn,
            "INSERT INTO transaction_probe (value) VALUES (?)",
            ("confirmado",),
        )

        assert conn.in_transaction is True

    with repository.connect() as conn:
        row = repository.execute(
            conn,
            "SELECT value FROM transaction_probe",
        ).fetchone()

    assert row["value"] == "confirmado"


def test_db_session_immediate_rolls_back_failed_transaction(
    isolated_db,
):
    with repository.db_session() as conn:
        repository.execute(
            conn,
            """
            CREATE TABLE transaction_probe (
                value TEXT NOT NULL
            )
            """,
        )

    with pytest.raises(RuntimeError, match="falha simulada"):
        with repository.db_session(immediate=True) as conn:
            repository.execute(
                conn,
                "INSERT INTO transaction_probe (value) VALUES (?)",
                ("não confirmado",),
            )

            raise RuntimeError("falha simulada")

    with repository.connect() as conn:
        row = repository.execute(
            conn,
            "SELECT COUNT(*) AS total FROM transaction_probe",
        ).fetchone()

    assert row["total"] == 0


def test_db_session_immediate_keeps_operations_in_same_transaction(
    isolated_db,
):
    with repository.db_session() as conn:
        repository.execute(
            conn,
            """
            CREATE TABLE transaction_probe (
                value TEXT NOT NULL
            )
            """,
        )

    with repository.db_session(immediate=True) as conn:
        repository.execute(
            conn,
            "INSERT INTO transaction_probe (value) VALUES (?)",
            ("primeiro",),
        )

        repository.execute(
            conn,
            "INSERT INTO transaction_probe (value) VALUES (?)",
            ("segundo",),
        )

        assert conn.in_transaction is True

    with repository.connect() as conn:
        rows = repository.execute(
            conn,
            "SELECT value FROM transaction_probe ORDER BY value",
        ).fetchall()

    assert [row["value"] for row in rows] == [
        "primeiro",
        "segundo",
    ]


# ============================================================
# Testes adicionais: inicialização e migrações
# ============================================================


def test_init_db_can_run_multiple_times(isolated_db):
    repository.save_candidate(make_candidate())
    repository.save_source(make_source())

    repository.init_db()
    repository.init_db()

    candidates = repository.list_candidates()
    sources = repository.list_sources()

    assert len(candidates) == 1
    assert len(sources) == 1


def test_init_db_preserves_existing_candidate_after_reinitialization(
    isolated_db,
):
    original = repository.save_candidate(make_candidate())

    repository.init_db()

    results = repository.list_candidates()

    assert len(results) == 1
    assert results[0]["id"] == original["id"]
    assert results[0]["title"] == "Auxiliar administrativo"


def test_init_db_preserves_existing_source_after_reinitialization(
    isolated_db,
):
    repository.save_source(make_source())

    repository.init_db()

    result = repository.get_source_record("empresa-teste")

    assert result is not None
    assert result["name"] == "Empresa de Teste"
    assert result["enabled"] is False


# ============================================================
# Testes adicionais: normalização de URLs
# ============================================================


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://EXAMPLE.COM/jobs/123",
            "https://example.com/jobs/123",
        ),
        (
            "https://example.com/jobs/123?utm_campaign=teste&id=10",
            "https://example.com/jobs/123?id=10",
        ),
        (
            "https://example.com/jobs/123?gclid=abc&ref=portal",
            "https://example.com/jobs/123?ref=portal",
        ),
        (
            "https://example.com/jobs/123#candidatura",
            "https://example.com/jobs/123",
        ),
        (
            "https://example.com:443/jobs/123",
            "https://example.com/jobs/123",
        ),
    ],
)
def test_normalize_job_url_additional_valid_cases(url, expected):
    assert repository.normalize_job_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        "example.com/jobs/123",
        "http:///jobs/123",
        "https://",
        "https://example.com:abc/jobs/123",
        "https://example.com/jobs/123\n?x=1",
    ],
)
def test_normalize_job_url_rejects_additional_invalid_cases(url):
    with pytest.raises(ValueError):
        repository.normalize_job_url(url)
        
