import os
import sys
import unittest
from pathlib import Path

os.environ["PLATFORM_AUTH_DISABLED"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from auth.deps import (  # noqa: E402
    get_current_user,
    get_current_user_optional,
    get_task_owner,
    get_user_or_service,
)


class NoAuthDependencyTest(unittest.TestCase):
    def test_anonymous_dependencies_use_local_identity(self):
        user = get_current_user(None)
        self.assertEqual(user.main_site_uid, "local-anonymous")
        self.assertEqual(user.role, "admin")

        optional_user = get_current_user_optional(None)
        self.assertEqual(optional_user.main_site_uid, "local-anonymous")

        service_user = get_user_or_service(None, None)
        self.assertEqual(service_user.main_site_uid, "local-anonymous")

        owner = get_task_owner(None, None, None, None)
        self.assertEqual(owner.user_id, "local-anonymous")
        self.assertEqual(owner.user_name, "本地用户")
