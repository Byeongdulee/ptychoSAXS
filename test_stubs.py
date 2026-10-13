#!/usr/bin/env python3
"""Quick test to verify debug stubs have necessary methods."""

import sys
import os

# Add debug path for stub imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'debug'))

from debug_stubs import _HexapodInfo, PilatusStub, SGStreamStub, InstrumentsStub

def test_hexapod_stubs():
    """Test that _HexapodInfo has all required methods."""
    hexapod = _HexapodInfo({'X': 0, 'Y': 0, 'Z': 0})

    # Test trajectory methods. The pulse counts are asserted, not just the
    # calls: the scan engine arms the detectors for exactly pulse_number
    # frames, so a stub that merely does not raise would hide a drift
    # between this arithmetic and _adjust_axis_length's.
    hexapod.set_traj('X', 1.0, 1.0, 0.0, 1, 0.01, 50)
    assert hexapod.pulse_step == 0.01
    assert hexapod.pulse_number == 100  # total_time / step_time

    hexapod.set_traj_SNAKE2(0.01, 0.0, 0.04, 0.01, 0.0, 0.02, 0.01)
    # X: 0, .01, .02, .03, .04 -> 5, +1 phantom trigger = 6.
    # Y: 0, .01, .02 -> 3, padded up to an even 4.
    assert hexapod.pulse_number == 24
    assert len(hexapod.pulse_positions_index) == 24

    # WaveGenID and pulse_positions_index are reached by the fly-scan paths
    # and used to be missing here, so those paths raised in debug mode.
    assert set(hexapod.WaveGenID) == {'X', 'Y', 'Z', 'U', 'V', 'W'}

    # Test analysis
    minstep, commonstep = hexapod.analyze_pulse_steps()
    assert isinstance(minstep, int)
    assert isinstance(commonstep, int)

    # Test waveform assignment
    hexapod.assign_axis2wavtable('X', 0)

    # Test positioning
    hexapod.goto_start_pos('X')

    # Test trajectory execution
    hexapod.run_traj('X')
    assert hexapod.istraj_running() == False

    # Test stopping
    hexapod.stop_traj()

    # Test wait
    hexapod.wait()

    # Test records
    records = hexapod.get_records()
    assert isinstance(records, list)

    # Test movement. The shared position dict is what keeps InstrumentsStub's
    # sub-objects agreeing with each other, so the move has to land in it.
    result = hexapod.mv('X', 1.0, 'Y', 2.0, wait=True)
    assert result == True
    assert hexapod.get_pos()['X'] == 1.0
    assert hexapod.get_pos('Y') == 2.0

    print("✓ Hexapod stubs working")

def test_detector_stubs():
    """Test that detector stubs have all required methods."""

    # Test Pilatus stub
    pilatus = PilatusStub()
    pilatus.fly_ready(0.1, 100)
    assert pilatus.fileGet("FileNumber_RBV") == 1
    assert pilatus.fileGet("FullFileName_RBV", as_string=True) == "/debug/image_0001.h5"
    assert pilatus.fileGet("WriteFile_RBV") == 0
    print("✓ PilatusStub working")

    # Test SGStream stub
    sgstream = SGStreamStub()
    sgstream.fly_ready(0.1, 100)
    assert sgstream.fileGet("FileNumber_RBV") == 1
    print("✓ SGStreamStub working")

def test_instruments_stub_moves_two_axes_at_once():
    """The 2-D step scan calls pts.mv_xy for every point. Without it the
    scan's own `except Exception` swallows the AttributeError and every
    point silently records the same position."""
    pts = InstrumentsStub()
    assert pts.can_coordinate('X', 'Z') is True
    assert pts.mv_xy('X', 1.0, 'Z', 2.0) is True
    assert pts.get_pos('X') == 1.0
    assert pts.get_pos('Z') == 2.0
    print("\u2713 InstrumentsStub mv_xy working")

def test_instruments_stub():
    """Test that InstrumentsStub works with motor names."""
    inst = InstrumentsStub()

    # Check hexapod has the new methods
    inst.hexapod.set_traj_SNAKE2(0.01, 0.0, 1.0, 0.1, 0.0, 1.0, 0.1)
    minstep, commonstep = inst.hexapod.analyze_pulse_steps()

    print("✓ InstrumentsStub working")

if __name__ == '__main__':
    try:
        test_hexapod_stubs()
        test_detector_stubs()
        test_instruments_stub_moves_two_axes_at_once()
        test_instruments_stub()
        print("\nAll tests passed!")
    except Exception as e:
        print(f"Test failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
