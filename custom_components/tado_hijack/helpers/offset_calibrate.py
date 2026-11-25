"""Auto-calibrate Tado device offset against a linked room thermostat."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..const import (
    CAPABILITY_INSIDE_TEMP,
    ZONE_TYPE_AIR_CONDITIONING,
    ZONE_TYPE_HEATING,
)
from .logging_utils import get_redacted_logger
from .zone_utils import cloud_reading_is_live

_LOGGER = get_redacted_logger(__name__)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant, State

    from ..coordinator import TadoDataUpdateCoordinator

OFFSET_MIN = -10.0
OFFSET_MAX = 10.0
OFFSET_STEP = 0.1
OFFSET_CAL_OFF = "off"
OFFSET_CAL_ON_RESET = "on_reset"
OFFSET_CAL_THRESHOLD = "threshold"
OFFSET_CAL_INHERIT = "inherit"
# Tado X experiment: one roomsAndDevices GET a minute for the home, then one
# write per device from the cloud's raw reading. No local reading, no hold.
OFFSET_CAL_MINUTE = "1m"
OFFSET_CAL_MINUTE_REASON = "minute"
OFFSET_CAL_HOUR_STEPS: tuple[int, ...] = (3, 6, 9, 12, 15, 18, 21, 24)
OFFSET_CAL_INTERVALS: tuple[str, ...] = tuple(
    f"{hours}h" for hours in OFFSET_CAL_HOUR_STEPS
)
OFFSET_CAL_OPTIONS: tuple[str, ...] = (
    OFFSET_CAL_OFF,
    *OFFSET_CAL_INTERVALS,
    OFFSET_CAL_ON_RESET,
    OFFSET_CAL_THRESHOLD,
    OFFSET_CAL_MINUTE,
)


# threshold writes only when a linked sensor moves far enough. One PUT is the
# daily budget guess for that device. The throttle floor covers a noisy day.
_THRESHOLD_DAILY_PUTS = 1


def _puts_for_cal_option(option: str) -> int:
    """PUTs one device is reserved for this resolved calibration mode."""
    if option in {OFFSET_CAL_OFF, OFFSET_CAL_INHERIT}:
        return 0
    if option == OFFSET_CAL_ON_RESET:
        return 1
    if option == OFFSET_CAL_THRESHOLD:
        return _THRESHOLD_DAILY_PUTS
    hours = hours_from_midnight(option)
    return len(hours) if hours else 0


def daily_offset_cal_puts(coordinator: TadoDataUpdateCoordinator) -> int:
    """How many offset PUTs auto-cal will spend per quota day (0 if off).

    Each linked measuring device uses its own zone mode. inherit is already
    resolved to the bridge option, so a threshold zone does not raise the
    reserve for an on_reset zone.
    """
    from ..const import (
        CONF_OFFSET_CAL_INTERVAL,
        CONF_ZONE_TEMP_ENTITIES,
        DEFAULT_OFFSET_CAL_INTERVAL,
    )

    if coordinator.config_entry is None:
        return 0

    linked = coordinator.config_entry.data.get(CONF_ZONE_TEMP_ENTITIES) or {}
    if not isinstance(linked, dict) or not linked:
        return 0

    resolver = getattr(coordinator, "effective_zone_offset_cal_interval", None)
    fallback = str(
        coordinator.config_entry.data.get(
            CONF_OFFSET_CAL_INTERVAL, DEFAULT_OFFSET_CAL_INTERVAL
        )
    )
    total = 0
    zone_options: dict[int, str] = {}
    for serial, zone_id in measuring_devices(coordinator):
        if not linked.get(str(zone_id)):
            continue
        option = str(resolver(zone_id) if resolver is not None else fallback)
        if serial not in device_climate_ids(coordinator, zone_id):
            zone_options[zone_id] = option
        total += _puts_for_cal_option(option)
    return total + _daily_device_reading_gets(coordinator, zone_options)


def _daily_device_reading_gets(
    coordinator: TadoDataUpdateCoordinator, zone_options: dict[int, str]
) -> int:
    """roomsAndDevices GETs per day for per-device readings (Tado X only).

    zone_options holds only rooms with a device that has no local climate.
    Rooms on the same clock slot share one GET. on_reset is one for the home.
    threshold is the same one-per-room guess as its write.
    """
    from ..const import GEN_X

    if getattr(coordinator, "generation", None) != GEN_X:
        return 0
    hours: set[int] = set()
    extra = 0
    options = set(zone_options.values())
    for option in options:
        hours.update(hours_from_midnight(option) or [])
    if OFFSET_CAL_ON_RESET in options:
        extra += 1
    extra += sum(
        _THRESHOLD_DAILY_PUTS
        for option in zone_options.values()
        if option == OFFSET_CAL_THRESHOLD
    )
    return len(hours) + extra


def hours_from_midnight(option: str) -> list[int] | None:
    """Local hours (from 00:00) for a 3h-grid interval, or None if not clock-based."""
    if option in {OFFSET_CAL_OFF, OFFSET_CAL_ON_RESET, OFFSET_CAL_THRESHOLD}:
        return None
    if not option.endswith("h"):
        return None
    try:
        step = int(option[:-1])
    except ValueError:
        return None
    return None if step not in OFFSET_CAL_HOUR_STEPS else list(range(0, 24, step))


def offset_reading_is_anchored(
    anchor: tuple[float, float] | None,
    *,
    current: float,
    tado_inside: float,
) -> bool:
    """True when this inside reading was already used for the current offset.

    The reading includes the offset, so another pass would add the same gap.
    """
    if anchor is None:
        return False
    anchored_inside, anchored_offset = anchor
    return (
        abs(current - anchored_offset) < OFFSET_STEP
        and abs(tado_inside - anchored_inside) < OFFSET_STEP
    )


def compute_device_offset(
    thermostat: float, tado_inside: float, current_offset: float
) -> float:
    """Offset so Tado's displayed temp matches the linked thermostat.

    Tado inside already includes the current offset, so the raw TRV is
    inside - current, and the new offset is thermostat - raw.
    """
    raw = tado_inside - current_offset
    desired = thermostat - raw
    clamped = max(OFFSET_MIN, min(OFFSET_MAX, desired))
    stepped = round(clamped / OFFSET_STEP) * OFFSET_STEP
    return round(stepped, 1)


def inside_from_zone_state(state: Any) -> float | None:
    """Tado's own zone measurement (not a linked HA thermostat)."""
    if not state:
        return None
    sdp = getattr(state, "sensor_data_points", None)
    if not sdp:
        return None
    inside = getattr(sdp, "inside_temperature", None)
    if inside is None or not cloud_reading_is_live(inside):
        return None
    val = getattr(inside, "celsius", None)
    if val is None:
        val = getattr(inside, "value", None)
    return float(val) if val is not None else None


