"""Pure multi-host capacity lease contract tests."""

import _isolation  # noqa: F401
import unittest

from capacity_lease import CapacityLeaseError, CapacityLeaseTable


class CapacityLeaseTest(unittest.TestCase):
    def setUp(self):
        ids = iter(("lease-a", "lease-b", "lease-c"))
        self.table = CapacityLeaseTable(lease_id_factory=lambda: next(ids))

    def test_acquire_is_idempotent_for_same_session(self):
        first = self.table.acquire(host_id="siege", session_id="s-1",
                                   capacity=1, ttl_seconds=60, now=100)
        again = self.table.acquire(host_id="siege", session_id="s-1",
                                   capacity=1, ttl_seconds=60, now=110)
        self.assertEqual(first.status, "acquired")
        self.assertEqual(again.status, "held")
        self.assertEqual(again.lease.lease_id, "lease-a")
        self.assertEqual(len(self.table.find(now=110)), 1)

    def test_capacity_rejects_other_session_without_overbooking(self):
        self.table.acquire(host_id="cashcamp", session_id="s-1",
                           capacity=1, ttl_seconds=60, now=100)
        result = self.table.acquire(host_id="cashcamp", session_id="s-2",
                                    capacity=1, ttl_seconds=60, now=101)
        self.assertEqual(result.status, "at_capacity")
        self.assertEqual(len(self.table.find(host_id="cashcamp", now=101)), 1)

    def test_expiry_frees_capacity(self):
        self.table.acquire(host_id="cashcamp", session_id="s-1",
                           capacity=1, ttl_seconds=5, now=100)
        self.assertEqual(self.table.reap(now=105), ("lease-a",))
        result = self.table.acquire(host_id="cashcamp", session_id="s-2",
                                    capacity=1, ttl_seconds=5, now=105)
        self.assertEqual(result.status, "acquired")

    def test_release_is_idempotent(self):
        lease = self.table.acquire(host_id="h", session_id="s",
                                   capacity=1, ttl_seconds=5, now=100).lease
        self.assertTrue(self.table.release(lease.lease_id))
        self.assertFalse(self.table.release(lease.lease_id))
        self.assertFalse(self.table.find(now=100))

    def test_ttl_and_identity_are_bounded(self):
        with self.assertRaises(CapacityLeaseError):
            self.table.acquire(host_id="", session_id="s", capacity=1,
                               ttl_seconds=60, now=100)
        with self.assertRaises(CapacityLeaseError):
            self.table.acquire(host_id="h", session_id="s", capacity=1,
                               ttl_seconds=4, now=100)
        with self.assertRaises(CapacityLeaseError):
            self.table.acquire(host_id="h", session_id="s", capacity=1,
                               ttl_seconds=3601, now=100)


if __name__ == "__main__":
    unittest.main()
