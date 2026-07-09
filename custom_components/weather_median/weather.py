"""Weather Median entity."""
from __future__ import annotations

import logging
import math
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

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

from .const import CONF_NAME, CONF_SOURCES, CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)

CANONICAL_TEMPERATURE_UNIT = UnitOfTemperature.CELSIUS
CANONICAL_PRESSURE_UNIT = UnitOfPressure.HPA
CANONICAL_SPEED_UNIT = UnitOfSpeed.KILOMETERS_PER_HOUR
CANONICAL_DISTANCE_UNIT = UnitOfLength.KILOMETERS
CANONICAL_PRECIPITATION_UNIT = UnitOfPrecipitationDepth.MILLIMETERS

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
    if not values:
        return None
    return statistics.median(values)


def _circular_median(degrees: list[float]) -> float | None:
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
    if not values:
        return None
    return max(set(values), key=values.count)


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_iso_datetime(value: Any) -> Optional[datetime]:
    """Parse an ISO datetime string to a timezone-aware datetime in UTC.

    Returns None if parsing fails or value is not a string.
    """
    if not isinstance(value, str):
        return None
    try:
        # Handle trailing Z (UTC) by replacing with +00:00 for fromisoformat
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        dt = datetime.fromisoformat(value)
        # Ensure timezone-aware in UTC
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


