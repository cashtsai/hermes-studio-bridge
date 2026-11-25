import unittest
from line_sessions import delivery_is_terminal


class ProDeliveryBudgetTests(unittest.TestCase):
    def test_cap_does_not_replay_old_results_next_month(self):
        self.assertTrue(delivery_is_terminal({"pushed": False, "reason": "quota_exhausted"}))
        self.assertTrue(delivery_is_terminal({"pushed": False, "reason": "delivery_expired"}))
        self.assertTrue(delivery_is_terminal({"pushed": True}))

    def test_transient_failures_still_retry(self):
        for reason in ["delivery_pending", "delivery_unknown", "push_rejected"]:
            self.assertFalse(delivery_is_terminal({"pushed": False, "reason": reason}))
