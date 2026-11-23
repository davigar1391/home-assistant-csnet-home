"""The unit's own heat counter (unitCapacity): the heat and COP sensors counted from it."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from homeassistant.util import dt as dt_util

from custom_components.csnet_home.dashboard import UNIT_ROLES
from custom_components.csnet_home.sensor import _unit_heat_sensors
from custom_components.csnet_home.server_energy import (
    CSNetHomeServerEnergySensor,
    EstimatedEnergyCounter,
    UnitHeatCounter,
    unit_heat_descriptions,
    unit_heat_storage_key,
)


def local(day, hour, minute=0):
    """Return a local time on a fixed November 2026 day."""
    return dt_util.now().replace(year=2026, month=11, day=day, hour=hour, minute=minute, second=0, microsecond=0)


def coordinator_with(unit_heat, counter=None):
    """Return a coordinator stand-in holding the counters."""
    return SimpleNamespace(
        unit_heat=unit_heat,
        estimated_energy=counter,
        get_server_energy=lambda: None,
        get_sensors_data=lambda: [],
        get_installation_devices_data=lambda: {},
        get_common_data=lambda: {"name": "Home", "device_status": {}},
        async_add_listener=lambda *a, **k: lambda: None,
        last_update_success=True,
    )


@pytest.mark.parametrize(
    ("previous", "reading", "rise"),
    [
        (100, 100, 0),
        (100, 104, 4),
        # seen on 21 November 2026: 249, then 0
        (249, 0, 1),
        (245, 3, 8),
        # a counter that goes up to 255 before it starts over
        (254, 1, 3),
        # far from the top: a reset, not the counter going round
        (120, 5, None),
        # one step back near the top: no 249 kWh in a minute
        (245, 244, None),
        # a stray reading between two of 230, seen in November 2026: no 196 kWh in a minute either
        (230, 34, None),
        (34, 230, None),
    ],
)
def test_rise_of_the_counter(previous, reading, rise):
    """The rise between two readings a minute apart, across the counter starting over."""
    assert UnitHeatCounter.rise(previous, reading, 1 / 60) == rise


def test_the_counter_rose_a_lot_during_an_outage():
    """34, then 230 eight hours later: 196 kWh, as the unit can make that much in that time."""
    assert UnitHeatCounter.rise(34, 230, 8) == 196
    assert UnitHeatCounter.rise(34, 230, 0.5) is None


def test_the_counter_went_round_during_an_outage():
    """150, then 20 eight hours later: 120 kWh, as the unit can make that much in that time."""
    assert UnitHeatCounter.rise(150, 20, 8) == 120
    assert UnitHeatCounter.rise(150, 20, 0.5) is None


async def test_heat_is_the_rise_of_the_counter_by_use(hass, hass_storage):
    """The first reading is the starting point; each rise goes to the use of the moment."""
    counter = UnitHeatCounter(hass, "entry")
    await counter.async_load()
    counter.async_update_reading(local(23, 8, 0), 129, "spaceHeating")
    assert counter.has_counted is False
    counter.async_update_reading(local(23, 9, 0), 135, "spaceHeating")
    counter.async_update_reading(local(23, 10, 0), 137, "dhw")
    now = local(23, 10, 0)
    assert counter.energy("today", None, now) == 8
    assert counter.energy("today", "spaceHeating", now) == 6
    assert counter.energy("today", "dhw", now) == 2
    assert counter.energy("month", None, now) == 8
    assert counter.has_counted is True
    assert counter.source == "unit_counter"


async def test_a_day_across_the_counter_starting_over(hass, hass_storage):
    """21 November 2026: 165 at midnight, 249 then 0 at 21:00, 8 at the next midnight; yesterday is complete from the day after."""
    counter = UnitHeatCounter(hass, "entry")
    counter.async_update_reading(local(20, 23, 55), 164, "spaceHeating")
    counter.async_update_reading(local(21, 0, 5), 165, "spaceHeating")
    counter.async_update_reading(local(21, 20, 50), 249, "spaceHeating")
    counter.async_update_reading(local(21, 21, 0), 0, "spaceHeating")
    counter.async_update_reading(local(21, 23, 55), 8, "spaceHeating")
    counter.async_update_reading(local(22, 0, 5), 8, "spaceHeating")
    # 165 - 164, then 249 - 165, then 1 across the wrap, then 8
    assert counter.energy("yesterday", None, local(22, 0, 5)) == 1 + 84 + 1 + 8


async def test_a_reset_or_a_long_gap_starts_counting_again(hass, hass_storage):
    """A drop far from the top, or a reading a day later, counts nothing and starts again from there."""
    counter = UnitHeatCounter(hass, "entry")
    counter.async_update_reading(local(23, 8, 0), 120, "spaceHeating")
    counter.async_update_reading(local(23, 8, 5), 5, "spaceHeating")
    counter.async_update_reading(local(23, 8, 10), 7, "spaceHeating")
    assert counter.energy("today", None, local(23, 8, 10)) == 2
    later = local(23, 8, 10) + timedelta(hours=30)
    counter.async_update_reading(later, 200, "spaceHeating")
    assert counter.energy("today", None, later) == 0


@pytest.mark.parametrize("stray", [34, 100, 120])
async def test_a_stray_reading_is_dropped(hass, hass_storage, stray):
    """230, a stray value for one poll, then 230 again and on: only the real rise is counted."""
    counter = UnitHeatCounter(hass, "entry")
    counter.async_update_reading(local(23, 8, 0), 228, "spaceHeating")
    counter.async_update_reading(local(23, 8, 1), 230, "spaceHeating")
    counter.async_update_reading(local(23, 8, 2), stray, "spaceHeating")
    counter.async_update_reading(local(23, 8, 3), 231, "spaceHeating")
    counter.async_update_reading(local(23, 8, 4), 232, "spaceHeating")
    assert counter.energy("today", None, local(23, 8, 4)) == 4


async def test_a_reset_is_followed_once_two_readings_agree(hass, hass_storage):
    """120, then 5 and 7: the counter was reset; the rise from 5 is counted."""
    counter = UnitHeatCounter(hass, "entry")
    counter.async_update_reading(local(23, 8, 0), 120, "spaceHeating")
    counter.async_update_reading(local(23, 8, 1), 5, "spaceHeating")
    counter.async_update_reading(local(23, 8, 2), 7, "spaceHeating")
    counter.async_update_reading(local(23, 8, 3), 8, "spaceHeating")
    assert counter.energy("today", None, local(23, 8, 3)) == 3


async def test_readings_that_are_not_a_counter_are_ignored(hass, hass_storage):
    """A missing or odd value leaves the count as it was."""
    counter = UnitHeatCounter(hass, "entry")
    counter.async_update_reading(local(23, 8, 0), 10, "spaceHeating")
    for odd in (None, "12", 12.5, True, -1):
        counter.async_update_reading(local(23, 8, 5), odd, "spaceHeating")
    counter.async_update_reading(local(23, 8, 10), 12, "spaceHeating")
    assert counter.energy("today", None, local(23, 8, 10)) == 2


async def test_the_last_reading_survives_a_restart(hass, hass_storage):
    """After a restart, the rise during the outage is counted."""
    counter = UnitHeatCounter(hass, "entry")
    counter.async_update_reading(local(23, 8, 0), 100, "spaceHeating")
    counter.async_update_reading(local(23, 8, 5), 101, "spaceHeating")
    hass_storage[unit_heat_storage_key("entry")] = {"version": 1, "key": unit_heat_storage_key("entry"), "data": counter._data_to_save()}

    again = UnitHeatCounter(hass, "entry")
    await again.async_load()
    assert again.has_counted is True
    again.async_update_reading(local(23, 9, 0), 106, "spaceHeating")
    assert again.energy("today", None, local(23, 9, 0)) == 6


async def test_heat_sensors_read_the_counter_and_say_so(hass, hass_storage):
    """The heat sensors of the counter stand beside CSNet's, with their own keys."""
    counter = UnitHeatCounter(hass, "entry")
    now = dt_util.now()
    counter.async_update_reading(now - timedelta(minutes=10), 40, "spaceHeating")
    counter.async_update_reading(now, 43, "spaceHeating")
    descriptions = unit_heat_descriptions()
    assert [d.key for d in descriptions] == ["unit_heat_today", "unit_heat_yesterday", "unit_heat_month"]
    sensor = CSNetHomeServerEnergySensor(coordinator_with(counter), descriptions[0], None, counter)
    assert sensor.native_value == 3
    assert sensor.extra_state_attributes == {"heat_source": "unit_counter"}
    assert sensor.unique_id == "csnet_home-unit_heat_today"