def read_entity_temperature(hass: HomeAssistant, entity_id: str) -> float | None:
    """Read current_temperature or the state from a climate/sensor entity."""
    state: State | None = hass.states.get(entity_id)
    if state is None or state.state in ("unavailable", "unknown"):
        return None
    raw = state.attributes.get("current_temperature")
    if raw is None:
        raw = state.state
    try:
        return float(raw)
    except TypeError, ValueError:
        return None


def _offset_number_state(
    coordinator: TadoDataUpdateCoordinator, serial: str
) -> float | None:
    """State of this device's offset number."""
    from homeassistant.helpers import entity_registry as er

    from ..const import DOMAIN

    reg = er.async_get(coordinator.hass)
    unique_ids = []
    if coordinator.config_entry is not None:
        entry_id = coordinator.config_entry.entry_id
        unique_ids.append(f"{entry_id}_temperature_offset_{serial}")

    for unique_id in unique_ids:
        entity_id = reg.async_get_entity_id("number", DOMAIN, unique_id)
        if entity_id is None:
            continue
        state = coordinator.hass.states.get(entity_id)
        if state is None or state.state in {"unknown", "unavailable"}:
            continue
        try:
            return float(state.state)
        except TypeError, ValueError:
            continue
    return None


def current_device_offset(
    coordinator: TadoDataUpdateCoordinator, serial: str
) -> float | None:
    """Optimistic offset, cache, device snapshot, or the offset number."""
    optimistic = coordinator.optimistic.get_offset(serial)
    if optimistic is not None:
        return float(optimistic)
    cached = coordinator.data_manager.offsets_cache.get(serial)
    if cached is not None:
        celsius = getattr(cached, "celsius", None)
        if celsius is not None:
            return float(celsius)
        if isinstance(cached, int | float) and not isinstance(cached, bool):
            return float(cached)
    device = coordinator.devices_meta.get(serial)
    if device is not None:
        value = getattr(device, "temperature_offset", None)
        if value is not None:
            return float(value)
    return _offset_number_state(coordinator, serial)


