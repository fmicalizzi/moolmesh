"""Cliente OpenAI-compatible — sirve para OpenRouter, Together, Groq, OpenAI."""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from hub import USER_AGENT
from hub.log import get as get_logger

_log = get_logger("OpenAICompatClient")


class OpenAICompatClient:
    """HTTP client para APIs OpenAI-compatible (/v1/chat/completions)."""

    def __init__(self, api_url: str = "https://openrouter.ai/api/v1",
                 model: str = "google/gemini-2.5-flash",
                 api_key: str | None = None):
        self._api_url = api_url.rstrip("/")
        self._model = model
        self._api_key = api_key or ""

    def chat(self, messages: list[dict], max_tokens: int = 500) -> str | None:
        """Envía mensajes al chat completions endpoint.

        Returns contenido generado o None si falla.
        Nunca lanza excepciones — caller puede asumir None = no disponible.
        """
        payload = {
            "model": self._model,
            "messages": messages,
            "max_tokens": max_tokens,
            "stream": False,
        }

        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        url = f"{self._api_url}/chat/completions"
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")

        try:
            resp = urllib.request.urlopen(req, timeout=120)
            if resp.status == 200:
                data = json.loads(resp.read())
                choices = data.get("choices", [])
                if choices:
                    return choices[0].get("message", {}).get("content")
        except (urllib.error.URLError, urllib.error.HTTPError,
                OSError, TimeoutError, json.JSONDecodeError,
                KeyError, TypeError, IndexError) as e:
            _log.debug("OpenAICompat chat falló: %s", e)

        return None

    def is_available(self) -> bool:
        """Health check — GET /models para verificar que la API key es válida."""
        if not self._api_key:
            return False

        try:
            req = urllib.request.Request(
                f"{self._api_url}/models",
                headers={
                    "User-Agent": USER_AGENT,
                    "Authorization": f"Bearer {self._api_key}",
                },
                method="GET"
            )
            resp = urllib.request.urlopen(req, timeout=5)
            return resp.status == 200
        except (urllib.error.URLError, urllib.error.HTTPError,
                OSError, TimeoutError) as e:
            _log.debug("OpenAICompat no disponible: %s", e)
            return False
