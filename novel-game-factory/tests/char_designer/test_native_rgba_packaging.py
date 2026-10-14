from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_char_designer_dockerfile_does_not_inherit_matting_base():
    dockerfile = (
        PROJECT_ROOT / "services" / "char_designer" / "Dockerfile"
    ).read_text(encoding="utf-8")

    assert "FROM python:3.10-slim" in dockerfile
    assert "base:char" not in dockerfile
    assert "torch==" not in dockerfile
    assert "torchvision==" not in dockerfile


def test_char_designer_requirements_exclude_local_matting_stack():
    requirements = (
        PROJECT_ROOT / "services" / "char_designer" / "requirements.txt"
    ).read_text(encoding="utf-8")
    blocked = {
        "transparent-background",
        "transformers",
        "huggingface-hub",
        "safetensors",
        "timm",
        "kornia",
        "einops",
        "accelerate",
        "pymatting",
        "opencv-python-headless",
        "scikit-image",
    }

    active = {
        line.strip().split("==", 1)[0].split(">=", 1)[0].split("<", 1)[0]
        for line in requirements.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert blocked.isdisjoint(active)


def test_docker_context_excludes_legacy_character_matting_weights():
    dockerignore = (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8")

    assert "services/char_designer/models/koutu/" in dockerignore
