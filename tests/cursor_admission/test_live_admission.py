"""Future live supervisor adapter interface. No Cursor spawn is implemented here.

The adapter is separately reviewed/authorized after installation; it must run
real negative controls, return parent observations and finalize in finally.
Default collection explicitly skips every binary-dependent case.
"""

import importlib.util
import os
from pathlib import Path
import pytest
from .contract import CHECKS, admission_yes, validate_provenance, validate_result


@pytest.fixture(scope="module")
def live_observations():
    if os.environ.get("CURSOR_ADMISSION_AUTHORIZED") != "1":
        pytest.skip("NOT_RUN: Cursor installation and separate operator-approved live admission required")
    binary = os.environ.get("CURSOR_ADMISSION_BINARY", "")
    if not binary or not Path(binary).is_absolute() or not Path(binary).is_file():
        pytest.skip("NOT_RUN: exact installed Cursor binary prerequisite absent")
    adapter = os.environ.get("CURSOR_ADMISSION_ADAPTER", "")
    if not adapter or not Path(adapter).is_absolute() or not Path(adapter).is_file():
        pytest.skip("NOT_RUN: separately reviewed containment/supervisor adapter prerequisite absent")
    spec = importlib.util.spec_from_file_location("cursor_live_supervisor", adapter)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Adapter must verify a trusted pin before invoking even --version, and
    # must finally finalize the exact child on success, crash and timeout.
    evidence = module.run_admission(binary=binary, required_checks=CHECKS)
    validate_provenance(evidence["provenance"], evidence["expected_parent_provenance"])
    validate_result(evidence["result"], **evidence["expected_result_receipt"])
    return evidence["checks"]


@pytest.mark.parametrize("control", CHECKS)
def test_live_control(control, live_observations):
    row = live_observations.get(control, {})
    assert row.get("status") == "PASS", f"{control}: {row}"
    assert row.get("evidence"), f"{control}: missing supervisor evidence"


def test_live_admission_yes_requires_every_control(live_observations):
    assert admission_yes(live_observations), "NO: incomplete or failed live admission"
