// Kickboard radar node — XIAO ESP32-C6 + Ai-Thinker RD-03D (PLAN.md §6)
//
// Dumb UART-to-UDP bridge: syncs on the RD-03D's 30-byte frames and
// forwards each one raw to the Pi at the sensor's native 10 Hz. Parsing
// lives in one place (the Pi). Also sends a JSON heartbeat every 5 s so
// the Pi can tell "radar dead" from "nobody here".
//
// Wiring, as Rob solders them (labels matched, not crossed):
//   RD-03D TX  -> XIAO D6 (GPIO16)   so the ESP *receives* on D6
//   RD-03D RX  -> XIAO D7 (GPIO17)   so the ESP *transmits* on D7
//   RD-03D VCC -> XIAO 5V            (the board is rated 5 V; UART is 3.3 V logic)
//   GND        -> GND
// Mount 1.3-1.5 m high at the end of the kitchen, boresight down the long
// axis (PLAN.md §6). Override pins per flash: RADAR_RX=D7 RADAR_TX=D6.

#include <ArduinoOTA.h>
#include <ESPmDNS.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#include "driver/gpio.h"

#include "wifi_credentials.h"

#define NODE_NAME "kick-radar"
#define PI_HOST "192.168.1.10"     // the Pi 5 — set your static address
#define PI_PORT 4049
#define RADAR_RX D6                // RD-03D TX -> this
#define RADAR_TX D7                // RD-03D RX -> this
#define RADAR_BAUD 256000
#define HEARTBEAT_MS 5000

#define FRAME_LEN 30
static const uint8_t FRAME_HEADER[4] = {0xAA, 0xFF, 0x03, 0x00};
static const uint8_t FRAME_TAIL[2] = {0x55, 0xCC};
// Multi-target mode command; ack frames start FD FC FB FA (PLAN.md §7)
static const uint8_t CMD_MULTI[] = {0xFD, 0xFC, 0xFB, 0xFA, 0x02, 0x00,
                                    0x90, 0x00, 0x04, 0x03, 0x02, 0x01};

WiFiUDP udp;
uint8_t buf[4 * FRAME_LEN];
size_t bufLen = 0;
uint32_t frames = 0, framesTotal = 0, resyncs = 0, bytesTotal = 0;
uint32_t lastHeartbeatMs = 0;
bool sawAck = false;

uint32_t lastByteMs = 0, uartRestarts = 0;

// Open (or re-open) the radar UART. Two C6 gotchas, both measured:
//  - D6/D7 (GPIO16/17) are UART0's console pins and the boot ROM leaves
//    GPIO16 as an OUTPUT; Serial1.begin() alone does not undo that, so the
//    radar's TX fought a driven-high pad. Reset both pads to plain GPIO.
//  - If the default 256 B RX buffer overflows (the radar streams ~600 B/s
//    while we block joining WiFi) reception never recovers. So: 4 KB buffer,
//    open only once WiFi is up, and end()/begin() if bytes ever stop.
void radarUartBegin() {
  Serial1.end();
  gpio_reset_pin((gpio_num_t) RADAR_RX);
  gpio_reset_pin((gpio_num_t) RADAR_TX);
  Serial1.setRxBufferSize(4096);
  Serial1.begin(RADAR_BAUD, SERIAL_8N1, RADAR_RX, RADAR_TX);
  bufLen = 0;
  lastByteMs = millis();
}

void setup() {
  Serial.begin(115200);

  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.printf("\n%s connecting to %s", NODE_NAME, WIFI_SSID);
  while (WiFi.status() != WL_CONNECTED) {
    delay(250);
    Serial.print(".");
  }
  Serial.printf("\nIP %s -> %s:%d\n", WiFi.localIP().toString().c_str(),
                PI_HOST, PI_PORT);

  MDNS.begin(NODE_NAME);
  ArduinoOTA.setHostname(NODE_NAME);
  ArduinoOTA.begin();

  radarUartBegin();
  // Switch the sensor to multi-target mode; the ack shows up in the
  // stream (FD FC FB FA header) and is logged, not forwarded.
  Serial1.write(CMD_MULTI, sizeof(CMD_MULTI));
  Serial.println("sent multi-target mode command");
}

void forwardFrame(const uint8_t* frame) {
  udp.beginPacket(PI_HOST, PI_PORT);
  udp.write(frame, FRAME_LEN);
  udp.endPacket();
  frames++;
  framesTotal++;
}

void pump() {
  while (Serial1.available() && bufLen < sizeof(buf)) {
    buf[bufLen++] = Serial1.read();
    bytesTotal++;
    lastByteMs = millis();
  }
  if (millis() - lastByteMs > 3000) {   // stalled: reopen the UART
    uartRestarts++;
    Serial.printf("radar uart stalled, restart %lu\n", uartRestarts);
    radarUartBegin();
    Serial1.write(CMD_MULTI, sizeof(CMD_MULTI));
  }
  size_t i = 0;
  while (bufLen - i >= FRAME_LEN) {
    if (memcmp(buf + i, FRAME_HEADER, 4) == 0) {
      if (memcmp(buf + i + FRAME_LEN - 2, FRAME_TAIL, 2) == 0) {
        forwardFrame(buf + i);
        i += FRAME_LEN;
        continue;
      }
      resyncs++;  // false header; fall through and skip one byte
    } else if (!sawAck && memcmp(buf + i, "\xFD\xFC\xFB\xFA", 4) == 0) {
      sawAck = true;
      Serial.println("mode command acked");
    }
    i++;
  }
  memmove(buf, buf + i, bufLen - i);
  bufLen -= i;
}

void heartbeat() {
  if (millis() - lastHeartbeatMs < HEARTBEAT_MS) return;
  char msg[160];
  snprintf(msg, sizeof(msg),
           "{\"hb\":1,\"node\":\"%s\",\"up_s\":%lu,\"rssi\":%d,"
           "\"frames\":%lu,\"fps\":%.1f,\"resyncs\":%lu,\"bytes\":%lu,"
           "\"uart_restarts\":%lu,\"ack\":%d}",
           NODE_NAME, millis() / 1000, WiFi.RSSI(), framesTotal,
           frames * 1000.0 / (millis() - lastHeartbeatMs), resyncs, bytesTotal,
           uartRestarts, sawAck);
  udp.beginPacket(PI_HOST, PI_PORT);
  udp.write((const uint8_t*)msg, strlen(msg));
  udp.endPacket();
  Serial.println(msg);
  frames = 0;
  lastHeartbeatMs = millis();
  if (!sawAck) Serial1.write(CMD_MULTI, sizeof(CMD_MULTI));   // retry until acked
}

void loop() {
  ArduinoOTA.handle();
  pump();
  heartbeat();
  if (WiFi.status() != WL_CONNECTED) {
    WiFi.reconnect();
    delay(500);
  }
}