def measuring_devices(
    coordinator: TadoDataUpdateCoordinator,
) -> list[tuple[str, int]]:
    """Serial + zone for devices that expose a temperature offset."""
    from .discovery import yield_devices

    found: list[tuple[str, int]] = []
    for device, zone_id in yield_devices(
        coordinator,
        include_zone_types={ZONE_TYPE_HEATING, ZONE_TYPE_AIR_CONDITIONING},
        capability=CAPABILITY_INSIDE_TEMP,
    ):
        if serial := getattr(device, "serial_no", None):
            found.append((str(serial), int(zone_id)))
    return found


# Tado X only. One roomsAndDevices GET serves every room, so a run that
# follows another inside this window reuses the readings.
_DEVICE_READINGS_REUSE_S = {"manual": 30.0, "threshold": 300.0, "minute": 30.0}
_DEVICE_READINGS_REUSE_DEFAULT_S = 60.0


def compute_measured_offset(thermostat: float, measured: float) -> float:
    """Offset from a device's own raw reading (Tado X temperatureAsMeasured).

    That reading does not include the offset, so nothing is subtracted.
    """
    return compute_device_offset(thermostat, measured, 0.0)


def _zone_inside(coordinator: TadoDataUpdateCoordinator, zone_id: int) -> float | None:
    zone_states = getattr(coordinator.data, "zone_states", None) or {}
    return inside_from_zone_state(
        zone_states.get(str(zone_id)) or zone_states.get(zone_id)
    )


def _per_device_candidates(
    coordinator: TadoDataUpdateCoordinator,
    linked: dict[str, Any],
    requested_zone: int | None,
    cloud_only: bool = False,
) -> dict[int, str]:
    """Tado X zones that need a cloud reading, with their reference entity.

    A device with a local reading is read there. Only a zone that has a
    device without one is worth a GET.
    """
    from ..const import GEN_X

    if coordinator.generation != GEN_X:
        return {}
    zones: dict[int, str] = {}
    for serial, zid in measuring_devices(coordinator):
        if zid in zones:
            continue
        if not cloud_only and serial in _device_local_temps(coordinator, zid):
            continue
        if entity_id := _linked_entity_id(coordinator, requested_zone, zid, linked):
            zones[zid] = entity_id
    return zones


def device_climate_ids(
    coordinator: TadoDataUpdateCoordinator, zone_id: int
) -> dict[str, str]:
    """Serial to its local Matter climate, for Tado X devices in this zone."""
    from ..const import GEN_X
    from .zone_utils import trv_serials

    if getattr(coordinator, "generation", None) != GEN_X:
        return {}
    tracker = getattr(coordinator, "availability_tracker", None)
    if tracker is None:
        return {}
    return {
        serial: entity_id
        for serial in trv_serials(coordinator.zones_meta, zone_id)
        if (entity_id := tracker.get_entity_id(serial))
    }


def _device_local_temps(
    coordinator: TadoDataUpdateCoordinator, zone_id: int
) -> dict[str, float]:
    """Live reading per device from its local climate. Free, no cloud call."""
    temps: dict[str, float] = {}
    for serial, entity_id in device_climate_ids(coordinator, zone_id).items():
        value = read_entity_temperature(coordinator.hass, entity_id)
        if value is not None:
            temps[serial] = value
    return temps


def _threshold_fetch_due(
    coordinator: TadoDataUpdateCoordinator, zone_id: int, entity_id: str
) -> bool:
    """True when the room moved enough since the last fetch to justify a GET.

    The gap is the reference against each device's local climate, and against
    the room reading. Only the movement since the last fetch counts, so it does
    not matter whether the local reading carries the offset. Half the spread,
    so a device near its limit is seen.
    """
    reference = read_entity_temperature(coordinator.hass, entity_id)
    if reference is None:
        return False
    baseline = coordinator._offset_cal_fetch_baseline.get(zone_id)
    if baseline is None:
        return True
    reference_then, inside_then, locals_then = baseline
    reference_moved = reference - reference_then
    moves: list[float] = []
    inside = _zone_inside(coordinator, zone_id)
    if inside is not None and inside_then is not None:
        moves.append(reference_moved - (inside - inside_then))
    for serial, value in _device_local_temps(coordinator, zone_id).items():
        if (then := locals_then.get(serial)) is not None:
            moves.append(reference_moved - (value - then))
    limit = max(coordinator.get_zone_offset_cal_threshold(zone_id) / 2, OFFSET_STEP)
    # Nothing to compare against: the reference alone decides.
    return any(abs(move) >= limit for move in moves or [reference_moved])


