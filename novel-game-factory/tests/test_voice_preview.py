from common import voice_preview


def test_generate_local_audition_caches_tts_result(monkeypatch, tmp_path):
    monkeypatch.setenv("STORAGE_PATH", str(tmp_path))
    monkeypatch.setenv("AUDIO_BASE_URL", "https://tts.test")
    monkeypatch.setenv("ELEVENLABS_RELAY_API_KEY", "test-key")
    fake_audio = b"RIFF" + b"\x00" * 64

    class FakeResponse:
        headers = {}
        content = b""

        def raise_for_status(self):
            pass

        def json(self):
            return {
                "base_resp": {"status_code": 0},
                "data": {"audio": fake_audio.hex()},
            }

    calls = []
    monkeypatch.setattr(
        voice_preview.requests,
        "post",
        lambda *args, **kwargs: calls.append((args, kwargs)) or FakeResponse(),
    )

    url = voice_preview.generate_local_audition(
        voice_id="Chinese_sweet_girl_vv1",
        gender="女",
        age_category="少年",
    )

    assert url == (
        "/api/v1/local-assets/storage/timbre/female/adolescent/"
        "Chinese_sweet_girl_vv1.wav"
    )
    assert len(calls) == 1
    assert calls[0][0][0] == "https://tts.test/v1/audio/speech"
    assert calls[0][1]["json"]["input"]
    assert calls[0][1]["json"]["voice"] == "Chinese_sweet_girl_vv1"
    assert (
        tmp_path / "timbre" / "female" / "adolescent"
        / "Chinese_sweet_girl_vv1.wav"
    ).read_bytes() == fake_audio

    voice_preview.generate_local_audition(
        voice_id="Chinese_sweet_girl_vv1",
        gender="女",
        age_category="少年",
    )
    assert len(calls) == 1
