"""Tests para OllamaCloudClient."""
import json
from unittest.mock import patch, MagicMock
import pytest
from hub.integrations.ollama_client import OllamaCloudClient


class TestOllamaCloudClient:
    def test_chat_success(self):
        """Mock successful chat response."""
        client = OllamaCloudClient(api_key="test-key")
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.read.return_value = json.dumps({
            "message": {"role": "assistant", "content": "Respuesta generada."},
            "done": True,
        }).encode()

        with patch('urllib.request.urlopen', return_value=mock_response):
            result = client.chat([{"role": "user", "content": "Hola"}])

        assert result == "Respuesta generada."

    def test_chat_network_error(self):
        """Network error returns None."""
        import urllib.error
        client = OllamaCloudClient(api_key="test-key")

        with patch('urllib.request.urlopen', side_effect=urllib.error.URLError("fail")):
            result = client.chat([{"role": "user", "content": "test"}])

        assert result is None

    def test_chat_timeout(self):
        """Timeout returns None."""
        client = OllamaCloudClient(api_key="test-key")

        with patch('urllib.request.urlopen', side_effect=TimeoutError()):
            result = client.chat([{"role": "user", "content": "test"}])

        assert result is None

    def test_is_available_success(self):
        """Health check returns True on 200."""
        client = OllamaCloudClient(api_key="test-key")
        mock_response = MagicMock()
        mock_response.status = 200

        with patch('urllib.request.urlopen', return_value=mock_response):
            assert client.is_available() is True

    def test_is_available_no_key(self):
        """No API key returns False."""
        client = OllamaCloudClient(api_key="")
        assert client.is_available() is False

    def test_is_available_network_error(self):
        """Network error returns False."""
        import urllib.error
        client = OllamaCloudClient(api_key="test-key")

        with patch('urllib.request.urlopen', side_effect=urllib.error.URLError("fail")):
            assert client.is_available() is False

    def test_bearer_token_in_header(self):
        """API key is sent as Bearer token."""
        client = OllamaCloudClient(api_key="my-secret-key")
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.read.return_value = json.dumps({
            "message": {"content": "ok"},
        }).encode()

        with patch('urllib.request.urlopen', return_value=mock_response) as mock_urlopen:
            client.chat([{"role": "user", "content": "test"}])

        request = mock_urlopen.call_args[0][0]
        assert request.get_header("Authorization") == "Bearer my-secret-key"

    def test_custom_model_and_url(self):
        """Custom api_url and model are used."""
        client = OllamaCloudClient(
            api_url="https://custom.api.com/v1",
            model="llama3.3:70b",
            api_key="key",
        )
        assert client._api_url == "https://custom.api.com/v1"
        assert client._model == "llama3.3:70b"