async def test_no_sensors_until_the_counter_rose(hass, hass_storage):
    """A unit whose counter stands still, or no counter, gets no sensors."""
    entry = SimpleNamespace(entry_id="entry", data={})
    assert _unit_heat_sensors(hass, entry, coordinator_with(None)) == []
    counter = UnitHeatCounter(hass, "entry")
    counter.async_update_reading(local(23, 8, 0), 50, "spaceHeating")
    counter.async_update_reading(local(23, 8, 5), 50, "spaceHeating")
    assert _unit_heat_sensors(hass, entry, coordinator_with(counter)) == []


@pytest.mark.parametrize(("meter", "electricity"), [("sensor.heat_pump_meter", "metered"), (None, "estimated")])
async def test_heat_and_cop_sensors_once_the_counter_rose(hass, hass_storage, meter, electricity):
    """Heat today, yesterday, this month, and the four COPs named by their electricity."""
    entry = SimpleNamespace(entry_id="entry", data={"energy_meter": meter} if meter else {})
    counter = UnitHeatCounter(hass, "entry")
    counter.async_update_reading(local(23, 8, 0), 50, "spaceHeating")
    counter.async_update_reading(local(23, 8, 5), 51, "spaceHeating")
    sensors = _unit_heat_sensors(hass, entry, coordinator_with(counter, EstimatedEnergyCounter(hass, "entry")))
    assert [s.entity_description.key for s in sensors] == [
        "unit_heat_today",
        "unit_heat_yesterday",
        "unit_heat_month",
        "unit_cop_today",
        "unit_cop_yesterday",
        "unit_cop_month",
        "unit_cop_last_month",
    ]
    assert sensors[3].entity_description.name == f"COP today (unit counter, {electricity})"
    assert sensors[3].extra_state_attributes["heat_source"] == "unit_counter"
    # the cards find them by role
    assert {UNIT_ROLES[f"unit_{key}"] for key in ("heat_today", "cop_today", "cop_last_month")} == {
        f"csnet_home-unit_{key}" for key in ("heat_today", "cop_today", "cop_last_month")
    }
