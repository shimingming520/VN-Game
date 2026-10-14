from pathlib import Path


INFO_EXTRACTOR_ROOT = Path(__file__).resolve().parents[2] / "services" / "info_extractor"


def test_name_mapping_llm_timeout_caps_allow_thirty_minutes():
    for relative_path in (
        "plot/add_npc_to_name_mappings.py",
        "plot/ranked_name_mappings.py",
    ):
        content = (INFO_EXTRACTOR_ROOT / relative_path).read_text(encoding="utf-8")
        assert "LLM_TIMEOUT_MAX_SECONDS = 1800.0" in content
        assert "LLM_TIMEOUT_MAX_SECONDS = 600.0" not in content
