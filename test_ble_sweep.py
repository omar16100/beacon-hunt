"""Tests for watch-mode rendering.

Watch mode used to overwrite a single terminal line with `\r`, which is fine to
glance at while walking but leaves no record. Hunting a beacon that emits ~6
packets a minute needs a scrollback you can compare against, so the renderer
emits one standalone timestamped line per packet instead.
"""

from datetime import datetime

from ble_sweep import format_watch_line

AT = datetime(2026, 9, 6, 21, 18, 3)


def test_line_starts_with_a_wall_clock_timestamp():
    assert format_watch_line(AT, -76, -76.0).startswith("21:18:03")


def test_line_reports_both_the_raw_packet_and_the_smoothed_average():
    # The 5-sample window lags by ~45s at this packet rate, so the hunter needs
    # the packet that just landed as well as the trend it is being folded into.
    line = format_watch_line(AT, -68, -75.4)
    assert "-68" in line
    assert "-75.4" in line


def test_a_stronger_signal_draws_a_longer_bar():
    weak = format_watch_line(AT, -90, -90.0).count("#")
    strong = format_watch_line(AT, -60, -60.0).count("#")
    assert strong > weak


def test_bar_is_clamped_at_both_ends():
    # -120 would drive the length negative, -20 would overflow the 40-col field.
    assert format_watch_line(AT, -120, -120.0).count("#") == 0
    assert format_watch_line(AT, -20, -20.0).count("#") == 40


def test_each_call_is_a_standalone_line():
    # The old renderer depended on \r to overwrite itself. A log line must not
    # carry its own control characters, or the scrollback eats itself.
    line = format_watch_line(AT, -76, -76.0)
    assert "\r" not in line
    assert "\n" not in line