async def _fetch_device_readings(
    coordinator: TadoDataUpdateCoordinator, reason: str
) -> dict[str, float]:
    """Raw reading per serial from roomsAndDevices. One GET, or the last one."""
    import time

    now = time.monotonic()
    fetched_at = coordinator._offset_cal_device_readings_at
    reuse_s = _DEVICE_READINGS_REUSE_S.get(reason, _DEVICE_READINGS_REUSE_DEFAULT_S)
    if fetched_at is not None and now - fetched_at < reuse_s:
        return coordinator._offset_cal_device_readings
    bridge = getattr(coordinator.provider, "bridge", None)
    if bridge is None:
        return {}
    try:
        snapshot = await bridge.async_get_rooms_and_devices()
    except Exception:
        _LOGGER.debug("Offset cal: roomsAndDevices fetch failed, using room readings")
        return {}
    readings: dict[str, float] = {}
    known = coordinator.data_manager.devices_meta
    for room in snapshot.rooms:
        for device in room.devices:
            serial = str(device.serial_no)
            if device.temperature_as_measured is not None:
                readings[serial] = float(device.temperature_as_measured)
            # A pending optimistic offset is newer than this snapshot.
            cached = known.get(serial)
            if (
                cached is not None
                and device.temperature_offset is not None
                and coordinator.optimistic.get_offset(serial) is None
            ):
                cached.temperature_offset = device.temperature_offset
    coordinator._offset_cal_device_readings = readings
    coordinator._offset_cal_device_readings_at = now
    return readings


async def _device_readings_for_run(
    coordinator: TadoDataUpdateCoordinator,
    reason: str,
    linked: dict[str, Any],
    requested_zone: int | None,
) -> dict[str, float] | None:
    """Cloud readings for devices with no local climate. None means no GET.

    An empty dict means a threshold run with no room that moved: no GET, and
    those devices are left alone.
    """
    zones = _per_device_candidates(
        coordinator, linked, requested_zone, reason == OFFSET_CAL_MINUTE_REASON
    )
    if not zones:
        return None
    if reason == "threshold":
        zones = {
            zid: entity_id
            for zid, entity_id in zones.items()
            if _threshold_fetch_due(coordinator, zid, entity_id)
        }
        if not zones:
            return {}
    readings = await _fetch_device_readings(coordinator, reason)
    if not readings:
        return None
    for zid, entity_id in zones.items():
        reference = read_entity_temperature(coordinator.hass, entity_id)
        if reference is not None:
            coordinator._offset_cal_fetch_baseline[zid] = (
                reference,
                _zone_inside(coordinator, zid),
                _device_local_temps(coordinator, zid),
            )
    return readings


async def queue_offset_cal_writes(
    coordinator: TadoDataUpdateCoordinator,
    reason: str,
    linked: dict[str, Any],
    zone_id: int | None = None,
) -> int:
    """Queue one offset write per measuring device that needs a change.

    A Tado X device is compared with its own sensor: its local Matter climate,
    or the cloud's raw reading when it has no such climate. Classic, and a
    Tado X device with neither, use the room reading.
    """
    wrote = 0
    zone_states = getattr(coordinator.data, "zone_states", None) or {}
    readings = await _device_readings_for_run(coordinator, reason, linked, zone_id)
    local_temps: dict[int, dict[str, float]] = {}
    cloud_only = reason == OFFSET_CAL_MINUTE_REASON
    for serial, zid in measuring_devices(coordinator):
        if cloud_only:
            # Cloud reading or nothing. No fallback that could disagree with it.
            measured = readings.get(serial) if readings else None
            if measured is not None and await _queue_measured_offset(
                coordinator, reason, linked, zone_id, serial, zid, measured
            ):
                wrote += 1
            continue
        if zid not in local_temps:
            local_temps[zid] = _device_local_temps(coordinator, zid)
        local = local_temps[zid].get(serial)
        measured = readings.get(serial) if readings else None
        if local is not None:
            queued = await _queue_local_offset(
                coordinator, reason, linked, zone_id, serial, zid, local
            )
        elif measured is not None:
            queued = await _queue_measured_offset(
                coordinator, reason, linked, zone_id, serial, zid, measured
            )
        elif readings is not None and not readings:
            # Threshold run, and the room has not moved since the last fetch.
            queued = False
        else:
            queued = await _queue_one_offset(
                coordinator, reason, linked, zone_id, serial, zid, zone_states
            )
        if queued:
            wrote += 1
    return wrote


