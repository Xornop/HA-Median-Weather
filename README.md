# HA Median Weather

A Home Assistant custom integration that combines multiple weather sources into a single `weather` entity by calculating the **median** of all sources.

## Why median?

The median is more robust than the average — if one source reports an outlier value, it gets outvoted by the others instead of skewing the result. With 4 sources reporting temperatures of `12, 13, 14, 21`, the median is `13.5` while the average would be `15`.

## Features

- Creates a real `weather.*` entity (not just sensors) that works with all standard HA weather cards
- Combines current conditions from all sources using median for numeric values and majority vote for condition (sunny, rainy, etc.)
- Automatically detects which sources support `daily` and `hourly` forecasts — no manual configuration needed
- Median forecast per time slot across all supporting sources
- Configurable update interval
- Fully configurable via the UI — no YAML needed
- Sources can be added or removed after setup via the integration options

## Installation

### Via HACS (recommended)
[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=Xornop&repository=HA-Median-Weather)
or:
1. Open HACS in Home Assistant
2. Go to **Integrations**
3. Click the three dots in the top right → **Custom repositories**
4. Add `https://github.com/Xornop/ha-median-weather` as category **Integration**
5. Search for **HA Median Weather** and install it
6. Restart Home Assistant

### Manual

1. Copy the `custom_components/weather_median` folder to your `config/custom_components/` directory
2. Restart Home Assistant

## Setup

1. Go to **Settings → Devices & Services → Add Integration**
2. Search for **HA Median Weather**
3. Enter a name for the entity
4. Select the weather sources you want to combine
5. Set the update interval (default: 60 minutes)
6. Click **Submit**

The integration will create a `weather.<name>` entity.

## Supported attributes

| Attribute | Method |
|---|---|
| Condition | Majority vote |
| Temperature | Median |
| Apparent temperature | Median |
| Humidity | Median |
| Wind speed | Median |
| Wind bearing | Circular median |
| Wind gust speed | Median |
| Pressure | Median |
| Visibility | Median |
| UV index | Median |
| Dew point | Median |
| Forecast daily | Median per time slot |
| Forecast hourly | Median per time slot |

## Options

After setup, click **Configure** on the integration to change:
- Weather sources
- Update interval

## Notes

- If a source is unavailable, it is skipped and the median is calculated from the remaining sources
- Forecast slots are matched by datetime — if a source does not have a matching time slot, it is skipped for that slot
- The update interval controls how often the median is recalculated from the current HA state. It does not affect how often the underlying weather integrations poll their external APIs
