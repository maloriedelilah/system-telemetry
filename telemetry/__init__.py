"""system-telemetry: cross-platform GPU + host telemetry to Home Assistant via MQTT.

Modules:
  config   - environment/.env config, shared helpers (slug, dbg)
  nvidia   - nvidia-smi GPU metrics + loaded-model adapters
  lhm      - LibreHardwareMonitor reader (host CPU/RAM/disk, optional non-nvidia GPUs)
  mqtt     - HA discovery + state publishing
  __main__ - orchestrator (one cycle or loop), service entrypoint
"""

__version__ = "1.2.4"
