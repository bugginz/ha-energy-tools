"""MQTT / Home Assistant integration (PLAN.md §8.6).

MQTT discovery publishes, under one "Kickboard lighting" device:
  - switch    kickboard_enable       master enable
  - select    kickboard_mode         auto | manual
  - light     kickboard_manual       plain kickboard light (rgb + brightness)
  - number    sigma / peak / ambient / colour temperature   live tunables
  - sensor    target count
  - binary_sensor  occupancy         useful for other automations

Turning the manual light ON switches mode to manual (the strips become
ordinary kickboard lights, radar ignored); turning it OFF returns to
auto. Config changes take effect on the next rendered frame.
"""

from __future__ import annotations

import json
import logging
import threading

log = logging.getLogger("kickboard.ha")


class HaMqtt:
    def __init__(self, cfg, params):
        self.cfg = cfg.mqtt
        self.params = params
        self.base = str(self.cfg.base_topic)
        self.avail_topic = f"{self.base}/availability"
        self.state_topic = f"{self.base}/state"
        self._client = None
        self._lock = threading.Lock()
        self._last_state = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        import paho.mqtt.client as mqtt

        try:  # paho 2.x
            client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION1, client_id="kickboard")
        except AttributeError:  # paho 1.x
            client = mqtt.Client(client_id="kickboard")
        if self.cfg.username:
            client.username_pw_set(str(self.cfg.username), str(self.cfg.password))
        client.will_set(self.avail_topic, "offline", retain=True)
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.connect_async(str(self.cfg.host), int(self.cfg.port), keepalive=30)
        client.loop_start()
        self._client = client

    def stop(self) -> None:
        if self._client:
            self._client.publish(self.avail_topic, "offline", retain=True)
            self._client.loop_stop()
            self._client.disconnect()

    # -- discovery ---------------------------------------------------------

    def _device(self) -> dict:
        return {"identifiers": ["kickboard"], "name": "Kickboard lighting",
                "manufacturer": "bugginz", "model": "RD-03D + WS2812B"}

    def _discover(self) -> None:
        prefix = str(self.cfg.discovery_prefix)
        common = {"availability_topic": self.avail_topic,
                  "device": self._device()}

        def pub(component: str, key: str, payload: dict) -> None:
            payload.update(common)
            payload["unique_id"] = f"kickboard_{key}"
            topic = f"{prefix}/{component}/kickboard/{key}/config"
            self._client.publish(topic, json.dumps(payload), retain=True)

        pub("switch", "enable", {
            "name": "Kickboard enable",
            "command_topic": f"{self.base}/enable/set",
            "state_topic": self.state_topic,
            "value_template": "{{ 'ON' if value_json.enabled else 'OFF' }}"})
        pub("select", "mode", {
            "name": "Kickboard mode", "options": ["auto", "manual"],
            "command_topic": f"{self.base}/mode/set",
            "state_topic": self.state_topic,
            "value_template": "{{ value_json.mode }}"})
        pub("light", "manual", {
            "name": "Kickboard manual", "schema": "json",
            "command_topic": f"{self.base}/manual/set",
            "state_topic": f"{self.base}/manual/state",
            "brightness": True, "supported_color_modes": ["rgb"]})
        for key, name, lo, hi, step in (
                ("sigma_mm", "Kickboard pool width σ", 300, 800, 10),
                ("peak", "Kickboard pool peak", 0.0, 1.0, 0.05),
                ("ambient", "Kickboard ambient floor", 0.0, 0.05, 0.005),
                ("cct_k", "Kickboard colour temperature", 2200, 6500, 50)):
            pub("number", key, {
                "name": name, "min": lo, "max": hi, "step": step,
                "command_topic": f"{self.base}/{key}/set",
                "state_topic": self.state_topic,
                "value_template": "{{ value_json.%s }}" % key})
        pub("sensor", "targets", {
            "name": "Kickboard targets", "state_topic": self.state_topic,
            "value_template": "{{ value_json.target_count }}"})
        pub("binary_sensor", "occupancy", {
            "name": "Kickboard occupancy", "device_class": "occupancy",
            "state_topic": self.state_topic,
            "value_template": "{{ 'ON' if value_json.occupancy else 'OFF' }}"})

    def _on_connect(self, client, _userdata, _flags, rc, *_args) -> None:
        if rc != 0:
            log.warning("mqtt connect failed rc=%s", rc)
            return
        log.info("mqtt connected")
        client.publish(self.avail_topic, "online", retain=True)
        self._discover()
        client.subscribe(f"{self.base}/+/set")

    # -- commands ----------------------------------------------------------

    def _on_message(self, _client, _userdata, msg) -> None:
        key = msg.topic.split("/")[-2]
        text = msg.payload.decode(errors="replace").strip()
        p = self.params
        try:
            if key == "enable":
                p.enabled = text.upper() == "ON"
            elif key == "mode" and text in ("auto", "manual"):
                p.mode = text
            elif key == "manual":
                self._handle_light(json.loads(text))
            elif key == "sigma_mm":
                p.sigma_mm = max(50.0, float(text))
            elif key == "peak":
                p.peak = min(1.0, max(0.0, float(text)))
            elif key == "ambient":
                p.ambient = min(0.2, max(0.0, float(text)))
            elif key == "cct_k":
                p.set_cct(float(text))
            else:
                return
        except (ValueError, KeyError):
            log.warning("bad mqtt command %s: %r", msg.topic, text)
            return
        self.publish_light_state()
        self._last_state = None          # force a state republish

    def _handle_light(self, cmd: dict) -> None:
        p = self.params
        on = cmd.get("state", "").upper() == "ON"
        p.manual_on = on
        p.mode = "manual" if on else "auto"
        if "brightness" in cmd:
            p.manual_brightness = int(cmd["brightness"])
        color = cmd.get("color")
        if color and all(k in color for k in "rgb"):
            p.manual_rgb = (int(color["r"]), int(color["g"]), int(color["b"]))

    # -- state -------------------------------------------------------------

    def publish_light_state(self) -> None:
        if not self._client:
            return
        p = self.params
        r, g, b = p.manual_rgb
        self._client.publish(f"{self.base}/manual/state", json.dumps({
            "state": "ON" if p.manual_on else "OFF",
            "brightness": p.manual_brightness,
            "color_mode": "rgb", "color": {"r": r, "g": g, "b": b}}),
            retain=True)

    def publish_state(self, occupancy: bool, target_count: int,
                      est_ma: float) -> None:
        """Called from the render loop ~1 Hz; only publishes on change."""
        if not self._client:
            return
        p = self.params
        state = {"enabled": p.enabled, "mode": p.mode,
                 "sigma_mm": round(p.sigma_mm), "peak": round(p.peak, 2),
                 "ambient": round(p.ambient, 3), "cct_k": round(p.cct_k),
                 "occupancy": occupancy, "target_count": target_count,
                 "est_ma": round(est_ma)}
        with self._lock:
            if state == self._last_state:
                return
            self._last_state = state
        self._client.publish(self.state_topic, json.dumps(state), retain=True)
