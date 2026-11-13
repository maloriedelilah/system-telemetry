"""Runs tests/test_release.sh (release.sh against a local "GitHub" and a fake gh).

CI's python job runs every tests/test_*.py; this puts the release checks there without
changing the workflow. The shell script is the test; its exit status is the verdict.
"""
import pathlib
import subprocess
import sys

script = pathlib.Path(__file__).with_name("test_release.sh")
sys.exit(subprocess.run(["bash", str(script)], check=False).returncode)
