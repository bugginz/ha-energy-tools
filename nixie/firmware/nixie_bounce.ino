#include <FastLED.h>
#define NUM_LEDS 6
CRGB leds[NUM_LEDS];
CLEDController *c;
int8_t idx=0, dir=1;
void setup(){ c=&FastLED.addLeds<WS2812,11,GRB>(leds,NUM_LEDS); }
void loop(){
  fill_solid(leds,NUM_LEDS,CRGB::Blue);
  leds[idx]=CRGB::Yellow;
  c->showLeds(255);
  delay(180);
  idx+=dir;
  if(idx==NUM_LEDS-1) dir=-1;
  if(idx==0) dir=1;
}
