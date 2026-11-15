"""Test tiers. Tests that start a real Hermes agent or other subprocesses are marked slow:
CI always runs them; set HERMES_GUESTS_FAST=1 to skip them while iterating."""

import os
import unittest

slow = unittest.skipIf(
    os.environ.get("HERMES_GUESTS_FAST") == "1", "slow (runs in CI; HERMES_GUESTS_FAST=1 skips)"
)
