"""system-telemetry: cross-platform GPU + host telemetry to Home Assistant via MQTT.

Modules:
  config   - environment/.env config, shared helpers (slug, dbg)
  nvidia   - nvidia-smi GPU metrics + loaded-model adapters
  lhm      - LibreHardwareMonitor reader (host CPU/RAM/disk, optional non-nvidia GPUs)
  mqtt     - HA discovery + state publishing
  __main__ - orchestrator (one cycle or loop), service entrypoint

The version is the release tag (vX.Y.Z) the install is checked out at; nothing
in the code carries it (release.sh tags main without a commit).
"""
