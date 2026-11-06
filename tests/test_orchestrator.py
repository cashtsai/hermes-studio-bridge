import _isolation  # noqa: F401

import unittest

from orchestrator import plan_placement


class PlacementPlannerTest(unittest.TestCase):
    def setUp(self):
        self.hosts = [
            {"id": "cashcamp", "capabilities": ["codex", "stream"],
             "capacity": 2, "load": 2},
            {"id": "siege", "capabilities": ["codex", "stream", "gpu"],
             "capacity": 4, "load": 1},
        ]

    def test_new_work_chooses_least_loaded_capable_host(self):
        result = plan_placement(self.hosts, required=["codex", "stream"])
        self.assertEqual(result.status, "assigned")
        self.assertEqual(result.host_id, "siege")

    def test_missing_capability_is_queued_not_silently_downgraded(self):
        result = plan_placement(self.hosts, required=["android-live-update"])
        self.assertEqual(result.status, "queued")
        self.assertIsNone(result.host_id)

    def test_active_session_is_sticky(self):
        result = plan_placement(self.hosts, current_host_id="cashcamp",
                                session_state="running")
        self.assertEqual(result.status, "sticky")
        self.assertEqual(result.host_id, "cashcamp")

    def test_failed_session_needs_checkpoint_and_explicit_failover(self):
        blocked = plan_placement(self.hosts, current_host_id="cashcamp",
                                 session_state="failed")
        self.assertEqual(blocked.status, "blocked")
        assigned = plan_placement(self.hosts, current_host_id="cashcamp",
                                  session_state="failed", checkpointed=True,
                                  allow_failover=True, required=["gpu"])
        self.assertEqual(assigned.host_id, "siege")

    def test_busy_current_host_does_not_get_over_capacity_new_work(self):
        hosts = [{"id": "a", "capabilities": ["codex"], "capacity": 1, "load": 1}]
        self.assertEqual(plan_placement(hosts, required=["codex"]).status, "queued")


if __name__ == "__main__":
    unittest.main()
