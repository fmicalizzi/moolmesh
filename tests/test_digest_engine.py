"""Tests para DigestEngine — orquestación L1→L2→L3."""
import pytest
from unittest.mock import MagicMock, patch
from hub.cache.git_store import GitStore
from hub.digests.engine import DigestEngine


class TestDigestEngine:
    def test_daily_l2_without_ollama(self, tmp_path):
        """Sin Ollama, retorna L2 template."""
        store = GitStore(tmp_path / "test.db")
        repo_id = store.register_repo(_mock_config())

        store.store_commits(repo_id, [
            {"sha": "abc", "author_name": "A", "author_email": "a@t.com",
             "timestamp": "2026-04-16T10:00:00", "message": "test"},
        ])

        engine = DigestEngine(store, ollama_client=None)
        digest = engine.get_daily_digest(repo_id, "2026-04-16", "test/repo")

        assert digest["level"] == 2
        assert digest["period"] == "daily"
        assert "1 commits" in digest["text"] or "1 commit" in digest["text"]
        assert digest["narrative"] == ""
        store.close()

    def test_daily_l3_with_ollama(self, tmp_path):
        """Con Ollama disponible, retorna L3 narrativa."""
        store = GitStore(tmp_path / "test.db")
        repo_id = store.register_repo(_mock_config())

        store.store_commits(repo_id, [
            {"sha": "abc", "author_name": "A", "author_email": "a@t.com",
             "timestamp": "2026-04-16T10:00:00", "message": "test"},
        ])

        mock_ollama = MagicMock()
        mock_ollama.is_available.return_value = True
        mock_ollama.chat.return_value = "El equipo avanzó con un commit importante."

        engine = DigestEngine(store, ollama_client=mock_ollama)
        digest = engine.get_daily_digest(repo_id, "2026-04-16", "test/repo")

        assert digest["level"] == 3
        assert "avanzó" in digest["text"]
        assert digest["narrative"] != ""
        store.close()

    def test_ollama_failure_falls_back_to_l2(self, tmp_path):
        """Ollama falla → fallback a L2."""
        store = GitStore(tmp_path / "test.db")
        repo_id = store.register_repo(_mock_config())

        store.store_commits(repo_id, [
            {"sha": "abc", "author_name": "A", "author_email": "a@t.com",
             "timestamp": "2026-04-16T10:00:00", "message": "test"},
        ])

        mock_ollama = MagicMock()
        mock_ollama.is_available.return_value = True
        mock_ollama.chat.return_value = None  # Falla

        engine = DigestEngine(store, ollama_client=mock_ollama)
        digest = engine.get_daily_digest(repo_id, "2026-04-16", "test/repo")

        assert digest["level"] == 2  # Fallback
        assert digest["narrative"] == ""
        store.close()

    def test_cache_hit(self, tmp_path):
        """Segundo pedido retorna de cache sin recomputar."""
        store = GitStore(tmp_path / "test.db")
        repo_id = store.register_repo(_mock_config())

        store.store_commits(repo_id, [
            {"sha": "abc", "author_name": "A", "author_email": "a@t.com",
             "timestamp": "2026-04-16T10:00:00", "message": "test"},
        ])

        engine = DigestEngine(store, ollama_client=None)

        # Primera vez: computa
        digest1 = engine.get_daily_digest(repo_id, "2026-04-16", "test/repo")
        assert digest1["level"] == 2

        # Segunda vez: cache
        digest2 = engine.get_daily_digest(repo_id, "2026-04-16", "test/repo")
        assert digest2["level"] == 2
        assert digest2["text"] == digest1["text"]
        store.close()

    def test_force_refresh_ignores_cache(self, tmp_path):
        """force_refresh=True recomputa incluso con cache."""
        store = GitStore(tmp_path / "test.db")
        repo_id = store.register_repo(_mock_config())

        engine = DigestEngine(store, ollama_client=None)

        # Compute and cache
        engine.get_daily_digest(repo_id, "2026-04-16", "test/repo")

        # Force refresh
        digest = engine.get_daily_digest(repo_id, "2026-04-16", "test/repo", force_refresh=True)
        assert digest["level"] == 2
        store.close()

    def test_weekly_digest(self, tmp_path):
        """Weekly digest funciona con rango de semana."""
        store = GitStore(tmp_path / "test.db")
        repo_id = store.register_repo(_mock_config())

        engine = DigestEngine(store, ollama_client=None)
        digest = engine.get_weekly_digest(repo_id, "2026-04-13", "test/repo")

        assert digest["period"] == "weekly"
        assert digest["level"] == 2
        store.close()

    def test_historical_date_no_llm_call(self, tmp_path):
        """allow_llm=False → Ollama nunca se llama aunque esté disponible."""
        store = GitStore(tmp_path / "test.db")
        repo_id = store.register_repo(_mock_config())

        store.store_commits(repo_id, [
            {"sha": "abc", "author_name": "A", "author_email": "a@t.com",
             "timestamp": "2026-04-07T10:00:00", "message": "test"},
        ])

        mock_ollama = MagicMock()
        mock_ollama.is_available.return_value = True
        mock_ollama.chat.return_value = "Narrativa LLM"

        engine = DigestEngine(store, ollama_client=mock_ollama)
        # allow_llm=False debe impedir llamada a Ollama
        digest = engine.get_daily_digest(repo_id, "2026-04-07", "test/repo", allow_llm=False)

        assert digest["level"] == 2  # Solo L2, no L3
        assert digest["narrative"] == ""
        mock_ollama.chat.assert_not_called()
        store.close()

    def test_historical_date_force_refresh_calls_llm(self, tmp_path):
        """force_refresh=True siempre llama Ollama aunque allow_llm=False."""
        store = GitStore(tmp_path / "test.db")
        repo_id = store.register_repo(_mock_config())

        store.store_commits(repo_id, [
            {"sha": "abc", "author_name": "A", "author_email": "a@t.com",
             "timestamp": "2026-04-07T10:00:00", "message": "test"},
        ])

        mock_ollama = MagicMock()
        mock_ollama.is_available.return_value = True
        mock_ollama.chat.return_value = "Narrativa regenerada"

        engine = DigestEngine(store, ollama_client=mock_ollama)
        # Primera llamada sin LLM (crea L2 en cache)
        engine.get_daily_digest(repo_id, "2026-04-07", "test/repo", allow_llm=False)
        # Regenerar forzado debe llamar Ollama
        digest = engine.get_daily_digest(repo_id, "2026-04-07", "test/repo", force_refresh=True)

        assert digest["level"] == 3
        assert digest["narrative"] == "Narrativa regenerada"
        mock_ollama.chat.assert_called()
        store.close()

    def test_historical_date_shows_cached_l3(self, tmp_path):
        """Si existe L3 cacheado para fecha histórica, se muestra sin llamar Ollama."""
        store = GitStore(tmp_path / "test.db")
        repo_id = store.register_repo(_mock_config())

        store.store_commits(repo_id, [
            {"sha": "abc", "author_name": "A", "author_email": "a@t.com",
             "timestamp": "2026-04-07T10:00:00", "message": "test"},
        ])

        mock_ollama = MagicMock()
        mock_ollama.is_available.return_value = True
        mock_ollama.chat.return_value = "Narrativa pre-generada"

        engine = DigestEngine(store, ollama_client=mock_ollama)
        # Primera llamada con LLM para crear L3 en cache
        digest1 = engine.get_daily_digest(repo_id, "2026-04-07", "test/repo", allow_llm=True)
        assert digest1["level"] == 3
        assert mock_ollama.chat.call_count == 1

        # Segunda llamada con allow_llm=False debe retornar L3 del cache sin llamar Ollama
        digest2 = engine.get_daily_digest(repo_id, "2026-04-07", "test/repo", allow_llm=False)
        assert digest2["level"] == 3  # Del cache
        assert digest2["narrative"] == "Narrativa pre-generada"
        # No debe haber llamado Ollama nuevamente
        assert mock_ollama.chat.call_count == 1
        store.close()

    def test_weekly_allow_llm_false(self, tmp_path):
        """allow_llm=False también funciona para weekly digest."""
        store = GitStore(tmp_path / "test.db")
        repo_id = store.register_repo(_mock_config())

        mock_ollama = MagicMock()
        mock_ollama.is_available.return_value = True
        mock_ollama.chat.return_value = "Narrativa semanal"

        engine = DigestEngine(store, ollama_client=mock_ollama)
        digest = engine.get_weekly_digest(repo_id, "2026-04-07", "test/repo", allow_llm=False)

        assert digest["level"] == 2  # Solo L2
        mock_ollama.chat.assert_not_called()
        store.close()


def _mock_config():
    from hub.config import RepoConfig
    return RepoConfig(
        path="/tmp/test-repo",
        remote_url="github.com/test/repo",
        owner="test",
        repo="repo",
        added_at="2026-04-16T00:00:00",
    )
