---
wizard: 0.2
id: home-assistant
name: Home Assistant
type: external
category: app
tags: [automation]
version: 0.1.0
summary: Connect the Home Assistant you already run at home, and your Wizard checks and controls your devices.
icon: icon.png
license: Apache-2.0
source: https://github.com/oscarhenrycollins/home-assistant
runtime:
  kind: runtipi
  config: runtipi/config.json
  compose: runtipi/docker-compose.yml
mcp:
  path: /mcp
  port: 8080
  transport: streamable-http
skill: SKILL.md
provides:
- smart-home
health: /home-assistant/healthz
view:
  path: /home-assistant/
  port: 8080
---

# Home Assistant

A connector to the [Home Assistant](https://www.home-assistant.io) you already run: on a Raspberry Pi, a Home Assistant Green, or anywhere else.

- **You** paste its address and a long-lived access token once. They stay on your box and are never shown again.
- **Your agent** checks and controls every device Home Assistant knows about: lights, thermostats, vacuums, purifiers, plugs, sensors, including Zigbee, Bluetooth and other local-only devices.

No Home Assistant runs here. Don't have one? Install **Smart Home** instead, which runs Home Assistant on your Wizard for you.
