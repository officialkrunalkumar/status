import unittest
from datetime import datetime, timezone

from monitor import evaluate_overall, rebuild_daily, security_headers


class MonitorLogicTests(unittest.TestCase):
    def test_evaluate_overall(self):
        healthy = {
            "home": {"ok": True, "sev": "critical"},
            "blog": {"ok": True, "sev": "major"},
            "css": {"ok": True, "sev": "major"},
        }
        self.assertEqual(evaluate_overall(healthy, 30), ("up", []))

        home_down = dict(healthy)
        home_down["home"] = {"ok": False, "sev": "critical"}
        self.assertEqual(evaluate_overall(home_down, 30), ("down", ["home"]))

        degraded = dict(healthy)
        degraded["blog"] = {"ok": False, "sev": "major"}
        self.assertEqual(evaluate_overall(degraded, 30), ("degraded", ["blog"]))

        cert_warning = dict(healthy)
        self.assertEqual(evaluate_overall(cert_warning, 5), ("degraded", []))

    def test_rebuild_daily_aggregates_recent_history(self):
        now = datetime.now(timezone.utc)
        day = now.date().isoformat()
        future_now = now.replace(hour=min(now.hour + 1, 23))
        history = [
            {"t": now.isoformat(timespec="seconds"), "r": {"home": [1, 120], "blog": [0, 500]}},
            {"t": future_now.isoformat(timespec="seconds"), "r": {"home": [1, 130], "blog": [1, 200]}},
        ]
        result = rebuild_daily([], history)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["d"], day)
        self.assertEqual(result[0]["r"]["home"][0], 2)
        self.assertEqual(result[0]["r"]["home"][1], 2)
        self.assertEqual(result[0]["r"]["blog"][0], 1)

    def test_security_headers_returns_score(self):
        headers = {
            "strict-transport-security": "max-age=31536000; includeSubDomains",
            "content-security-policy": "default-src 'self'",
            "x-frame-options": "DENY",
        }
        result = security_headers(headers)
        self.assertEqual(result["score"], 3)
        self.assertIn("HSTS", result["present"])
        self.assertIn("CSP", result["present"])
        self.assertIn("X-Frame-Options", result["present"])
        self.assertIn("X-Content-Type-Options", result["missing"])


if __name__ == "__main__":
    unittest.main()
