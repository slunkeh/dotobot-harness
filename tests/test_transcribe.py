"""Speech-to-text uses the configured provider; keys stay on the harness."""

from __future__ import annotations

import json

import pytest

from harness.paths import HarnessPaths
from harness.transcribe import TranscribeError, _multipart, transcribe


def test_multipart_puts_file_last():
    bound, body = _multipart([("language", "en")], "clip.wav", b"AUDIO", "audio/wav")
    text = body.decode("latin1")
    assert text.index('name="language"') < text.index('filename="clip.wav"')
    assert text.endswith(f"--{bound}--\r\n")
    assert b"AUDIO" in body


def test_transcribe_requires_a_clip(tmp_path):
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout([])
    with pytest.raises(TranscribeError, match="empty"):
        transcribe(paths, b"")


def test_transcribe_errors_without_credentials(tmp_path, monkeypatch):
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout([])
    monkeypatch.setattr("harness.transcribe._xai_token", lambda _p: None)
    monkeypatch.setattr("harness.transcribe.get_secret", lambda *_a, **_k: None)
    monkeypatch.setattr("harness.transcribe._available", lambda *_a, **_k: False)
    with pytest.raises(TranscribeError, match="Grok|OpenAI|Voice"):
        transcribe(paths, b"RIFF....")


def test_transcribe_uses_grok_when_configured(tmp_path, monkeypatch):
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout([])
    monkeypatch.setattr("harness.transcribe._xai_token", lambda _p: "tok")

    class _Resp:
        def read(self):
            return json.dumps({"text": "pause bark too", "duration": 1.2}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_open(req, timeout=60):
        assert "api.x.ai" in req.full_url
        assert req.get_header("Authorization") == "Bearer tok"
        return _Resp()

    monkeypatch.setattr("harness.transcribe.urllib.request.urlopen", fake_open)
    out = transcribe(paths, b"RIFF....", filename="clip.wav")
    assert out["text"] == "pause bark too"
    assert out["provider"] == "grok"
