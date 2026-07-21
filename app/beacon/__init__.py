"""Authenticated Hyrule Beacon worker protocol."""

from app.beacon.client import BeaconClient
from app.beacon.models import BeaconLease, BeaconRun, WorkerEvent

__all__ = ["BeaconClient", "BeaconLease", "BeaconRun", "WorkerEvent"]
