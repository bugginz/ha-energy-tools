// Live SoC display. Parses "SSSPPPD\n" over hardware UART (57600) from the ESP32.
// Digits 1-3 (left) = SoC%, leading-zero blanked. Digits 4-6 = |net power| in
// 0.1kW, always full 3 digits. Under-tube LEDs: green=charging, red=discharging.
#include <FastLED.h>
#define NUM_LEDS 6
CRGB leds[NUM_LEDS];
CLEDController *ledC;

const uint16_t TOP=511; const int16_t BT=280;
const uint8_t DPIN=A0,KPIN=A3,LPIN=A1,EN=A2,SEP=3;
const uint8_t ANODE[6]={10,8,7,6,5,4};       // tube1(left)..tube6(right)
uint8_t digitVal[6]={0xFF,0xFF,0xFF,0xFF,0xFF,0xFF};  // 0xFF = blank
bool charging=true;

char rxBuf[9]; uint8_t rxLen=0;

void pwmInit(){ICR1=TOP;OCR1A=0;TCCR1A=_BV(WGM11)|_BV(COM1A1);TCCR1B=_BV(WGM13)|_BV(WGM12)|_BV(CS10);DDRB|=_BV(DDB1);}
void shift16(uint16_t v){
  for(int8_t b=15;b>=0;b--){digitalWrite(DPIN,(v>>b)&1);
    digitalWrite(KPIN,HIGH);delayMicroseconds(2);digitalWrite(KPIN,LOW);delayMicroseconds(2);}
  digitalWrite(LPIN,HIGH);delayMicroseconds(2);digitalWrite(LPIN,LOW);
}

void applyPacket(const char* p){
  // expects exactly "SSSPPPD" (7 chars)
  for(uint8_t i=0;i<7;i++) if(i!=6 && (p[i]<'0'||p[i]>'9')) return;
  if(p[6]!='C' && p[6]!='D') return;
  int soc = (p[0]-'0')*100 + (p[1]-'0')*10 + (p[2]-'0');
  int pw  = (p[3]-'0')*100 + (p[4]-'0')*10 + (p[5]-'0');
  if(soc>100) soc=100;
  if(pw>999) pw=999;
  // SoC right-aligned on the rightmost three tubes; left three blank.
  digitVal[0] = 0xFF;
  digitVal[1] = 0xFF;
  digitVal[2] = 0xFF;
  digitVal[3] = (soc >= 100) ? (soc / 100) : 0xFF;
  digitVal[4] = (soc >= 10)  ? ((soc / 10) % 10) : 0xFF;
  digitVal[5] = soc % 10;
  charging = (p[6]=='C');

  // LED bar: ceil(soc/25) segments from the left. 100% = all six green.
  fill_solid(leds, NUM_LEDS, CRGB::Black);
  if (soc >= 100) {
    fill_solid(leds, NUM_LEDS, CRGB::Green);
  } else {
    uint8_t count = (soc + 24) / 25;
    if (count == 0) count = 1;              // 0% still warns
    CRGB col = (count >= 3) ? CRGB::Green
             : (count == 2) ? CRGB::Yellow
                            : CRGB::Red;
    for (uint8_t i = 0; i < count && i < NUM_LEDS; i++) leds[i] = col;
  }
  ledC->showLeds(255);
}

void pollSerial(){
  while(Serial.available()){
    char c = Serial.read();
    if(c=='\n'){ rxBuf[rxLen]=0; if(rxLen==7) applyPacket(rxBuf); rxLen=0; }
    else if(rxLen<8) rxBuf[rxLen++]=c;
    else rxLen=0;  // overflow guard, resync
  }
}

void setup(){
  Serial.begin(57600);
  pinMode(DPIN,OUTPUT);pinMode(KPIN,OUTPUT);pinMode(LPIN,OUTPUT);
  digitalWrite(DPIN,LOW);digitalWrite(KPIN,LOW);digitalWrite(LPIN,LOW);
  pinMode(EN,OUTPUT);digitalWrite(EN,LOW);
  pinMode(SEP,OUTPUT);digitalWrite(SEP,LOW);
  for(uint8_t i=0;i<6;i++){pinMode(ANODE[i],OUTPUT);digitalWrite(ANODE[i],LOW);}
  shift16(0);
  pwmInit(); for(int16_t d=0;d<=BT;d++){OCR1A=d;delay(20);}
  ledC=&FastLED.addLeds<WS2812,11,GRB>(leds,NUM_LEDS);
  fill_solid(leds,NUM_LEDS,CRGB::Black);
  ledC->showLeds(255);
}

void loop(){
  pollSerial();
  for(uint8_t i=0;i<6;i++){
    if(digitVal[i]>9) continue;   // blank tube: no anode strobe
    shift16(1U<<digitVal[i]);
    digitalWrite(ANODE[i],HIGH);
    delayMicroseconds(2800);
    digitalWrite(ANODE[i],LOW);
    shift16(0);
  }
  pollSerial();
}