# A written offset reaches the device a little later, and shows there as one
# jump of that size. Until the jump is seen, the old reading would add the
# change again. A small change cannot be told from drift, and a second pass
# costs at most that small change, so it waits only briefly.
_LOCAL_SETTLE_MAX_S = 900.0
_LOCAL_SETTLE_SMALL_S = 120.0
_LOCAL_SETTLE_SMALL_CHANGE = 0.5


def local_write_is_pending(
    hold: tuple[float, float, float, float] | None,
    *,
    local: float,
    current: float,
    now: float,
) -> bool:
    """True while the last write is not yet visible in the local reading.

    hold is (reading at the last look, old offset, new offset, time written).
    The jump is measured from the last look, so drift the other way before
    the device applies the offset does not hide it. A different offset now
    means someone else set it, so the hold is over.
    """
    if hold is None:
        return False
    local_then, old_offset, new_offset, written_at = hold
    if abs(current - new_offset) >= OFFSET_STEP:
        return False
    change = new_offset - old_offset
    small = abs(change) < _LOCAL_SETTLE_SMALL_CHANGE
    limit = _LOCAL_SETTLE_SMALL_S if small else _LOCAL_SETTLE_MAX_S
    if now - written_at >= limit:
        return False
    jumped = (local - local_then) * (1 if change > 0 else -1)
    return jumped < abs(change) / 2 - 1e-6


async def _queue_local_offset(
    coordinator: TadoDataUpdateCoordinator,
    reason: str,
    linked: dict[str, Any],
    requested_zone: int | None,
    serial: str,
    zone_id: int,
    local: float,
) -> bool:
    """Queue one write from this device's local climate reading (Tado X).

    That reading includes the offset, the same as the room reading does.
    """
    import time

    entity_id = _linked_entity_id(coordinator, requested_zone, zone_id, linked)
    if entity_id is None:
        return False
    thermostat = read_entity_temperature(coordinator.hass, entity_id)
    current = await _current_offset(coordinator, serial, zone_id)
    if thermostat is None or current is None:
        _log_missing_offset(reason, zone_id, serial, thermostat, local, current)
        return False
    now = time.monotonic()
    holds = coordinator._offset_cal_local_hold
    hold = holds.get(serial)
    if local_write_is_pending(hold, local=local, current=current, now=now):
        if hold is not None:
            holds[serial] = (local, hold[1], hold[2], hold[3])
        if reason == "manual":
            _LOGGER.info(
                "Offset cal (manual) %s: the last write is not on the device yet",
                serial,
            )
        return False
    holds.pop(serial, None)
    desired = compute_device_offset(thermostat, local, current)
    threshold = coordinator.get_zone_offset_cal_threshold(zone_id)
    if abs(desired - current) < max(threshold, OFFSET_STEP):
        return False
    _LOGGER.info(
        "Offset cal (%s) %s: thermostat=%.2f device=%.2f offset %.1f -> %.1f",
        reason,
        serial,
        thermostat,
        local,
        current,
        desired,
    )
    await coordinator.async_set_temperature_offset(serial, desired)
    holds[serial] = (local, current, desired, now)
    coordinator._offset_cal_inside_anchor.pop(serial, None)
    return True


