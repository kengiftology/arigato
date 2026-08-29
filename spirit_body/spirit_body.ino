/*
 * spirit_body — 地霊の統合機（AtomS3 Lite 単体・PCレス）
 * ======================================================
 * 顔・口・肌感覚を1台に統合。電源を挿すだけで動く。
 *
 * ハード:
 *   AtomS3 Lite（Voice Baseは外す）
 *   液晶 ST7789 240x240 …… SCL=G5 SDA=G6 RES=G7 DC=G8（HW SPI MODE3）
 *   アンプ MAX98357     …… BCLK=G38 LRC=G39 DIN=G1（I2S 16kHz mono）
 *   人感 SR-602         …… OUT=G2（3.3V直結・HIGH=検知）
 *   配線の詳細: docs/配線図_統合機.md
 *
 * ふるまい:
 *   起動 → 孵化 → 呼吸(idle) ＋ 足元の会話窓（7秒ごと・あつ森語つき）
 *   人を検知(SR-602) → 「!」notice ＋ チャイム＋鳴き声
 *   本体ボタン → happy（テスト用）
 *   10分間 人の気配なし → 眠る（次の気配で目覚める）
 *
 * アセット（キャラ絵・会話窓・声の楽譜）は assets.h に埋め込み。
 * 作り直し: python spirits/gen_assets.py cute_07
 */
#include <SPI.h>
#include <ESP_I2S.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#include <WiFiClientSecure.h>
#include <ArduinoOTA.h>
#include <Preferences.h>
#include "assets.h"

// ---------------- ピン（ターゲットで自動切替） ----------------
#if CONFIG_IDF_TARGET_ESP32C3
// ★ XIAO ESP32C3（実配線 2026-08-24: 液晶=D7〜D10 / アンプ=D4〜D6 / PIR=D3）
static const int PIN_SCL = 10, PIN_SDA = 9,  PIN_RES = 8, PIN_DC = 20;  // D10/D9/D8/D7
static const int PIN_BCLK = 4, PIN_LRC = 3,  PIN_DIN = 5;               // D2/D1/D3
static const int PIN_PIR = 2;                                           // D0
static const int PIN_BTN = -1;                                          // ボタンなし
#else
// AtomS3 Lite 版（予備）
static const int PIN_SCL = 5, PIN_SDA = 6, PIN_RES = 7, PIN_DC = 8;
static const int PIN_BCLK = 38, PIN_LRC = 39, PIN_DIN = 1;
static const int PIN_PIR = 2;
static const int PIN_BTN = 41;
#endif

// ---------------- 表示（device_player.py の移植） ----------------
static const int GRID = 32, CELL = 7, OFF_X = 8, OFF_Y = 1;
static const int WIN_Y0 = 197;
#ifdef FSPI
SPIClass lcdSPI(FSPI);
#else
SPIClass lcdSPI(HSPI);
#endif
static uint8_t prevGrid[1024];
static uint8_t prevPal[12];
static bool havePrev = false;

static inline void lcdCmd(uint8_t c) { digitalWrite(PIN_DC, LOW); lcdSPI.write(c); }
static inline void lcdData(const uint8_t *d, size_t n) { digitalWrite(PIN_DC, HIGH); lcdSPI.writeBytes(d, n); }

static void lcdWindow(int x0, int y0, int x1, int y1) {
    uint8_t b[4];
    lcdCmd(0x2A); b[0] = 0; b[1] = (uint8_t)x0; b[2] = 0; b[3] = (uint8_t)x1; lcdData(b, 4);
    lcdCmd(0x2B); b[0] = 0; b[1] = (uint8_t)y0; b[2] = 0; b[3] = (uint8_t)y1; lcdData(b, 4);
    lcdCmd(0x2C);
}

