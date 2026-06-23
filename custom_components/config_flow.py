"""Config flow for Weather Median."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import entity_registry as er

from .const import CONF_NAME, CONF_SOURCES, DOMAIN

_LOGGER = logging.getLogger(__name__)


def _parse_sources(raw: str) -> list[str]:
    """Parse a comma-separated string into a list of entity IDs."""
    return [s.strip() for s in raw.split(",") if s.strip()]


def _validate_sources(hass: HomeAssistant, sources: list[str]) -> list[str]:
    """Return list of error keys, empty if all valid."""
    errors = []
    if not sources:
        errors.append("no_sources")
        return errors
    for entity_id in sources:
        state = hass.states.get(entity_id)
        if state is None or not entity_id.startswith("weather."):
            errors.append("invalid_entity")
            break
    return errors


class WeatherMedianConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the initial config flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            sources = _parse_sources(user_input[CONF_SOURCES])
            error_keys = _validate_sources(self.hass, sources)

            if not error_keys:
                name = user_input[CONF_NAME].strip()
                await self.async_set_unique_id(f"weather_median_{name.lower().replace(' ', '_')}")
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title=name,
                    data={
                        CONF_NAME: name,
                        CONF_SOURCES: sources,
                    },
                )
            else:
                for key in error_keys:
                    errors["base"] = key

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_NAME, default="Weer mediaan"): str,
                    vol.Required(CONF_SOURCES): str,
                }
            ),
            errors=errors,
            description_placeholders={
                "example": "weather.home, weather.buienradar, weather.google"
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return WeatherMedianOptionsFlow(config_entry)


class WeatherMedianOptionsFlow(OptionsFlow):
    """Handle options (edit sources after setup)."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        current_sources = self._config_entry.data.get(CONF_SOURCES, [])
        current_sources_str = ", ".join(current_sources)

        if user_input is not None:
            sources = _parse_sources(user_input[CONF_SOURCES])
            error_keys = _validate_sources(self.hass, sources)

            if not error_keys:
                return self.async_create_entry(
                    title="",
                    data={CONF_SOURCES: sources},
                )
            else:
                for key in error_keys:
                    errors["base"] = key

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SOURCES, default=current_sources_str): str,
                }
            ),
            errors=errors,
        )
