from __future__ import annotations

import json
from pathlib import Path

from taskfoundry.model_probe import StrongModelProbe


def test_probe_missing_configuration_writes_sanitized_evidence(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("STRONG_API_KEY=secret\n", encoding="utf-8")
    output = tmp_path / "probe.json"

    result = StrongModelProbe().run(env_file=env, output_path=output)

    assert result.succeeded is False
    assert result.error_type == "MISSING_CONFIGURATION"
    encoded = output.read_text(encoding="utf-8")
    assert "secret" not in encoded
    assert "STRONG_" not in encoded


def test_probe_rejects_duplicate_env_keys_without_echoing_values(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("STRONG_API_KEY=first\nSTRONG_API_KEY=second\n", encoding="utf-8")

    try:
        StrongModelProbe().run(env_file=env, output_path=tmp_path / "probe.json")
    except Exception as error:
        assert "first" not in str(error)
        assert "second" not in str(error)
    else:
        raise AssertionError("duplicate dotenv key was accepted")