static void lcdInit() {
    pinMode(PIN_DC, OUTPUT); pinMode(PIN_RES, OUTPUT);
    lcdSPI.begin(PIN_SCL, -1, PIN_SDA, -1);
    lcdSPI.setFrequency(40000000);
    lcdSPI.setDataMode(SPI_MODE3);
    digitalWrite(PIN_RES, HIGH); delay(120);
    digitalWrite(PIN_RES, LOW);  delay(120);
    digitalWrite(PIN_RES, HIGH); delay(250);
    lcdCmd(0x01); delay(200); lcdCmd(0x11); delay(250);
    uint8_t fmt = 0x55; lcdCmd(0x3A); lcdData(&fmt, 1); delay(50);
    uint8_t mad = 0x00; lcdCmd(0x36); lcdData(&mad, 1);
    lcdCmd(0x21); lcdCmd(0x13); delay(10); lcdCmd(0x29); delay(50);
}

static void fillRect(int y0, int h, const uint8_t *col2) {
    static uint8_t line[480];
    for (int i = 0; i < 240; i++) { line[i * 2] = col2[0]; line[i * 2 + 1] = col2[1]; }
    lcdWindow(0, y0, 239, y0 + h - 1);
    digitalWrite(PIN_DC, HIGH);
    for (int r = 0; r < h; r++) lcdSPI.writeBytes(line, 480);
}

static void drawRun(int c0, int c1, int r, const uint8_t *col2) {
    int w = (c1 - c0 + 1) * CELL;
    static uint8_t buf[240 * 2];
    for (int i = 0; i < w; i++) { buf[i * 2] = col2[0]; buf[i * 2 + 1] = col2[1]; }
    lcdWindow(OFF_X + c0 * CELL, OFF_Y + r * CELL,
              OFF_X + (c1 + 1) * CELL - 1, OFF_Y + r * CELL + CELL - 1);
    digitalWrite(PIN_DC, HIGH);
    for (int y = 0; y < CELL; y++) lcdSPI.writeBytes(buf, w * 2);
}

// 差分描画（prevとの違いだけ）。repaint=trueでキャラ部全塗り直し
static void drawGrid(const uint8_t *grid, const uint8_t *pal, bool repaint) {
    for (int r = 0; r < GRID; r++) {
        int base = r * GRID;
        for (int c = 0; c < GRID; ) {
            uint8_t v = grid[base + c];
            bool need = !havePrev ? (v != 0)
                       : repaint ? (v != 0 || prevGrid[base + c] != v)
                                 : (prevGrid[base + c] != v);
            if (!need) { c++; continue; }
            int c1 = c;
            while (c1 + 1 < GRID && grid[base + c1 + 1] == v) {
                int i = base + c1 + 1;
                bool nn = !havePrev ? (v != 0)
                         : repaint ? (v != 0 || prevGrid[i] != v) : (prevGrid[i] != v);
                if (!nn) break;
                c1++;
            }
            drawRun(c, c1, r, &pal[v * 2]);
            c = c1 + 1;
        }
    }
    memcpy(prevGrid, grid, 1024);
    memcpy(prevPal, pal, 12);
    havePrev = true;
}

// アニメ再生（assets.h の bin 形式: [n][dur2 pal12 grid1024]xN）
// between(): フレーム間に呼ばれる。trueを返すと中断
typedef bool (*BetweenFn)();
static void playAnim(const uint8_t *bin, int loops, BetweenFn between = nullptr) {
    int n = bin[0];
    for (int l = 0; l < loops; l++) {
        const uint8_t *p = bin + 1;
        for (int f = 0; f < n; f++) {
            uint16_t dur = ((uint16_t)p[0] << 8) | p[1];
            const uint8_t *pal = p + 2;
            const uint8_t *grid = p + 14;
            bool palChanged = havePrev && memcmp(prevPal, pal, 12) != 0;
            if (!havePrev) fillRect(0, WIN_Y0, pal);   // 初回だけ背景を塗る（窓より上）
            drawGrid(grid, pal, palChanged);
            uint32_t t0 = millis();
            while (millis() - t0 < dur) {
                if (between && between()) return;
                delay(10);
            }
            p += 2 + 12 + 1024;
        }
    }
}

