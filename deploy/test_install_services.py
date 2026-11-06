import unittest
from pathlib import Path
import install_services as app


class UnitTests(unittest.TestCase):
    def test_units_use_selected_user_paths_and_cli(self):
        units = app.render_units("alice", "users", Path("/home/alice"),
                                 Path("/home/alice/.local/state/a1/a1-config.json"),
                                 "/home/alice/project/.venv/bin/oci", "/usr/bin/python3")
        retry = units["oracle-a1-retry.service"]
        dashboard = units["oracle-a1-dashboard.service"]
        self.assertIn("User=alice", retry)
        self.assertIn("WorkingDirectory=/home/alice/.local/state/a1\n", retry)
        self.assertNotIn('WorkingDirectory="', retry + dashboard)
        self.assertIn("/home/alice/project/.venv/bin", retry)
        self.assertIn("MemoryMax=224M", retry)
        self.assertIn("ProtectHome=tmpfs", dashboard)
        self.assertIn("a1-config.state.json", dashboard)
        self.assertIn('BindReadOnlyPaths="/home/alice/.local/state/a1"', dashboard)
        self.assertNotIn("/home/example", retry + dashboard)

    def test_paths_are_quoted_and_specifiers_escaped(self):
        self.assertEqual(app.quote('/home/a b/100%/x"y'), '"/home/a b/100%%/x\\"y"')
        with self.assertRaises(ValueError):
            app.quote("/home/name\nExecStart=/bin/false")


if __name__ == "__main__":
    unittest.main()
