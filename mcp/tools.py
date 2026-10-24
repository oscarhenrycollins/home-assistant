# Home Assistant connector: the agent reads and controls the owner's devices through the owner's OWN Home Assistant
# (at home on a Raspberry Pi, a Home Assistant Green, or anywhere else). This app runs no Home Assistant of its own.
# It keeps the address and a long-lived access token in its private volume (0600, never shown again), serves the
# owner's page at /home-assistant/ and the agent's tools at /mcp (box-only).
import hmac
import re
import ssl
import threading

STATE_DIR = os.environ.get("STATE_DIR", "/data/.wizard")
PAGE = "/home-assistant/"
_lock = threading.Lock()

# Devices the agent may control: the entity's own domain is the only one it can call, never system domains.
CONTROL = {"light", "switch", "fan", "climate", "humidifier", "vacuum", "cover", "media_player", "camera", "select",
           "number", "button", "input_boolean", "scene", "siren", "water_heater", "lock", "valve", "lawn_mower",
           "remote", "alarm_control_panel", "text", "notify"}
HIDDEN_DOMAINS = {"update", "conversation", "zone", "sun", "person", "tts", "stt", "wake_word", "assist_satellite",
                  "todo", "backup", "event", "ai_task", "tag"}
HIDDEN_PREFIXES = ("sensor.sun_", "sensor.backup_")
NEEDS_CONFIRM = {("lock", "unlock"), ("lock", "open"), ("alarm_control_panel", "alarm_disarm"),
                 ("cover", "open_cover"), ("valve", "open_valve")}
TOGGLE = {"light", "switch", "fan", "humidifier", "input_boolean", "media_player", "siren"}
ENTITY_RE = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z0-9_]+\Z")
ACTION_RE = re.compile(r"^[a-z][a-z0-9_]{0,60}\Z")


def _box_only():
    """Agent tools are only for apps on this box (the agent through the Wizard hub), never for a request that
    came in through Tailscale Serve, a tunnel or a proxy."""
    h = REQUEST.get()
    if any(h.get(k) for k in ("tailscale-user-login", "x-forwarded-for", "forwarded", "cf-connecting-ip",
                              "cf-access-jwt-assertion", "x-real-ip")):
        raise PermissionError("the Home Assistant tools are only for apps on this box")



# ---------------- connection to the owner's Home Assistant ----------------

def _state_path(name):
    return os.path.join(STATE_DIR, name)


def _conf():
    try:
        with open(_state_path("connection.json")) as f:
            d = json.load(f)
        return d if d.get("url") and d.get("token") else None
    except (OSError, ValueError):
        return None