// 会話窓（win_*.bin: [h][n][RGB565 240*h]）
static void blitWindow(const uint8_t *win) {
    int h = win[0];
    lcdWindow(0, WIN_Y0, 239, WIN_Y0 + h - 1);
    digitalWrite(PIN_DC, HIGH);
    lcdSPI.writeBytes(win + 2, 240 * h * 2);
}

// ---------------- 音（あつ森語・オンデバイス合成） ----------------
// C3はFPU無し → sinfのリアルタイム計算は間に合わない（ぷつぷつの原因）。
// 対策: サイン表(LUT)＋整数位相アキュムレータ。無音も明示的にゼロを流してDMAを絶やさない。
I2SClass i2s;
static const int RATE = 16000;
static const int LUTN = 1024;
static int16_t SINLUT[LUTN];

static void audioInit() {
    for (int i = 0; i < LUTN; i++)
        SINLUT[i] = (int16_t)(32767.0f * sinf(2.0f * PI * i / LUTN));
    i2s.setPins(PIN_BCLK, PIN_LRC, PIN_DIN, -1, -1);
    i2s.begin(I2S_MODE_STD, RATE, I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO);
}

static inline uint32_t phaseInc(float freq) {          // 1サンプルあたりの位相進み（Q32）
    return (uint32_t)(freq * 4294967296.0f / RATE);
}

static void writeSilence(int ms) {
    static int16_t z[256] = {0};
    int n = RATE * ms / 1000;
    while (n > 0) {
        int k = min(256, n);
        i2s.write((uint8_t *)z, k * 2);
        n -= k;
    }
}

// 基本波+2倍音のトーンを整数演算で流す。h2_q8=2倍音の量(0..256)
static void toneLUT(float freq, int ms, int gain_q8, int h2_q8, bool noisyHead) {
    uint32_t ph = 0, inc = phaseInc(freq);
    int n = RATE * ms / 1000;
    int atk = RATE * 5 / 1000, rel = RATE * 8 / 1000;    // 消え際8ms（ため息感を消しハキハキ）
    static int16_t buf[256];
    for (int i = 0; i < n; ) {
        int k = min(256, n - i);
        for (int j = 0; j < k; j++) {
            int t = i + j;
            int32_t s1 = SINLUT[ph >> 22];                    // 基本波
            int32_t s2 = SINLUT[(ph >> 21) & (LUTN - 1)];     // 2倍音
            int32_t s = (s1 * (256 - h2_q8) + s2 * h2_q8) >> 8;
            if (noisyHead && t < RATE / 125) {                // 子音ノイズ8ms（減衰・控えめ）
                int32_t head = RATE / 125;
                int32_t nz = ((int32_t)(esp_random() & 0x1FFF) - 4096) * (head - t) / head;
                s = (s * 77 >> 8) + nz;
            }
            int32_t env = 256;
            if (t < atk) env = t * 256 / atk;
            else if (t > n - rel) env = (n - t) * 256 / rel;
            buf[j] = (int16_t)((s * env >> 8) * gain_q8 >> 9);
            ph += inc;
        }
        i2s.write((uint8_t *)buf, k * 2);
        i += k;
    }
}

static void playTone(float freq, int ms, float gain) {
    toneLUT(freq, ms, (int)(gain * 256), 0, false);
}

// 母音→明るい音階（メジャーペンタトニック: ラ低・ド・レ・ミ・ソ）に割り当て
// 前版は無調な音程間隔で日本語の下降形が「悲しい節」になった（2026-08-24 実機診断）
static const int VPITCH_Q8[5]  = {256, 265, 250, 260, 245};   // ほぼ平坦（マイナー化を防ぐ）
static const int VTIMBRE_Q8[5] = {128, 38, 77, 64, 154};
// ほむりの声の遺伝子
static float V_BASE = 640.0f;   // voiceコマンドで実行中に変更可
static int   V_PACE = 85;
static int   V_GAIN = 230;      // 音量(0-256)。voiceコマンド第3引数で変更可

