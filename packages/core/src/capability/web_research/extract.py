"""HTML -> clean text for an agent. Extractors are replaceable; the Core depends on none of them.

  Crawl4AIExtractor  runs crawl4ai_worker.py with Crawl4AI's own interpreter (a separate venv), in a
                     child process with a minimal environment (no secrets inherited), no network, a
                     deadline and a cap on what it may print. Off unless WEB_RESEARCH_CRAWL4AI_PYTHON is set.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

WORKER = Path(__file__).with_name("crawl4ai_worker.py")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# A link target may itself contain one level of parentheses: javascript:alert(1)
_UNSAFE_LINK = re.compile(r"\]\(\s*(?:javascript|data|vbscript|file):(?:[^()]|\([^()]*\))*\)", re.IGNORECASE)


class ExtractionFailed(Exception):
    """The page could not be turned into text. `reason` is for logs."""


@dataclass(frozen=True)
class Extracted:
    title: str
    markdown: str
    links_internal: int
    links_external: int


def sanitize(markdown: str, limit: int) -> str:
    """Control characters out, script-ish links neutralised, length capped."""
    text = _UNSAFE_LINK.sub("](#)", _CONTROL.sub("", markdown or ""))
    return text[:limit]


class Extractor(ABC):
    name: str = "extractor"

    @abstractmethod
    def available(self) -> bool:
        """Configured and runnable. Never raises."""

    @abstractmethod
    async def extract(self, html: str, url: str) -> Extracted:
        """Raises ExtractionFailed."""


class Crawl4AIExtractor(Extractor):
    name = "crawl4ai"

    def __init__(self, python: str, *, timeout_s: float = 30.0, max_output_bytes: int = 4_000_000,
                 home: str | None = None) -> None:
        self._python = python
        self._timeout_s = timeout_s
        self._max_output = max_output_bytes
        self._home = home or os.path.join(tempfile.gettempdir(), "trinity-crawl4ai")

    def available(self) -> bool:
        return bool(self._python) and Path(self._python).is_file() and WORKER.is_file()

    def _env(self) -> dict[str, str]:
        # Only what a Python process needs to start on this OS — never the Core's environment (secrets).
        keep = ("SYSTEMROOT", "SystemRoot", "WINDIR", "TEMP", "TMP", "PATH", "LANG", "LC_ALL")
        env = {key: os.environ[key] for key in keep if key in os.environ}
        env.update({"CRAWL4_AI_BASE_DIRECTORY": self._home, "PYTHONIOENCODING": "utf-8", "NO_PROXY": "*",
                    "HOME": self._home, "USERPROFILE": self._home})
        return env

    async def extract(self, html: str, url: str) -> Extracted:
        if not self.available():
            raise ExtractionFailed("crawl4ai not configured")
        os.makedirs(self._home, exist_ok=True)
        process = await asyncio.create_subprocess_exec(
            self._python, "-I", str(WORKER), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=self._env(), cwd=self._home)
        payload = json.dumps({"url": url, "html": html}, ensure_ascii=False).encode("utf-8")
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(payload), timeout=self._timeout_s)
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.wait()
            raise ExtractionFailed("worker timeout") from exc
        if process.returncode != 0:
            raise ExtractionFailed(f"worker exit {process.returncode}")
        if len(stdout) > self._max_output:
            raise ExtractionFailed("worker output too large")
        try:
            data = json.loads(stdout.decode("utf-8"))
        except ValueError as exc:
            raise ExtractionFailed("worker output not json") from exc
        return Extracted(title=str(data.get("title") or "")[:300], markdown=str(data.get("markdown") or ""),
                         links_internal=int(data.get("links_internal") or 0),
                         links_external=int(data.get("links_external") or 0))
