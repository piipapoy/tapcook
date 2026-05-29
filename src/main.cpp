#include <SPI.h>
#include <MFRC522.h>
#include <LiquidCrystal_I2C.h>
#include <WiFi.h>
#include <WebServer.h>
#include <DNSServer.h>
#include <Preferences.h>
#include <PubSubClient.h>
#include "config.h"

#define RST_PIN     22
#define SS_PIN      5
#define RELAY_PIN   2
#define ACS712_PIN  34

MFRC522 mfrc522(SS_PIN, RST_PIN);
LiquidCrystal_I2C lcd(0x27, 16, 2);

WiFiClient wifiClient;
PubSubClient mqtt(wifiClient);

bool relayState = false;
unsigned long lastPrompt = 0;
unsigned long cardCooldown = 0;
unsigned long lastMonitor = 0;

const float SENSITIVITY = 0.185;
float kalmanX = 0, kalmanP = 1, kalmanQ = 0.01, kalmanR = 0.1;

enum State { IDLE, WAITING_AUTH, SHOWING_REGISTER, SHOWING_RELAY_ON, SHOWING_BILL };
State state = IDLE;
unsigned long stateStart = 0;

String activeCardUID = "";
unsigned long lastMqttReconnect = 0;
bool mqttConnected = false;
unsigned long lastScroll = 0;
int scrollPos = 0;

String activeUserName = "";
unsigned long relayOnTime = 0;
float energyKWh = 0;
float totalCost = 0;
float tariff = 1444.7;
unsigned long lastEnergyCalc = 0;
unsigned long lastIdleRefresh = 0;

char topicCard[64];
char topicAuth[64];
char topicCmd[64];
char topicStatus[64];

Preferences prefs;

// --- Config Portal ---
WebServer server(80);
DNSServer dns;
bool configMode = false;
const char* apPassword = NULL;
const byte DNS_PORT = 53;