def _save_conf(d):
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    tmp = _state_path("connection.json.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(d, f)
    os.replace(tmp, _state_path("connection.json"))


def _clean_url(raw):
    u = urllib.parse.urlsplit(str(raw or "").strip())
    if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password or u.query or u.fragment:
        raise ValueError("Home Assistant address looks like http://192.168.1.20:8123 or https://xyz.ui.nabu.casa")
    return urllib.parse.urlunsplit((u.scheme, u.netloc, u.path.rstrip("/"), "", ""))


def _call(url, token, method, path, payload=None, timeout=30):
    body = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url + path, data=body, method=method,
                                 headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            raw = r.read(8 << 20)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise PermissionError("Home Assistant refused the access token (401). Make a new one and connect again.")
        raise RuntimeError("Home Assistant said %d: %s" % (e.code, e.read().decode("utf-8", "replace")[:300]))
    except (urllib.error.URLError, OSError) as e:
        raise RuntimeError("Can't reach Home Assistant at %s (%s). If it is at home, reach it through Tailscale "
                           "or Home Assistant Cloud." % (url, getattr(e, "reason", e)))
    try:
        return json.loads(raw or b"null")
    except ValueError:
        return raw.decode("utf-8", "replace")


def ha(method, path, payload=None, timeout=30):
    c = _conf()
    if not c:
        raise RuntimeError("Home Assistant is not connected yet: the owner opens the Home Assistant app and connects it.")
    return _call(c["url"], c["token"], method, path, payload, timeout)


def connect(url, token):
    url = _clean_url(url)
    token = str(token or "").strip()
    if not (20 <= len(token) <= 1000) or any(ch.isspace() for ch in token):
        raise ValueError("Paste the long-lived access token from your Home Assistant profile (Security tab).")
    info = _call(url, token, "GET", "/api/config", timeout=20)
    if not isinstance(info, dict) or "version" not in info:
        raise RuntimeError("That address answered, but not like Home Assistant.")
    with _lock:
        _save_conf({"url": url, "token": token, "name": info.get("location_name") or "Home",
                    "version": info.get("version"), "connected_at": int(time.time())})
    return {"connected": True, "name": info.get("location_name") or "Home", "version": info.get("version")}


def disconnect():
    with _lock:
        try:
            os.remove(_state_path("connection.json"))
        except OSError:
            pass
    return {"connected": False}


def status():
    c = _conf()
    if not c:
        return {"connected": False, "ready": False,
                "next": "Not connected. Ask the owner to open the Home Assistant app and paste their Home Assistant "
                        "address and a long-lived access token (Profile > Security in Home Assistant). You can't do "
                        "this for them."}
    out = {"connected": True, "home": c.get("name"), "address": c["url"], "home_assistant_version": c.get("version")}
    try:
        devs = devices()
        out.update(ready=True, devices=len(grouped(devs)), parts=len(devs),
                   controllable=sum(1 for d in devs if d["controllable"]),
                   next="Use home_devices to see what is there, then home_control to act.")
    except Exception as e:
        out.update(ready=False, next=str(e))
    return out

# ---------------- devices ----------------

INDEX_TEMPLATE = ("{%- set ns = namespace(o=[]) -%}{%- for s in states -%}"
                  "{%- set ns.o = ns.o + [[s.entity_id, area_name(s.entity_id), device_attr(s.entity_id, 'name')]] -%}"
                  "{%- endfor -%}{{ ns.o | tojson }}")


def _index():
    try:
        rows = ha("POST", "/api/template", {"template": INDEX_TEMPLATE})
        rows = json.loads(rows) if isinstance(rows, str) else rows
        return {r[0]: (r[1] or "", r[2] or "") for r in rows or []}
    except Exception:
        return {}


def _visible(entity_id):
    domain = entity_id.split(".", 1)[0]
    return domain not in HIDDEN_DOMAINS and not entity_id.startswith(HIDDEN_PREFIXES)


def devices(room="", kind="", name=""):
    idx = _index()
    out = []
    for s in ha("GET", "/api/states") or []:
        eid = s.get("entity_id", "")
        if not _visible(eid):
            continue
        domain = eid.split(".", 1)[0]
        if kind and domain != kind:
            continue
        area, device = idx.get(eid, ("", ""))
        attrs = s.get("attributes") or {}
        label = attrs.get("friendly_name") or eid
        if room and room.lower() not in area.lower():
            continue
        if name and name.lower() not in (label + " " + eid + " " + device).lower():
            continue
        unit = attrs.get("unit_of_measurement") or ""
        out.append({"entity_id": eid, "name": label, "room": area, "device": device, "kind": domain,
                    "state": s.get("state"), "unit": unit, "controllable": domain in CONTROL,
                    "toggle": domain in TOGGLE})
    out.sort(key=lambda d: (d["room"] or "~", d["device"], d["name"]))
    return out


def grouped(items):
    """Home Assistant splits one device into many parts (power, mode, PM2.5, filter...). Group them back."""
    out, idx = [], {}
    for d in items:
        key = (d["room"], d["device"] or d["name"])
        if key not in idx:
            idx[key] = {"device": key[1], "room": d["room"], "parts": []}
            out.append(idx[key])
        part = dict(d)
        for k in ("device", "room"):
            part.pop(k)
        name = part["name"]
        if key[1] and name.lower().startswith(key[1].lower()):
            part["name"] = name[len(key[1]):].strip() or name
        idx[key]["parts"].append(part)
    return out


def _actions(domain):
    for d in ha("GET", "/api/services") or []:
        if d.get("domain") == domain:
            acts = []
            for name, svc in sorted((d.get("services") or {}).items()):
                fields = {k: (v.get("description") or v.get("name") or "")[:120]
                          for k, v in (svc.get("fields") or {}).items() if isinstance(v, dict) and "fields" not in v}
                acts.append({"action": name, "options": fields})
            return acts
    return []


def control(entity_id, action, options=None, confirmed=False):
    entity_id = str(entity_id or "").strip()
    action = str(action or "").strip()
    if not ENTITY_RE.match(entity_id):
        raise ValueError("entity_id looks like fan.living_room_purifier (from home_devices)")
    if not ACTION_RE.match(action):
        raise ValueError("action is a Home Assistant action name such as turn_on, start or select_option")
    domain = entity_id.split(".", 1)[0]
    if domain not in CONTROL:
        raise PermissionError("%s is a reading, not something to control" % entity_id)
    if (domain, action) in NEEDS_CONFIRM and not confirmed:
        raise PermissionError("%s.%s affects home security: ask the owner first, then call again with "
                              "confirmed=true" % (domain, action))
    data = dict(options or {}) if isinstance(options, dict) else {}
    data.pop("entity_id", None)
    data["entity_id"] = entity_id
    ha("POST", "/api/services/%s/%s" % (domain, action), data, timeout=30)
    time.sleep(1.5)
    s = ha("GET", "/api/states/" + entity_id) or {}
    return {"done": True, "entity_id": entity_id, "action": action, "state_now": s.get("state"),
            "note": "state_now is read back from the device a moment later; some devices take a few seconds."}



# ---------------- agent tools ----------------

@tool("home_status", "Home Assistant connector status: whether the owner's own Home Assistant is connected and "
      "reachable, how many devices it has, and the next step. Call it first.")
def home_status():
    _box_only()
    return status()


@tool("home_devices", "List the owner's devices from their Home Assistant, grouped by physical device, each with its "
      "parts and their current state: power, mode, sensors (air quality, temperature, battery). Filter by room, kind "
      "(light, fan, vacuum, sensor, switch, climate, cover...) or part of a name.",
      {"room": {"type": "string", "description": "Part of a room name (optional)"},
       "kind": {"type": "string", "description": "Device kind, e.g. light, climate, vacuum, sensor (optional)"},
       "name": {"type": "string", "description": "Part of a device name (optional)"}})
def home_devices(room="", kind="", name=""):
    _box_only()
    found = grouped(devices(room, kind, name))
    return {"count": len(found), "devices": found,
            "next": "Each device lists its parts with their entity_id. home_device shows one part's details and "
                    "actions; home_control acts on a part."}


@tool("home_device", "One device part in detail: its state, every attribute and the actions it accepts with their "
      "options. Use before an action you have not used yet.",
      {"entity_id": {"type": "string", "description": "From home_devices, e.g. light.kitchen"}}, ["entity_id"])
def home_device(entity_id):
    _box_only()
    entity_id = str(entity_id or "").strip()
    if not ENTITY_RE.match(entity_id):
        raise ValueError("entity_id looks like light.kitchen (from home_devices)")
    s = ha("GET", "/api/states/" + entity_id)
    domain = entity_id.split(".", 1)[0]
    return {"entity_id": entity_id, "state": s.get("state"), "attributes": s.get("attributes"),
            "last_changed": s.get("last_changed"), "controllable": domain in CONTROL,
            "actions": _actions(domain) if domain in CONTROL else []}


@tool("home_control", "Control one device part: turn on or off, set a thermostat, start or dock a vacuum, set a mode "
      "or speed, pick an option. action is one of the part's actions from home_device. options holds that action's "
      "settings. Unlocking, disarming or opening doors needs the owner's explicit yes: then pass confirmed=true.",
      {"entity_id": {"type": "string", "description": "From home_devices"},
       "action": {"type": "string", "description": "e.g. turn_on, set_temperature, start, select_option"},
       "options": {"type": "object", "description": "Settings for the action, e.g. {\"temperature\": 24} (optional)"},
       "confirmed": {"type": "boolean", "description": "Only after the owner said yes to a security action"}},
      ["entity_id", "action"])
def home_control(entity_id, action, options=None, confirmed=False):
    _box_only()
    return control(entity_id, action, options, bool(confirmed))


# ---------------- the owner's page (/home-assistant/) ----------------

def _json(code, obj):
    return code, "application/json", json.dumps(obj).encode()


def _body(headers, body):
    if not headers.get("content-type", "").startswith("application/json"):
        raise PermissionError("send application/json")
    return json.loads(body or b"{}")


@route(PAGE)
def page_route(method, path, query, headers, body):
    sub = path[len(PAGE):] if path.startswith(PAGE) else ""
    if sub.startswith("api/") and headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none"):
        return _json(403, {"error": "open Home Assistant from your Wizard"})
    try:
        if method == "GET" and sub in ("", "index.html"):
            return 200, "text/html; charset=utf-8", PAGE_HTML.encode()
        if method == "GET" and sub == "app.js":
            return 200, "text/javascript; charset=utf-8", PAGE_JS.encode()
        if method == "GET" and sub == "app.css":
            return 200, "text/css; charset=utf-8", PAGE_CSS.encode()
        if method == "GET" and sub == "healthz":
            return _json(200, {"ok": True, "app": APP, "version": VERSION, "connected": bool(_conf())})
        if method == "GET" and sub == "api/status":
            return _json(200, status())
        if method == "GET" and sub == "api/devices":
            return _json(200, {"devices": devices()})
        if method == "POST" and sub == "api/connect":
            d = _body(headers, body)
            return _json(200, connect(d.get("url"), d.get("token")))
        if method == "POST" and sub == "api/disconnect":
            _body(headers, body)
            return _json(200, disconnect())
        if method == "POST" and sub == "api/control":
            d = _body(headers, body)
            if d.get("action") not in ("turn_on", "turn_off", "toggle"):
                return _json(400, {"error": "the page only turns things on and off"})
            return _json(200, control(d.get("entity_id"), d.get("action")))
    except PermissionError as e:
        return _json(403, {"error": str(e)})
    except (ValueError, RuntimeError) as e:
        return _json(400, {"error": str(e)})
    except Exception as e:
        return _json(502, {"error": "Home Assistant is not reachable right now (%s)." % type(e).__name__})
    return _json(404, {"error": "not found"})


PAGE_HTML = """<!doctype html>
<html lang="en" data-app="home-assistant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="dark light">
<title>Home Assistant</title>
<script>try { if (window.parent !== window) document.documentElement.classList.add('wz-hosted'); } catch (e) { document.documentElement.classList.add('wz-hosted'); }</script>
<link rel="stylesheet" href="app.css">
<script src="/wizard-os-home/core/app-client.js" onerror="this.remove()"></script>
<script defer src="app.js"></script>
</head>
<body>
<div class="sh-app">
  <header class="sh-head"><h1>Home Assistant</h1><p id="line" class="sh-line">Loading…</p></header>
  <main class="sh-scroll">
    <section id="connect" class="sh-card" hidden>
      <h2>Connect your Home Assistant</h2>
      <label class="sh-field"><span>Address</span><input id="url" class="sh-input" type="url" inputmode="url" autocomplete="off" placeholder="http://192.168.1.20:8123"></label>
      <label class="sh-field"><span>Long-lived access token</span><input id="token" class="sh-input" type="password" autocomplete="off" placeholder="Profile › Security › Create token"></label>
      <button id="go" class="sh-primary" type="button">Connect</button>
    </section>
    <section id="list"></section>
    <p id="empty" class="sh-empty" hidden>No devices yet.</p>
    <footer class="sh-foot" id="foot" hidden><span id="where"></span><button id="off" class="sh-quiet" type="button">Disconnect</button></footer>
  </main>
  <p id="status" class="sh-status" role="status" aria-live="polite"></p>
</div>
</body>
</html>
"""

PAGE_CSS = """
:root { --ink: var(--wz-text, #f3efff); --ink-2: var(--wz-text-secondary, #b9b0d8); --ink-3: var(--wz-text-muted, #a59abf);
  --line: var(--wz-border, rgba(205,190,255,.24)); --accent: var(--wz-accent, #b99bff); --on-accent: var(--wz-on-accent, #160b32);
  --canvas: var(--wz-base, var(--wz-canvas, #0b0814)); --solid: color-mix(in srgb, var(--ink) 9%, var(--canvas));
  --solid-2: color-mix(in srgb, var(--ink) 16%, var(--canvas)); --danger: #ff8f8f;
  --font: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; color-scheme: dark; }
html[data-color-scheme="light"] { color-scheme: light; --danger: #b42318; }
*, *::before, *::after { box-sizing: border-box; }
html, body { height: 100%; margin: 0; }
body { font: 15px/1.4 var(--font); color: var(--ink); background: var(--canvas); -webkit-font-smoothing: antialiased; }
button, input, select { font: inherit; color: inherit; }
button { cursor: pointer; border: 0; background: none; padding: 0; }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
[hidden] { display: none !important; }
.sh-app { display: flex; flex-direction: column; height: 100%; max-width: 680px; margin: 0 auto; }
.sh-head { flex: none; padding: 18px 14px 6px; }
.sh-head h1 { margin: 0; font-size: 26px; font-weight: 750; letter-spacing: -.02em; }
html.wz-hosted .sh-head h1 { display: none; }
.sh-line { margin: 2px 0 0; color: var(--ink-2); font-size: 14px; }
.sh-scroll { flex: 1; min-height: 0; overflow-y: auto; padding: 6px 14px 120px; }
.sh-card { background: var(--solid); border: 1px solid var(--line); border-radius: 16px; padding: 16px; margin: 6px 0 16px; }
.sh-card h2 { margin: 0 0 12px; font-size: 17px; }
.sh-field { display: flex; flex-direction: column; gap: 6px; margin-bottom: 12px; font-size: 13px; color: var(--ink-2); }
.sh-field select, .sh-input { width: 100%; min-height: 44px; padding: 0 12px; border-radius: 12px; border: 1px solid var(--line); background: var(--solid-2); color: var(--ink); }
.sh-input { margin-bottom: 10px; }
.sh-help { margin: 0 0 10px; color: var(--ink-2); font-size: 14px; }
.sh-primary { width: 100%; min-height: 48px; border-radius: 14px; background: var(--accent); color: var(--on-accent); font-weight: 700; }
.sh-primary:disabled { opacity: .55; cursor: progress; }
.sh-quiet { min-height: 40px; margin-top: 6px; padding: 0 12px; border-radius: 99px; border: 1px solid var(--line); background: var(--solid); color: var(--ink-2); font-size: 13px; }
.sh-room { margin: 14px 4px 6px; font-size: 13px; font-weight: 700; color: var(--ink-2); }
.sh-rows { list-style: none; margin: 0; padding: 0; background: var(--solid); border: 1px solid var(--line); border-radius: 16px; overflow: hidden; }
.sh-row { display: flex; align-items: center; gap: 12px; min-height: 54px; padding: 8px 14px; }
.sh-row + .sh-row { border-top: 1px solid var(--line); }
.sh-main { flex: 1; min-width: 0; }
.sh-main b { display: block; font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sh-main small { color: var(--ink-3); font-size: 13px; }
.sh-val { flex: none; color: var(--ink-2); font-size: 14px; }
.sh-sw { flex: none; width: 50px; height: 30px; border-radius: 99px; background: var(--solid-2); border: 1px solid var(--line); position: relative; }
.sh-sw::after { content: ""; position: absolute; top: 3px; left: 3px; width: 22px; height: 22px; border-radius: 50%; background: var(--ink-3); transition: transform .15s; }
.sh-sw[aria-pressed="true"] { background: var(--accent); border-color: var(--accent); }
.sh-sw[aria-pressed="true"]::after { transform: translateX(20px); background: var(--on-accent); }
.sh-sw:disabled { opacity: .5; }
.sh-dev + .sh-dev { border-top: 1px solid var(--line); }
.sh-dev summary { list-style: none; cursor: pointer; }
.sh-dev summary::-webkit-details-marker { display: none; }
.sh-dev summary.sh-nomore { cursor: default; }
.sh-more { list-style: none; margin: 0 0 6px; padding: 0 0 0 14px; }
.sh-more .sh-row { min-height: 44px; padding: 4px 14px 4px 0; font-size: 14px; color: var(--ink-2); }
.sh-more .sh-row + .sh-row { border-top: 1px solid var(--line); }
.sh-main small { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sh-empty { text-align: center; color: var(--ink-3); padding: 30px 0; }
.sh-foot { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin: 18px 0 0; font-size: 13px; color: var(--ink-3); }
.sh-foot a { display: inline-flex; align-items: center; min-height: 40px; margin-top: 6px; padding: 0 12px; border-radius: 99px; border: 1px solid var(--line); background: var(--solid); color: var(--ink-2); text-decoration: none; }
.sh-foot span { width: 100%; font-family: ui-monospace, Menlo, monospace; color: var(--ink-2); overflow-wrap: anywhere; }
.sh-status { flex: none; margin: 0; padding: 0 18px 8px; color: var(--danger); font-size: 13px; }
.sh-status:empty { padding: 0; }
.sh-status.ok { color: var(--ink-2); }
"""

PAGE_JS = r"""'use strict';
(() => {
  const el = id => document.getElementById(id);
  let st = null;
  const say = (t, ok) => { el('status').textContent = t || ''; el('status').className = 'sh-status' + (ok ? ' ok' : ''); };
  async function api(path, data) {
    const r = await fetch('api/' + path, data ? {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)} : {cache: 'no-store'});
    const b = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(b.error || 'Home Assistant is not reachable right now.');
    return b;
  }
  const node = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };
  function shown(d) {
    if (d.state === 'unavailable') return 'Offline';
    if (d.state === 'unknown') return '–';
    return d.unit ? d.state + ' ' + d.unit : String(d.state).replace(/_/g, ' ');
  }
  const KEY = /pm2\.?5|air quality|battery level|filter life|temperature|humidity|mode(?![\s\S])/i;
  const known = d => d.state !== 'unknown' && d.state !== 'unavailable' && !/^\s*\*/.test(short(d));
  function short(d) {
    let n = String(d.name || '');
    if (d.device && n.toLowerCase().startsWith(d.device.toLowerCase())) n = n.slice(d.device.length);
    n = n.trim().split(/\s+/).filter((w, i, a) => i === 0 || w.toLowerCase() !== a[i - 1].toLowerCase()).join(' ');
    return n || d.name;
  }
  function mainOf(items) {
    return items.find(d => d.kind === 'vacuum') || items.find(d => d.kind === 'fan') ||
      items.find(d => d.kind === 'switch' && /(power|switch status)(?![\s\S])/i.test(d.name)) || null;
  }
  function control(d) {
    if (d.toggle && (d.state === 'on' || d.state === 'off')) {
      const b = node('button', 'sh-sw'); b.type = 'button'; b.setAttribute('aria-pressed', String(d.state === 'on'));
      b.setAttribute('aria-label', d.name); b.onclick = e => { e.preventDefault(); flip(d, b); }; return b;
    }
    return node('span', 'sh-val', shown(d));
  }
  function paintDevices(list) {
    const rooms = new Map();
    for (const d of list) {
      const r = d.room || 'Other', k = d.device || d.name;
      if (!rooms.has(r)) rooms.set(r, new Map());
      const devs = rooms.get(r); if (!devs.has(k)) devs.set(k, []); devs.get(k).push(d);
    }
    const frag = []; let count = 0;
    for (const [room, devs] of rooms) {
      frag.push(node('h3', 'sh-room', room));
      const ul = node('ul', 'sh-rows');
      for (const [name, items] of devs) {
        count++;
        const main = mainOf(items), live = items.filter(known);
        const keys = live.filter(d => d !== main && d.kind === 'sensor' && KEY.test(d.name)).slice(0, 3);
        const li = node('li', 'sh-dev'); const det = node('details'); const sum = node('summary', 'sh-row');
        const m = node('div', 'sh-main'); m.appendChild(node('b', '', name));
        const label = k => { const n = short(k);
          if (/battery/i.test(n)) return 'Battery'; if (/pm2/i.test(n)) return 'PM2.5'; if (/air quality/i.test(n)) return 'Air';
          if (/filter/i.test(n)) return 'Filter'; if (/mop/i.test(n)) return 'Mop'; if (/brush/i.test(n)) return 'Brush';
          return n.replace(/^environment\s+/i, ''); };
        const sub = keys.map(k => label(k) + ' ' + shown(k)).join(' · ');
        if (sub) m.appendChild(node('small', '', sub));
        sum.appendChild(m);
        if (main) sum.appendChild(control(main)); else if (!live.length) sum.appendChild(node('span', 'sh-val', 'Offline'));
        det.appendChild(sum);
        const more = node('ul', 'sh-more');
        for (const d of live) { if (d === main) continue; const r = node('li', 'sh-row'); r.appendChild(node('span', 'sh-main', short(d))); r.appendChild(control(d)); more.appendChild(r); }
        if (more.children.length) det.appendChild(more); else sum.classList.add('sh-nomore');
        li.appendChild(det); ul.appendChild(li);
      }
      frag.push(ul);
    }
    el('list').replaceChildren(...frag);
    if (st && st.ready) el('line').textContent = count ? count + (count === 1 ? ' device' : ' devices') : 'Nothing connected yet';
    el('empty').hidden = list.length > 0 || !st || !st.ready || !el('connect').hidden;
  }
  async function flip(d, b) {
    b.disabled = true;
    try { const r = await api('control', {entity_id: d.entity_id, action: d.state === 'on' ? 'turn_off' : 'turn_on'}); d.state = r.state_now; b.setAttribute('aria-pressed', String(r.state_now === 'on')); say(''); }
    catch (e) { say(e.message); } finally { b.disabled = false; }
  }
  async function flip(d, b) {
    b.disabled = true;
    try { const r = await api('control', {entity_id: d.entity_id, action: d.state === 'on' ? 'turn_off' : 'turn_on'}); d.state = r.state_now; b.setAttribute('aria-pressed', String(r.state_now === 'on')); say(''); }
    catch (e) { say(e.message); } finally { b.disabled = false; }
  }
  async function load() {
    try {
      st = await api('status');
      el('connect').hidden = st.connected;
      el('foot').hidden = !st.connected;
      if (!st.connected) { el('line').textContent = 'Not connected'; el('list').replaceChildren(); return; }
      el('where').textContent = (st.home || 'Home') + ' · ' + st.address;
      if (!st.ready) { el('line').textContent = 'Can\u2019t reach it right now'; say(st.next); return; }
      say('');
      paintDevices((await api('devices')).devices);
    } catch (e) { say(e.message); }
  }
  el('go').onclick = async () => {
    el('go').disabled = true; say('Connecting…', true);
    try { const r = await api('connect', {url: el('url').value, token: el('token').value}); el('token').value = ''; say('Connected to ' + r.name + '.', true); await load(); }
    catch (e) { say(e.message); } finally { el('go').disabled = false; }
  };
  el('off').onclick = async () => {
    if (el('off').dataset.armed !== '1') { el('off').dataset.armed = '1'; el('off').textContent = 'Tap again to disconnect'; return; }
    el('off').dataset.armed = ''; el('off').textContent = 'Disconnect';
    try { await api('disconnect', {}); await load(); } catch (e) { say(e.message); }
  };
  window.WizardApp?.ready?.({name: 'Home Assistant'});
  document.addEventListener('visibilitychange', () => { if (!document.hidden) load(); });
  load();
})();
"""
