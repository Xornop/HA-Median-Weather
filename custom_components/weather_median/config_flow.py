"""Config flow for Weather Median."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)

from .const import CONF_NAME, CONF_SOURCES, CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)


def _validate_sources(sources: list[str]) -> str | None:
    """Return error key if sources are invalid, else None."""
    if not sources:
        return "no_sources"
    return None


class WeatherMedianConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the initial config flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            sources: list[str] = user_input[CONF_SOURCES]
            error = _validate_sources(sources)

            if not error:
                name = user_input[CONF_NAME].strip()
                await self.async_set_unique_id(
                    f"weather_median_{name.lower().replace(' ', '_')}"
                )
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title=name,
                    data={
                        CONF_NAME: name,
                        CONF_SOURCES: sources,
                        CONF_UPDATE_INTERVAL: user_input[CONF_UPDATE_INTERVAL],
                    },
                )
            else:
                errors["base"] = error

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_NAME, default="Weather Median"): str,
                vol.Required(CONF_SOURCES): EntitySelector(
                    EntitySelectorConfig(domain="weather", multiple=True)
                ),
                vol.Required(CONF_UPDATE_INTERVAL, default=DEFAULT_UPDATE_INTERVAL): NumberSelector(
                    NumberSelectorConfig(min=5, max=1440, step=5, mode=NumberSelectorMode.BOX, unit_of_measurement="min")
                ),
            }),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return WeatherMedianOptionsFlow(config_entry)


class WeatherMedianOptionsFlow(OptionsFlow):
    """Handle options (edit sources after setup)."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._config_entry = config_entry

    def _own_entity_id(self) -> str | None:
        """Return this integration's own weather entity_id, if it exists yet.

        Used to exclude it from the source picker — selecting your own
        aggregated entity as one of its own sources would create a loop.
        """
        registry = er.async_get(self.hass)
        return registry.async_get_entity_id(
            "weather", DOMAIN, f"{DOMAIN}_{self._config_entry.entry_id}"
        )

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        current_sources: list[str] = self._config_entry.data.get(CONF_SOURCES, [])
        current_interval: int = self._config_entry.data.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)

        if user_input is not None:
            sources: list[str] = user_input[CONF_SOURCES]
            error = _validate_sources(sources)

            if not error:
                return self.async_create_entry(
                    title="",
                    data={
                        CONF_SOURCES: sources,
                        CONF_UPDATE_INTERVAL: user_input[CONF_UPDATE_INTERVAL],
                    },
                )
            else:
                errors["base"] = error

        own_entity_id = self._own_entity_id()
        exclude_entities = [own_entity_id] if own_entity_id else []

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required(CONF_SOURCES, default=current_sources): EntitySelector(
                    EntitySelectorConfig(
                        domain="weather",
                        multiple=True,
                        exclude_entities=exclude_entities,
                    )
                ),
                vol.Required(CONF_UPDATE_INTERVAL, default=current_interval): NumberSelector(
                    NumberSelectorConfig(min=5, max=1440, step=5, mode=NumberSelectorMode.BOX, unit_of_measurement="min")
                ),
            }),
            errors=errors,
        )