// あつ森語: 母音列（gen_assets.pyの楽譜）を鳴らす
static void speak(const uint8_t *vows, int n, uint32_t noisyMask) {
    for (int m = 0; m < n; m++) {
        uint8_t v = vows[m];
        if (v == 5) { writeSilence(140); continue; }
        if (v == 6) { writeSilence(60);  continue; }
        // ピッチダウンは廃止（下がる＝悲しい）。常に一定の高さで
        float f0 = V_BASE * VPITCH_Q8[v] / 256.0f
                   * (0.96f + 0.08f * (float)(esp_random() % 100) / 100.0f);
        if (m == n - 1 && (esp_random() % 100) < 35) f0 *= 1.10f;  // たまに語尾だけ小さく上がる（?の気配）
        int pace = V_PACE - 20 + (int)(esp_random() % 41);          // 1文字の長さを±20ms揺らす（機械感を消す）
        toneLUT(f0, pace, V_GAIN, VTIMBRE_Q8[v], (noisyMask >> m) & 1);
        writeSilence(pace / 6 + esp_random() % (pace / 6));
    }
    writeSilence(30);
}

static void chimeNotice() { playTone(659.25f, 120, 0.5f); writeSilence(30); playTone(880.0f, 200, 0.5f); }
static void chimeBoot() {
    playTone(523.25f, 100, 0.4f); writeSilence(20);
    playTone(659.25f, 100, 0.4f); writeSilence(20);
    playTone(783.99f, 180, 0.4f);
}
static void melodyHatch() {
    playTone(523.25f, 90, 0.35f); writeSilence(20); playTone(659.25f, 90, 0.38f); writeSilence(20);
    playTone(783.99f, 90, 0.42f); writeSilence(20); playTone(1046.5f, 200, 0.45f); writeSilence(60);
    playTone(1568.0f, 70, 0.3f);  writeSilence(20); playTone(2093.0f, 90, 0.25f);
}
static void melodyHappy() {
    playTone(659.25f, 70, 0.4f); writeSilence(15); playTone(783.99f, 70, 0.42f); writeSilence(15);
    playTone(1046.5f, 80, 0.45f); writeSilence(25); playTone(1318.5f, 160, 0.45f);
}


