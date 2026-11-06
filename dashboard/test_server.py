import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
import server


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = server.Store(Path(self.temp.name) / "history.db")
        self.base = dt.datetime(2026, 11, 5, 9, 0, tzinfo=server.CST).timestamp()

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def row(self, sequence, offset, message):
        return {"__CURSOR": "cursor-" + str(sequence), "__REALTIME_TIMESTAMP": str(int((self.base+offset)*1000000)), "MESSAGE": message}

    def ingest_attempt(self, number=219):
        self.store.ingest([self.row(1, 0, "第 {} 次请求，可用域 KKmt:AP-TOKYO-1-AD-1。".format(number)),
                           self.row(2, 6, "容量不足，稍后重试。"),
                           self.row(3, 6, "等待 311 秒。按 Ctrl+C 停止；下次运行会读取已有状态。")])

    def snapshot(self, active="active", **state):
        return self.store.snapshot(dict({"attempts": 219}, **state), {"ActiveState": active}, self.base+30)

    def test_capacity_wait_and_next_attempt_use_real_log_times(self):
        self.ingest_attempt()
        data = self.snapshot()
        self.assertEqual(data["phase"], "waiting")
        self.assertEqual(data["next_attempt_at"], self.base+317)
        self.assertEqual(data["total_attempts"], 219)
        self.assertEqual(data["counts"]["capacity"], 1)
        self.assertEqual(data["last_attempt"]["ad"], "KKmt:AP-TOKYO-1-AD-1")

    def test_replaying_journal_does_not_double_count(self):
        self.ingest_attempt()
        self.ingest_attempt()
        self.assertEqual(self.snapshot()["today_attempts"], 1)
        self.assertEqual(len(self.snapshot()["events"]), 3)

    def test_beijing_midnight_and_not_utc_midnight(self):
        self.store.ingest([self.row(1, -9*3600-1, "第 1 次请求，可用域 AD-1。"),
                           self.row(2, -9*3600+1, "第 2 次请求，可用域 AD-1。")])
        self.assertEqual(self.snapshot()["today_attempts"], 1)

    def test_running_request_overrides_previous_wait(self):
        self.ingest_attempt()
        self.store.ingest([self.row(4, 20, "第 220 次请求，可用域 AD-1。")])
        self.assertEqual(self.snapshot()["phase"], "requesting")

    def test_stopped_service_has_no_false_countdown(self):
        self.ingest_attempt()
        self.assertEqual(self.snapshot(active="inactive")["phase"], "paused")
        self.assertIsNone(self.snapshot(active="failed")["next_attempt_at"])

    def test_success_state_wins_over_inactive_service(self):
        self.ingest_attempt()
        self.store.ingest([self.row(4, 10, "实例已创建/已存在，停止所有创建重试。")])
        result = self.snapshot(active="inactive", instance_id="ocid1.instance.example")
        self.assertEqual(result["phase"], "success")
        self.assertEqual(result["last_attempt"]["status"], "success")

    def test_state_and_events_never_expose_request_tokens_or_keys(self):
        file = Path(self.temp.name) / "state.json"
        file.write_text(json.dumps({"attempts": 3, "instance_id": None,
                                    "pending": {"token": "TOPSECRET", "body": {"ssh_authorized_keys": "KEY"}}}))
        self.assertEqual(server.read_state(file), {"attempts": 3, "instance_id": None})
        kind, message, meta = server.classify("停止：sensitive ocid1.tenancy.example /home/example/.oci/a1_api_key.pem")
        self.assertNotIn("ocid1", message)
        self.assertNotIn("a1_api_key", message)

    def test_target_uses_user_region_and_whitelists_config_fields(self):
        file = Path(self.temp.name) / "a1-config.json"
        file.write_text(json.dumps({"region": "ap-osaka-1", "display_name": "My-A1",
                                    "ssh_authorized_keys": "SECRET_PUBLIC_KEY", "tenancy_id": "PRIVATE_ACCOUNT"}))
        target = server.load_target(file)
        self.assertEqual(target["name"], "My-A1")
        self.assertEqual(target["region_label"], "大阪")
        self.assertNotIn("ssh_authorized_keys", target)
        self.assertNotIn("PRIVATE_ACCOUNT", json.dumps(target))

    def test_pagination_and_filter_use_attempt_numbers(self):
        self.ingest_attempt(1)
        self.store.ingest([self.row(4, 10, "第 2 次请求，可用域 AD-1。"),
                           self.row(5, 11, "临时错误 429 TooManyRequests，保留原请求令牌，退避后核查。")])
        page = self.store.history(limit=1)
        self.assertEqual(page["items"][0]["number"], 2)
        self.assertTrue(page["has_more"])
        self.assertEqual(self.store.history(before=2)["items"][0]["number"], 1)
        self.assertEqual(self.store.history(status="error")["items"][0]["number"], 2)


if __name__ == "__main__":
    unittest.main()
