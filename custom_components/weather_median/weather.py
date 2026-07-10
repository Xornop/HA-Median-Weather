"""Weather Median entity."""
from __future__ import annotations

import logging
import math
import statistics
from datetime import timedelta
from typing import Any

from homeassistant.components.weather import (
    ATTR_FORECAST_CONDITION,
    ATTR_FORECAST_HUMIDITY,
    ATTR_FORECAST_PRECIPITATION,
    ATTR_FORECAST_TEMP,
    ATTR_FORECAST_TEMP_LOW,
    ATTR_FORECAST_TIME,
    ATTR_FORECAST_WIND_BEARING,
    ATTR_FORECAST_WIND_SPEED,
    Forecast,
    WeatherEntity,
    WeatherEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    UnitOfLength,
    UnitOfPrecipitationDepth,
    UnitOfPressure,
    UnitOfSpeed,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util.unit_conversion import (
    BaseUnitConverter,
    DistanceConverter,
    PressureConverter,
    SpeedConverter,
    TemperatureConverter,
)

from .const import (
    CONF_NAME,
    CONF_SOURCES,
    CONF_UPDATE_INTERVAL,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

# Canonical internal units — every value is normalized to these before any
# median is calculated, regardless of which unit each individual source
# happens to report in. Home Assistant handles user-facing conversion
# (e.g. to °F or mph) downstream, based on the native_*_unit properties.
CANONICAL_TEMPERATURE_UNIT = UnitOfTemperature.CELSIUS
CANONICAL_PRESSURE_UNIT = UnitOfPressure.HPA
CANONICAL_SPEED_UNIT = UnitOfSpeed.KILOMETERS_PER_HOUR
CANONICAL_DISTANCE_UNIT = UnitOfLength.KILOMETERS
CANONICAL_PRECIPITATION_UNIT = UnitOfPrecipitationDepth.MILLIMETERS

# Weather entity attribute keys that carry a per-entity unit alongside them.
ATTR_TEMPERATURE_UNIT = "temperature_unit"
ATTR_PRESSURE_UNIT = "pressure_unit"
ATTR_WIND_SPEED_UNIT = "wind_speed_unit"
ATTR_VISIBILITY_UNIT = "visibility_unit"
ATTR_PRECIPITATION_UNIT = "precipitation_unit"

# One entry per "kind" of measurement: which converter normalizes it, and
# which canonical unit it's normalized to. Adding a new convertible
# measurement only requires adding a row here — every call site (current
# conditions and forecast) shares the same normalization logic.
_CONVERTERS: dict[str, tuple[type[BaseUnitConverter], str]] = {
    "temperature": (TemperatureConverter, CANONICAL_TEMPERATURE_UNIT),
    "pressure": (PressureConverter, CANONICAL_PRESSURE_UNIT),
    "speed": (SpeedConverter, CANONICAL_SPEED_UNIT),
    "distance": (DistanceConverter, CANONICAL_DISTANCE_UNIT),
    "precipitation": (DistanceConverter, CANONICAL_PRECIPITATION_UNIT),
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Weather Median entity."""
    config = dict(entry.data)
    if entry.options:
        config[CONF_SOURCES] = entry.options.get(CONF_SOURCES, config[CONF_SOURCES])
        config[CONF_UPDATE_INTERVAL] = entry.options.get(
            CONF_UPDATE_INTERVAL,
            config.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL),
        )

    entity = WeatherMedianEntity(hass, config, entry.entry_id)
    async_add_entities([entity], update_before_add=True)


def _median(values: list[float]) -> float | None:
    """Return the median of a list, or None if empty."""
    if not values:
        return None
    return statistics.median(values)


def _circular_median(degrees: list[float]) -> float | None:
    """Approximate circular median for wind bearing via mean vector."""
    if not degrees:
        return None
    if len(degrees) == 1:
        return degrees[0]
    rads = [math.radians(d) for d in degrees]
    sin_sum = sum(math.sin(r) for r in rads)
    cos_sum = sum(math.cos(r) for r in rads)
    mean_angle = math.degrees(math.atan2(sin_sum, cos_sum)) % 360
    return round(mean_angle, 1)


def _majority_vote(values: list[str]) -> str | None:
    """Return the most common string value."""
    if not values:
        return None
    return max(set(values), key=values.count)


def _to_float(value: Any) -> float | None:
    """Best-effort conversion of a raw value to float."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class WeatherMedianEntity(WeatherEntity):
    """A weather entity that exposes the median of multiple weather sources.

    Every source can legitimately report in a different unit — one
    integration might be metric, another might be misconfigured or use an
    older API that reports imperial units. Every numeric value is therefore
    normalized to a canonical metric unit, *per source, per value*, using
    that specific source's own reported unit, before it ever enters a
    median calculation. This applies equally to current conditions and to
    every forecast slot of every source.
    """

    _attr_should_poll = False
    _attr_native_temperature_unit = CANONICAL_TEMPERATURE_UNIT
    _attr_native_pressure_unit = CANONICAL_PRESSURE_UNIT
    _attr_native_wind_speed_unit = CANONICAL_SPEED_UNIT
    _attr_native_visibility_unit = CANONICAL_DISTANCE_UNIT
    _attr_native_precipitation_unit = CANONICAL_PRECIPITATION_UNIT

    def __init__(
        self,
        hass: HomeAssistant,
        config: dict[str, Any],
        entry_id: str,
    ) -> None:
        self.hass = hass
        self._sources: list[str] = config[CONF_SOURCES]
        self._name: str = config[CONF_NAME]
        self._entry_id = entry_id
        self._update_interval = timedelta(
            minutes=int(config.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL))
        )

        self._attr_unique_id = f"{DOMAIN}_{entry_id}"
        self._attr_name = self._name

        # Cache keyed by source entity_id -> list of raw forecast dicts.
        # Sources that don't support a forecast type simply have no entry,
        # so a missing provider never shifts or misaligns the others.
        self._forecast_cache: dict[str, list[dict]] = {}
        self._forecast_cache_type: dict[str, str] = {}

        self._attr_supported_features = WeatherEntityFeature(0)
        self._remove_time_listener = None

    async def async_added_to_hass(self) -> None:
        """Start periodic updates when entity is added."""
        await self._async_update_forecasts()

        self._remove_time_listener = async_track_time_interval(
            self.hass,
            self._async_scheduled_update,
            self._update_interval,
        )

    async def async_will_remove_from_hass(self) -> None:
        """Clean up timer."""
        if self._remove_time_listener:
            self._remove_time_listener()

    async def _async_scheduled_update(self, _now=None) -> None:
        await self._async_update_forecasts()
        self.async_write_ha_state()

    # --- Unit normalization ---------------------------------------------------
    #
    # A single, reusable normalization path for every measurement "kind".
    # Handles three real-world cases:
    #   1. The source reports a unit and it already matches canonical -> no-op.
    #   2. The source reports a unit that differs -> convert via HA's own
    #      official converter.
    #   3. The source doesn't report a unit at all -> fall back to whatever
    #      Home Assistant's configured unit system uses for that
    #      measurement, which is what most integrations implicitly assume
    #      when they omit the attribute. If even that isn't available,
    #      assume canonical as a last resort.
    # A conversion that fails (e.g. an unrecognized/garbled unit string from
    # a misbehaving source) is logged and excluded rather than allowed to
    # raise and take down the whole aggregation.

    def _fallback_unit(self, kind: str) -> str:
        """Return the configured Home Assistant unit for a measurement kind."""
        units = self.hass.config.units
        converter, canonical = _CONVERTERS[kind]
        attr_candidates = {
            "temperature": ("temperature_unit",),
            "pressure": ("pressure_unit",),
            "speed": ("wind_speed_unit",),
            "distance": ("length_unit",),
            "precipitation": ("accumulated_precipitation_unit", "accumulated_precipitation", "length_unit"),
        }
        for attr in attr_candidates.get(kind, ()):
            value = getattr(units, attr, None)
            if value:
                return value
        return canonical

    def _normalize(
        self,
        value: float | None,
        reported_unit: str | None,
        kind: str,
        *,
        context: str = "",
    ) -> float | None:
        """Normalize a single value of a given measurement kind to its canonical unit."""
        if value is None:
            return None

        converter, canonical = _CONVERTERS[kind]
        unit = reported_unit or self._fallback_unit(kind)

        if unit == canonical:
            return value

        try:
            return converter.convert(value, unit, canonical)
        except Exception as err:  # noqa: BLE001 - defend against any bad/unknown unit string
            _LOGGER.debug(
                "Skipping unconvertible %s value (%s %s -> %s) from %s: %s",
                kind,
                value,
                unit,
                canonical,
                context,
                err,
            )
            return None

    # --- Forecast fetching ---------------------------------------------------

    async def _async_update_forecasts(self) -> None:
        """Fetch forecasts from all sources, auto-discovering daily/hourly support."""
        supports_daily: list[str] = []
        supports_hourly: list[str] = []

        for source in self._sources:
            state = self.hass.states.get(source)
            if state is None:
                _LOGGER.warning("Weather source %s not found, skipping.", source)
                continue
            supported = state.attributes.get("supported_features", 0)
            if supported & WeatherEntityFeature.FORECAST_DAILY:
                supports_daily.append(source)
            if supported & WeatherEntityFeature.FORECAST_HOURLY:
                supports_hourly.append(source)

        await self._async_fetch_forecast_type(supports_daily, "daily")
        await self._async_fetch_forecast_type(supports_hourly, "hourly")

        # Drop cached forecasts for sources that no longer support/return
        # them, so stale data can't linger and misalign future aggregation.
        still_valid = set(supports_daily) | set(supports_hourly)
        for source in list(self._forecast_cache):
            if source not in still_valid:
                self._forecast_cache.pop(source, None)
                self._forecast_cache_type.pop(source, None)

        features = WeatherEntityFeature(0)
        if supports_daily:
            features |= WeatherEntityFeature.FORECAST_DAILY
        if supports_hourly:
            features |= WeatherEntityFeature.FORECAST_HOURLY
        self._attr_supported_features = features

        _LOGGER.debug(
            "Forecast sources — daily: %s, hourly: %s", supports_daily, supports_hourly
        )

    async def _async_fetch_forecast_type(
        self, sources: list[str], forecast_type: str
    ) -> None:
        """Fetch and cache a single forecast type for the given sources."""
        if not sources:
            return

        try:
            response = await self.hass.services.async_call(
                "weather",
                "get_forecasts",
                {"type": forecast_type},
                blocking=True,
                return_response=True,
                target={"entity_id": sources},
            )
        except Exception as err:  # noqa: BLE001 - external service call
            _LOGGER.error("Error fetching %s forecasts: %s", forecast_type, err)
            return

        if not response:
            return

        for source in sources:
            if source not in response:
                continue
            self._forecast_cache[source] = response[source].get("forecast", [])
            self._forecast_cache_type[source] = forecast_type

    # --- Current condition source access -------------------------------------

    def _get_source_states(self) -> list[State]:
        """Return state objects for all available sources."""
        states = []
        for source in self._sources:
            state = self.hass.states.get(source)
            if state and state.state not in ("unavailable", "unknown"):
                states.append(state)
        return states

    def _converted_attr_from_sources(
        self,
        attribute: str,
        unit_attribute: str,
        kind: str,
    ) -> list[float]:
        """Collect a numeric attribute from all sources, each normalized using its own unit."""
        values: list[float] = []
        for state in self._get_source_states():
            raw = _to_float(state.attributes.get(attribute))
            if raw is None:
                continue
            unit = state.attributes.get(unit_attribute)
            normalized = self._normalize(
                raw, unit, kind, context=f"{state.entity_id}.{attribute}"
            )
            if normalized is not None:
                values.append(normalized)
        return values

    # --- Current conditions ---------------------------------------------------

    @property
    def native_temperature(self) -> float | None:
        return _median(
            self._converted_attr_from_sources(
                "temperature", ATTR_TEMPERATURE_UNIT, "temperature"
            )
        )

    @property
    def native_apparent_temperature(self) -> float | None:
        return _median(
            self._converted_attr_from_sources(
                "apparent_temperature", ATTR_TEMPERATURE_UNIT, "temperature"
            )
        )

    @property
    def dew_point(self) -> float | None:
        return _median(
            self._converted_attr_from_sources(
                "dew_point", ATTR_TEMPERATURE_UNIT, "temperature"
            )
        )

    @property
    def humidity(self) -> float | None:
        # Humidity is always a percentage — no unit conversion applies.
        values = [
            v
            for state in self._get_source_states()
            if (v := _to_float(state.attributes.get("humidity"))) is not None
        ]
        return _median(values)

    @property
    def native_wind_speed(self) -> float | None:
        return _median(
            self._converted_attr_from_sources(
                "wind_speed", ATTR_WIND_SPEED_UNIT, "speed"
            )
        )

    @property
    def native_wind_gust_speed(self) -> float | None:
        return _median(
            self._converted_attr_from_sources(
                "wind_gust_speed", ATTR_WIND_SPEED_UNIT, "speed"
            )
        )

    @property
    def wind_bearing(self) -> float | None:
        # Bearings are always degrees — no unit conversion applies.
        values = [
            v
            for state in self._get_source_states()
            if (v := _to_float(state.attributes.get("wind_bearing"))) is not None
        ]
        return _circular_median(values)

    @property
    def native_pressure(self) -> float | None:
        return _median(
            self._converted_attr_from_sources(
                "pressure", ATTR_PRESSURE_UNIT, "pressure"
            )
        )

    @property
    def native_visibility(self) -> float | None:
        return _median(
            self._converted_attr_from_sources(
                "visibility", ATTR_VISIBILITY_UNIT, "distance"
            )
        )

    @property
    def uv_index(self) -> float | None:
        # UV index is a dimensionless scale — no unit conversion applies.
        values = [
            v
            for state in self._get_source_states()
            if (v := _to_float(state.attributes.get("uv_index"))) is not None
        ]
        return _median(values)

    @property
    def condition(self) -> str | None:
        conditions = [
            state.state
            for state in self._get_source_states()
            if state.state not in ("unavailable", "unknown", "")
        ]
        return _majority_vote(conditions)

    # --- Forecasts ---------------------------------------------------------

    def _forecast_slot_units(self, source: str) -> dict[str, str | None]:
        """Return the per-measurement units this source's forecast values are in.

        Per the Home Assistant weather.get_forecasts schema, forecast values
        are expressed in the unit indicated by that source's own current-state
        unit attributes (e.g. temperature_unit) — not necessarily the same
        unit another source uses.
        """
        state = self.hass.states.get(source)
        attrs = state.attributes if state else {}
        return {
            "temperature": attrs.get(ATTR_TEMPERATURE_UNIT),
            "speed": attrs.get(ATTR_WIND_SPEED_UNIT),
            "precipitation": attrs.get(ATTR_PRECIPITATION_UNIT),
        }

    def _build_median_forecast(self, forecast_type: str) -> list[Forecast]:
        """Build a median forecast from all cached sources for the given type.

        Each cached forecast stays paired with its source entity_id, so a
        provider that lacks a forecast (or a matching time slot) is simply
        skipped for that slot rather than shifting or misaligning the rest.
        Every value is normalized using *that specific source's* reported
        unit before being included in the median.
        """
        available: list[tuple[str, list[dict]]] = [
            (source, cached)
            for source in self._sources
            if self._forecast_cache_type.get(source) == forecast_type
            and (cached := self._forecast_cache.get(source))
        ]

        if not available:
            return []

        # Use the first available source purely as the datetime spine —
        # every provider's own values are looked up and normalized
        # independently per slot.
        _, lead = available[0]
        result: list[Forecast] = []

        for lead_slot in lead:
            dt = lead_slot.get(ATTR_FORECAST_TIME)
            if not dt:
                continue

            temps, templows, dew_points, winds, bearings, precips, humids, uv_indices, conditions = (
                [], [], [], [], [], [], [], [], []
            )

            for source, forecast_slots in available:
                slot = next(
                    (s for s in forecast_slots if s.get(ATTR_FORECAST_TIME) == dt),
                    None,
                )
                if slot is None:
                    continue

                units = self._forecast_slot_units(source)
                context = f"{source} forecast[{dt}]"

                if (v := _to_float(slot.get(ATTR_FORECAST_TEMP))) is not None:
                    if (c := self._normalize(v, units["temperature"], "temperature", context=context)) is not None:
                        temps.append(c)
                if (v := _to_float(slot.get(ATTR_FORECAST_TEMP_LOW))) is not None:
                    if (c := self._normalize(v, units["temperature"], "temperature", context=context)) is not None:
                        templows.append(c)
                if (v := _to_float(slot.get("dew_point"))) is not None:
                    if (c := self._normalize(v, units["temperature"], "temperature", context=context)) is not None:
                        dew_points.append(c)
                if (v := _to_float(slot.get(ATTR_FORECAST_WIND_SPEED))) is not None:
                    if (c := self._normalize(v, units["speed"], "speed", context=context)) is not None:
                        winds.append(c)
                if (v := _to_float(slot.get(ATTR_FORECAST_WIND_BEARING))) is not None:
                    bearings.append(v)  # degrees — no unit conversion applies
                if (v := _to_float(slot.get(ATTR_FORECAST_PRECIPITATION))) is not None:
                    if (c := self._normalize(v, units["precipitation"], "precipitation", context=context)) is not None:
                        precips.append(c)
                if (v := _to_float(slot.get(ATTR_FORECAST_HUMIDITY))) is not None:
                    humids.append(v)  # percentage — no unit conversion applies
                if (v := _to_float(slot.get("uv_index"))) is not None:
                    uv_indices.append(v)  # dimensionless — no unit conversion applies
                if (v := slot.get(ATTR_FORECAST_CONDITION)) is not None:
                    conditions.append(str(v))

            entry: Forecast = {ATTR_FORECAST_TIME: dt}

            if (v := _median(temps)) is not None:
                entry[ATTR_FORECAST_TEMP] = round(v, 1)
            if (v := _median(templows)) is not None:
                entry[ATTR_FORECAST_TEMP_LOW] = round(v, 1)
            if (v := _median(dew_points)) is not None:
                entry["dew_point"] = round(v, 1)
            if (v := _median(winds)) is not None:
                entry[ATTR_FORECAST_WIND_SPEED] = round(v, 1)
            if (v := _circular_median(bearings)) is not None:
                entry[ATTR_FORECAST_WIND_BEARING] = round(v, 1)
            if (v := _median(precips)) is not None:
                entry[ATTR_FORECAST_PRECIPITATION] = round(v, 2)
            if (v := _median(humids)) is not None:
                entry[ATTR_FORECAST_HUMIDITY] = round(v, 1)
            if (v := _median(uv_indices)) is not None:
                entry["uv_index"] = round(v, 1)
            if (v := _majority_vote(conditions)) is not None:
                entry[ATTR_FORECAST_CONDITION] = v

            result.append(entry)

        return result

    async def async_forecast_daily(self) -> list[Forecast] | None:
        return self._build_median_forecast("daily") or None

    async def async_forecast_hourly(self) -> list[Forecast] | None:
        return self._build_median_forecast("hourly") or None

    async def async_update(self) -> None:
        await self._async_update_forecasts()
