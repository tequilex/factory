"""Всё время в базе — UTC. Часовой пояс проекта применяется только на границе."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from factory.core.clock import from_iso, now_utc, to_iso


def test_now_is_timezone_aware_utc():
    now = now_utc()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


def test_iso_format_ends_with_z_and_has_no_microseconds():
    stamp = to_iso(datetime(2026, 8, 23, 10, 15, 30, 123456, tzinfo=timezone.utc))
    assert stamp == "2026-08-23T10:15:30Z"


def test_to_iso_converts_other_zones_to_utc():
    moscow = datetime(2026, 8, 23, 23, 30, tzinfo=ZoneInfo("Europe/Moscow"))
    assert to_iso(moscow) == "2026-08-23T20:30:00Z"


def test_to_iso_rejects_naive_datetime():
    with pytest.raises(ValueError):
        to_iso(datetime(2026, 8, 23, 10, 15))


def test_roundtrip_survives_the_string_form():
    original = now_utc().replace(microsecond=0)
    assert from_iso(to_iso(original)) == original


def test_from_iso_accepts_both_z_and_offset_suffixes():
    assert from_iso("2026-08-23T10:15:30Z") == from_iso("2026-08-23T13:15:30+03:00")


def test_from_iso_treats_naive_string_as_utc():
    parsed = from_iso("2026-08-23T10:15:30")
    assert parsed == datetime(2026, 8, 23, 10, 15, 30, tzinfo=timezone.utc)


def test_iso_strings_sort_chronologically():
    """Порядок строк должен совпадать с порядком времени — на этом держатся запросы."""
    earlier = to_iso(datetime(2026, 8, 23, 9, 0, tzinfo=timezone.utc))
    later = to_iso(datetime(2026, 8, 23, 10, 0, tzinfo=timezone.utc))
    assert earlier < later
