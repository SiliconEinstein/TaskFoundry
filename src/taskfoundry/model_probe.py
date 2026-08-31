"""Credential-safe fixed probe for the configured strong model transport."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from .labwright import atomic_json
from .model import ContractError


@dataclass(frozen=True)
class ProbeEvidence:
    """Sanitized probe result; never contains credentials, URLs, or response bytes."""

    succeeded: bool
    checked_at: str
    elapsed_sec: float
    route: str = "codex-strong-model-transport"
    http_status: int | None = None
    error_type: str | None = None
    schema_version: int = 1


class StrongModelProbe:
    """Issue one tiny OpenAI-compatible Responses request through the fixed proxy."""

    def run(self, *, env_file: Path, output_path: Path, timeout_sec: int = 90) -> ProbeEvidence:
        """Run one bounded probe and write only sanitized evidence fields."""
        if not 5 <= timeout_sec <= 120:
            raise ContractError("model probe timeout must be 5..120 seconds")
        environment = _read_env(env_file)
        required = ("STRONG_API_KEY", "STRONG_BASE_URL", "STRONG_PROXY")
        if any(not environment.get(name) for name in required):
            evidence = ProbeEvidence(
                succeeded=False,
                checked_at=datetime.now(UTC).isoformat(),
                elapsed_sec=0.0,
                error_type="MISSING_CONFIGURATION",
            )
            atomic_json(output_path, asdict(evidence))
            return evidence
        started = time.monotonic()
        succeeded, status, error_type = self._request(
            environment, timeout_sec=timeout_sec
        )
        evidence = ProbeEvidence(
            succeeded=succeeded,
            checked_at=datetime.now(UTC).isoformat(),
            elapsed_sec=round(time.monotonic() - started, 6),
            http_status=status,
            error_type=error_type,
        )
        atomic_json(output_path, asdict(evidence))
        return evidence

    @staticmethod
    def _request(
        environment: dict[str, str], *, timeout_sec: int
    ) -> tuple[bool, int | None, str | None]:
        """Execute the bounded transport call without exposing response content."""
        base = str(environment["STRONG_BASE_URL"]).rstrip("/")
        endpoint = base if base.endswith("/responses") else f"{base}/responses"
        proxy = str(environment["STRONG_PROXY"])
        status: int | None = None
        error_type: str | None = None
        succeeded = False
        request = _probe_request(endpoint, environment["STRONG_API_KEY"])
        opener = build_opener(ProxyHandler({"http": proxy, "https": proxy}))
        try:
            with opener.open(request, timeout=timeout_sec) as response:
                status = int(response.status)
                response.read(4096)
                succeeded = 200 <= status < 300
                if not succeeded:
                    error_type = "HTTP_STATUS"
        except HTTPError as error:
            status = int(error.code)
            error_type = "HTTP_ERROR"
        except (URLError, TimeoutError):
            error_type = "TRANSPORT_ERROR"
        except OSError:
            error_type = "LOCAL_IO_ERROR"
        return succeeded, status, error_type


def _probe_request(endpoint: str, api_key: str) -> Request:
    """Build the fixed minimal inference request in memory only."""
    return Request(
        endpoint,
        data=json.dumps(
            {
                "model": "matmaster/gpt-5.6-sol",
                "input": "Reply with PROBE_OK.",
                "max_output_tokens": 8,
            }
        ).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )


def _read_env(path: Path) -> dict[str, str]:
    """Read a restricted dotenv subset without interpolation or logging."""
    if not path.is_file() or path.is_symlink():
        raise ContractError("model probe env file is unavailable")
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            raise ContractError("model probe env line is invalid")
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip()
        if value[:1] == value[-1:] and value[:1] in {"'", '"'}:
            value = value[1:-1]
        if name in values:
            raise ContractError("model probe env contains a duplicate key")
        values[name] = value
    return values
