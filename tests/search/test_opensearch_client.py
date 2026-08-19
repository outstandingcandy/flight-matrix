"""Tests for OpenSearch settings resolution and client construction.

The behaviour that matters here is what happens when the cluster is *not*
configured, which is the normal state on a laptop and on the aws target: every
path has to end in "search is off", never in an exception reaching a request.

`YAMLConfig.get` only interpolates `${VAR}` for string leaves, so reading the
whole `search.opensearch` block would hand back literal placeholders — hence
`from_config` reading one leaf at a time, and the test that pins it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src.core.exceptions import SearchError
from src.search.opensearch_client import (
    DEFAULT_INDEX,
    OpenSearchSettings,
    build_client,
    get_client,
    reset_client,
)
from src.utils.yaml_config import YAMLConfig

CONFIG = """
search:
  opensearch:
    url: "${OPENSEARCH_URL}"
    index: "${OPENSEARCH_INDEX}"
    timeout: 3
    max_results: 250
"""


@pytest.fixture(autouse=True)
def _clean_singleton() -> Any:
    reset_client()
    yield
    reset_client()


@pytest.fixture
def yaml_config(tmp_path: Path) -> YAMLConfig:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(CONFIG, encoding="utf-8")
    return YAMLConfig(str(config_file))


class TestSettings:
    def test_nothing_configured_leaves_the_feature_off(self) -> None:
        assert OpenSearchSettings().enabled is False

    def test_a_url_turns_it_on(self) -> None:
        assert OpenSearchSettings(url="http://127.0.0.1:9201").enabled is True

    def test_unresolved_placeholders_fall_back_to_the_defaults(self) -> None:
        """An unset `${VAR}` arrives as None or an empty string; letting it
        through would replace the index name with nothing."""
        settings = OpenSearchSettings.from_mapping({"url": None, "index": "", "timeout": None})

        assert settings.enabled is False
        assert settings.index == DEFAULT_INDEX
        assert settings.timeout == 5.0

    def test_unknown_keys_are_ignored(self) -> None:
        """Config files outlive the code that reads them."""
        settings = OpenSearchSettings.from_mapping({"url": "http://os:9200", "shards": 5})

        assert settings.url == "http://os:9200"

    def test_yaml_leaves_are_read_individually_so_env_vars_resolve(
        self, yaml_config: YAMLConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENSEARCH_URL", "http://127.0.0.1:9201")
        monkeypatch.setenv("OPENSEARCH_INDEX", "aircraft-v2")

        settings = OpenSearchSettings.from_config(yaml_config)

        assert settings.url == "http://127.0.0.1:9201"
        assert settings.index == "aircraft-v2"
        assert settings.timeout == 3.0
        assert settings.max_results == 250

    def test_an_unset_url_in_the_yaml_leaves_the_feature_off(
        self, yaml_config: YAMLConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("OPENSEARCH_URL", raising=False)
        monkeypatch.delenv("OPENSEARCH_INDEX", raising=False)

        settings = OpenSearchSettings.from_config(yaml_config)

        assert settings.enabled is False
        assert settings.index == DEFAULT_INDEX


class TestBuildClient:
    def test_no_url_is_an_error_for_a_caller_that_insists(self) -> None:
        """`build_client` is the explicit path (the reindex script); it says so
        rather than handing back something that cannot work."""
        with pytest.raises(SearchError, match="not configured"):
            build_client(OpenSearchSettings())

    def test_a_client_is_built_without_connecting(self) -> None:
        client = build_client(OpenSearchSettings(url="http://127.0.0.1:9201"))

        assert hasattr(client, "indices")

    def test_credentials_are_only_sent_when_a_username_is_set(self) -> None:
        """The single-node deployment runs with the security plugin disabled;
        sending empty basic-auth headers to it is a 401 waiting to happen."""
        anonymous = build_client(OpenSearchSettings(url="http://127.0.0.1:9201"))
        authenticated = build_client(
            OpenSearchSettings(url="http://127.0.0.1:9201", username="admin", password="secret")
        )

        assert anonymous.transport.kwargs["http_auth"] is None
        assert authenticated.transport.kwargs["http_auth"] == ("admin", "secret")


class TestGetClient:
    def test_an_unconfigured_cluster_returns_none_instead_of_raising(self) -> None:
        """Every caller has a SQL fallback; search is not worth a 500."""
        assert get_client(OpenSearchSettings()) is None

    def test_the_client_is_reused_across_calls(self) -> None:
        settings = OpenSearchSettings(url="http://127.0.0.1:9201")

        assert get_client(settings) is get_client(settings)

    def test_changed_settings_produce_a_new_client(self) -> None:
        first = get_client(OpenSearchSettings(url="http://127.0.0.1:9201"))
        second = get_client(OpenSearchSettings(url="http://127.0.0.1:9202"))

        assert first is not second

    def test_reset_drops_the_cached_client(self) -> None:
        settings = OpenSearchSettings(url="http://127.0.0.1:9201")
        first = get_client(settings)

        reset_client()

        assert get_client(settings) is not first
