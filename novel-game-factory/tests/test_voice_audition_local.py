from common.voice_audition import _audition_url


def test_audition_url_is_empty_when_local_sample_is_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("STORAGE_PATH", str(tmp_path))

    assert _audition_url("男", "青年", "missing_voice") == ""


def test_audition_url_uses_local_sample_when_present(monkeypatch, tmp_path):
    monkeypatch.setenv("STORAGE_PATH", str(tmp_path))
    sample = tmp_path / "timbre" / "male" / "young_adult" / "local_voice.wav"
    sample.parent.mkdir(parents=True)
    sample.write_bytes(b"RIFF")

    assert _audition_url("男", "青年", "local_voice") == (
        "/api/v1/local-assets/storage/timbre/male/young_adult/local_voice.wav"
    )
