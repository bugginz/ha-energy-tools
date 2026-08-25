// Live SoC display with anti-poisoning and LED brightness control.
//
// Serial protocol (57600, from the ESP32): "SSSPPPDBMLLL\n"
//   SSS = SoC 0-100, PPP = |net battery power| 0.1kW, D = C/D,
//   B = LED brightness '0'-'9', M = mode ('0' SoC walk, '1' Load+SoC),
//   LLL = house load in 0.1kW. Shorter legacy lines (7/8 chars) accepted.
//
// Mode 1 layout: load "HH.H" on tubes 1-3 (separator LED = decimal point,
// right separator physically masked), SoC on tubes 5-6 (or 4-6 at 100).
// LEDs: under load tubes green->red with rising load (0-5kW); under SoC
// tubes red->green with rising SoC.
//
// Display: SoC digits right-aligned within a 3-tube block that slowly
// ping-pongs across all six tubes (one step every 20s) so tube wear evens
// out. Every 10 minutes a ~2.6s slot-machine spin exercises every cathode.
// LED bar: ceil(SoC/25) segments, green/yellow/red; all six green at 100%.
#include <FastLED.h>
#define NUM_LEDS 6
CRGB leds[NUM_LEDS];
CLEDController *ledC;

const uint16_t TOP=511; const int16_t BT=320;
const uint8_t DPIN=A0,KPIN=A3,LPIN=A1,EN=A2,SEP=3;
const uint8_t ANODE[6]={10,8,7,6,5,4};       // tube1(left)..tube6(right)

const unsigned long WALK_STEP_MS = 5000;     // caterpillar: one digit-move every 5s
const unsigned long SPIN_EVERY_MS = 600000;  // slot spin every 10 min
const unsigned long SPIN_LEN_MS = 2600;
const unsigned long SPIN_FRAME_MS = 65;

uint8_t digitVal[6]={0xFF,0xFF,0xFF,0xFF,0xFF,0xFF};  // 0xFF = blank
int lastSoc=-1; uint8_t ledBright=255;
uint8_t dispMode=0; int lastLoad=0;          // load in 0.1kW
uint16_t tubeDwell=2800;                     // us per tube per refresh
int battDisT=0;                              // battery discharge, 0.1kW
uint8_t animMode=0; uint16_t animPhase=0;    // LED animation; 4 = live flow
int gridT=0, solarT=0; bool gridExport=false; // 0.1kW units
unsigned long lastAnimFrame=0;
uint8_t socDig[3]; uint8_t socLen=0;         // visible digits, no blanks
int8_t dpos[3];                              // tube position of each digit
int8_t wdir=-1;                              // -1 walking left, +1 right
int8_t mover=0;                              // round-robin: which digit steps next
unsigned long lastWalk=0, lastSpin=0, spinStart=0;
bool spinning=false;

char rxBuf[23]; uint8_t rxLen=0;

void pwmInit(){ICR1=TOP;OCR1A=0;TCCR1A=_BV(WGM11)|_BV(COM1A1);TCCR1B=_BV(WGM13)|_BV(WGM12)|_BV(CS10);DDRB|=_BV(DDB1);}
void shift16(uint16_t v){
  for(int8_t b=15;b>=0;b--){digitalWrite(DPIN,(v>>b)&1);
    digitalWrite(KPIN,HIGH);delayMicroseconds(2);digitalWrite(KPIN,LOW);delayMicroseconds(2);}
  digitalWrite(LPIN,HIGH);delayMicroseconds(2);digitalWrite(LPIN,LOW);
}

void render(){
  for(uint8_t i=0;i<6;i++) digitVal[i]=0xFF;
  for(uint8_t k=0;k<socLen;k++) digitVal[dpos[k]]=socDig[k];
}

void buildDigits(bool resetPos){
  uint8_t oldLen=socLen;
  if(lastSoc<0){ socLen=0; render(); return; }
  if(lastSoc>=100){ socLen=3; socDig[0]=1; socDig[1]=(lastSoc/10)%10; socDig[2]=lastSoc%10; }
  else if(lastSoc>=10){ socLen=2; socDig[0]=lastSoc/10; socDig[1]=lastSoc%10; }
  else { socLen=1; socDig[0]=lastSoc; }
  if(resetPos || socLen!=oldLen)
    for(uint8_t k=0;k<socLen;k++) dpos[k]=6-socLen+k;   // right-aligned
  render();
}

