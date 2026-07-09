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

# Canonical internal units — every value is normalized to these before
# any median is calculated. Home Assistant handles user-facing conversion
# (e.g. to °F or mph) downstream, based on native_*_unit below.
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

    All incoming values are normalized to a canonical metric unit before any
    median is calculated, regardless of the unit each source happens to
    report in. Home Assistant performs user-facing unit conversion (e.g. to
    °F or mph) downstream, based on the native_*_unit properties below.
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

    # --- Unit conversion helpers -------------------------------------------
    #
    # These wrap Home Assistant's official converters. Each helper is a
    # no-op (returns the value unchanged) if the source doesn't report a
    # unit, since we then assume it's already in the canonical unit.

    @staticmethod
    def _convert_temperature(value: float | None, unit: str | None) -> float | None:
        if value is None or not unit:
            return value
        return TemperatureConverter.convert(value, unit, CANONICAL_TEMPERATURE_UNIT)

    @staticmethod
    def _convert_pressure(value: float | None, unit: str | None) -> float | None:
        if value is None or not unit:
            return value
        return PressureConverter.convert(value, unit, CANONICAL_PRESSURE_UNIT)

    @staticmethod
    def _convert_speed(value: float | None, unit: str | None) -> float | None:
        if value is None or not unit:
            return value
        return SpeedConverter.convert(value, unit, CANONICAL_SPEED_UNIT)

    @staticmethod
    def _convert_distance(value: float | None, unit: str | None) -> float | None:
        if value is None or not unit:
            return value
        return DistanceConverter.convert(value, unit, CANONICAL_DISTANCE_UNIT)

    @staticmethod
    def _convert_precipitation(value: float | None, unit: str | None) -> float | None:
        if value is None or not unit:
            return value
        return DistanceConverter.convert(value, unit, CANONICAL_PRECIPITATION_UNIT)

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
        converter: Any,
    ) -> list[float]:
        """Collect a numeric attribute from all sources, normalized to the canonical unit."""
        values: list[float] = []
        for state in self._get_source_states():
            raw = _to_float(state.attributes.get(attribute))
            if raw is None:
                continue
            unit = state.attributes.get(unit_attribute)
            converted = converter(raw, unit)
            if converted is not None:
                values.append(converted)
        return values

    # --- Current conditions ---------------------------------------------------

    @property
    def native_temperature(self) -> float | None:
        return _median(
            self._converted_attr_from_sources(
                "temperature", ATTR_TEMPERATURE_UNIT, self._convert_temperature
            )
        )

    @property
    def native_apparent_temperature(self) -> float | None:
        return _median(
            self._converted_attr_from_sources(
                "apparent_temperature", ATTR_TEMPERATURE_UNIT, self._convert_temperature
            )
        )

    @property
    def dew_point(self) -> float | None:
        return _median(
            self._converted_attr_from_sources(
                "dew_point", ATTR_TEMPERATURE_UNIT, self._convert_temperature
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
                "wind_speed", ATTR_WIND_SPEED_UNIT, self._convert_speed
            )
        )

    @property
    def native_wind_gust_speed(self) -> float | None:
        return _median(
            self._converted_attr_from_sources(
                "wind_gust_speed", ATTR_WIND_SPEED_UNIT, self._convert_speed
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
                "pressure", ATTR_PRESSURE_UNIT, self._convert_pressure
            )
        )

    @property
    def native_visibility(self) -> float | None:
        return _median(
            self._converted_attr_from_sources(
                "visibility", ATTR_VISIBILITY_UNIT, self._convert_distance
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

    def _build_median_forecast(self, forecast_type: str) -> list[Forecast]:
        """Build a median forecast from all cached sources for the given type.

        Each cached forecast stays paired with its source entity_id, so a
        provider that lacks a forecast (or a matching time slot) is simply
        skipped for that slot rather than shifting or misaligning the rest.
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
        # every provider's own values are looked up independently per slot.
        _, lead = available[0]
        result: list[Forecast] = []

        for lead_slot in lead:
            dt = lead_slot.get(ATTR_FORECAST_TIME)
            if not dt:
                continue

            temps, templows, winds, bearings, precips, humids, conditions = (
                [], [], [], [], [], [], []
            )

            for source, forecast_slots in available:
                slot = next(
                    (s for s in forecast_slots if s.get(ATTR_FORECAST_TIME) == dt),
                    None,
                )
                if slot is None:
                    continue

                unit_state = self.hass.states.get(source)
                attrs = unit_state.attributes if unit_state else {}
                temp_unit = attrs.get(ATTR_TEMPERATURE_UNIT)
                speed_unit = attrs.get(ATTR_WIND_SPEED_UNIT)
                precip_unit = attrs.get(ATTR_PRECIPITATION_UNIT)

                if (v := _to_float(slot.get(ATTR_FORECAST_TEMP))) is not None:
                    if (c := self._convert_temperature(v, temp_unit)) is not None:
                        temps.append(c)
                if (v := _to_float(slot.get(ATTR_FORECAST_TEMP_LOW))) is not None:
                    if (c := self._convert_temperature(v, temp_unit)) is not None:
                        templows.append(c)
                if (v := _to_float(slot.get(ATTR_FORECAST_WIND_SPEED))) is not None:
                    if (c := self._convert_speed(v, speed_unit)) is not None:
                        winds.append(c)
                if (v := _to_float(slot.get(ATTR_FORECAST_WIND_BEARING))) is not None:
                    bearings.append(v)
                if (v := _to_float(slot.get(ATTR_FORECAST_PRECIPITATION))) is not None:
                    if (c := self._convert_precipitation(v, precip_unit)) is not None:
                        precips.append(c)
                if (v := _to_float(slot.get(ATTR_FORECAST_HUMIDITY))) is not None:
                    humids.append(v)
                if (v := slot.get(ATTR_FORECAST_CONDITION)) is not None:
                    conditions.append(str(v))

            entry: Forecast = {ATTR_FORECAST_TIME: dt}

            if (v := _median(temps)) is not None:
                entry[ATTR_FORECAST_TEMP] = round(v, 1)
            if (v := _median(templows)) is not None:
                entry[ATTR_FORECAST_TEMP_LOW] = round(v, 1)
            if (v := _median(winds)) is not None:
                entry[ATTR_FORECAST_WIND_SPEED] = round(v, 1)
            if (v := _circular_median(bearings)) is not None:
                entry[ATTR_FORECAST_WIND_BEARING] = round(v, 1)
            if (v := _median(precips)) is not None:
                entry[ATTR_FORECAST_PRECIPITATION] = round(v, 2)
            if (v := _median(humids)) is not None:
                entry[ATTR_FORECAST_HUMIDITY] = round(v, 1)
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