// PC操縦: pcm <bytes>=音声試聴 / scene notice|happy|sad|sleep|hatch / mur=次の独り言
static int ctlScene = 0;
static bool ctlMurmur = false;
static bool QUIET = false;       // 設置版: 音あり（quiet on で試験用の無音に）
// ---- 場所の状態（目=WROVERから受信 / テストはシリアル m コマンド） ----
static float g_M = 0.10f;        // 散らかり度 0..1（目から取得）
static float g_N = 0.0f;         // 放置度 0..1（Mが高いまま経った時間）
static uint32_t lastMrecv = 0;   // 最後にM値を取れた時刻
// 情報源のサーバ。GET /m → "score N flag"。
// 目(WROVER 192.168.0.202:80)か、クラウドの脳(443=HTTPS・パスに/spiritが付く)。シリアルsrcで切替。
static String srcIP = "192.168.0.202";   // 既定は目。クラウド脳なら `src arigato-3ipecjbnha-an.a.run.app 443`
static uint16_t srcPort = 80;
WiFiUDP mUdp;                            // 無線コマンド口（UDP 5006・netCmdで使用）
// 情報源へHTTP(S) GET。443ならTLSで接続しパスに/spiritを前置。短いタイムアウトで本体を止めない。
static bool httpGet(const char *path, char *body, int bodysz) {
    body[0] = 0;
    if (WiFi.status() != WL_CONNECTED) return false;
    WiFiClient plain;
    WiFiClientSecure tls;
    Client *c;
    bool https = (srcPort == 443);
    if (https) {
        tls.setInsecure();                       // 証明書検証なし（送るのは在室フラグだけ・受けるのはスコア）
        tls.setTimeout(4000);
        if (!tls.connect(srcIP.c_str(), 443)) return false;
        c = &tls;
    } else {
        if (!plain.connect(srcIP.c_str(), srcPort)) return false;
        c = &plain;
    }
    c->print("GET ");
    if (https) c->print("/spirit");              // クラウド脳はパスが /spirit/m /spirit/presence
    c->print(path);
    c->print(" HTTP/1.0\r\nHost: ");
    c->print(srcIP);
    c->print("\r\nConnection: close\r\n\r\n");
    String resp; uint32_t t0 = millis();
    uint32_t budget = https ? 6000 : 1500;       // TLSは握手が重いので長めに待つ
    while (millis() - t0 < budget) {
        while (c->available()) { resp += (char)c->read(); t0 = millis(); }
        if (!c->connected() && !c->available()) break;
        delay(5);
    }
    c->stop();
    int i = resp.indexOf("\r\n\r\n");
    if (i < 0) return false;
    String b = resp.substring(i + 4); b.trim();
    strncpy(body, b.c_str(), bodysz - 1); body[bodysz - 1] = 0;
    return b.length() > 0;
}
static bool voiceOnce = false;   // murコマンドの1回だけ発声許可
// 世話イベント（定義は下・serialPcmから使うため前方宣言）
Preferences prefs;
static uint32_t careCount = 0;
static float peakM = 0.0f;
static uint32_t lastCareAt = 0;
static uint32_t lastMotion = 0, lastNotice = 0;
static bool sleeping = false;
static void onM(float mv);
// 1行コマンドを処理して返事を返す（シリアル・無線UDPの共通部）。
// pcmストリーミングだけはシリアル専用（serialPcm側で処理）。
static String processCmd(String cmd) {
    String out = "";
    if (cmd.startsWith("scene ")) cmd = cmd.substring(6);   // scene有無どちらでも可
    if (cmd == "notice")      ctlScene = 1;
    else if (cmd == "happy")  ctlScene = 2;
    else if (cmd == "sad")    ctlScene = 3;
    else if (cmd == "sleep")  ctlScene = 4;
    else if (cmd == "hatch")  ctlScene = 5;
    else if (cmd == "mur")    { ctlMurmur = true; voiceOnce = true; }
    else if (cmd.startsWith("m ")) {                        // m 0.8 … M値を手で注入（テスト）
        float mv;
        if (sscanf(cmd.c_str(), "m %f", &mv) == 1) onM(mv);
    }
    else if (cmd.startsWith("n ")) {                        // n 0.6 … 放置度Nを手で注入（テスト）
        float nv;
        if (sscanf(cmd.c_str(), "n %f", &nv) == 1) g_N = nv;
    }
    else if (cmd == "quiet on")  QUIET = true;
    else if (cmd == "quiet off") QUIET = false;
    else if (cmd.startsWith("src ")) {                     // src <host> <port> … 情報源を切替（目/クラウド脳）
        char ip[64] = {0}; int port = 80;
        if (sscanf(cmd.c_str(), "src %63s %d", ip, &port) >= 1) {
            srcIP = ip; srcPort = (uint16_t)port;
            prefs.putString("srcIP", srcIP);
            prefs.putUShort("srcPort", srcPort);
            out += "SRC " + srcIP + ":" + String(srcPort) + "\n";
        }
    }
    else if (cmd == "care") { out += "CARE " + String(careCount) + "\n"; }
    else if (cmd.startsWith("care set ")) {
        careCount = atoi(cmd.c_str() + 9);
        prefs.putUInt("care", careCount);
    }
    else if (cmd == "stat") {                              // 状態まとめ（無線からの健康診断用）
        out += "VER ota-1\n";                              // 無線更新の動作確認用の版数
        out += "IP " + WiFi.localIP().toString() + "\n";
        out += "SRC " + srcIP + ":" + String(srcPort) + "\n";
        out += "M " + String(g_M, 3) + " N " + String(g_N, 3) + "\n";
        out += "CARE " + String(careCount) + "\n";
        out += String("QUIET ") + (QUIET ? "on" : "off") + "\n";
    }
    else if (cmd.startsWith("voice ")) {                    // voice 550 85 （高さHz・1文字ms）
        float b; int p;
        if (sscanf(cmd.c_str(), "voice %f %d", &b, &p) == 2) { V_BASE = b; V_PACE = p; }
    }
    else if (cmd.startsWith("say ")) {                      // say <母音hex> <noisymask hex>
        char vh[64] = {0}; unsigned long mask = 0;
        if (sscanf(cmd.c_str(), "say %63s %lx", vh, &mask) >= 1) {
            uint8_t vb[30]; int vn = 0;
            for (int i = 0; vh[i] && vh[i + 1] && vn < 30; i += 2) {
                char hx[3] = {vh[i], vh[i + 1], 0};
                vb[vn++] = (uint8_t)strtol(hx, NULL, 16);
            }
            out += "OK say\n";
            speak(vb, vn, (uint32_t)mask);
            return out;
        }
    }
    if (cmd.length()) out += "OK " + cmd + "\n";
    return out;
}