void layoutSoc(){ buildDigits(true); }   // demo/spin restore: right-aligned

void layoutMode1(){
  for(uint8_t i=0;i<6;i++) digitVal[i]=0xFF;
  int lo=lastLoad; if(lo>999) lo=999;
  digitVal[0]=(lo>=100)?lo/100:0xFF;      // tens of kW, blank-led
  digitVal[1]=(lo/10)%10;                 // units of kW (always shown: "0.4")
  digitVal[2]=lo%10;                      // tenths
  if(lastSoc>=100){ digitVal[3]=1; digitVal[4]=0; digitVal[5]=0; }
  else if(lastSoc>=0){
    digitVal[4]=(lastSoc>=10)?lastSoc/10:0xFF;
    digitVal[5]=lastSoc%10;
  }
}

void showMode1Leds(){
  // load tubes 1-3: hue 96 (green) at 0kW -> 0 (red) at 5kW
  int lo=lastLoad; if(lo>50) lo=50;
  CRGB loadCol; loadCol.setHSV(96-(96L*lo)/50,255,255);
  // SoC tubes: hue 0 (red) at 0% -> 96 (green) at 100%
  CRGB socCol; socCol.setHSV(lastSoc<0?0:(96L*lastSoc)/100,255,255);
  leds[0]=leds[1]=leds[2]=loadCol;
  leds[3]=(lastSoc>=100)?socCol:CRGB::Black;
  leds[4]=leds[5]=socCol;
  ledC->showLeds(ledBright);
}

bool canMove(int8_t k){
  if(wdir<0) return (k==0) ? (dpos[0]>0) : (dpos[k]-1>dpos[k-1]);
  return (k==socLen-1) ? (dpos[socLen-1]<5) : (dpos[k]+1<dpos[k+1]);
}

void walkStep(){
  // Ripple: one digit per tick, round-robin - leftmost steps, then its
  // neighbour into the gap, then the next, repeating until all arrive.
  if(socLen==0) return;
  for(uint8_t tries=0;tries<socLen;tries++){
    int8_t k=mover;
    mover=(wdir<0) ? (mover+1)%socLen : (mover+socLen-1)%socLen;
    if(canMove(k)){ dpos[k]+=wdir; render(); return; }
  }
  wdir=-wdir;                             // nobody can move: arrived, turn around
  mover=(wdir<0) ? 0 : socLen-1;
}

void showBar(){
  fill_solid(leds,NUM_LEDS,CRGB::Black);
  if(lastSoc>=100){ fill_solid(leds,NUM_LEDS,CRGB::Green); }
  else if(lastSoc>=0){
    uint8_t count=(lastSoc+24)/25; if(count==0) count=1;
    CRGB col=(count>=3)?CRGB::Green:(count==2)?CRGB::Yellow:CRGB::Red;
    for(uint8_t i=0;i<count&&i<NUM_LEDS;i++) leds[i]=col;
  }
  ledC->showLeds(ledBright);
}