const char configPage[] PROGMEM = R"rawliteral(
<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TapCook - WiFi Setup</title>
<style>
  *{margin:0;padding:0;box-sizing:border-box}
  body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;background:linear-gradient(135deg,#0f172a 0,#1e293b 100%);display:flex;justify-content:center;align-items:center;min-height:100vh;padding:1rem}
  .card{background:#fff;padding:2rem;border-radius:16px;box-shadow:0 20px 60px rgba(0,0,0,.3);width:100%;max-width:420px}
  .logo{display:flex;align-items:center;gap:.6rem;margin-bottom:.25rem}
  .logo-icon{width:36px;height:36px;background:#2563eb;border-radius:10px;display:flex;align-items:center;justify-content:center;font-size:1.2rem}
  .logo h1{font-size:1.4rem;font-weight:700;color:#0f172a}
  .sub{color:#64748b;font-size:.85rem;margin-bottom:1.5rem;line-height:1.4}
  .search-box{display:flex;gap:.5rem;margin-bottom:.5rem}
  .search-box input{flex:1;padding:.75rem;border:2px solid #e2e8f0;border-radius:10px;font-size:.95rem;outline:0;transition:border-color .2s;background:#f8fafc}
  .search-box input:focus{border-color:#2563eb;background:#fff}
  .search-box input::placeholder{color:#94a3b8}
  .scan-btn{padding:.75rem 1rem;background:#f1f5f9;border:2px solid #e2e8f0;border-radius:10px;color:#475569;font-size:.85rem;font-weight:600;cursor:pointer;white-space:nowrap;transition:all .15s}
  .scan-btn:hover{background:#e2e8f0;border-color:#cbd5e1}
  .scan-btn:disabled{opacity:.6;cursor:wait}
  #ssid-list{margin-bottom:1rem;max-height:200px;overflow-y:auto;display:none;border:2px solid #e2e8f0;border-radius:10px;background:#f8fafc}
  #ssid-list .net-item{padding:.6rem .75rem;cursor:pointer;display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #e2e8f0;transition:background .1s}
  #ssid-list .net-item:last-child{border-bottom:none}
  #ssid-list .net-item:hover{background:#eff6ff}
  #ssid-list .net-item .net-name{font-size:.9rem;color:#0f172a;font-weight:500}
  #ssid-list .net-item .net-rssi{font-size:.75rem;color:#64748b}
  #ssid-list .net-item .net-rssi .bars{color:#2563eb;margin-right:2px}
  .field{margin-bottom:1rem}
  .field label{display:block;font-size:.8rem;font-weight:600;color:#475569;margin-bottom:.3rem;text-transform:uppercase;letter-spacing:.03em}
  .field .input-wrap{display:flex;align-items:center;border:2px solid #e2e8f0;border-radius:10px;background:#f8fafc;transition:border-color .2s}
  .field .input-wrap:focus-within{border-color:#2563eb;background:#fff}
  .field .input-wrap input{flex:1;padding:.75rem;border:none;border-radius:10px 0 0 10px;font-size:.95rem;outline:0;background:transparent}
  .field .input-wrap input::placeholder{color:#94a3b8}
  .field .input-wrap .toggle-pass{padding:.75rem .85rem;background:transparent;border:none;cursor:pointer;font-size:1.1rem;color:#94a3b8;line-height:1;transition:color .15s}
  .field .input-wrap .toggle-pass:hover{color:#475569}
  .save-btn{width:100%;padding:.85rem;background:#2563eb;color:#fff;border:none;border-radius:10px;font-size:1rem;font-weight:700;cursor:pointer;transition:background .15s;margin-top:.25rem}
  .save-btn:hover{background:#1d4ed8}
  .save-btn:disabled{background:#93c5fd;cursor:wait}
  #status{margin-top:1rem;padding:.75rem 1rem;border-radius:10px;display:none;font-size:.9rem;font-weight:500;text-align:center}
  #status.error{background:#fef2f2;color:#dc2626;border:1px solid #fecaca;display:block}
  #status.ok{background:#f0fdf4;color:#16a34a;border:1px solid #bbf7d0;display:block}
  #status.loading{background:#eff6ff;color:#2563eb;border:1px solid #bfdbfe;display:block}
  .dots{display:inline-block;animation:dotAnim 1.4s steps(4) infinite}
  @keyframes dotAnim{0%{content:''}25%{content:'.'}50%{content:'..'}75%{content:'...'}}
  .wifi-icon{display:inline-block;margin-right:4px;font-size:1rem}
  .empty{text-align:center;color:#94a3b8;padding:1rem;font-size:.85rem}
  @keyframes spin{0%{transform:rotate(0)}100%{transform:rotate(360deg)}}
  .spinner{display:inline-block;width:16px;height:16px;border:2px solid #e2e8f0;border-top-color:#2563eb;border-radius:50%;animation:spin .7s linear infinite;vertical-align:middle;margin-right:4px}
</style>
</head>
<body>
<div class="card">
  <div class="logo">
    <div class="logo-icon">🍳</div>
    <h1>TapCook</h1>
  </div>
  <p class="sub">Hubungkan kompor pintar kamu ke WiFi rumah. Pilih jaringan di bawah, masukkan password, lalu simpan.</p>
  <div class="search-box">
    <input type="text" id="ssid" placeholder="Cari atau ketik SSID" autocomplete="off">
    <button class="scan-btn" id="scanBtn" onclick="scan()">Scan</button>
  </div>
  <div id="ssid-list"></div>
  <div class="field">
    <label>Password WiFi</label>
    <div class="input-wrap">
      <input type="password" id="pass" placeholder="Kosongkan jika tidak ada" autocomplete="off">
      <button class="toggle-pass" id="togglePass" onclick="togglePass()" tabindex="-1" aria-label="Tampilkan password">
        <svg id="eyeIcon" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/>
          <circle cx="12" cy="12" r="3"/>
        </svg>
      </button>
    </div>
  </div>
  <button class="save-btn" id="saveBtn" onclick="save()">Simpan &amp; Hubungkan</button>
  <div id="status"></div>
</div>
<script>
  let scanning=false;
  const eyeOpen='<svg id="eyeIcon" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>';
  const eyeOff='<svg id="eyeIcon" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>';
  function togglePass(){
    const p=document.getElementById('pass');
    const icon=document.getElementById('eyeIcon');
    if(p.type==='password'){p.type='text';icon.outerHTML=eyeOff}
    else{p.type='password';icon.outerHTML=eyeOpen}
  }
  function scan(){
    if(scanning)return;scanning=true;
    const btn=document.getElementById('scanBtn');btn.disabled=true;btn.innerHTML='<span class="spinner"></span>';
    const list=document.getElementById('ssid-list');
    list.innerHTML='<div class="empty" style="color:#94a3b8">Memindai jaringan...</div>';
    list.style.display='block';
    fetch('/scan').then(r=>r.json()).then(nets=>{
      list.innerHTML='';
      if(nets.length===0){
        list.innerHTML='<div class="empty" style="color:#94a3b8">Tidak ada jaringan ditemukan</div>';
        btn.disabled=false;btn.textContent='Scan';return
      }
      nets.sort((a,b)=>b.rssi-a.rssi);
      let topRssi=nets[0].rssi;
      nets.forEach(n=>{
        let bars='',barCount=0;
        if(n.rssi>-50)barCount=4;else if(n.rssi>-65)barCount=3;else if(n.rssi>-80)barCount=2;else barCount=1;
        for(let i=0;i<4;i++)bars+=i<barCount?'█':'░';
        const d=document.createElement('div');d.className='net-item';
        d.innerHTML='<span class="net-name"><span class="wifi-icon">📶</span>'+escHtml(n.ssid)+'</span><span class="net-rssi"><span class="bars">'+bars+'</span> '+n.rssi+'dBm</span>';
        d.onclick=()=>{document.getElementById('ssid').value=n.ssid;list.style.display='none'};
        list.appendChild(d);
      });
      list.style.display='block';
    }).catch(()=>{
      list.innerHTML='<div class="empty" style="color:#dc2626">Gagal memindai. Coba lagi.</div>';
    }).finally(()=>{scanning=false;btn.disabled=false;btn.textContent='Scan'});
  }
  function escHtml(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML}
  function save(){
    const ssid=document.getElementById('ssid').value.trim();
    const pass=document.getElementById('pass').value;
    if(!ssid){showStatus('Masukkan nama WiFi terlebih dahulu','error');return}
    const btn=document.getElementById('saveBtn');btn.disabled=true;btn.textContent='Menghubungkan...';
    showStatus('Mencoba menghubungkan ke <strong>'+escHtml(ssid)+'</strong>...','loading');
    fetch('/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ssid:ssid,pass:pass})
    }).then(r=>r.json()).then(d=>{
      if(d.ok){showStatus('Berhasil! Kompor akan restart...','ok');btn.textContent='Berhasil ✓';setTimeout(()=>location.reload(),3000)}
      else{showStatus('Gagal: '+d.msg,'error');btn.disabled=false;btn.textContent='Simpan & Hubungkan'}
    }).catch(()=>{showStatus('Gagal terhubung ke kompor. Coba lagi.','error');btn.disabled=false;btn.textContent='Simpan & Hubungkan'});
  }
  function showStatus(msg,type){
    const el=document.getElementById('status');
    el.innerHTML=msg;el.className=type;
    el.style.display='block';
  }
</script>
</body>
</html>
)rawliteral";

// --- Kalman ---
void kalmanInit() {
  kalmanX = 0; kalmanP = 1;
}

float kalmanUpdate(float measurement) {
  kalmanP += kalmanQ;
  float k = kalmanP / (kalmanP + kalmanR);
  kalmanX += k * (measurement - kalmanX);
  kalmanP *= (1 - k);
  return kalmanX;
}

// --- Helpers ---
String uidToString() {
  String res = "";
  for (byte i = 0; i < mfrc522.uid.size; i++) {
    if (mfrc522.uid.uidByte[i] < 0x10) res += "0";
    res += String(mfrc522.uid.uidByte[i], HEX);
    if (i < mfrc522.uid.size - 1) res += " ";
  }
  res.toUpperCase();
  return res;
}

float readCurrentAC() {
  int minRaw = 4095, maxRaw = 0;
  unsigned long tStart = millis();
  while (millis() - tStart < 100) {
    int sum = 0;
    for (int j = 0; j < 10; j++) sum += analogRead(ACS712_PIN);
    int avg = sum / 10;
    if (avg < minRaw) minRaw = avg;
    if (avg > maxRaw) maxRaw = avg;
    delay(1);
  }
  int vppRaw = maxRaw - minRaw;
  if (vppRaw < 20) return 0;
  float vpp = (vppRaw / 4095.0) * 3.3;
  float iPeak = vpp / SENSITIVITY;
  float iRms = iPeak / (2.0 * 1.414);
  if (iRms < 0.02) return 0;
  return kalmanUpdate(iRms);
}

void calibrateACS712() {
  lcd.clear(); lcd.setCursor(0, 0); lcd.print("Kalibrasi ACS...");
  Serial.println("\n>> Kalibrasi ACS712");
  float sumV = 0;
  for (int i = 0; i < 200; i++) {
    int r = analogRead(ACS712_PIN);
    sumV += (r / 4095.0) * 3.3;
    delay(2);
  }
  float offset = sumV / 200.0;
  Serial.print(">> Offset DC: "); Serial.print(offset, 3); Serial.println("V");
  lcd.clear(); lcd.setCursor(0, 0); lcd.print("Kalibrasi OK");
  lcd.setCursor(0, 1); lcd.print("Offset: "); lcd.print(offset, 2); lcd.print("V");
  delay(1000);
}

void showIdle() {
  lcd.clear();
  lcd.setCursor(0, 0); lcd.print("  TAPCOOK v1.0");
  lcd.setCursor(0, 1); lcd.print("  Tempelkan RFID");
}

void lcdScrollLine(int row, const String& text, int pos) {
  int len = text.length();
  lcd.setCursor(0, row);
  if (len <= 16) {
    int pad = (16 - len) / 2;
    for (int i = 0; i < pad; i++) lcd.print(' ');
    lcd.print(text);
    return;
  }
  String buf = text + "   " + text.substring(0, 3);
  lcd.print(buf.substring(pos, pos + 16));
}

// --- WiFi Storage ---
bool loadWiFi(String &ssid, String &pass) {
  prefs.begin("tapcook", true);
  ssid = prefs.getString("ssid", "");
  pass = prefs.getString("pass", "");
  prefs.end();
  return ssid.length() > 0;
}

void saveWiFi(String ssid, String pass) {
  prefs.begin("tapcook", false);
  prefs.putString("ssid", ssid);
  prefs.putString("pass", pass);
  prefs.end();
}

String extractName(String msg) {
  int s = msg.indexOf("\"name\": \"");
  if (s < 0) {
    s = msg.indexOf("\"name\":\"");
    if (s < 0) return "";
    s += 8;
  } else {
    s += 9;
  }
  int e = msg.indexOf("\"", s);
  if (e < 0) return "";
  return msg.substring(s, e);
}

float loadTariff() {
  prefs.begin("tapcook", true);
  float t = prefs.getFloat("tariff", 1444.7f);
  prefs.end();
  return t;
}

void saveTariff(float t) {
  prefs.begin("tapcook", false);
  prefs.putFloat("tariff", t);
  prefs.end();
}

// --- MQTT ---
String extractUid(String msg) {
  int s = msg.indexOf("\"uid\": \"");
  if (s < 0) {
    s = msg.indexOf("\"uid\":\"");
    if (s < 0) return "";
    s += 7;
  } else {
    s += 8;
  }
  int e = msg.indexOf("\"", s);
  if (e < 0) return "";
  return msg.substring(s, e);
}

void publishRelayState() {
  String state = relayState ? "true" : "false";
  String payload = "{\"relay\":" + state + "}";
  mqtt.publish(topicStatus, payload.c_str());
  Serial.print(">> Status relay: "); Serial.println(payload);
}

void mqttCallback(char* topic, byte* payload, unsigned int length) {
  String msg;
  for (unsigned int i = 0; i < length; i++) msg += (char)payload[i];
  Serial.print(">> MQTT datang: "); Serial.println(msg);

  String cardUid = extractUid(msg);
  String name = extractName(msg);

  if (msg.indexOf("\"ok\"") > 0) {
    if (relayState && cardUid != activeCardUID) {
      lcd.clear();
      lcd.setCursor(0, 0); lcd.print("  Kompor ON!");
      lcd.setCursor(0, 1); lcd.print("Tap kartu yg sama");
      state = SHOWING_RELAY_ON;
      stateStart = millis();
      return;
    }
    relayState = !relayState;
    digitalWrite(RELAY_PIN, relayState ? HIGH : LOW);
    publishRelayState();
    kalmanInit();

    if (relayState) {
      activeCardUID = cardUid;
      activeUserName = name;
      relayOnTime = millis();
      energyKWh = 0;
      totalCost = 0;
      lastEnergyCalc = millis();
      lcd.clear();
      lcd.setCursor(0, 0); lcd.print(activeUserName.substring(0, 16));
      lcd.setCursor(0, 1); lcd.print("Daya:    0W 00:00");
      state = IDLE;
    } else {
      activeCardUID = "";
      lcd.clear();
      lcd.setCursor(0, 0); lcd.print("Energi: "); lcd.print(energyKWh, 2); lcd.print(" kWh");
      lcd.setCursor(0, 1); lcd.print("Biaya: Rp"); lcd.print((int)totalCost);
      state = SHOWING_BILL;
      stateStart = millis();
    }
    Serial.print("relay: "); Serial.println(relayState ? "ON" : "OFF");
  } else if (msg.indexOf("\"unknown\"") > 0) {
    if (relayState) {
      lcd.clear();
      lcd.setCursor(0, 0); lcd.print("  Kompor ON!");
      lcd.setCursor(0, 1); lcd.print("Tap kartu yg sama");
      state = SHOWING_RELAY_ON;
    } else {
      lcd.clear();
      lcd.setCursor(0, 0); lcd.print("tinyurl.com/");
      lcd.setCursor(0, 1); lcd.print("  tc-regis  ");
      state = SHOWING_REGISTER;
    }
    stateStart = millis();
  } else if (msg.indexOf("\"reset_wifi\"") > 0) {
    lcd.clear();
    lcd.setCursor(0, 0); lcd.print("  Reset WiFi...");
    prefs.begin("tapcook", false);
    prefs.remove("ssid");
    prefs.remove("pass");
    prefs.end();
    delay(2000);
    ESP.restart();
  } else if (msg.indexOf("\"relay_on\"") > 0) {
    relayState = true;
    digitalWrite(RELAY_PIN, HIGH);
    activeCardUID = "";
    activeUserName = "Admin";
    relayOnTime = millis();
    energyKWh = 0;
    totalCost = 0;
    lastEnergyCalc = millis();
    publishRelayState();
    kalmanInit();
    lcd.clear();
    lcd.setCursor(0, 0); lcd.print("Admin");
    lcd.setCursor(0, 1); lcd.print("Daya:    0W 00:00");
    Serial.println(">> Relay ON via web");
    state = IDLE;
  } else if (msg.indexOf("\"relay_off\"") > 0) {
    relayState = false;
    digitalWrite(RELAY_PIN, LOW);
    activeCardUID = "";
    activeUserName = "";
    energyKWh = 0;
    totalCost = 0;
    publishRelayState();
    lcd.clear();
    lcd.setCursor(0, 0); lcd.print("Relay: OFF");
    lcd.setCursor(0, 1); lcd.print("Arus: 0.00A ");
    Serial.println(">> Relay OFF via web");
    state = IDLE;
  } else if (msg.indexOf("\"set_tariff\"") > 0) {
    int s = msg.indexOf("\"value\":");
    if (s > 0) {
      s += 8;
      while (s < (int)msg.length() && (msg[s] == ' ' || msg[s] == '\t')) s++;
      int e = s;
      while (e < (int)msg.length() && msg[e] != ',' && msg[e] != '}' && msg[e] != ']') e++;
      tariff = msg.substring(s, e).toFloat();
      saveTariff(tariff);
      Serial.print(">> Tarif diubah: Rp"); Serial.println(tariff);
    }
    lcd.clear();
    lcd.setCursor(0, 0); lcd.print("  Tarif OK!");
    lcd.setCursor(0, 1); lcd.print("Rp"); lcd.print(tariff, 0); lcd.print("/kWh");
    delay(1500);
    if (relayState) {
      lcd.clear();
      lcd.setCursor(0, 0); lcd.print(activeUserName.substring(0, 16));
      lcd.setCursor(0, 1); lcd.print("Daya:    0W 00:00");
    } else {
      showIdle();
    }
  }
}

bool mqttReconnect() {
  if (!mqtt.connected()) {
    Serial.print(">> MQTT reconnect...");
    if (mqtt.connect(DEVICE_ID)) {
      Serial.println(" OK");
      mqtt.subscribe(topicAuth);
      mqtt.subscribe(topicCmd);
      publishRelayState();
      mqttConnected = true;
    } else {
      Serial.print(" GAGAL("); Serial.print(mqtt.state()); Serial.println(")");
      mqttConnected = false;
    }
  }
  return mqtt.connected();
}

// --- Config Portal ---
void handleRoot() {
  server.send_P(200, "text/html", configPage);
}

void handleScan() {
  int n = WiFi.scanNetworks();
  String json = "[";
  bool first = true;
  for (int i = 0; i < n; i++) {
    String ssid = WiFi.SSID(i);
    if (ssid.length() == 0) continue;
    bool dup = false;
    for (int j = 0; j < i; j++) {
      if (WiFi.SSID(j) == ssid && WiFi.RSSI(j) >= WiFi.RSSI(i)) {
        dup = true; break;
      }
    }
    if (dup) continue;
    if (!first) json += ",";
    first = false;
    json += "{\"ssid\":\"" + ssid + "\",\"rssi\":" + String(WiFi.RSSI(i)) + "}";
  }
  json += "]";
  server.send(200, "application/json", json);
  WiFi.scanDelete();
}

void handleSave() {
  String body = server.arg("plain");
  int s1 = body.indexOf("\"ssid\":\"") + 8;
  int s2 = body.indexOf("\",\"pass\"");
  int p1 = body.indexOf("\"pass\":\"") + 8;
  int p2 = body.lastIndexOf("\"}");
  if (s1 < 8 || p1 < 8) {
    server.send(400, "application/json", "{\"ok\":false,\"msg\":\"Format salah\"}");
    return;
  }
  String ssid = body.substring(s1, s2);
  String pass = body.substring(p1, p2);

  ssid.replace("\\\"", "\""); ssid.replace("\\\\", "\\");
  pass.replace("\\\"", "\""); pass.replace("\\\\", "\\");

    lcd.clear();
    lcd.setCursor(0, 0); lcd.print("  Mencoba WiFi...");
    int ssidPad = (16 - ssid.length()) / 2;
    if (ssidPad < 0) ssidPad = 0;
    lcd.setCursor(ssidPad, 1); lcd.print(ssid);

  WiFi.begin(ssid.c_str(), pass.c_str());
  int tries = 0;
  while (WiFi.status() != WL_CONNECTED && tries < 30) {
    delay(500);
    tries++;
    server.handleClient();
    dns.processNextRequest();
  }

  if (WiFi.status() == WL_CONNECTED) {
    saveWiFi(ssid, pass);
    lcd.clear();
    lcd.setCursor(0, 0); lcd.print("  WiFi OK!");
    lcd.setCursor(0, 1); lcd.print(WiFi.localIP().toString());
    server.send(200, "application/json", "{\"ok\":true}");
    delay(2000);
    ESP.restart();
  } else {
    WiFi.disconnect(true);
    server.send(200, "application/json", "{\"ok\":false,\"msg\":\"Tidak bisa connect. Cek SSID/Password\"}");
  }
}

void handleNotFound() {
  server.send_P(200, "text/html", configPage);
}

void startConfigMode() {
  configMode = true;

  String apName = "TAPCOOK-" + String((uint32_t)ESP.getEfuseMac() % 10000);

  WiFi.mode(WIFI_AP);
  WiFi.softAP(apName.c_str(), apPassword);

  lcd.clear();
  lcd.setCursor(0, 0); lcd.print("  Hubungkan ke:");
  int apPad = (16 - apName.length()) / 2;
  lcd.setCursor(apPad, 1); lcd.print(apName);

  Serial.println("\n=== CONFIG MODE ===");
  Serial.print("AP Name: "); Serial.println(apName);
  Serial.print("AP IP: "); Serial.println(WiFi.softAPIP());

  dns.start(DNS_PORT, "*", WiFi.softAPIP());

  server.on("/", handleRoot);
  server.on("/scan", handleScan);
  server.on("/save", HTTP_POST, handleSave);
  server.onNotFound(handleNotFound);
  server.begin();

  int lastLcdToggle = 0;
  while (configMode) {
    dns.processNextRequest();
    server.handleClient();
    if (millis() - lastLcdToggle > 5000) {
      lastLcdToggle = millis();
      static bool toggle = false;
      toggle = !toggle;
      lcd.clear();
      if (toggle) {
        lcd.setCursor(0, 0); lcd.print("  Hubungkan ke:");
        int apPad = (16 - apName.length()) / 2;
        lcd.setCursor(apPad, 1); lcd.print(apName);
      } else {
        lcd.setCursor(0, 0); lcd.print("  Buka browser:");
        String ip = WiFi.softAPIP().toString();
        int ipPad = (16 - ip.length()) / 2;
        lcd.setCursor(ipPad, 1); lcd.print(ip);
      }
    }
    delay(10);
  }
}

// --- Normal Operation ---
void connectWiFi(String ssid, String pass) {
  Serial.print(">> WiFi: "); Serial.print(ssid);
  WiFi.mode(WIFI_STA);
  WiFi.begin(ssid.c_str(), pass.c_str());
  int tries = 0;
  while (WiFi.status() != WL_CONNECTED && tries < 40) {
    delay(500); Serial.print("."); tries++;
  }
  if (WiFi.status() == WL_CONNECTED) {
    Serial.println(" OK"); Serial.print("   IP: "); Serial.println(WiFi.localIP());
  } else {
    Serial.println(" GAGAL");
  }
}

void setup() {
  Serial.begin(115200); delay(500);
  Serial.println("\n=== TAPCOOK v1.0 ===");

  pinMode(RELAY_PIN, OUTPUT); digitalWrite(RELAY_PIN, LOW);
  Serial.println("Relay OK");

  Wire.begin(21, 15);
  lcd.init(); lcd.backlight();
  lcd.setCursor(0, 0); lcd.print("  TAPCOOK v1.0");
  Serial.println("LCD OK");

  SPI.begin(18, 19, 23, 5);
  mfrc522.PCD_Init();
  Serial.println("RFID OK");

  pinMode(ACS712_PIN, INPUT);

  snprintf(topicCard, sizeof(topicCard), "tapcook/%s/card", DEVICE_ID);
  snprintf(topicAuth, sizeof(topicAuth), "tapcook/%s/auth", DEVICE_ID);
  snprintf(topicCmd, sizeof(topicCmd), "tapcook/%s/cmd", DEVICE_ID);
  snprintf(topicStatus, sizeof(topicStatus), "tapcook/%s/status", DEVICE_ID);

  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setCallback(mqttCallback);

  String savedSSID, savedPass;
  bool hasCreds = loadWiFi(savedSSID, savedPass);

  if (!hasCreds) {
    Serial.println(">> Tidak ada WiFi tersimpan -> Config Mode");
    startConfigMode();
    return;
  }

  lcd.clear(); lcd.setCursor(0, 0); lcd.print("Menghubungkan...");
  connectWiFi(savedSSID, savedPass);

  if (WiFi.status() != WL_CONNECTED) {
    Serial.println(">> WiFi gagal -> Config Mode");
    lcd.clear();
    lcd.setCursor(0, 0); lcd.print("  WiFi Gagal");
    lcd.setCursor(0, 1); lcd.print("  Mode Config...");
    delay(2000);
    startConfigMode();
    return;
  }

  mqttReconnect();
  tariff = loadTariff();
  Serial.print(">> Tarif: Rp"); Serial.println(tariff);
  calibrateACS712();
  showIdle();

  Serial.println("\nSistem siap.");
  lastPrompt = millis();
}

void loop() {
  if (configMode) {
    dns.processNextRequest();
    server.handleClient();
    return;
  }

  if (WiFi.status() != WL_CONNECTED) {
    lcd.clear();
    lcd.setCursor(0, 0); lcd.print("  TIDAK ADA");
    lcd.setCursor(0, 1); lcd.print("  KONEKSI WiFi");
    delay(3000);
    ESP.restart();
  }

  if (!mqtt.connected()) {
    if (millis() - lastMqttReconnect > 5000) {
      mqttReconnect();
      lastMqttReconnect = millis();
    }
  }
  mqtt.loop();

  if (millis() - lastPrompt >= 5000) {
    float c = readCurrentAC();
    int r = analogRead(ACS712_PIN);
    float v = (r / 4095.0) * 3.3;
    Serial.print("--- tap kartu sekarang --- raw:"); Serial.print(r);
    Serial.print(" "); Serial.print(v, 3); Serial.print("V arusAC:"); Serial.print(c, 3); Serial.println("A");
    lastPrompt = millis();
  }

  unsigned long now = millis();
  if (state == IDLE && !relayState && now - lastIdleRefresh > 60000 && WiFi.status() == WL_CONNECTED) {
    showIdle();
    lastIdleRefresh = now;
  }

  if (state == SHOWING_REGISTER && millis() - stateStart >= 10000) {
    showIdle();
    state = IDLE;
  }

  if (state == SHOWING_RELAY_ON && millis() - stateStart >= 3000) {
    showIdle();
    state = IDLE;
  }

  if (state == SHOWING_BILL && millis() - stateStart >= 8000) {
    activeUserName = "";
    showIdle();
    state = IDLE;
  }

  if (state == WAITING_AUTH && millis() - stateStart >= 5000) {
    lcd.clear();
    lcd.setCursor(0, 0); lcd.print("  WAKTU HABIS");
    lcd.setCursor(0, 1); lcd.print("Coba lagi nanti");
    delay(1500);
    showIdle();
    state = IDLE;
  }

  if (millis() - cardCooldown < 500) return;

  if (mfrc522.PICC_IsNewCardPresent() && mfrc522.PICC_ReadCardSerial()) {
    if (state == WAITING_AUTH) {
      mfrc522.PICC_HaltA();
      cardCooldown = millis();
      return;
    }

    String uid = uidToString();
    Serial.print("kartu: "); Serial.println(uid);

    if (mqtt.connected()) {
      String payload = "{\"uid\":\"" + uid + "\"}";
      mqtt.publish(topicCard, payload.c_str());
      lcd.clear();
      lcd.setCursor(0, 0); lcd.print("  Verifikasi...");
      lcd.setCursor(0, 1); lcd.print("  Mohon tunggu");
      state = WAITING_AUTH;
      stateStart = millis();
    } else {
      lcd.clear();
      lcd.setCursor(0, 0); lcd.print("  TIDAK ADA");
      lcd.setCursor(0, 1); lcd.print("  KONEKSI");
      delay(1500);
      showIdle();
    }

    mfrc522.PICC_HaltA();
    cardCooldown = millis();
  }

  if (relayState && state == IDLE && millis() - lastMonitor >= 1000) {
    float c = readCurrentAC();
    float daya = 220.0 * c * 0.85;

    float dt = (millis() - lastEnergyCalc) / 1000.0;
    energyKWh += (daya * dt) / 3600000.0;
    totalCost = energyKWh * tariff;
    lastEnergyCalc = millis();

    unsigned long elapsed = (millis() - relayOnTime) / 1000;
    int mins = elapsed / 60;
    int secs = elapsed % 60;

    lcd.setCursor(0, 0);
    if (activeUserName.length() > 16) {
      String buf = activeUserName + "   ";
      lcd.print(buf.substring(scrollPos, scrollPos + 16));
      if (millis() - lastScroll > 400) {
        lastScroll = millis();
        scrollPos = (scrollPos + 1) % (activeUserName.length() + 3);
      }
    } else {
      lcd.print(activeUserName);
      for (int i = activeUserName.length(); i < 16; i++) lcd.print(' ');
    }

    lcd.setCursor(0, 1);
    char buf[17];
    snprintf(buf, 17, "Daya:%4dW %02d:%02d", (int)daya, mins, secs);
    lcd.print(buf);

    Serial.print("monitor: "); Serial.print(c, 3); Serial.print("A ");
    Serial.print((int)daya); Serial.print("W ");
    Serial.print(energyKWh, 4); Serial.print("kWh Rp");
    Serial.println((int)totalCost);
    lastMonitor = millis();
  }
}
