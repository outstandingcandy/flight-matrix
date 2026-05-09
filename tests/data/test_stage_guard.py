"""Tests for the STAGE=local production-URL safety guard.

Regression history: a shared `.env` file mixed `STAGE=local` with a
production Aurora `DATABASE_URL`, so every local run silently connected
to prod. The guard in DatabaseManager catches that by refusing to create
an engine for an AWS-hosted host when STAGE=local.
"""

from __future__ import annotations

import pytest

from src.data.db_manager import DatabaseManager, _looks_like_prod_host


class TestProdHostHeuristic:
    @pytest.mark.parametrize(
        "url",
        [
            "postgresql+psycopg2://u:p@cluster.cluster-abc.us-east-1.rds.amazonaws.com:5432/db",
            "postgresql://u:p@myhost.redshift.amazonaws.com:5439/db",
            "postgresql+psycopg2://u:p@writer.cluster-xyz.us-west-2.rds.amazonaws.com/db",
        ],
    )
    def test_true_for_aws_hosts(self, url: str) -> None:
        assert _looks_like_prod_host(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "sqlite:///aircraft_data.db",
            "sqlite:///:memory:",
            "postgresql+psycopg2://u:p@localhost:5432/db",
            "postgresql://u:p@127.0.0.1:5432/db",
            "postgresql://u:p@db.internal.example.com/db",
        ],
    )
    def test_false_for_non_prod(self, url: str) -> None:
        assert _looks_like_prod_host(url) is False


class TestStageGuard:
    def test_local_stage_with_prod_url_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("STAGE", "local")
        monkeypatch.delenv("ALLOW_PROD_DB_FROM_LOCAL", raising=False)
        prod_url = "postgresql+psycopg2://u:p@x.cluster-abc.us-east-1.rds.amazonaws.com:5432/db"
        with pytest.raises(RuntimeError, match="production database"):
            DatabaseManager(prod_url)

    def test_local_stage_with_sqlite_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("STAGE", "local")
        dm = DatabaseManager(":memory:")
        assert dm.is_sqlite
        dm.close()

    def test_prod_stage_with_prod_url_skips_guard(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Guard must not fire when STAGE != local. We can't actually connect
        # without real credentials, so stub out the engine lookup by pointing
        # at a prod-looking URL and catching the SQLAlchemy-level error from
        # the subsequent bootstrap, not the guard.
        monkeypatch.setenv("STAGE", "prod")
        prod_url = "postgresql+psycopg2://u:p@x.cluster-abc.us-east-1.rds.amazonaws.com:5432/db"
        # The guard is pre-engine. Any failure beyond the guard is a
        # SQLAlchemy connection error, which is fine — it means we passed
        # the guard. A RuntimeError matching 'production database' would
        # mean the guard fired, which is what we're asserting against.
        with pytest.raises(Exception) as excinfo:
            DatabaseManager(prod_url)
        assert "production database" not in str(excinfo.value)

    def test_override_env_var_bypasses_guard(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("STAGE", "local")
        monkeypatch.setenv("ALLOW_PROD_DB_FROM_LOCAL", "1")
        prod_url = "postgresql+psycopg2://u:p@x.cluster-abc.us-east-1.rds.amazonaws.com:5432/db"
        # Same argument as above: guard must not fire; downstream connection
        # error is acceptable evidence.
        with pytest.raises(Exception) as excinfo:
            DatabaseManager(prod_url)
        assert "production database" not in str(excinfo.value)
