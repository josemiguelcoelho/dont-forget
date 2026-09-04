from __future__ import annotations

import re
import socket
from datetime import datetime
from http.client import HTTPConnection, HTTPSConnection
from html import unescape
from ipaddress import ip_address
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import SplitResult, urlsplit, urlunsplit
from urllib.request import url2pathname

from pydantic import BaseModel, Field

from .models import Evidence


class SourceFetcher(Protocol):
    def fetch(self, source_url: str) -> str: ...


class UrlSourceFetcher:
    def __init__(
        self,
        *,
        transport: Callable[[str, str, float, int], bytes] | None = None,
        resolver: Callable[..., Any] = socket.getaddrinfo,
        allowed_file_roots: list[str | Path] | None = None,
        timeout: float = 5.0,
        max_bytes: int = 200_000,
    ) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        self.transport = transport
        self.resolver = resolver
        self.allowed_file_roots = [
            Path(root).resolve() for root in (allowed_file_roots or [])
        ]
        self.timeout = timeout
        self.max_bytes = max_bytes

    def fetch(self, source_url: str) -> str:
        _, target = self._validated_target(source_url)
        if isinstance(target, Path):
            with target.open("rb") as source_file:
                content = source_file.read(self.max_bytes + 1)
        else:
            transport = self.transport or self._fetch_pinned
            content = transport(source_url, target, self.timeout, self.max_bytes)
        content = content[: self.max_bytes]
        return content.decode("utf-8", errors="replace")

    def _validated_target(self, source_url: str) -> tuple[SplitResult, Path | str]:
        parsed = urlsplit(source_url)
        if parsed.scheme == "file":
            if parsed.netloc not in {"", "localhost"}:
                raise PermissionError("File source is outside the approved source roots.")
            path = Path(url2pathname(parsed.path)).resolve()
            if not any(
                path == root or path.is_relative_to(root)
                for root in self.allowed_file_roots
            ):
                raise PermissionError("File source is outside the approved source roots.")
            return parsed, path
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Source URL must use http, https, or an approved file path.")
        try:
            addresses = [ip_address(parsed.hostname)]
        except ValueError:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            addresses = [
                ip_address(result[4][0])
                for result in self.resolver(
                    parsed.hostname,
                    port,
                    type=socket.SOCK_STREAM,
                )
            ]
        if not addresses or any(not address.is_global for address in addresses):
            raise PermissionError("Source URL must resolve only to the public network.")
        return parsed, str(addresses[0])

    @staticmethod
    def _fetch_pinned(
        source_url: str,
        address: str,
        timeout: float,
        max_bytes: int,
    ) -> bytes:
        parsed = urlsplit(source_url)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        connection_type = (
            _PinnedHTTPSConnection if parsed.scheme == "https" else _PinnedHTTPConnection
        )
        connection = connection_type(parsed.hostname or "", address, port, timeout)
        target = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        try:
            connection.request(
                "GET",
                target,
                headers={"Accept-Encoding": "identity", "User-Agent": "dont-forget/0.1"},
            )
            response = connection.getresponse()
            if 300 <= response.status < 400:
                raise OSError("Source redirects are not allowed.")
            if response.status >= 400:
                raise OSError(f"Source returned HTTP {response.status}.")
            return response.read(max_bytes + 1)
        finally:
            connection.close()


class _PinnedHTTPConnection(HTTPConnection):
    def __init__(self, host: str, address: str, port: int, timeout: float) -> None:
        super().__init__(host, port=port, timeout=timeout)
        self._validated_address = address

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._validated_address, self.port),
            self.timeout,
            self.source_address,
        )


class _PinnedHTTPSConnection(HTTPSConnection):
    def __init__(self, host: str, address: str, port: int, timeout: float) -> None:
        super().__init__(host, port=port, timeout=timeout)
        self._validated_address = address

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._validated_address, self.port),
            self.timeout,
            self.source_address,
        )
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


class ExtractedRequirement(BaseModel):
    description: str
    evidence: Evidence


class SourceEnrichment(BaseModel):
    title: str | None = None
    deadline_at: datetime | None = None
    deadline_evidence: list[Evidence] = Field(default_factory=list)
    requirements: list[ExtractedRequirement] = Field(default_factory=list)
    context_evidence: list[Evidence] = Field(default_factory=list)


class SourceExtractor(Protocol):
    def extract(self, source_url: str, source_text: str, observed_at: datetime) -> SourceEnrichment: ...


