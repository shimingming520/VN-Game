import unittest

from common.plot_llm_config import resolve_plot_llm_config


class PlotLlmConfigTests(unittest.TestCase):
    def test_local_farmmx_configuration_uses_shared_text_model_credentials(self):
        config = resolve_plot_llm_config(
            {
                "GPT_IMAGE_API_KEY": "image-key",
                "GPT_IMAGE_BASE_URL": "https://ai.farmmx.com",
                "LLM_MODEL": "gpt-5.6-sol",
            }
        )

        self.assertEqual(config.api_key, "image-key")
        self.assertEqual(config.base_url, "https://ai.farmmx.com/v1")
        self.assertEqual(config.model, "gpt-5.6-sol")

    def test_explicit_plot_values_override_shared_defaults(self):
        config = resolve_plot_llm_config(
            {
                "PLOT_API_KEY": "plot-key",
                "PLOT_BASE_URL": "https://plot.example/v1",
                "PLOT_MODEL": "plot-model",
                "GPT_IMAGE_API_KEY": "image-key",
                "LLM_MODEL": "shared-model",
            }
        )

        self.assertEqual(config.api_key, "plot-key")
        self.assertEqual(config.base_url, "https://plot.example/v1")
        self.assertEqual(config.model, "plot-model")


if __name__ == "__main__":
    unittest.main()
