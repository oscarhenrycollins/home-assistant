# Home Assistant (Wizard connector)

Connect the [Home Assistant](https://www.home-assistant.io) you already run, and your Wizard checks and controls your devices.

A [Wizard app](https://www.aiwizards.com/wizard-apps). Don't have Home Assistant? Install [Smart Home](https://github.com/oscarhenrycollins/smart-home), which runs it on your Wizard for you.

- `runtipi/`: stock Runtipi `config.json` and `docker-compose.yml`: one digest-pinned `python:3.12-alpine` service, no build.
- `mcp/`: `front.py` + `tools.py`, inlined into the compose by `python3 mcp/inline.py`. Serves the connect page at `/home-assistant/` and the agent's tools at `/mcp` (`home_status`, `home_devices`, `home_device`, `home_control`; box-only).
- `SKILL.md`: the skill the agent loads.

## Connect

1. In Home Assistant: your profile → Security → Long-lived access tokens → Create token.
2. Open the Home Assistant app on your Wizard, paste the address (e.g. `http://192.168.1.20:8123`, a Tailscale address, or your Home Assistant Cloud URL) and the token.

The token is stored in the app's private volume (0600) and never returned to the page or the agent. Disconnect deletes it; revoke it in Home Assistant too.

## Limits

- Your Wizard must reach Home Assistant: same network, Tailscale, or Home Assistant Cloud.
- The token has your Home Assistant user's rights. For tighter control, create a dedicated non-admin Home Assistant user for the Wizard.
- Unlocking, disarming and opening covers need the owner's explicit yes. System actions (shell commands, restarts, add-ons) are never callable.

## License

Apache-2.0.