class DeterministicSourceExtractor:
    """Extract only explicitly labelled facts from bounded source text."""

    MAX_SOURCE_CHARS = 200_000
    MAX_REQUIREMENTS = 20
    MAX_FACT_CHARS = 300

    def extract(
        self, source_url: str, source_text: str, observed_at: datetime
    ) -> SourceEnrichment:
        source_text = source_text[: self.MAX_SOURCE_CHARS]
        source_text = re.sub(
            r"<(script|style|noscript|template)\b[^>]*>.*?</\1>",
            "",
            source_text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        title_match = re.search(
            r"<title[^>]*>(?P<title>.*?)</title>",
            source_text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        lines = self._visible_lines(source_text)
        title = self._clean_text(title_match.group("title")) if title_match else None
        if title is None:
            for line in lines:
                context_match = re.fullmatch(
                    r"(?:Hackathon|Event|Task):\s*(.+)", line, flags=re.IGNORECASE
                )
                if context_match:
                    title = context_match.group(1).strip()
                    break

        deadline_at = None
        deadline_evidence: list[Evidence] = []
        for line in lines:
            match = re.fullmatch(r"Deadline:\s*(.+)", line, flags=re.IGNORECASE)
            if not match:
                continue
            try:
                candidate = datetime.fromisoformat(match.group(1).strip().replace("Z", "+00:00"))
            except ValueError:
                continue
            if candidate.tzinfo is None:
                continue
            deadline_at = candidate
            deadline_evidence = [
                Evidence(
                    claim=f"Deadline is {candidate.isoformat()}",
                    source=source_url,
                    excerpt=line,
                    observed_at=observed_at,
                    confidence=1.0,
                )
            ]
            break

        context_evidence = []
        if title:
            context_evidence.append(
                Evidence(
                    claim=f"Source title is {title}",
                    source=source_url,
                    excerpt=title,
                    observed_at=observed_at,
                    confidence=1.0,
                )
            )

        requirements = [
            ExtractedRequirement(
                description=description,
                evidence=Evidence(
                    claim=f"Source states requirement: {description}",
                    source=source_url,
                    excerpt=description,
                    observed_at=observed_at,
                    confidence=1.0,
                ),
            )
            for description in self._extract_requirements(source_text)
        ]

        return SourceEnrichment(
            title=title,
            deadline_at=deadline_at,
            deadline_evidence=deadline_evidence,
            requirements=requirements,
            context_evidence=context_evidence,
        )

    def _extract_requirements(self, source_text: str) -> list[str]:
        candidates: list[str] = []
        heading_pattern = re.compile(
            r"<h[1-6][^>]*>(?P<heading>.*?)</h[1-6]>",
            flags=re.IGNORECASE | re.DOTALL,
        )
        headings = list(heading_pattern.finditer(source_text))
        for index, heading_match in enumerate(headings):
            heading = self._clean_text(re.sub(r"<[^>]+>", "", heading_match.group("heading")))
            if not self._is_requirement_heading(heading):
                continue
            end = headings[index + 1].start() if index + 1 < len(headings) else len(source_text)
            section = source_text[heading_match.end() : end]
            candidates.extend(
                self._clean_text(re.sub(r"<[^>]+>", "", item))
                for item in re.findall(
                    r"<li[^>]*>(.*?)</li>", section, flags=re.IGNORECASE | re.DOTALL
                )
            )

        lines = self._visible_lines(source_text)
        for index, line in enumerate(lines):
            if not line.endswith(":") or not self._is_requirement_heading(line[:-1]):
                continue
            for item in lines[index + 1 :]:
                match = re.fullmatch(r"[-*]\s+(.+)", item)
                if not match:
                    break
                candidates.append(match.group(1).strip())

        unique: list[str] = []
        for candidate in candidates:
            if not candidate or len(candidate) > self.MAX_FACT_CHARS or candidate in unique:
                continue
            unique.append(candidate)
            if len(unique) == self.MAX_REQUIREMENTS:
                break
        return unique

    @staticmethod
    def _is_requirement_heading(heading: str) -> bool:
        normalized = re.sub(r"\s+", " ", heading.casefold()).strip()
        return "requirement" in normalized or bool(
            re.fullmatch(
                r"eligibility|who can apply|submission|participation|"
                r"how to (?:submit|enter|participate|apply)",
                normalized,
            )
        )

    @classmethod
    def _visible_lines(cls, source_text: str) -> list[str]:
        text = re.sub(
            r"</?(?:br|p|div|li|h[1-6])\b[^>]*>",
            "\n",
            source_text,
            flags=re.IGNORECASE,
        )
        text = re.sub(r"<[^>]+>", "", text)
        return [cls._clean_text(line) for line in text.splitlines() if cls._clean_text(line)]

    @staticmethod
    def _clean_text(value: str) -> str:
        return re.sub(r"\s+", " ", unescape(value)).strip()