static void serialPcm() {
    if (!Serial.available()) return;
    String cmd = Serial.readStringUntil(10);
    cmd.trim();
    lastMotion = millis();                       // シリアル操作＝目の前に人がいる（机上テスト時）
    sleeping = false;
    long nbytes = 0;
    if (sscanf(cmd.c_str(), "pcm %ld", &nbytes) == 1 && nbytes > 0) {
        Serial.println("READY");
        uint8_t buf[512];
        long got = 0;
        uint32_t t0 = millis();
        while (got < nbytes && millis() - t0 < 30000) {
            int n = Serial.readBytes((char *)buf, min((long)512, nbytes - got));
            if (n > 0) { i2s.write(buf, n); got += n; t0 = millis(); }
        }
        Serial.println("DONE");
        return;
    }
    Serial.print(processCmd(cmd));
}

// 無線コマンド口（UDP 5006）。シリアルと同じ文法・返事は送り主へ返す。
// ※遠隔編集は「人の気配」扱いにしない（lastMotionを触らない）＝世話判定を汚さない。
static void netCmd() {
    int psz = mUdp.parsePacket();
    if (psz <= 0) return;
    char buf[128];
    int n = mUdp.read(buf, sizeof(buf) - 1);
    buf[n > 0 ? n : 0] = 0;
    String cmd = String(buf);
    cmd.trim();
    String out;
    if (cmd == "ping") out = "SPIRIT " + WiFi.localIP().toString() + "\n";   // 探索に応答
    else out = processCmd(cmd);
    if (out.length()) {
        mUdp.beginPacket(mUdp.remoteIP(), mUdp.remotePort());
        mUdp.print(out);
        mUdp.endPacket();
    }
}

// ---------------- 肌感覚（SR-602）と状態 ----------------
static const uint32_t PIR_WARMUP_MS   = 30000;
static const uint32_t PIR_COOLDOWN_MS = 8000;
static const uint32_t SLEEP_AFTER_MS  = 10UL * 60UL * 1000UL;   // 10分気配なしで眠る
static int pendingScene = 0;                   // 0=なし 1=notice 2=happy
// うるさい対策: 通過（すぐ去る）は音なし・顔だけ／滞在（居続ける）だけ声を出す・回数に上限
static const uint32_t STAY_MS         = 15000;                  // これ以上いたら「滞在」＝声を許す
static const uint32_t PRESENCE_GAP_MS = 20000;                  // これだけ気配が絶えたら滞在おわり
static const int      VOICE_BUDGET    = 3;                      // 1回の滞在で声を出すのは最大3回
static bool     inEpisode = false;             // 今この場に人がいる一続き
static uint32_t episodeStart = 0;              // その滞在が始まった時刻
static int      voiceUsed = 0;                 // その滞在で声を出した回数

// 目から "M N" を受け取る入口。Nを更新してからM（世話判定つき）へ回す。
static void onMN(float m, float n) { g_N = n; onM(m); }

