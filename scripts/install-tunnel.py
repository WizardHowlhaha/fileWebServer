#!/usr/bin/env python3
"""Install the user's FileWeb SSH tunnel as a macOS LaunchAgent."""

import argparse
import getpass
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import tempfile


LABEL = f"com.{getpass.getuser()}.fileweb-tunnel"


def configuration(host):
    user_home = Path.home()
    logs = user_home / "Library/Logs/FileWebTunnel"
    return {
        "Label": LABEL,
        "ProgramArguments": [
            "/usr/bin/ssh", "-nNT",
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=yes",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "ConnectTimeout=10",
            "-o", "ConnectionAttempts=1",
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=3",
            "-o", "TCPKeepAlive=yes",
            "-o", "ControlMaster=no",
            "-o", "ControlPath=none",
            "-o", "ForkAfterAuthentication=no",
            "-o", "ForwardAgent=no",
            "-L", "127.0.0.1:8787:127.0.0.1:8787",
            host,
        ],
        "WorkingDirectory": str(user_home),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 15,
        "ExitTimeOut": 5,
        "ProcessType": "Background",
        "StandardOutPath": str(logs / "stdout.log"),
        "StandardErrorPath": str(logs / "stderr.log"),
        "Umask": 0o077,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, help="SSH host that runs the FileWeb service")
    parser.add_argument("--prepare", type=Path, help="Write the plist for review without installing")
    args = parser.parse_args()
    data = plistlib.dumps(configuration(args.host), sort_keys=False)
    if args.prepare:
        args.prepare.parent.mkdir(parents=True, exist_ok=True)
        args.prepare.write_bytes(data)
        print(args.prepare)
        return

    user_home = Path.home()
    domain = f"gui/{os.getuid()}"
    service = f"{domain}/{LABEL}"
    # Fail before changing anything if this user's login session is unavailable.
    subprocess.run(["launchctl", "print", domain], check=True, stdout=subprocess.DEVNULL)
    destination = user_home / f"Library/LaunchAgents/{LABEL}.plist"
    destination.parent.mkdir(parents=True, exist_ok=True)
    (user_home / "Library/Logs/FileWebTunnel").mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(dir=destination.parent, suffix=".plist", delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(data)
    try:
        subprocess.run(["plutil", "-lint", str(temporary)], check=True)
        if destination.exists():
            shutil.copy2(destination, destination.with_suffix(".plist.previous"))
        subprocess.run(["launchctl", "bootout", service], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.replace(temporary, destination)
        destination.chmod(0o600)
        subprocess.run(["launchctl", "enable", service], check=True)
        subprocess.run(["launchctl", "bootstrap", domain, str(destination)], check=True)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"Installed {service}\nConfiguration: {destination}\nURL: http://127.0.0.1:8787/")


if __name__ == "__main__":
    main()
