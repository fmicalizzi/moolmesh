"""Tests para hub/log.py."""
import logging
from pathlib import Path

from hub import log


class TestLogSetup:
    def test_setup_idempotent(self):
        """setup() puede llamarse múltiples veces sin duplicar handlers."""
        log._CONFIGURED = False
        log.setup(level="WARNING", log_to_file=False)
        handler_count = len(logging.getLogger("hub").handlers)
        log.setup(level="DEBUG", log_to_file=False)
        assert len(logging.getLogger("hub").handlers) == handler_count
        log._CONFIGURED = False  # Reset

    def test_get_returns_namespaced_logger(self):
        """get('X') retorna logger 'hub.X'."""
        logger = log.get("TestComponent")
        assert logger.name == "hub.TestComponent"

    def test_setup_creates_stderr_handler(self):
        """setup() agrega al menos un StreamHandler."""
        log._CONFIGURED = False
        log.setup(level="INFO", log_to_file=False)
        root = logging.getLogger("hub")
        stream_handlers = [h for h in root.handlers if isinstance(h, logging.StreamHandler)]
        assert len(stream_handlers) >= 1
        log._CONFIGURED = False

    def test_setup_creates_file_handler(self, tmp_path, monkeypatch):
        """setup() con log_to_file crea RotatingFileHandler."""
        monkeypatch.setattr(log, "LOG_DIR", tmp_path)
        monkeypatch.setattr(log, "LOG_FILE", tmp_path / "test.log")
        log._CONFIGURED = False
        log.setup(level="INFO", log_to_file=True)
        root = logging.getLogger("hub")
        from logging.handlers import RotatingFileHandler
        file_handlers = [h for h in root.handlers if isinstance(h, RotatingFileHandler)]
        assert len(file_handlers) >= 1
        log._CONFIGURED = False

    def test_log_message_written(self, tmp_path, monkeypatch):
        """Un mensaje de log se escribe al archivo."""
        monkeypatch.setattr(log, "LOG_DIR", tmp_path)
        monkeypatch.setattr(log, "LOG_FILE", tmp_path / "test.log")
        log._CONFIGURED = False
        log.setup(level="DEBUG", log_to_file=True)
        logger = log.get("TestWrite")
        logger.warning("test message 12345")
        # Flush handlers
        for h in logging.getLogger("hub").handlers:
            h.flush()
        content = (tmp_path / "test.log").read_text()
        assert "test message 12345" in content
        log._CONFIGURED = False


class TestLogLevels:
    def test_debug_not_shown_at_info(self, tmp_path, monkeypatch):
        """A nivel INFO, mensajes DEBUG no se escriben al archivo."""
        monkeypatch.setattr(log, "LOG_DIR", tmp_path)
        monkeypatch.setattr(log, "LOG_FILE", tmp_path / "test.log")
        log._CONFIGURED = False
        log.setup(level="INFO", log_to_file=True)
        logger = log.get("TestLevel")
        logger.debug("this is debug HIDDEN_TOKEN")
        for h in logging.getLogger("hub").handlers:
            h.flush()
        content = (tmp_path / "test.log").read_text()
        assert "HIDDEN_TOKEN" not in content
        log._CONFIGURED = False
