"""Runs tests/test_install.sh (install.sh fleet mode) as root through sudo -n.

CI's python job runs every tests/test_*.py; GitHub's runners have passwordless sudo. A step in
the shell job (`sudo bash tests/test_install.sh`) would be cleaner; this session can't push
workflow changes. The shell script is the test; its exit status is the verdict.
"""
import os
import pathlib
import subprocess
import sys

script = pathlib.Path(__file__).with_name("test_install.sh")
cmd = ["bash", str(script)] if os.geteuid() == 0 else ["sudo", "-n", "bash", str(script)]
sys.exit(subprocess.run(cmd, check=False).returncode)
