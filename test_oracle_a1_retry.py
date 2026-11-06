#!/usr/bin/env python3
"""Offline tests. No OCI credentials and no cloud requests are used."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

import oracle_a1_retry as app


def config():
    return {"region": "ap-tokyo-1", "tenancy_id": "ocid1.tenancy.test", "compartment_id": "root",
            "subnet_id": "subnet", "image_id": "arm-image", "job_id": "job-123",
            "display_name": "Oracle-A1-1C6G", "ssh_authorized_keys": "ssh-ed25519 PUBLICKEY",
            "availability_domains": ["AD-1", "AD-2"], "interval_seconds": 300, "max_attempts": 0}


class FakeOCI:
    def __init__(self, effects=()):
        self.effects = iter(effects)
        self.posts = []

    def call(self, *args):
        if args[:3] == ("compute", "instance", "list"):
            return []
        if args[:3] == ("network", "subnet", "get"):
            return {"prohibit-public-ip-on-vnic": False}
        if args[0] == "raw-request":
            body = app.read_json(Path(args[args.index("--request-body") + 1][7:]))
            headers = json.loads(args[args.index("--request-headers") + 1])
            self.posts.append((body, headers))
            effect = next(self.effects)
            if isinstance(effect, BaseException):
                raise effect
            return effect
        raise AssertionError(args)


class RetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "a1-config.json"
        app.write_json(self.path, config())
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()

    def tearDown(self):
        self.output.__exit__(None, None, None)
        self.temp.cleanup()

    def execute(self, fake, check=False):
        with patch.object(app, "OCI", return_value=fake), patch.object(app, "budget_check"), \
             patch.object(app.time, "sleep"), patch.object(app.random, "randint", return_value=0):
            app.run(self.path, check)

    def test_capacity_rotates_domain_and_token(self):
        fake = FakeOCI([app.ApiError(500, "InternalError", "Out of host capacity."), {"id": "created"}])
        self.execute(fake)
        self.assertEqual([p[0]["availabilityDomain"] for p in fake.posts], ["AD-1", "AD-2"])
        self.assertNotEqual(fake.posts[0][1]["opc-retry-token"], fake.posts[1][1]["opc-retry-token"])
        self.assertEqual(app.read_json(self.path.with_suffix(".state.json"))["instance_id"], "created")

    def test_timeout_and_rate_limit_reuse_body_and_token(self):
        fake = FakeOCI([app.ApiError(0, "TransportTimeout", "timeout"),
                        app.ApiError(429, "TooManyRequests", "rate limit"), {"id": "created"}])
        self.execute(fake)
        self.assertEqual(fake.posts[0], fake.posts[1])
        self.assertEqual(fake.posts[1], fake.posts[2])

    def test_restart_preserves_ambiguous_request(self):
        first = FakeOCI([KeyboardInterrupt()])
        with self.assertRaises(KeyboardInterrupt):
            self.execute(first)
        second = FakeOCI([{"id": "created"}])
        self.execute(second)
        self.assertEqual(first.posts[0], second.posts[0])

    def test_restart_after_success_never_posts_again(self):
        self.execute(FakeOCI([{"id": "created"}]))
        fake = FakeOCI()
        self.execute(fake)
        self.assertEqual(fake.posts, [])

    def test_existing_instance_stops_without_post(self):
        fake = FakeOCI()
        existing = [{"id": "existing", "lifecycle-state": "PROVISIONING"}]
        with patch.object(app, "find_existing", return_value=existing):
            self.execute(fake)
        self.assertEqual(fake.posts, [])

    def test_unknown_server_error_keeps_token(self):
        fake = FakeOCI([app.ApiError(500, "InternalError", "upstream failure"), {"id": "created"}])
        self.execute(fake)
        self.assertEqual(fake.posts[0], fake.posts[1])

    def test_quota_error_stops_immediately(self):
        fake = FakeOCI([app.ApiError(400, "LimitExceeded", "quota exceeded")])
        with self.assertRaises(app.Stop):
            self.execute(fake)
        self.assertEqual(len(fake.posts), 1)

    def test_check_never_posts(self):
        fake = FakeOCI()
        self.execute(fake, check=True)
        self.assertEqual(fake.posts, [])

    def test_config_change_refused_after_interruption(self):
        with self.assertRaises(KeyboardInterrupt):
            self.execute(FakeOCI([KeyboardInterrupt()]))
        changed = config()
        changed["subnet_id"] = "another-subnet"
        app.write_json(self.path, changed)
        fake = FakeOCI()
        with self.assertRaises(app.Stop):
            self.execute(fake)
        self.assertEqual(fake.posts, [])

    def test_stale_token_never_posted(self):
        with self.assertRaises(KeyboardInterrupt):
            self.execute(FakeOCI([KeyboardInterrupt()]))
        state_path = self.path.with_suffix(".state.json")
        state = app.read_json(state_path)
        state["pending"]["created"] = time.time() - 21 * 3600
        app.write_json(state_path, state)
        fake = FakeOCI()
        with self.assertRaises(app.Stop):
            self.execute(fake)
        self.assertEqual(fake.posts, [])

    def test_fixed_shape_and_no_fault_domain(self):
        body = app.make_request(config(), "AD-1")
        self.assertEqual(body["shapeConfig"], {"ocpus": 1, "memoryInGBs": 6})
        self.assertEqual(body["sourceDetails"]["bootVolumeSizeInGBs"], 50)
        self.assertNotIn("faultDomain", body)
        self.assertNotIn("user_data", body["metadata"])


class ResourceTests(unittest.TestCase):
    def test_unrelated_same_name_is_not_claimed_as_success(self):
        reader = type("Reader", (), {"call": lambda *args:
                      [{"display-name": "Oracle-A1-1C6G", "lifecycle-state": "RUNNING", "freeform-tags": {}}]})()
        with self.assertRaises(app.Stop):
            app.find_existing(reader, config())

    def test_other_compartment_and_stopped_a1_count_toward_limit(self):
        class Reader:
            region = "ap-tokyo-1"

            def call(self, *args):
                if args[:3] == ("iam", "region-subscription", "list"):
                    return [{"is-home-region": True, "region-name": self.region}]
                if args[:3] == ("iam", "compartment", "list"):
                    return [{"id": "child", "lifecycle-state": "ACTIVE"}]
                if args[:3] == ("iam", "availability-domain", "list"):
                    return [{"name": "AD-1"}]
                if args[:3] == ("compute", "instance", "list"):
                    if args[args.index("--compartment-id") + 1] == "child":
                        return [{"shape": app.SHAPE, "lifecycle-state": "STOPPED",
                                 "shape-config": {"ocpus": 2, "memory-in-gbs": 12}}]
                    return []
                if args[:3] == ("bv", "volume", "list") or args[:3] == ("bv", "boot-volume", "list"):
                    return []
                raise AssertionError(args)

        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(app.Stop):
            app.budget_check(Reader(), config())

    def test_home_region_enforced(self):
        reader = type("Reader", (), {"region": "ap-tokyo-1", "call": lambda *args:
                      [{"is-home-region": True, "region-name": "ap-osaka-1"}]})()
        with self.assertRaises(app.Stop):
            app.subscriptions(reader, "tenancy")


class CliTests(unittest.TestCase):
    def test_empty_successful_all_list_is_empty_collection(self):
        response = subprocess.CompletedProcess([], 0, "", "")
        with patch.object(app.shutil, "which", return_value="oci"), \
             patch.object(app.subprocess, "run", return_value=response):
            self.assertEqual(app.OCI("ap-tokyo-1").call("iam", "compartment", "list", "--all"), [])

    def test_empty_post_response_remains_ambiguous(self):
        response = subprocess.CompletedProcess([], 0, "", "")
        with patch.object(app.shutil, "which", return_value="oci"), \
             patch.object(app.subprocess, "run", return_value=response):
            with self.assertRaises(app.ApiError) as caught:
                app.OCI("ap-tokyo-1").call("raw-request")
        self.assertEqual(caught.exception.code, "UnknownResponse")

    def test_inherited_auto_prompt_is_removed_for_unattended_calls(self):
        response = subprocess.CompletedProcess([], 0, '{"data":[]}', "")
        with patch.dict(app.os.environ, {"OCI_CLI_AUTO_PROMPT": "True"}), \
             patch.object(app.shutil, "which", return_value="oci"), \
             patch.object(app.subprocess, "run", return_value=response) as invoked:
            app.OCI("ap-tokyo-1").call("iam", "region-subscription", "list")
        self.assertNotIn("OCI_CLI_AUTO_PROMPT", invoked.call_args.kwargs["env"])

    def test_raw_http_error_even_when_cli_returns_zero(self):
        response = subprocess.CompletedProcess([], 0, json.dumps({"status": "429 Too Many Requests",
                    "data": {"code": "TooManyRequests", "message": "slow down"}}), "")
        with patch.object(app.shutil, "which", return_value="oci"), \
             patch.object(app.subprocess, "run", return_value=response):
            with self.assertRaises(app.ApiError) as caught:
                app.OCI("ap-tokyo-1").call("raw-request")
        self.assertEqual(caught.exception.status, 429)

    def test_successful_raw_response(self):
        response = subprocess.CompletedProcess([], 0, '{"status":"200 OK","data":{"id":"created"}}', "")
        with patch.object(app.shutil, "which", return_value="oci"), \
             patch.object(app.subprocess, "run", return_value=response):
            self.assertEqual(app.OCI("ap-tokyo-1").call("raw-request"), {"id": "created"})


class InitializeTests(unittest.TestCase):
    def test_manual_subnet_initialization_without_existing_vm(self):
        class Reader:
            region = "ap-osaka-1"

            def call(self, *args):
                if args[:3] == ("iam", "region-subscription", "list"):
                    return [{"region-name": "ap-osaka-1", "is-home-region": True}]
                if args[:3] == ("network", "subnet", "get"):
                    return {"id": "subnet-example", "prohibit-public-ip-on-vnic": False}
                if args[:3] == ("compute", "image", "list"):
                    return [{"id": "image-example", "display-name": "Oracle-Linux-9.8-aarch64", "operating-system-version": "9.8"}]
                if args[:3] == ("iam", "availability-domain", "list"):
                    return [{"name": "AD-1"}]
                raise AssertionError(args)

        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            public = folder / "test.pub"
            public.write_text("ssh-ed25519 EXAMPLE_PUBLIC_KEY")
            path = folder / "a1-config.json"
            with patch.object(app, "OCI", return_value=Reader()), \
                 patch("builtins.input", side_effect=["ap-osaka-1", "ocid1.tenancy.example", "ocid1.tenancy.example"]), \
                 patch.dict(app.os.environ, {"OCI_CLI_CONFIG_FILE": str(folder / "absent")}), \
                 contextlib.redirect_stdout(io.StringIO()):
                app.initialize(path, None, "subnet-example", str(public))
            saved = app.read_json(path)
            self.assertEqual(saved["region"], "ap-osaka-1")
            self.assertEqual(saved["subnet_id"], "subnet-example")
            self.assertEqual(saved["nsg_ids"], [])
            self.assertFalse(path.with_suffix(".state.json").exists())


if __name__ == "__main__":
    unittest.main()
