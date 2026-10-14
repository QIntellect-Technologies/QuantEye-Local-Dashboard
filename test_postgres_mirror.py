"""Local cloud Postgres mirror is opt-in. Face embeddings stay in SQLite/EXE."""
import os
import unittest
from unittest.mock import patch

import postgres_mirror


class PostgresMirrorConfigTests(unittest.TestCase):
    def test_unconfigured_runtime_does_not_use_localhost(self):
        with patch.dict(os.environ, {"DATABASE_URL": "", "POSTGRES_URL": "", "PGHOST": ""}, clear=False):
            os.environ.pop("DATABASE_URL", None)
            os.environ.pop("POSTGRES_URL", None)
            os.environ.pop("PGHOST", None)
            self.assertFalse(postgres_mirror.postgres_is_configured())
            self.assertFalse(postgres_mirror.mirror_employee({
                "id": 1,
                "name": "Ada",
                "organization_id": "ORG-A",
                "branch_id": "BR-A",
            }))

    def test_placeholder_org_is_not_mirrored(self):
        self.assertFalse(postgres_mirror._real_tenant("DEFAULT_ORG", "BR-01"))
        self.assertTrue(postgres_mirror._real_tenant("ORG-100", "BR-01"))

    def test_event_type_maps_to_schema_values(self):
        self.assertEqual(postgres_mirror._map_event_type("CHECKIN"), "CHECK_IN")
        self.assertEqual(postgres_mirror._map_event_type("CHECKOUT"), "CHECK_OUT")

    def test_hydrate_branch_employees_noops_without_postgres(self):
        with patch.dict(os.environ, {"DATABASE_URL": "", "POSTGRES_URL": "", "PGHOST": ""}, clear=False):
            os.environ.pop("DATABASE_URL", None)
            os.environ.pop("POSTGRES_URL", None)
            os.environ.pop("PGHOST", None)
            self.assertEqual(
                postgres_mirror.hydrate_branch_employees("ORG-A", "BR-A"),
                0,
            )


if __name__ == "__main__":
    unittest.main()