class WeatherMedianEntity(WeatherEntity):
    _attr_should_poll = False
    _attr_native_temperature_unit = CANONICAL_TEMPERATURE_UNIT
    _attr_native_pressure_unit = CANONICAL_PRESSURE_UNIT
    _attr_native_wind_speed_unit = CANONICAL_SPEED_UNIT
    _attr_native_visibility_unit = CANONICAL_DISTANCE_UNIT
    _attr_native_precipitation_unit = CANONICAL_PRECIPITATION_UNIT

    def __init__(self, hass: HomeAssistant, config: dict[str, Any], entry_id: str) -> None:
        self.hass = hass
        self._sources: list[str] = config[CONF_SOURCES]
        self._name: str = config[CONF_NAME]
        self._update_interval = timedelta(minutes=int(config.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)))
        self._attr_unique_id = f"{DOMAIN}_{entry_id}"
        self._attr_name = self._name
        # forecast_cache maps (source, type) -> {"data": [...], "units": {...}}
        self._forecast_cache: dict[tuple[str, str], dict[str, Any]] = {}
        self._attr_supported_features = WeatherEntityFeature(0)
        self._remove_time_listener = None

    async def async_added_to_hass(self) -> None:
        await self._async_update_forecasts()
        self._remove_time_listener = async_track_time_interval(self.hass, self._async_scheduled_update, self._update_interval)

    async def async_will_remove_from_hass(self) -> None:
        if self._remove_time_listener:
            self._remove_time_listener()

    async def _async_scheduled_update(self, _now=None) -> None:
        await self._async_update_forecasts()
        self.async_write_ha_state()

    @staticmethod
    def _convert_temperature(v: float | None, u: str | None) -> float | None:
        return TemperatureConverter.convert(v, u, CANONICAL_TEMPERATURE_UNIT) if v is not None and u else v

    @staticmethod
    def _convert_pressure(v: float | None, u: str | None) -> float | None:
        return PressureConverter.convert(v, u, CANONICAL_PRESSURE_UNIT) if v is not None and u else v

    @staticmethod
    def _convert_speed(v: float | None, u: str | None) -> float | None:
        return SpeedConverter.convert(v, u, CANONICAL_SPEED_UNIT) if v is not None and u else v

    @staticmethod
    def _convert_distance(v: float | None, u: str | None) -> float | None:
        return DistanceConverter.convert(v, u, CANONICAL_DISTANCE_UNIT) if v is not None and u else v

    @staticmethod
    def _convert_precipitation(v: float | None, u: str | None) -> float | None:
        return DistanceConverter.convert(v, u, CANONICAL_PRECIPITATION_UNIT) if v is not None and u else v

    async def _async_update_forecasts(self) -> None:
        supports_daily: list[str] = []
        supports_hourly: list[str] = []
        for source in self._sources:
            if (state := self.hass.states.get(source)):
                feat = state.attributes.get("supported_features", 0)
                if feat & WeatherEntityFeature.FORECAST_DAILY:
                    supports_daily.append(source)
                if feat & WeatherEntityFeature.FORECAST_HOURLY:
                    supports_hourly.append(source)

        # For each forecast type, call the weather.get_forecasts service for the relevant targets.
        for f_type in ["daily", "hourly"]:
            targets = supports_daily if f_type == "daily" else supports_hourly
            if not targets:
                continue
            try:
                resp = await self.hass.services.async_call(
                    "weather",
                    "get_forecasts",
                    {"type": f_type},
                    return_response=True,
                    target={"entity_id": targets},
                )
                for src in targets:
                    if src in resp:
                        state = self.hass.states.get(src)
                        attrs = state.attributes if state else {}
                        raw_forecast = resp[src].get("forecast", []) or []

                        # Filter hourly forecasts to whole-hour slots only.
                        if f_type == "hourly":
                            filtered = []
                            for slot in raw_forecast:
                                dt_raw = slot.get(ATTR_FORECAST_TIME)
                                dt = _parse_iso_datetime(dt_raw)
                                if dt is None:
                                    # If we can't parse, skip the slot
                                    continue
                                # Keep only whole-hour slots (minute == 0)
                                if dt.minute == 0:
                                    filtered.append(slot)
                        else:
                            # daily: keep as-is
                            filtered = raw_forecast

                        # Only store if there is at least one slot after filtering
                        if filtered:
                            self._forecast_cache[(src, f_type)] = {
                                "data": filtered,
                                "units": {
                                    ATTR_TEMPERATURE_UNIT: attrs.get(ATTR_TEMPERATURE_UNIT),
                                    ATTR_WIND_SPEED_UNIT: attrs.get(ATTR_WIND_SPEED_UNIT),
                                    ATTR_PRECIPITATION_UNIT: attrs.get(ATTR_PRECIPITATION_UNIT),
                                },
                            }
                        else:
                            # Remove any previous cache for this source/type if no valid slots now
                            if (src, f_type) in self._forecast_cache:
                                del self._forecast_cache[(src, f_type)]
            except Exception as e:
                _LOGGER.error("Error fetching %s forecasts: %s", f_type, e)

        feat = WeatherEntityFeature(0)
        if supports_daily:
            feat |= WeatherEntityFeature.FORECAST_DAILY
        if supports_hourly:
            feat |= WeatherEntityFeature.FORECAST_HOURLY
        self._attr_supported_features = feat

    def _get_source_states(self) -> list[State]:
        return [s for src in self._sources if (s := self.hass.states.get(src)) and s.state not in ("unavailable", "unknown")]

    def _converted_attr(self, attr: str, unit_attr: str, conv: Any) -> list[float]:
        return [
            c
            for s in self._get_source_states()
            if (r := _to_float(s.attributes.get(attr))) is not None
            and (c := conv(r, s.attributes.get(unit_attr))) is not None
        ]

    @property
    def native_temperature(self) -> float | None:
        return _median(self._converted_attr("temperature", ATTR_TEMPERATURE_UNIT, self._convert_temperature))

    @property
    def native_apparent_temperature(self) -> float | None:
        return _median(self._converted_attr("apparent_temperature", ATTR_TEMPERATURE_UNIT, self._convert_temperature))

    @property
    def humidity(self) -> float | None:
        return _median([v for s in self._get_source_states() if (v := _to_float(s.attributes.get("humidity"))) is not None])

    @property
    def native_wind_speed(self) -> float | None:
        return _median(self._converted_attr("wind_speed", ATTR_WIND_SPEED_UNIT, self._convert_speed))

    @property
    def wind_bearing(self) -> float | None:
        return _circular_median([v for s in self._get_source_states() if (v := _to_float(s.attributes.get("wind_bearing"))) is not None])

    @property
    def native_wind_gust_speed(self) -> float | None:
        return _median(self._converted_attr("wind_gust_speed", ATTR_WIND_SPEED_UNIT, self._convert_speed))

    @property
    def native_pressure(self) -> float | None:
        return _median(self._converted_attr("pressure", ATTR_PRESSURE_UNIT, self._convert_pressure))

    @property
    def native_visibility(self) -> float | None:
        return _median(self._converted_attr("visibility", ATTR_VISIBILITY_UNIT, self._convert_distance))

    @property
    def uv_index(self) -> float | None:
        return _median([v for s in self._get_source_states() if (v := _to_float(s.attributes.get("uv_index"))) is not None])

    @property
    def dew_point(self) -> float | None:
        return _median(self._converted_attr("dew_point", ATTR_TEMPERATURE_UNIT, self._convert_temperature))

    @property
    def condition(self) -> str | None:
        return _majority_vote([s.state for s in self._get_source_states() if s.state not in ("unavailable", "unknown", "")])

    def _build_median_forecast(self, f_type: str) -> list[Forecast]:
        # Build list of available sources that have cached forecasts for this type
        available = []
        for src in self._sources:
            key = (src, f_type)
            if key in self._forecast_cache:
                data = self._forecast_cache[key]["data"]
                units = self._forecast_cache[key]["units"]
                # Ensure data is sorted by time for deterministic output
                def _slot_dt(slot):
                    dt = _parse_iso_datetime(slot.get(ATTR_FORECAST_TIME))
                    return dt or datetime.min.replace(tzinfo=timezone.utc)
                data_sorted = sorted(data, key=_slot_dt)
                available.append((src, data_sorted, units))

        if not available:
            return []

        # Use the lead source (first available) as the timeline to iterate over
        lead = available[0][1]
        res: list[Forecast] = []
        for l_slot in lead:
            dt_raw = l_slot.get(ATTR_FORECAST_TIME)
            if not dt_raw:
                continue
            # Collect values from all sources that have a slot for this exact time
            temps, templows, winds, bearings, precips, humids, conds = [], [], [], [], [], [], []
            for src, slots, units in available:
                slot = next((s for s in slots if s.get(ATTR_FORECAST_TIME) == dt_raw), None)
                if not slot:
                    continue
                if (v := _to_float(slot.get(ATTR_FORECAST_TEMP))) is not None and (c := self._convert_temperature(v, units.get(ATTR_TEMPERATURE_UNIT))) is not None:
                    temps.append(c)
                if (v := _to_float(slot.get(ATTR_FORECAST_TEMP_LOW))) is not None and (c := self._convert_temperature(v, units.get(ATTR_TEMPERATURE_UNIT))) is not None:
                    templows.append(c)
                if (v := _to_float(slot.get(ATTR_FORECAST_WIND_SPEED))) is not None and (c := self._convert_speed(v, units.get(ATTR_WIND_SPEED_UNIT))) is not None:
                    winds.append(c)
                if (v := _to_float(slot.get(ATTR_FORECAST_WIND_BEARING))) is not None:
                    bearings.append(v)
                if (v := _to_float(slot.get(ATTR_FORECAST_PRECIPITATION))) is not None and (c := self._convert_precipitation(v, units.get(ATTR_PRECIPITATION_UNIT))) is not None:
                    precips.append(c)
                if (v := _to_float(slot.get(ATTR_FORECAST_HUMIDITY))) is not None:
                    humids.append(v)
                if (v := slot.get(ATTR_FORECAST_CONDITION)):
                    conds.append(str(v))

            ent: Forecast = {ATTR_FORECAST_TIME: dt_raw}
            if (v := _median(temps)) is not None:
                ent[ATTR_FORECAST_TEMP] = round(v, 1)
            if (v := _median(templows)) is not None:
                ent[ATTR_FORECAST_TEMP_LOW] = round(v, 1)
            if (v := _median(winds)) is not None:
                ent[ATTR_FORECAST_WIND_SPEED] = round(v, 1)
            if (v := _circular_median(bearings)) is not None:
                ent[ATTR_FORECAST_WIND_BEARING] = round(v, 1)
            if (v := _median(precips)) is not None:
                ent[ATTR_FORECAST_PRECIPITATION] = round(v, 2)
            if (v := _median(humids)) is not None:
                ent[ATTR_FORECAST_HUMIDITY] = round(v, 1)
            if (v := _majority_vote(conds)) is not None:
                ent[ATTR_FORECAST_CONDITION] = v
            res.append(ent)
        return res

    async def async_forecast_daily(self) -> list[Forecast] | None:
        return self._build_median_forecast("daily") or None

    async def async_forecast_hourly(self) -> list[Forecast] | None:
        return self._build_median_forecast("hourly") or None

    async def async_update(self) -> None:
        await self._async_update_forecasts()
