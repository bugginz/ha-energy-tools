// Calm static state: "123456" on tubes, LEDs off, separators off, no blinking.
#include <FastLED.h>
#define NUM_LEDS 6
CRGB leds[NUM_LEDS];
CLEDController *ledC;
const uint16_t TOP=511; const int16_t BT=280;
const uint8_t DPIN=A0,KPIN=A3,LPIN=A1,EN=A2,SEP=3;
const uint8_t ANODE[6]={10,8,7,6,5,4};
uint8_t digitVal[6]={1,2,3,4,5,6};
void pwmInit(){ICR1=TOP;OCR1A=0;TCCR1A=_BV(WGM11)|_BV(COM1A1);TCCR1B=_BV(WGM13)|_BV(WGM12)|_BV(CS10);DDRB|=_BV(DDB1);}
void shift16(uint16_t v){
  for(int8_t b=15;b>=0;b--){digitalWrite(DPIN,(v>>b)&1);
    digitalWrite(KPIN,HIGH);delayMicroseconds(2);digitalWrite(KPIN,LOW);delayMicroseconds(2);}
  digitalWrite(LPIN,HIGH);delayMicroseconds(2);digitalWrite(LPIN,LOW);
}
void setup(){
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
  for(uint8_t i=0;i<6;i++){
    if(digitVal[i]>9) continue;
    shift16(1U<<digitVal[i]);
    digitalWrite(ANODE[i],HIGH);
    delayMicroseconds(2800);
    digitalWrite(ANODE[i],LOW);
    shift16(0);
  }
}
