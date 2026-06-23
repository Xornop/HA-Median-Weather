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
    UnitOfPressure,
    UnitOfSpeed,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import CONF_NAME, CONF_SOURCES, DOMAIN, UPDATE_INTERVAL_MINUTES

_LOGGER = logging.getLogger(__name__)

FORECAST_TYPES = ("daily", "hourly")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Weather Median entity."""
    # Merge data and options — options override data when sources are edited
    config = dict(entry.data)
    if entry.options:
        config[CONF_SOURCES] = entry.options.get(CONF_SOURCES, config[CONF_SOURCES])

    entity = WeatherMedianEntity(hass, config, entry.entry_id)
    async_add_entities([entity], update_before_add=True)


def _median(values: list[float]) -> float | None:
    """Return the median of a list, or None if empty."""
    if not values:
        return None
    return statistics.median(values)


def _circular_median(degrees: list[float]) -> float | None:
    """
    Approximate circular median for wind bearing.
    Uses the mean angle of the two middle values after sorting on the unit circle.
    Good enough for weather sources that are typically close together.
    """
    if not degrees:
        return None
    if len(degrees) == 1:
        return degrees[0]

    # Convert to radians, compute mean vector
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


class WeatherMedianEntity(WeatherEntity):
    """A weather entity that exposes the median of multiple weather sources."""

    _attr_should_poll = False
    _attr_native_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_native_pressure_unit = UnitOfPressure.HPA
    _attr_native_wind_speed_unit = UnitOfSpeed.KILOMETERS_PER_HOUR
    _attr_native_visibility_unit = UnitOfLength.KILOMETERS
    _attr_native_precipitation_unit = "mm"

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

        self._attr_unique_id = f"{DOMAIN}_{entry_id}"
        self._attr_name = self._name

        # Cached forecast data keyed by (source, type)
        self._forecast_cache: dict[tuple[str, str], list[dict]] = {}

        # Which sources support which forecast type — discovered on first update
        self._supports: dict[str, set[str]] = {}  # entity_id -> {"daily", "hourly"}

        # Supported features — set after discovery
        self._attr_supported_features = WeatherEntityFeature(0)

        self._remove_time_listener = None

    async def async_added_to_hass(self) -> None:
        """Start periodic updates when entity is added."""
        await self._async_update_forecasts()

        self._remove_time_listener = async_track_time_interval(
            self.hass,
            self._async_scheduled_update,
            timedelta(minutes=UPDATE_INTERVAL_MINUTES),
        )

    async def async_will_remove_from_hass(self) -> None:
        """Clean up timer."""
        if self._remove_time_listener:
            self._remove_time_listener()

    async def _async_scheduled_update(self, _now=None) -> None:
        await self._async_update_forecasts()
        self.async_write_ha_state()

    async def _async_update_forecasts(self) -> None:
        """
        Fetch forecasts from all sources.
        Discovers which sources support daily/hourly on the fly.
        """
        supports_daily = []
        supports_hourly = []

        for source in self._sources:
            state = self.hass.states.get(source)
            if state is None:
                _LOGGER.warning("Weather source %s not found, skipping.", source)
                continue

            supported = state.attributes.get("supported_features", 0)
            # WeatherEntityFeature.FORECAST_DAILY = 1, FORECAST_HOURLY = 2
            if supported & 1:
                supports_daily.append(source)
            if supported & 2:
                supports_hourly.append(source)

        # Fetch daily
        if supports_daily:
            try:
                response = await self.hass.services.async_call(
                    "weather",
                    "get_forecasts",
                    {"type": "daily"},
                    blocking=True,
                    return_response=True,
                    target={"entity_id": supports_daily},
                )
                for source in supports_daily:
                    if source in response:
                        self._forecast_cache[(source, "daily")] = response[source].get("forecast", [])
            except Exception as err:
                _LOGGER.error("Error fetching daily forecasts: %s", err)

        # Fetch hourly
        if supports_hourly:
            try:
                response = await self.hass.services.async_call(
                    "weather",
                    "get_forecasts",
                    {"type": "hourly"},
                    blocking=True,
                    return_response=True,
                    target={"entity_id": supports_hourly},
                )
                for source in supports_hourly:
                    if source in response:
                        self._forecast_cache[(source, "hourly")] = response[source].get("forecast", [])
            except Exception as err:
                _LOGGER.error("Error fetching hourly forecasts: %s", err)

        # Update supported features based on what we actually got
        features = WeatherEntityFeature(0)
        if supports_daily:
            features |= WeatherEntityFeature.FORECAST_DAILY
        if supports_hourly:
            features |= WeatherEntityFeature.FORECAST_HOURLY
        self._attr_supported_features = features

        _LOGGER.debug(
            "Forecast sources — daily: %s, hourly: %s",
            supports_daily,
            supports_hourly,
        )

    def _get_source_states(self) -> list[Any]:
        """Return state objects for all available sources."""
        states = []
        for source in self._sources:
            state = self.hass.states.get(source)
            if state and state.state not in ("unavailable", "unknown"):
                states.append(state)
        return states

    def _attr_from_sources(self, attribute: str) -> list[float]:
        """Collect numeric attribute values from all sources."""
        values = []
        for state in self._get_source_states():
            val = state.attributes.get(attribute)
            if val is not None:
                try:
                    values.append(float(val))
                except (TypeError, ValueError):
                    pass
        return values

    # --- Current conditions ---

    @property
    def native_temperature(self) -> float | None:
        return _median(self._attr_from_sources("temperature"))

    @property
    def native_apparent_temperature(self) -> float | None:
        return _median(self._attr_from_sources("apparent_temperature"))

    @property
    def humidity(self) -> float | None:
        return _median(self._attr_from_sources("humidity"))

    @property
    def native_wind_speed(self) -> float | None:
        return _median(self._attr_from_sources("wind_speed"))

    @property
    def wind_bearing(self) -> float | None:
        return _circular_median(self._attr_from_sources("wind_bearing"))

    @property
    def native_wind_gust_speed(self) -> float | None:
        return _median(self._attr_from_sources("wind_gust_speed"))

    @property
    def native_pressure(self) -> float | None:
        return _median(self._attr_from_sources("pressure"))

    @property
    def native_visibility(self) -> float | None:
        return _median(self._attr_from_sources("visibility"))

    @property
    def uv_index(self) -> float | None:
        return _median(self._attr_from_sources("uv_index"))

    @property
    def dew_point(self) -> float | None:
        return _median(self._attr_from_sources("dew_point"))

    @property
    def condition(self) -> str | None:
        conditions = []
        for state in self._get_source_states():
            if state.state not in ("unavailable", "unknown", ""):
                conditions.append(state.state)
        return _majority_vote(conditions)

    # --- Forecasts ---

    def _build_median_forecast(self, forecast_type: str) -> list[Forecast]:
        """
        Build a median forecast list from all cached sources for the given type.
        Uses the first available source as the leading datetime spine.
        Sources without a matching datetime slot are skipped for that slot.
        """
        # Collect all cached forecasts for this type
        available: list[list[dict]] = []
        for source in self._sources:
            cached = self._forecast_cache.get((source, forecast_type))
            if cached:
                available.append(cached)

        if not available:
            return []

        # Use the first source as the datetime spine
        lead = available[0]
        result: list[Forecast] = []

        for lead_slot in lead:
            dt = lead_slot.get(ATTR_FORECAST_TIME)
            if not dt:
                continue

            temps, templows, winds, bearings, precips, humids, conditions = (
                [], [], [], [], [], [], []
            )

            for fc in available:
                # Find matching slot by datetime
                slot = next(
                    (s for s in fc if s.get(ATTR_FORECAST_TIME) == dt), None
                )
                if slot is None:
                    continue

                if (v := slot.get(ATTR_FORECAST_TEMP)) is not None:
                    try:
                        temps.append(float(v))
                    except (TypeError, ValueError):
                        pass
                if (v := slot.get(ATTR_FORECAST_TEMP_LOW)) is not None:
                    try:
                        templows.append(float(v))
                    except (TypeError, ValueError):
                        pass
                if (v := slot.get(ATTR_FORECAST_WIND_SPEED)) is not None:
                    try:
                        winds.append(float(v))
                    except (TypeError, ValueError):
                        pass
                if (v := slot.get(ATTR_FORECAST_WIND_BEARING)) is not None:
                    try:
                        bearings.append(float(v))
                    except (TypeError, ValueError):
                        pass
                if (v := slot.get(ATTR_FORECAST_PRECIPITATION)) is not None:
                    try:
                        precips.append(float(v))
                    except (TypeError, ValueError):
                        pass
                if (v := slot.get(ATTR_FORECAST_HUMIDITY)) is not None:
                    try:
                        humids.append(float(v))
                    except (TypeError, ValueError):
                        pass
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
        """Return daily forecast."""
        return self._build_median_forecast("daily") or None

    async def async_forecast_hourly(self) -> list[Forecast] | None:
        """Return hourly forecast."""
        return self._build_median_forecast("hourly") or None

    async def async_update(self) -> None:
        """Called by HA — refresh forecast cache."""
        await self._async_update_forecasts()