void animFrame(){
  CRGB base=CRGB::Green;
  fill_solid(leds,NUM_LEDS,CRGB::Black);
  if(animMode==1){                        // traveling sine wave
    for(uint8_t i=0;i<NUM_LEDS;i++){
      uint8_t b=sin8((uint8_t)(animPhase>>2)-i*42);
      leds[i]=base; leds[i].nscale8(b<20?20:b);
    }
  } else if(animMode==2){                 // comet with fading tail
    uint8_t head=(animPhase>>5)%(NUM_LEDS+3);   // runs off the end
    for(uint8_t i=0;i<NUM_LEDS;i++){
      if(i>head) continue;
      uint8_t d=head-i;
      if(d<4){ leds[i]=base; leds[i].nscale8(255>>(2*d)); }
    }
  } else if(animMode==3){                 // fill and flush
    uint8_t level=(animPhase>>6)%(NUM_LEDS+2);
    for(uint8_t i=0;i<NUM_LEDS&&i<level;i++) leds[i]=base;
  } else if(animMode==4){                 // live supply-mix flow
    // shares of what's powering the house: solar / battery / grid import
    int sSol=solarT;
    int sBat=0; // battery share = discharge only
    // lastLoad etc available; battery discharge encoded via last packet dir:
    sBat=battDisT;
    int sGrid=gridExport?0:gridT;
    long tot=(long)sSol+sBat+sGrid;
    if(tot<=0){ fill_solid(leds,NUM_LEDS,CRGB::Black); ledC->showLeds(ledBright); animPhase+=8; return; }
    // allocate 6 LEDs proportionally, left to right: solar, battery, grid
    uint8_t nSol=(uint8_t)((6L*sSol+tot/2)/tot);
    uint8_t nBat=(uint8_t)((6L*sBat+tot/2)/tot);
    if(sSol>0&&nSol==0)nSol=1;
    if(sBat>0&&nBat==0)nBat=1;
    if(nSol+nBat>6){ if(nSol>nBat)nSol=6-nBat; else nBat=6-nSol; }
    uint8_t nGrid=(sGrid>0)?6-nSol-nBat:0;
    uint8_t idx=0;
    for(uint8_t k=0;k<nSol&&idx<6;k++) leds[idx++]=CRGB(255,180,0);   // solar yellow
    for(uint8_t k=0;k<nBat&&idx<6;k++) leds[idx++]=CRGB(0,255,0);     // battery green
    for(uint8_t k=0;k<nGrid&&idx<6;k++) leds[idx++]=CRGB(160,0,255);  // grid purple
    while(idx<6) leds[idx++]=CRGB::Black;
    // traveling brightness wave over the colored segments.
    // battery discharge dominant -> wave flows right-to-left (drawing from
    // the stored side); otherwise left-to-right (import/solar arriving).
    bool fromBattery = battDisT > (gridExport?0:gridT);
    for(uint8_t i=0;i<NUM_LEDS;i++){
      uint8_t ph=(uint8_t)(animPhase>>2);
      uint8_t b=sin8(fromBattery ? ph+i*42 : ph-i*42);
      leds[i].nscale8(b<70?70:b);
    }
    // speed scales with total power: crawl near zero, max at ~10kW
    ledC->showLeds(ledBright);
    long spd=2+(tot>100?100:tot)*22/100;   // 2..24 phase units per frame
    animPhase+=spd;
    return;
  }
  ledC->showLeds(ledBright);
  animPhase+=8;
}

void restoreLeds(){
  if(dispMode==1) showMode1Leds(); else showBar();
}

void applyPacket(const char* p, uint8_t len){
  for(uint8_t i=0;i<6;i++) if(p[i]<'0'||p[i]>'9') return;
  if(p[6]!='C'&&p[6]!='D') return;
  int soc=(p[0]-'0')*100+(p[1]-'0')*10+(p[2]-'0');
  if(soc>100) soc=100;
  lastSoc=soc;
  if(len>=8 && p[7]>='0' && p[7]<='9') ledBright=(p[7]-'0')*28;
  else ledBright=255;
  uint8_t newMode=(len>=9 && p[8]=='1')?1:0;
  if(len>=12) lastLoad=(p[9]-'0')*100+(p[10]-'0')*10+(p[11]-'0');
  int pw=(p[3]-'0')*100+(p[4]-'0')*10+(p[5]-'0');
  battDisT=(p[6]=='D')?pw:0;
  if(len>=19){
    gridExport=(p[12]=='E');
    gridT=(p[13]-'0')*100+(p[14]-'0')*10+(p[15]-'0');
    solarT=(p[16]-'0')*100+(p[17]-'0')*10+(p[18]-'0');
  }
  if(len>=20 && p[19]>='0' && p[19]<='9')
    tubeDwell=600+(uint16_t)(p[19]-'0')*250;   // 600..2850us
  if(len>=21 && p[20]>='0' && p[20]<='4'){
    uint8_t na=p[20]-'0';
    if(na!=animMode){ animMode=na; if(animMode==0) restoreLeds(); }
  }
  dispMode=newMode;
  digitalWrite(SEP, dispMode==1);         // decimal point on in mode 1
  if(dispMode==1) layoutMode1(); else buildDigits(false);
  if(animMode==0) restoreLeds();          // don't fight an active animation
}

void mux(unsigned long ms){
  unsigned long t0=millis();
  while(millis()-t0<ms){
    for(uint8_t i=0;i<6;i++){
      if(digitVal[i]>9) continue;
      shift16(1U<<digitVal[i]);
      digitalWrite(ANODE[i],HIGH);
      delayMicroseconds(tubeDwell);
      digitalWrite(ANODE[i],LOW);
      shift16(0);
      if(tubeDwell<2800) delayMicroseconds(2800-tubeDwell);
    }
  }
}

