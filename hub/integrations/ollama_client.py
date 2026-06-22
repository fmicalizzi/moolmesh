"""Cliente para Ollama Cloud API — zero dependencies."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from hub import USER_AGENT
from hub.log import get as get_logger

_log = get_logger("OllamaClient")


class OllamaCloudClient:
    """HTTP client para Ollama Cloud API.

    API specs documentados en PHASE0_RESULTS.md:
    - Endpoint: https://ollama.com/api
    - Auth: Bearer token (OLLAMA_API_KEY)
    - Modelo recomendado: qwen3.5:35b-cloud
    """

    def __init__(self, api_url: str = "https://ollama.com/api",
                 model: str = "qwen3.5:35b-cloud",
                 api_key: str | None = None):
        self._api_url = api_url.rstrip("/")
        self._model = model
        self._api_key = api_key or os.getenv("OLLAMA_API_KEY", "")

    def chat(self, messages: list[dict], max_tokens: int = 500) -> str | None:
        """Envía mensajes al chat endpoint.

        Returns contenido generado o None si falla.
        Nunca lanza excepciones — caller puede asumir None = no disponible.
        """
        payload = {
            "model": self._model,
            "messages": messages,
            "stream": False,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens

        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        url = f"{self._api_url}/chat"
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")

        try:
            resp = urllib.request.urlopen(req, timeout=120)
            if resp.status == 200:
                data = json.loads(resp.read())
                return data.get("message", {}).get("content")
        except (urllib.error.URLError, urllib.error.HTTPError,
                OSError, TimeoutError, json.JSONDecodeError,
                KeyError, TypeError) as e:
            _log.debug("Ollama chat falló: %s", e)

        return None

    def is_available(self) -> bool:
        """Health check rápido — GET al base URL.

        Retorna True si el servidor responde 200.
        Timeout de 5s para no bloquear.
        """
        if not self._api_key:
            return False

        try:
            req = urllib.request.Request(
                self._api_url,
                headers={"User-Agent": USER_AGENT},
                method="GET"
            )
            resp = urllib.request.urlopen(req, timeout=5)
            return resp.status == 200
        except (urllib.error.URLError, urllib.error.HTTPError,
                OSError, TimeoutError) as e:
            _log.debug("Ollama no disponible: %s", e)
            return False
