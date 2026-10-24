---
name: home-assistant
description: Use when the owner asks about or wants to control devices at home through their own Home Assistant (lights, thermostat, vacuum, purifier, plugs, sensors, "is X on", "set the AC to 24"). Read and control them with the Home Assistant connector app.
---

# Home Assistant connector

Connects to the owner's own Home Assistant. Same tools as the Smart Home app; if both are installed, use the one whose `home_status` lists the device.

## Tools

- `home_status`: call first. Says whether it is connected and reachable, and what to do next.
- `home_devices`: devices grouped by physical device, each with its parts (power, mode, sensors) and their `entity_id`s. Filter by `room`, `kind` or part of a `name`.
- `home_device`: one part's full state and the actions it accepts with their options.
- `home_control`: run one action on one part, e.g. `turn_on`, `set_temperature` with `{"temperature": 24}`, `start`. The result reads the state back.

## Rules

- Only do what was asked. Don't change other settings or devices on the side.
- Unlocking, disarming or opening doors needs the owner's explicit yes in this chat; then pass `confirmed: true`.
- Not connected: the owner opens the Home Assistant app and pastes their address and a long-lived access token. Never ask for the token in chat.
- Can't reach it: Home Assistant is at home and the Wizard isn't on that network. Suggest Tailscale on both, or Home Assistant Cloud.
- A part showing `unavailable` is offline. Say that rather than retrying.
