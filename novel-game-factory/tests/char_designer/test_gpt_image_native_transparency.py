from PIL import Image

from services.char_designer.image_providers.gpt_image import (
    GPTImageEditProvider,
    GPTImageTextProvider,
)


def test_text_provider_forces_transparent_png_and_prompt_contract(monkeypatch):
    captured = {}

    class Response:
        ok = True

        @staticmethod
        def json():
            return {"data": [{"b64_json": "aW1hZ2U="}]}

    def fake_post(url, *, json, headers, timeout):
        captured.update({"url": url, "body": json})
        return Response()

    monkeypatch.setattr(
        "services.char_designer.image_providers.gpt_image.requests.post",
        fake_post,
    )
    provider = GPTImageTextProvider(
        api_key="test-key",
        base_url="https://provider.example",
        transparent_character_output=True,
        max_retries=1,
    )

    provider.generate("one person on a pure white background")

    assert captured["body"]["background"] == "transparent"
    assert captured["body"]["output_format"] == "png"
    assert "pure white background" not in captured["body"]["prompt"].lower()
    assert "genuinely transparent canvas" in captured["body"]["prompt"]


def test_edit_provider_forces_transparent_png(monkeypatch, tmp_path):
    captured = {}
    source = tmp_path / "source.png"
    Image.new("RGBA", (10, 10), (0, 0, 0, 0)).save(source, format="PNG")

    class Response:
        ok = True

        @staticmethod
        def json():
            return {"data": [{"b64_json": "aW1hZ2U="}]}

    def fake_post(url, *, data, files, headers, timeout):
        uploaded = files["image[]"][1]
        uploaded.seek(0)
        captured.update(
            {"url": url, "data": data, "uploaded_bytes": uploaded.read()}
        )
        return Response()

    monkeypatch.setattr(
        "services.char_designer.image_providers.gpt_image.requests.post",
        fake_post,
    )
    provider = GPTImageEditProvider(
        api_key="test-key",
        base_url="https://provider.example",
        transparent_character_output=True,
        max_retries=1,
    )

    provider.edit(source, "preserve the person on a white background")

    assert captured["data"]["background"] == "transparent"
    assert captured["data"]["output_format"] == "png"
    assert "genuinely transparent canvas" in provider.last_prompt
    from io import BytesIO

    with Image.open(BytesIO(captured["uploaded_bytes"])) as image:
        assert image.mode == "RGBA"


def test_transparency_repair_can_upload_opaque_rgb_while_requesting_alpha(
    monkeypatch,
    tmp_path,
):
    captured = {}
    source = tmp_path / "source.png"
    Image.new("RGB", (10, 10), (255, 255, 255)).save(source, format="PNG")

    class Response:
        ok = True

        @staticmethod
        def json():
            return {"data": [{"b64_json": "aW1hZ2U="}]}

    def fake_post(url, *, data, files, headers, timeout):
        uploaded = files["image[]"][1]
        uploaded.seek(0)
        captured["uploaded_bytes"] = uploaded.read()
        captured["data"] = data
        return Response()

    monkeypatch.setattr(
        "services.char_designer.image_providers.gpt_image.requests.post",
        fake_post,
    )
    provider = GPTImageEditProvider(
        api_key="test-key",
        base_url="https://provider.example",
        transparent_character_output=True,
        preserve_input_transparency=False,
        max_retries=1,
    )

    provider.edit(source, "remove only the background")

    from io import BytesIO

    with Image.open(BytesIO(captured["uploaded_bytes"])) as image:
        assert image.mode == "RGB"
    assert captured["data"]["background"] == "transparent"


def test_nontransparent_provider_can_save_opaque_return(tmp_path):
    import base64
    from io import BytesIO

    buffer = BytesIO()
    Image.new("RGBA", (32, 32), (20, 30, 40, 255)).save(buffer, format="PNG")
    provider = GPTImageTextProvider(
        api_key="test-key",
        base_url="https://provider.example",
        transparent_character_output=True,
        max_retries=1,
    )
    target = tmp_path / "opaque.png"

    provider.transparent_character_output = False
    assert provider.save_image_item(
        {"b64_json": base64.b64encode(buffer.getvalue()).decode("ascii")}, target
    ) is True
    assert target.exists()


def test_opaque_character_return_uses_provider_native_transparency_repair(
    monkeypatch,
    tmp_path,
):
    import base64
    from io import BytesIO

    opaque_buffer = BytesIO()
    Image.new("RGB", (40, 50), (255, 255, 255)).save(
        opaque_buffer,
        format="PNG",
    )
    transparent_buffer = BytesIO()
    transparent = Image.new("RGBA", (40, 50), (0, 0, 0, 0))
    for x in range(10, 30):
        for y in range(5, 45):
            transparent.putpixel((x, y), (20, 30, 40, 220))
    transparent.save(transparent_buffer, format="PNG")

    monkeypatch.setattr(
        GPTImageEditProvider,
        "edit",
        lambda self, image_path, prompt, prompt_profile="balanced": [
            {
                "b64_json": base64.b64encode(
                    transparent_buffer.getvalue()
                ).decode("ascii")
            }
        ],
    )
    provider = GPTImageTextProvider(
        api_key="test-key",
        base_url="https://provider.example",
        transparent_character_output=True,
        max_retries=1,
    )
    target = tmp_path / "repaired.png"

    assert provider.save_image_item(
        {"b64_json": base64.b64encode(opaque_buffer.getvalue()).decode("ascii")},
        target,
    ) is True
    with Image.open(target) as repaired:
        assert repaired.mode == "RGBA"
        assert repaired.getchannel("A").getextrema() == (0, 220)