// ---- 世話イベント（研究の心臓）: 人が居た後にMが下がった＝誰かが片づけた ----
static void onM(float mv) {
    g_M = mv;
    lastMrecv = millis();
    if (mv > peakM) peakM = mv;
    bool recentPerson = (millis() - lastMotion) < 10UL * 60UL * 1000UL;  // 10分以内に気配
    bool bigDrop = (peakM >= 0.35f) && (peakM - mv >= 0.15f);            // 散らかりが大きく減った
    bool cooled = (millis() - lastCareAt) > 10UL * 60UL * 1000UL;        // 連続カウント防止
    if (recentPerson && bigDrop && cooled) {
        careCount++;
        prefs.putUInt("care", careCount);
        lastCareAt = millis();
        peakM = mv;
        pendingScene = 2;                        // 世話された！→ 喜び
        Serial.print("CARE ");
        Serial.println(careCount);
        char t[8];                               // クラウドへ時刻つきで記録（研究の主要指標）
        String p = "/care?n=" + String(careCount);
        httpGet(p.c_str(), t, sizeof t);
    }
}

static bool pirNow() { return digitalRead(PIN_PIR) == HIGH; }
static bool btnNow() { return PIN_BTN >= 0 && digitalRead(PIN_BTN) == LOW; }

// 人の気配を更新する。新しい来訪で滞在を開始し、気配が絶えたら滞在を終える。
static void updatePresence(uint32_t now) {
    bool sensed = (now > PIR_WARMUP_MS) && pirNow();
    if (sensed) {
        if (!inEpisode) {                     // 新しい来訪
            inEpisode = true;
            episodeStart = now;
            voiceUsed = 0;
            pendingScene = 1;                 // 「!」（絵だけ・音は出さない）
        }
        lastMotion = now;
    } else if (inEpisode && now - lastMotion > PRESENCE_GAP_MS) {
        inEpisode = false;                    // 立ち去った → 滞在おわり
    }
}

// フレーム間の割り込み判定（新規来訪やボタンでアニメを中断）。無線の受付もここで回す
static void otaService();
static void netCmd();
static volatile bool otaBusy = false;   // OTA転送中フラグ（転送に専念するため他の仕事を止める）
static bool checkInterrupt() {
    otaService();                                // アニメ中も無線更新・無線コマンドを受ける
    if (otaBusy) return true;                    // 転送開始→アニメを即中断して転送に専念
    netCmd();
    uint32_t now = millis();
    bool wasEpisode = inEpisode;
    updatePresence(now);
    if (btnNow()) { pendingScene = 2; return true; }
    if (!wasEpisode && inEpisode) return true;   // 新規来訪 → 「!」へ切り替え
    return false;
}

// ---------------- メイン ----------------
static int winIdx = -1;
static uint32_t nextMurmur = 0;
static uint32_t nextPoll = 0;    // 次に目へM/Nを取りに行く時刻
static uint32_t nextBeat = 0;    // 次に目へ在室/不在を伝える時刻

void setup() {
    Serial.begin(115200);
    pinMode(PIN_PIR, INPUT_PULLDOWN);
    if (PIN_BTN >= 0) pinMode(PIN_BTN, INPUT_PULLUP);
    lcdInit();
    audioInit();
    if (!QUIET) chimeBoot();
    playAnim(anim_hatch, 1);                   // 誕生（絵はいつも通り）
    if (!QUIET) melodyHatch();
    lastMotion = millis();
    nextMurmur = millis() + 3000;
    prefs.begin("spirit", false);
    careCount = prefs.getUInt("care", 0);
    srcIP = prefs.getString("srcIP", srcIP);          // 情報源(目/脳)を記憶から復元
    srcPort = prefs.getUShort("srcPort", srcPort);
    // 情報源(目/クラウド脳)との接続: WiFiに参加。繋がらなくても本体は動く
    WiFi.mode(WIFI_STA);
    WiFi.setHostname("spirit-c3");
    WiFi.begin("TP-Link_C452", "40568478");
    mUdp.begin(5006);                          // 無線コマンド口（シリアルと同じ文法・ping応答）
    Serial.println("SPIRIT READY");
}

