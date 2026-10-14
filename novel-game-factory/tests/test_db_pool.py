import importlib
import importlib.util
import os
import unittest
from unittest.mock import patch


class WorkerDbPoolConfigTests(unittest.TestCase):
    def _module(self):
        module_name = "common.db_pool"
        self.assertIsNotNone(
            importlib.util.find_spec(module_name),
            "main 分支引用了 common.db_pool，但该模块缺失",
        )
        return importlib.import_module(module_name)

    def test_uses_call_site_defaults_without_environment_overrides(self):
        module = self._module()
        with patch.dict(os.environ, {}, clear=True):
            result = module.worker_pool_kwargs(5, 10)

        self.assertEqual(result, {"pool_size": 5, "max_overflow": 10})

    def test_uses_worker_environment_overrides(self):
        module = self._module()
        with patch.dict(
            os.environ,
            {"WORKER_DB_POOL_SIZE": "2", "WORKER_DB_MAX_OVERFLOW": "3"},
            clear=True,
        ):
            result = module.worker_pool_kwargs(5, 10)

        self.assertEqual(result, {"pool_size": 2, "max_overflow": 3})

    def test_invalid_environment_values_fall_back_to_call_site_defaults(self):
        module = self._module()
        with patch.dict(
            os.environ,
            {"WORKER_DB_POOL_SIZE": "bad", "WORKER_DB_MAX_OVERFLOW": "-1"},
            clear=True,
        ):
            result = module.worker_pool_kwargs(4, 6)

        self.assertEqual(result, {"pool_size": 4, "max_overflow": 6})


if __name__ == "__main__":
    unittest.main()
