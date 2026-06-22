"""Tests para OpenAICompatClient."""
import json
from unittest.mock import patch, MagicMock
import pytest
from hub.integrations.openai_compat_client import OpenAICompatClient


class TestOpenAICompatClient:
    def test_chat_success(self):
        client = OpenAICompatClient(api_key="test-key")
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.read.return_value = json.dumps({
            "choices": [{"message": {"role": "assistant", "content": "Respuesta generada."}}],
        }).encode()

        with patch('urllib.request.urlopen', return_value=mock_response):
            result = client.chat([{"role": "user", "content": "Hola"}])

        assert result == "Respuesta generada."

    def test_chat_empty_choices(self):
        client = OpenAICompatClient(api_key="test-key")
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.read.return_value = json.dumps({"choices": []}).encode()

        with patch('urllib.request.urlopen', return_value=mock_response):
            result = client.chat([{"role": "user", "content": "test"}])

        assert result is None

    def test_chat_network_error(self):
        import urllib.error
        client = OpenAICompatClient(api_key="test-key")

        with patch('urllib.request.urlopen', side_effect=urllib.error.URLError("fail")):
            result = client.chat([{"role": "user", "content": "test"}])

        assert result is None

    def test_chat_timeout(self):
        client = OpenAICompatClient(api_key="test-key")

        with patch('urllib.request.urlopen', side_effect=TimeoutError()):
            result = client.chat([{"role": "user", "content": "test"}])

        assert result is None

    def test_is_available_success(self):
        client = OpenAICompatClient(api_key="test-key")
        mock_response = MagicMock()
        mock_response.status = 200

        with patch('urllib.request.urlopen', return_value=mock_response):
            assert client.is_available() is True

    def test_is_available_no_key(self):
        client = OpenAICompatClient(api_key="")
        assert client.is_available() is False

    def test_is_available_network_error(self):
        import urllib.error
        client = OpenAICompatClient(api_key="test-key")

        with patch('urllib.request.urlopen', side_effect=urllib.error.URLError("fail")):
            assert client.is_available() is False

    def test_bearer_token_in_header(self):
        client = OpenAICompatClient(api_key="my-secret-key")
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.read.return_value = json.dumps({
            "choices": [{"message": {"content": "ok"}}],
        }).encode()

        with patch('urllib.request.urlopen', return_value=mock_response) as mock_urlopen:
            client.chat([{"role": "user", "content": "test"}])

        request = mock_urlopen.call_args[0][0]
        assert request.get_header("Authorization") == "Bearer my-secret-key"

    def test_endpoint_path(self):
        client = OpenAICompatClient(
            api_url="https://openrouter.ai/api/v1",
            api_key="key",
        )
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.read.return_value = json.dumps({
            "choices": [{"message": {"content": "ok"}}],
        }).encode()

        with patch('urllib.request.urlopen', return_value=mock_response) as mock_urlopen:
            client.chat([{"role": "user", "content": "test"}])

        request = mock_urlopen.call_args[0][0]
        assert request.full_url == "https://openrouter.ai/api/v1/chat/completions"

    def test_custom_model_and_url(self):
        client = OpenAICompatClient(
            api_url="https://api.together.xyz/v1",
            model="meta-llama/Llama-3.3-70B-Instruct-Turbo",
            api_key="key",
        )
        assert client._api_url == "https://api.together.xyz/v1"
        assert client._model == "meta-llama/Llama-3.3-70B-Instruct-Turbo"
