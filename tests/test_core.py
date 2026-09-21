import unittest

from ai_gateway_control_plane.core import Request, fixture


class GatewayTests(unittest.TestCase):
    def test_routes_to_low_score_endpoint(self):
        gateway = fixture()
        record = gateway.handle(Request("1", "demo", "chat", 100, "assistant"))
        self.assertEqual(record["endpoint"], "fast")
        self.assertEqual(record["prompt_version"], "v2")

    def test_budget_is_settled_from_actual_tokens(self):
        gateway = fixture()
        before = gateway.budgets["demo"]
        gateway.handle(Request("1", "demo", "chat", 1000, "assistant"), actual_tokens=500)
        self.assertAlmostEqual(before - gateway.budgets["demo"], 0.4)

    def test_breaker_forces_fallback(self):
        gateway = fixture()
        gateway.breaker.record("fast", False)
        gateway.breaker.record("fast", False)
        record = gateway.handle(Request("1", "demo", "chat", 100, "assistant"))
        self.assertNotEqual(record["endpoint"], "fast")

    def test_unknown_key_is_rejected(self):
        with self.assertRaises(PermissionError):
            fixture().handle(Request("1", "missing", "chat", 1, "assistant"))


if __name__ == "__main__":
    unittest.main()
