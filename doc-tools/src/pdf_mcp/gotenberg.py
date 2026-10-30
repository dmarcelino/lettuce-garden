"""A small client for the host's shared Gotenberg. Files go up as multipart
form data from bytes the jail read; nothing is ever sent as a URL, so
Gotenberg never fetches anything on our behalf."""

from __future__ import annotations

import httpx

PDF = "application/pdf"


class GotenbergError(Exception):
    """Gotenberg failed or is unreachable; safe to show the agent."""


class Gotenberg:
    def __init__(self, base_url: str, timeout: float, max_bytes: int, transport: httpx.BaseTransport | None = None):
        # trust_env=False: no proxy (pipelock) between the stack and Gotenberg.
        self._client = httpx.Client(
            base_url=base_url, timeout=timeout, trust_env=False, transport=transport, follow_redirects=False
        )
        self._max = max_bytes

    def close(self) -> None:
        self._client.close()

    def post(self, route: str, files: list[tuple[str, bytes]], fields: dict[str, str] | None = None) -> tuple[bytes, str]:
        """(body, content type) of a successful conversion."""
        upload = [("files", (name, data, "application/octet-stream")) for name, data in files]
        try:
            with self._client.stream("POST", route, files=upload, data=fields or {}) as r:
                if r.status_code != 200:
                    detail = r.read()[:300].decode("utf-8", "replace").strip()
                    raise GotenbergError(_status_message(r.status_code, detail))
                ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
                if ctype != PDF:
                    raise GotenbergError(f"unexpected answer from the converter ({ctype or 'no content type'})")
                body = bytearray()
                for chunk in r.iter_bytes():
                    body += chunk
                    if len(body) > self._max:
                        raise GotenbergError("the converter's result is larger than the size limit")
        except httpx.TimeoutException:
            raise GotenbergError("the converter took too long; try a smaller file or fewer pages") from None
        except httpx.HTTPError:
            raise GotenbergError(
                "the converter (Gotenberg) is unreachable; the operator may need to run `lettucectl up` with the garden-doc-tools profile"
            ) from None
        if not body.startswith(b"%PDF-"):
            raise GotenbergError("the converter returned something that is not a PDF")
        return bytes(body), ctype


def _status_message(status: int, detail: str) -> str:
    if status == 503:
        return "the converter is busy; try again in a moment"
    if status == 413:
        return "the file is larger than the converter accepts"
    if status == 400:
        return f"the converter refused the request: {detail}" if detail else "the converter refused the request"
    return f"the converter failed (HTTP {status})"
