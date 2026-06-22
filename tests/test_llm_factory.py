"""Tests para create_llm_client factory."""
from hub.integrations import create_llm_client
from hub.integrations.ollama_client import OllamaCloudClient
from hub.integrations.openai_compat_client import OpenAICompatClient


class TestCreateLlmClient:
    def test_factory_ollama(self):
        client = create_llm_client("ollama", "https://ollama.com/api", "qwen3.5:35b-cloud", "key")
        assert isinstance(client, OllamaCloudClient)

    def test_factory_openrouter(self):
        client = create_llm_client("openrouter", "https://openrouter.ai/api/v1", "google/gemini-2.5-flash", "key")
        assert isinstance(client, OpenAICompatClient)

    def test_factory_openai(self):
        client = create_llm_client("openai", "https://api.openai.com/v1", "gpt-4.1-mini", "key")
        assert isinstance(client, OpenAICompatClient)

    def test_factory_no_key(self):
        result = create_llm_client("openrouter", "https://openrouter.ai/api/v1", "model", "")
        assert result is None
