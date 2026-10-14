import unittest

from common.local_pipeline_schema import (
    CHARACTERS_REQUIRED_COLUMNS,
    LOCAL_VISUAL_TABLES,
    SCENES_REQUIRED_COLUMNS,
)


class LocalPipelineSchemaTests(unittest.TestCase):
    def test_schema_covers_visual_pipeline_tables_and_legacy_scene_columns(self):
        self.assertIn("characters", LOCAL_VISUAL_TABLES)
        self.assertIn("backgrounds", LOCAL_VISUAL_TABLES)
        self.assertIn("character_prompts", LOCAL_VISUAL_TABLES)
        self.assertIn("scene_prompts", LOCAL_VISUAL_TABLES)
        self.assertIn("atmosphere", LOCAL_VISUAL_TABLES)
        self.assertTrue(
            {
                "task_id",
                "raw",
                "failed_scenes",
                "update_time",
            }.issubset(SCENES_REQUIRED_COLUMNS)
        )
        self.assertIn("raw", CHARACTERS_REQUIRED_COLUMNS)


if __name__ == "__main__":
    unittest.main()
