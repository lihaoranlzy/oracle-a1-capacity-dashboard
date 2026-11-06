#!/usr/bin/env python3
"""Install systemd units for the current user's task; never start cloud creation."""
import argparse
import grp
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import sys


def quote(value, exec_arg=False):
    value = str(value)
    if any(c in value for c in ("\n", "\r", "\0")):
        raise ValueError("Paths and settings cannot contain newlines or NULs")
    value = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    if exec_arg:
        value = value.replace("$", "$$")
    return '"' + value + '"'


def scalar_path(value):
    # WorkingDirectory is parsed as a single path, unlike ExecStart/list settings.
    # Outer quotation marks are interpreted literally by systemd here.
    value = str(value)
    if any(c in value for c in ("\n", "\r", "\0")):
        raise ValueError("Paths cannot contain newlines or NULs")
    return value.replace("%", "%%")


def render_units(user, group, home, config, oci_bin, python_bin):
    config = Path(config)
    job_dir = config.parent
    code = Path("/opt/oracle-a1-capacity-dashboard")
    path = str(Path(oci_bin).parent) + ":/usr/local/bin:/usr/bin:/bin"
    shared = """UMask=0077
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectSystem=strict
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX AF_NETLINK
"""
    retry = """[Unit]
Description=Retry Oracle A1 1 OCPU 6 GB instance allocation
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User={user}
Group={group}
WorkingDirectory={workingdir}
Environment={envpath}
Environment={envhome}
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=TZ=Asia/Shanghai
ExecStart={python} {program} --config {config}
Restart=no
Nice=10
CPUWeight=10
CPUQuota=20%
MemoryAccounting=true
MemoryHigh=160M
MemoryMax=224M
MemorySwapMax=96M
OOMScoreAdjust=500
TasksMax=48
ProtectHome=read-only
ReadWritePaths={directory}
TimeoutStopSec=20
{shared}
[Install]
WantedBy=multi-user.target
""".format(user=user, group=group, directory=quote(job_dir), workingdir=scalar_path(job_dir),
           envpath=quote("PATH=" + path), envhome=quote("HOME=" + str(home)),
           python=quote(python_bin, True), program=quote(code / "oracle_a1_retry.py", True),
           config=quote(config, True), shared=shared)
    dashboard = """[Unit]
Description=Read-only Oracle A1 allocation dashboard
After=network.target

[Service]
Type=simple
User={user}
Group={group}
SupplementaryGroups=systemd-journal
WorkingDirectory={code}
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=TZ=Asia/Shanghai
ExecStart={python} {program} --config-file {config} --state-file {state} --data-dir /var/lib/oracle-a1-dashboard
Restart=on-failure
RestartSec=10
StateDirectory=oracle-a1-dashboard
StateDirectoryMode=0700
Nice=15
CPUWeight=5
CPUQuota=10%
MemoryAccounting=true
MemoryHigh=48M
MemoryMax=80M
MemorySwapMax=32M
OOMScoreAdjust=600
TasksMax=32
ProtectHome=tmpfs
BindReadOnlyPaths={directory}
ReadWritePaths=/var/lib/oracle-a1-dashboard
{shared}
[Install]
WantedBy=multi-user.target
""".format(user=user, group=group, code=scalar_path(code / "dashboard"), python=quote(python_bin, True),
           program=quote(code / "dashboard/server.py", True), config=quote(config, True),
           state=quote(config.with_suffix(".state.json"), True), directory=quote(job_dir), shared=shared)
    return {"oracle-a1-retry.service": retry, "oracle-a1-dashboard.service": dashboard}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", default=os.environ.get("SUDO_USER"))
    parser.add_argument("--config", required=True)
    parser.add_argument("--oci-bin", required=True, help="Absolute path of the user's OCI CLI")
    parser.add_argument("--python", default="/usr/bin/python3", help="Absolute path of Python 3.9+")
    args = parser.parse_args()
    if os.geteuid() != 0 or not args.user or args.user == "root":
        parser.error("Run with sudo and --user YOUR_NORMAL_USERNAME; tasks must not run as root")
    if not shutil.which("systemctl"):
        parser.error("This installer requires Linux with systemd")
    user = pwd.getpwnam(args.user)
    group = grp.getgrgid(user.pw_gid).gr_name
    grp.getgrnam("systemd-journal")
    config = Path(args.config).expanduser().resolve(strict=True)
    home = Path(user.pw_dir).resolve()
    job_dir = config.parent
    # The dashboard bind mount must not reveal the entire home or API credentials.
    if job_dir == home or job_dir in home.parents or ".oci" in job_dir.parts:
        parser.error("Keep config in a dedicated directory such as ~/.local/state/oracle-a1/a1-config.json")
    if config.stat().st_uid != user.pw_uid or job_dir.stat().st_uid != user.pw_uid:
        parser.error("Config and its dedicated directory must belong to --user")
    if not os.path.isabs(args.oci_bin) or not os.access(args.oci_bin, os.X_OK):
        parser.error("--oci-bin must point to an existing executable")
    if not os.path.isabs(args.python) or not os.access(args.python, os.X_OK):
        parser.error("--python must point to an existing Python executable")
    # A venv made by another user/root may not be usable by the service account.
    subprocess.run(["runuser", "-u", user.pw_name, "--", args.oci_bin, "--version"], check=True)
    version = subprocess.check_output([args.python, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"], text=True)
    if tuple(map(int, version.strip().split("."))) < (3, 9):
        parser.error("Python 3.9+ is required")
    source = Path(__file__).resolve().parents[1]
    destination = Path("/opt/oracle-a1-capacity-dashboard")
    destination.mkdir(mode=0o755, parents=True, exist_ok=True)
    for relative in ["oracle_a1_retry.py", "dashboard/server.py", "dashboard/static/index.html",
                     "dashboard/static/style.css", "dashboard/static/app.js"]:
        target = destination / relative
        target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        shutil.copyfile(str(source / relative), str(target))
        target.chmod(0o644)
    units = render_units(user.pw_name, group, home, config, args.oci_bin, args.python)
    for name, body in units.items():
        path = Path("/etc/systemd/system") / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o644)
    subprocess.run(["systemd-analyze", "verify"] + ["/etc/systemd/system/" + name for name in units], check=True)
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    print("Installed units. No service has been started and no cloud resource has been created.")
    print("After reviewing --check output, start explicitly:")
    print("  sudo systemctl enable --now oracle-a1-retry oracle-a1-dashboard")
    print("The dashboard listens on 127.0.0.1:28181; use an SSH tunnel.")


if __name__ == "__main__":
    try:
        main()
    except (KeyError, OSError, ValueError, subprocess.CalledProcessError) as error:
        sys.exit("Installation stopped: {}".format(error))
