"""Integraciones con servicios externos (GitHub, Ollama, LLM genérico)."""
from hub.integrations.github_client import GitHubClient
from hub.integrations.ollama_client import OllamaCloudClient
from hub.integrations.openai_compat_client import OpenAICompatClient


def create_llm_client(provider: str, api_url: str, model: str,
                      api_key: str):
    """Factory: retorna el client correcto según provider.

    Returns OllamaCloudClient, OpenAICompatClient, o None si no hay api_key.
    """
    if not api_key:
        return None

    if provider == "ollama":
        return OllamaCloudClient(api_url=api_url, model=model, api_key=api_key)

    return OpenAICompatClient(api_url=api_url, model=model, api_key=api_key)


__all__ = ["GitHubClient", "OllamaCloudClient", "OpenAICompatClient",
           "create_llm_client"]
