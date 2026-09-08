// Kickboard strip node — XIAO ESP32-C6 (PLAN.md §5)
//
// Dumb DDP-to-pixels bridge: listens on UDP 4048, copies payloads into the
// pixel buffer, shows on the push flag. All rendering happens on the Pi.
//
// Toolchain: arduino-esp32 core 3.x (the C6 needs 3.x), FastLED >= 3.9
// (RMT5 C6 support). Verify the sketch compiles for board "XIAO_ESP32C6"
// before wiring anything.
//
// Per-node config: set NODE_NAME (kick-left / kick-right) and NUM_LEDS
// below; copy wifi_credentials.h.example to wifi_credentials.h (gitignored).
//
// Hardware reminders (PLAN.md §3.3): 74AHCT125 level shifter on data,
// 330R series resistor at the strip DI pad, data run <= 1 m, power the
// XIAO from the strip's 5 V rail — never back-feed USB while it's on.

#include <ArduinoOTA.h>
#include <ESPmDNS.h>
#include <FastLED.h>
#include <WiFi.h>
#include <WiFiUdp.h>

#include "wifi_credentials.h"

#define NODE_NAME "kick-left"      // kick-left | kick-right
#define NUM_LEDS 240               // ASSUMED 60/m x 4 m — count them
#define DATA_PIN D1                // not D0/GPIO0 (strapping pin)
#define COLOR_ORDER GRB            // WS2812B
#define DDP_PORT 4048
#define MAX_MILLIAMPS 6000         // hardware-side backstop = PSU rating share
#define WATCHDOG_MS 2000           // no DDP this long -> fade out
#define FADE_MS 1000

CRGB leds[NUM_LEDS];
WiFiUDP udp;
uint8_t pkt[1500];

uint32_t lastPacketMs = 0;
uint32_t packets = 0, badPackets = 0, shows = 0;
uint8_t lastSeq = 0;
uint32_t seqGaps = 0;
uint32_t lastStatsMs = 0;
bool fading = false;

void setup() {
  Serial.begin(115200);
  FastLED.addLeds<WS2812B, DATA_PIN, COLOR_ORDER>(leds, NUM_LEDS);
  FastLED.setMaxPowerInVoltsAndMilliamps(5, MAX_MILLIAMPS);
  FastLED.clear(true);

  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);            // latency beats power draw here
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.printf("\n%s connecting to %s", NODE_NAME, WIFI_SSID);
  while (WiFi.status() != WL_CONNECTED) {
    delay(250);
    Serial.print(".");
  }
  Serial.printf("\nIP %s\n", WiFi.localIP().toString().c_str());

  MDNS.begin(NODE_NAME);           // kick-left.local
  ArduinoOTA.setHostname(NODE_NAME);
  ArduinoOTA.begin();              // never take the node out of the toe kick
  udp.begin(DDP_PORT);
  lastPacketMs = millis();
}

// DDP header: flags(1) seq(1) type(1) dest(1) offset(4 BE) len(2 BE)
void handlePacket(int size) {
  if (size < 10 || (pkt[0] & 0xC0) != 0x40) {  // version must be 1
    badPackets++;
    return;
  }
  uint8_t seq = pkt[1] & 0x0F;
  if (seq && lastSeq && seq != (lastSeq % 15) + 1) seqGaps++;
  if (seq) lastSeq = seq;

  uint32_t offset = ((uint32_t)pkt[4] << 24) | ((uint32_t)pkt[5] << 16) |
                    ((uint32_t)pkt[6] << 8) | pkt[7];
  uint16_t len = ((uint16_t)pkt[8] << 8) | pkt[9];
  if (10 + len > (uint32_t)size) {
    badPackets++;
    return;
  }
  uint8_t* dst = (uint8_t*)leds;
  uint32_t max_bytes = NUM_LEDS * 3;
  // Multi-packet frames (offset != 0) supported even though 240 LEDs fit
  // in one packet — costs nothing.
  for (uint16_t i = 0; i < len && offset + i < max_bytes; i++) {
    // wire is RGB order at these offsets; FastLED CRGB is r,g,b in memory
    dst[offset + i] = pkt[10 + i];
  }
  packets++;
  lastPacketMs = millis();
  fading = false;
  if (pkt[0] & 0x01) {  // push flag
    FastLED.show();
    shows++;
  }
}

void watchdog() {
  // A crashed Pi must not leave the kitchen lit: fade to black over
  // FADE_MS once WATCHDOG_MS passes with no DDP.
  uint32_t silent = millis() - lastPacketMs;
  if (silent < WATCHDOG_MS) return;
  if (!fading) {
    Serial.println("watchdog: no DDP, fading out");
    fading = true;
  }
  static uint32_t lastFadeMs = 0;
  if (millis() - lastFadeMs > FADE_MS / 20) {
    lastFadeMs = millis();
    fadeToBlackBy(leds, NUM_LEDS, 26);  // ~20 steps to black
    FastLED.show();
  }
}

void stats() {
  if (millis() - lastStatsMs < 5000) return;
  uint32_t dt = (millis() - lastStatsMs) / 1000;
  Serial.printf("pkts/s %lu  shows/s %lu  seq %u  gaps %lu  bad %lu  rssi %d\n",
                packets / dt, shows / dt, lastSeq, seqGaps, badPackets,
                WiFi.RSSI());
  packets = shows = 0;
  lastStatsMs = millis();
}

void loop() {
  ArduinoOTA.handle();
  int size = udp.parsePacket();
  if (size > 0) {
    int n = udp.read(pkt, sizeof(pkt));
    handlePacket(n);
  }
  watchdog();
  stats();
  if (WiFi.status() != WL_CONNECTED) {
    WiFi.reconnect();
    delay(500);
  }
}