void runSweep(){
  uint8_t saved[6];
  for(uint8_t i=0;i<6;i++) saved[i]=digitVal[i];
  // 1: wave sweeps left->right, each tube counting up to 9 (staggered)
  for(int8_t ph=0; ph<=9+2*5; ph++){
    for(uint8_t i=0;i<6;i++){
      int8_t v=ph-2*i;
      digitVal[i] = (v<0) ? saved[i] : (v>9?9:v);
    }
    mux(150);
  }
  // 2: random decay 9->0, each tube at its own pace (~4-10s)
  uint8_t val[6]; unsigned long per[6], nxt[6];
  unsigned long t0=millis();
  for(uint8_t i=0;i<6;i++){ val[i]=9; per[i]=random(400,1100); nxt[i]=t0+per[i]; digitVal[i]=9; }
  bool busy=true;
  while(busy){
    busy=false;
    unsigned long now=millis();
    for(uint8_t i=0;i<6;i++){
      if(val[i]>0){
        busy=true;
        if(now>=nxt[i]){ val[i]--; nxt[i]+=per[i]; digitVal[i]=val[i]; }
      }
    }
    mux(30);
  }
  mux(400);
  // 3: blanks go blank; live digits grow 0 -> current value
  for(int8_t ph=0; ph<=9; ph++){
    for(uint8_t i=0;i<6;i++){
      digitVal[i] = (saved[i]>9) ? 0xFF : (ph<saved[i]?ph:saved[i]);
    }
    mux(80);
  }
  for(uint8_t i=0;i<6;i++) digitVal[i]=saved[i];
}

void runDemo(){
  // 1. slot spin, 3s
  unsigned long t0=millis();
  while(millis()-t0<3000){
    uint8_t frame=(millis()-t0)/SPIN_FRAME_MS;
    for(uint8_t i=0;i<6;i++) digitVal[i]=(frame+i)%10;
    mux(SPIN_FRAME_MS);
  }
  // 2. SoC sweep 0->100: digits + LED bar through every threshold
  int savedSoc=lastSoc;
  for(int s2=0;s2<=100;s2++){
    lastSoc=s2; layoutSoc(); showBar(); mux(45);
  }
  mux(1200);
  // 3. red base, blue dot bounce (the alarm pattern), 3s
  int8_t bi=0,bd=1;
  t0=millis();
  while(millis()-t0<3000){
    fill_solid(leds,NUM_LEDS,CRGB::Red);
    leds[bi]=CRGB::Blue;
    ledC->showLeds(ledBright);
    mux(160);
    bi+=bd; if(bi>=NUM_LEDS-1)bd=-1; if(bi<=0)bd=1;
  }
  // restore live state
  lastSoc=savedSoc; layoutSoc(); showBar();
}

void pollSerial(){
  while(Serial.available()){
    char c=Serial.read();
    if(c=='\n'){ rxBuf[rxLen]=0;
      if(rxLen==7||rxLen==8||rxLen==9||rxLen==12||rxLen==19||rxLen==20||rxLen==21) applyPacket(rxBuf,rxLen);
      else if(rxLen==1&&rxBuf[0]=='X') runDemo();
      else if(rxLen==1&&rxBuf[0]=='S') runSweep();
      else if(rxLen==2&&rxBuf[0]=='A'&&rxBuf[1]>='0'&&rxBuf[1]<='4'){
        animMode=rxBuf[1]-'0';
        if(animMode==0) restoreLeds();
      }
      rxLen=0; }
    else if(rxLen<22) rxBuf[rxLen++]=c;
    else rxLen=0;
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
  randomSeed(analogRead(A6)^micros());
  lastWalk=lastSpin=millis();
}

void loop(){
  pollSerial();
  unsigned long now=millis();

  if(now-lastSpin>=SPIN_EVERY_MS){ lastSpin=now; runSweep(); }
  else if(dispMode==0 && now-lastWalk>=WALK_STEP_MS){
    lastWalk=now;
    walkStep();
  }

  if(animMode>0 && now-lastAnimFrame>=40){ lastAnimFrame=now; animFrame(); }

  for(uint8_t i=0;i<6;i++){
    if(digitVal[i]>9) continue;
    shift16(1U<<digitVal[i]);
    digitalWrite(ANODE[i],HIGH);
    delayMicroseconds(tubeDwell);
    digitalWrite(ANODE[i],LOW);
    shift16(0);
    if(tubeDwell<2800) delayMicroseconds(2800-tubeDwell);
  }
  pollSerial();
}