// 無線ファーム更新（ArduinoOTA）。WiFiが繋がってから一度だけ起動する。
// 転送が始まったら otaBusy を立て、他の仕事（TLS通信・声・アニメ）を全部止めて転送に専念する。
static bool otaUp = false;
static void otaService() {
    if (!otaUp) {
        if (WiFi.status() != WL_CONNECTED) return;
        ArduinoOTA.setHostname("spirit-c3");
        ArduinoOTA.setPassword("40568478");    // 同じWiFiに居ても合言葉なしでは書き込めない
        ArduinoOTA.onStart([]() { otaBusy = true; });
        ArduinoOTA.onEnd([]() { otaBusy = false; });
        ArduinoOTA.onError([](ota_error_t) { otaBusy = false; });
        ArduinoOTA.begin();
        otaUp = true;
        Serial.print("OTA READY ");
        Serial.println(WiFi.localIP());
    }
    ArduinoOTA.handle();
}

void loop() {
    otaService();                              // 無線更新の受付（WiFi接続後）
    if (otaBusy) { delay(1); return; }         // 転送中は全仕事を止める（窒息防止）
    netCmd();                                  // 無線コマンド（UDP 5006）
    serialPcm();                               // PC操縦（声の試聴・シーン発火）
    if (ctlScene)  { pendingScene = ctlScene; ctlScene = 0; sleeping = false; }
    if (ctlMurmur) { ctlMurmur = false; nextMurmur = 0; }
    uint32_t now = millis();

    if (pendingScene == 1) {                   // 人が来た → 「!」の絵だけ（音なし・通過でうるさくしない）
        pendingScene = 0;
        sleeping = false;
        playAnim(anim_notice, 1);
        return;
    }
    if (pendingScene == 2) {                   // 喜び
        pendingScene = 0;
        sleeping = false;
        melodyHappy();
        playAnim(anim_happy, 1);
        return;
    }
    if (pendingScene == 3) { pendingScene = 0; playAnim(anim_sad, 1); return; }
    if (pendingScene == 4) { pendingScene = 0; playAnim(anim_sleep, 2); return; }
    if (pendingScene == 5) { pendingScene = 0; playAnim(anim_hatch, 1); if (!QUIET) melodyHatch(); return; }

    if (!sleeping && now - lastMotion > SLEEP_AFTER_MS) sleeping = true;
    if (sleeping) {                            // 眠り（長い無人）
        playAnim(anim_sleep, 1, checkInterrupt);
        return;
    }

    if (now >= nextMurmur) {                   // 独り言（字幕は常時・声は滞在中だけ上限つき）
        winIdx = (winIdx + 1) % N_WINS;
        blitWindow(WINS[winIdx]);
        bool staying = inEpisode && (now - episodeStart >= STAY_MS);   // 通過でなく居続けている
        if (!QUIET && staying && voiceUsed < VOICE_BUDGET) {
            speak(VOWS[winIdx], VOW_LEN[winIdx], VOW_NOISY[winIdx]);
            voiceUsed++;
        } else if (voiceOnce) {                // murコマンドの強制発声（テスト用・上限外）
            voiceOnce = false;
            speak(VOWS[winIdx], VOW_LEN[winIdx], VOW_NOISY[winIdx]);
        }
        nextMurmur = millis() + 12000;
    }

    // 目へ在室/不在を伝える（8秒ごと）。人の気配が15秒以内なら「在室」＝目はMを凍結し人を写さない
    if (now >= nextBeat) {
        nextBeat = now + 8000;
        bool occ = (now - lastMotion) < 15000;
        char t[8];
        httpGet(occ ? "/presence?state=occupied" : "/presence?state=empty", t, sizeof t);
    }
    // 目からM・Nを取りに行く（10秒ごと）。"M N flag" を受けてonMNへ
    if (now >= nextPoll) {
        nextPoll = now + 10000;
        char body[48];
        if (httpGet("/m", body, sizeof body)) {
            float m, n; int f;
            if (sscanf(body, "%f %f %d", &m, &n, &f) >= 2) onMN(m, n);
        }
    }

    // 気分: 放置(N>=0.5)ならしょんぼり。散らかっていても使用中(N低)は責めずふだんの呼吸
    if (g_N >= 0.5f) playAnim(anim_sad, 1, checkInterrupt);
    else             playAnim(anim_idle, 1, checkInterrupt);
}