async def _queue_measured_offset(
    coordinator: TadoDataUpdateCoordinator,
    reason: str,
    linked: dict[str, Any],
    requested_zone: int | None,
    serial: str,
    zone_id: int,
    measured: float,
) -> bool:
    """Queue one write from this device's own raw reading (Tado X)."""
    entity_id = _linked_entity_id(coordinator, requested_zone, zone_id, linked)
    if entity_id is None:
        return False
    thermostat = read_entity_temperature(coordinator.hass, entity_id)
    current = await _current_offset(coordinator, serial, zone_id)
    if thermostat is None or current is None:
        _log_missing_offset(reason, zone_id, serial, thermostat, measured, current)
        return False
    desired = compute_measured_offset(thermostat, measured)
    threshold = coordinator.get_zone_offset_cal_threshold(zone_id)
    if abs(desired - current) < max(threshold, OFFSET_STEP):
        return False
    _LOGGER.info(
        "Offset cal (%s) %s: thermostat=%.2f device raw=%.2f offset %.1f -> %.1f",
        reason,
        serial,
        thermostat,
        measured,
        current,
        desired,
    )
    await coordinator.async_set_temperature_offset(serial, desired)
    # The raw reading carries no offset, so there is nothing to anchor.
    coordinator._offset_cal_inside_anchor.pop(serial, None)
    return True


def _linked_entity_id(
    coordinator: TadoDataUpdateCoordinator,
    requested_zone: int | None,
    zone_id: int,
    linked: dict[str, Any],
) -> str | None:
    """Linked thermostat, or None when this device is outside the request."""
    if requested_zone is not None and zone_id != requested_zone:
        return None
    dummy = coordinator.dummy_handler
    if dummy and dummy.is_dummy_zone(zone_id):
        return None
    entity_id = linked.get(str(zone_id))
    return str(entity_id) if entity_id else None


async def _current_offset(
    coordinator: TadoDataUpdateCoordinator, serial: str, zone_id: int
) -> float | None:
    """Device offset, fetched once when the cache has none."""
    current = current_device_offset(coordinator, serial)
    if current is not None:
        return current
    try:
        await coordinator.data_manager._fetch_offset_for(serial)
    except Exception:
        _LOGGER.debug(
            "Offset cal zone %s device %s: offset fetch failed",
            zone_id,
            serial,
        )
    return current_device_offset(coordinator, serial)


def _log_missing_offset(
    reason: str,
    zone_id: int,
    serial: str,
    thermostat: float | None,
    tado_inside: float | None,
    current: float | None,
) -> None:
    """Manual skips warn. Scheduled skips stay at debug."""
    values = (zone_id, serial, thermostat, tado_inside, current)
    if reason == "manual":
        _LOGGER.warning(
            "Offset cal skipped: zone %s device %s (thermostat=%s tado=%s offset=%s)",
            *values,
        )
        return
    _LOGGER.debug(
        "Skip offset cal zone %s device %s (thermostat=%s tado=%s offset=%s)",
        *values,
    )


async def _queue_one_offset(
    coordinator: TadoDataUpdateCoordinator,
    reason: str,
    linked: dict[str, Any],
    requested_zone: int | None,
    serial: str,
    zone_id: int,
    zone_states: Any,
) -> bool:
    """Queue one device write. False when the device needs no change."""
    entity_id = _linked_entity_id(coordinator, requested_zone, zone_id, linked)
    if entity_id is None:
        return False
    thermostat = read_entity_temperature(coordinator.hass, entity_id)
    tado_inside = inside_from_zone_state(
        zone_states.get(str(zone_id)) or zone_states.get(zone_id)
    )
    current = await _current_offset(coordinator, serial, zone_id)
    if thermostat is None or tado_inside is None or current is None:
        _log_missing_offset(reason, zone_id, serial, thermostat, tado_inside, current)
        return False
    if reason != "manual" and offset_reading_is_anchored(
        coordinator._offset_cal_inside_anchor.get(serial),
        current=current,
        tado_inside=tado_inside,
    ):
        return False
    desired = compute_device_offset(thermostat, tado_inside, current)
    threshold = coordinator.get_zone_offset_cal_threshold(zone_id)
    if abs(desired - current) < max(threshold, OFFSET_STEP):
        return False
    _LOGGER.info(
        "Offset cal (%s) %s: thermostat=%.2f tado=%.2f offset %.1f -> %.1f",
        reason,
        serial,
        thermostat,
        tado_inside,
        current,
        desired,
    )
    await coordinator.async_set_temperature_offset(serial, desired)
    coordinator._offset_cal_inside_anchor[serial] = (tado_inside, desired)
    return True
