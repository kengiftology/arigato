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

// 合言葉（Wi-Fi・書き込み）は git に上げない。本物は全トーク共通の場所に置く（2026-09-23）。
// 作り方は spirit_body/secrets_example.h の先頭に書いてある。
#if __has_include("C:/Users/kengk/.arigato/spirit_secrets.h")
  #include "C:/Users/kengk/.arigato/spirit_secrets.h"
#else
  #error "C:/Users/kengk/.arigato/spirit_secrets.h がありません。spirit_body/secrets_example.h を写して作ってください"
#endif

// ---------------- ピン（ターゲットで自動切替） ----------------
#if CONFIG_IDF_TARGET_ESP32C3
// ★ XIAO ESP32C3（実配線 2026-08-24: 液晶=D7〜D10 / アンプ=D4〜D6 / PIR=D3）
static const int PIN_SCL = 10, PIN_SDA = 9,  PIN_RES = 8, PIN_DC = 20;  // D10/D9/D8/D7
static const int PIN_BCLK = 4, PIN_LRC = 3,  PIN_DIN = 5;               // D2/D1/D3
static const int PIN_PIR = 6;                                           // D4（旧D0=GPIO2は起動モード判定ピンで
                                                                        //  電源投入時にコケるため2026-08-30引っ越し）
static const int PIN_BTN = -1;                                          // ボタンなし
#else
// AtomS3 Lite 版（予備）
static const int PIN_SCL = 5, PIN_SDA = 6, PIN_RES = 7, PIN_DC = 8;
static const int PIN_BCLK = 38, PIN_LRC = 39, PIN_DIN = 1;
static const int PIN_PIR = 2;
static const int PIN_BTN = 41;
#endif

// ---------------- 表示（device_player.py の移植） ----------------
// 2026-09-13：字幕をやめたぶん下に15pxのすき間が残っていたので、中央へ寄せた。
// 画面240x240・キャラ32マス×7px＝224px。左右も上下も (240-224)/2 = 8px。
// これ以上大きくはできない（8pxにすると256pxで画面からはみ出す）。
static const int GRID = 32, CELL = 7, OFF_X = 8, OFF_Y = 8;
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
            if (!havePrev) fillRect(0, 240, pal);      // 初回だけ背景を塗る（画面ぜんぶ）
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

// ---------------- 顔をその場で描き替える（2026-09-23） ----------------
// 絵は assets.h の読み出し専用のもとから、いったん控え（gWork）に写してから描く。
// 控えのマスを書き替えれば、絵を足さずに「考えている顔」や「開いた口」が作れる。
// 重ねて塗るのではなく控えを描き直すので、差分描画（前のコマとの違いだけ）がそのまま効く。
// ＝印が顔に残る心配がない。
static uint8_t gWork[1024];           // これから描くマス目
static uint8_t mouthBase[1024];       // 喋る前の顔（口を戻すときの元）

// 目・口の色（6色のうちいちばん暗いもの）。絵を作り直しても付いていけるよう、毎回さがす
static int darkIndex(const uint8_t *pal) {
    int best = 1; long dim = 1L << 30;
    for (int i = 1; i < 6; i++) {
        uint16_t v = ((uint16_t)pal[i * 2] << 8) | pal[i * 2 + 1];
        long lum = (long)((v >> 11) & 31) * 2 + ((v >> 5) & 63) + (long)(v & 31) * 2;
        if (lum < dim) { dim = lum; best = i; }
    }
    return best;
}

// 暗いマスを「目（上のかたまり）」と「口（下）」に分ける。
// part には順に 目の上端・目の下端・口の上端・口の下端 が入る（無ければ -1）。
// ※ひとまとめの値を返さないのは、.ino の前処理が型より先に宣言を並べてしまうため。
static void darkParts(const uint8_t *grid, int dk, int part[4]) {
    part[0] = part[1] = part[2] = part[3] = -1;
    for (int r = 0; r < GRID; r++) {
        bool any = false;
        for (int c = 0; c < GRID; c++) if (grid[r * GRID + c] == dk) { any = true; break; }
        if (!any) continue;
        if (part[0] < 0)            { part[0] = part[1] = r; }
        else if (r <= part[0] + 2)  { part[1] = r; }
        else { if (part[2] < 0) part[2] = r; part[3] = r; }
    }
}

// 「？」5×6マス（頭の右上・絵のない場所）
static const uint8_t QMARK[6] = {0x0E, 0x11, 0x02, 0x04, 0x00, 0x04};
static const int Q_COL = 26, Q_ROW = 0;

// 聞いている顔（2026-09-23 本人が選んだ案B）：目を1マス上と右へ・口を小さく・「？」を出す
static void buildThinking(const uint8_t *grid, const uint8_t *pal, int qDown) {
    memcpy(gWork, grid, 1024);
    int dk = darkIndex(pal), d[4];
    darkParts(grid, dk, d);
    if (d[0] > 0) {
        int skin = grid[(d[0] - 1) * GRID + GRID / 2];           // 目のすぐ上＝顔の色
        for (int r = d[0]; r <= d[1]; r++)
            for (int c = 0; c < GRID; c++)
                if (grid[r * GRID + c] == dk) gWork[r * GRID + c] = skin;
        for (int r = d[0]; r <= d[1]; r++)
            for (int c = 0; c < GRID - 1; c++)
                if (grid[r * GRID + c] == dk) gWork[(r - 1) * GRID + c + 1] = dk;
    }
    if (d[2] > 0) {                                              // 口は真ん中2マスだけ残す
        int lo = GRID, hi = -1;
        for (int r = d[2]; r <= d[3]; r++)
            for (int c = 0; c < GRID; c++)
                if (grid[r * GRID + c] == dk) { if (c < lo) lo = c; if (c > hi) hi = c; }
        int mid = (lo + hi) / 2;
        for (int r = d[2]; r <= d[3]; r++)
            for (int c = 0; c < GRID; c++)
                if (grid[r * GRID + c] == dk && (c < mid || c > mid + 1))
                    gWork[r * GRID + c] = grid[(d[2] - 1) * GRID + c];
    }
    for (int r = 0; r < 6; r++)                                  // 「？」
        for (int c = 0; c < 5; c++)
            if (QMARK[r] & (0x10 >> c)) gWork[(Q_ROW + qDown + r) * GRID + Q_COL + c] = dk;
}

// 喋るときの口（0=閉じる 1=すこし開く 2=大きく開く）。いまの顔の口だけを描き替える
static void buildMouth(const uint8_t *base, const uint8_t *pal, int level) {
    memcpy(gWork, base, 1024);
    if (level <= 0) return;                                      // 0＝もとのまま
    int dk = darkIndex(pal), d[4];
    darkParts(base, dk, d);
    if (d[2] <= 0) return;
    int lo = GRID, hi = -1;
    for (int r = d[2]; r <= d[3]; r++)
        for (int c = 0; c < GRID; c++)
            if (base[r * GRID + c] == dk) { if (c < lo) lo = c; if (c > hi) hi = c; }
    if (hi <= lo + 1) return;
    int skin = base[(d[2] - 1) * GRID + (lo + hi) / 2];
    for (int r = d[2]; r <= d[3]; r++)                           // いまの口を消す
        for (int c = 0; c < GRID; c++)
            if (base[r * GRID + c] == dk) gWork[r * GRID + c] = skin;
    int top = (level >= 2) ? d[2] - 1 : d[2];                    // 大きく開くときだけ1行上へ
    int bot = d[2] + 1;
    for (int r = top; r <= bot; r++)
        for (int c = lo + 1; c <= hi - 1; c++) {
            if (level >= 2 && (r == top || r == bot) && (c == lo + 1 || c == hi - 1)) continue;
            gWork[r * GRID + c] = dk;                            // 角を落として丸く見せる
        }
}

// 喋っている間だけ口を動かす。いま出ている顔を元にするので、どの表情でもそのまま使える
static uint8_t palWork[12];
static bool    mouthOn = false;
static int     mouthNow = -1;

static void mouthBegin() {
    if (!havePrev) return;                       // まだ何も描いていない＝動かさない
    memcpy(mouthBase, prevGrid, 1024);
    memcpy(palWork, prevPal, 12);                // 描くときに prevPal を自分自身へ写さないため
    mouthOn = true;
    mouthNow = 0;
}

static void mouthSet(int level) {
    if (!mouthOn || level == mouthNow) return;
    buildMouth(mouthBase, palWork, level);
    drawGrid(gWork, palWork, false);
    mouthNow = level;
}

static void mouthEnd() {
    if (!mouthOn) return;
    mouthSet(0);                                 // もとの口に戻す
    mouthOn = false;
}

// 音の大きさから口の開き具合を決める（0〜2）。しきい値は実機で詰める
static const long MOUTH_TH1 = 900, MOUTH_TH2 = 3000;
static int mouthLevelOf(long avgAbs) {
    return avgAbs < MOUTH_TH1 ? 0 : (avgAbs < MOUTH_TH2 ? 1 : 2);
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
    mouthBegin();                       // 喋っている間は口を動かす（2026-09-23）
    for (int m = 0; m < n; m++) {
        uint8_t v = vows[m];
        if (v == 5) { mouthSet(0); writeSilence(140); continue; }
        if (v == 6) { mouthSet(0); writeSilence(60);  continue; }
        // ピッチダウンは廃止（下がる＝悲しい）。常に一定の高さで
        float f0 = V_BASE * VPITCH_Q8[v] / 256.0f
                   * (0.96f + 0.08f * (float)(esp_random() % 100) / 100.0f);
        if (m == n - 1 && (esp_random() % 100) < 35) f0 *= 1.10f;  // たまに語尾だけ小さく上がる（?の気配）
        int pace = V_PACE - 20 + (int)(esp_random() % 41);          // 1文字の長さを±20ms揺らす（機械感を消す）
        mouthSet((v == 0 || v == 4) ? 2 : 1);                       // あ・お は大きく、い・う・え は小さく
        toneLUT(f0, pace, V_GAIN, VTIMBRE_Q8[v], (noisyMask >> m) & 1);
        mouthSet(0);
        writeSilence(pace / 6 + esp_random() % (pace / 6));
    }
    writeSilence(30);
    mouthEnd();
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
static int ctlStage = -1;        // stage コマンドで手入れした段階（-1＝なし）。loop で受け取る
static uint32_t stageHold = 0;   // この時刻まで、手入れした段階をクラウドで上書きしない
static bool QUIET = false;       // 設置版: 音あり（quiet on で試験用の無音に）
// ---- 場所の状態（目=WROVERから受信 / テストはシリアル m コマンド） ----
static float g_M = 0.10f;        // 散らかり度 0..1（目から取得）
static float g_N = 0.0f;         // 放置度 0..1（Mが高いまま経った時間）
static int   g_stage = 0;        // いま居る人のなつき度の段階 0..4（/m の4つめ。誰か分からなければ0）
// 見張り（2026-09-19）：本体は生きているのにクラウドへ行けなくなることが2回あった
// （9/17 19:33・9/19 12:1x。UDPとpingは通り、HTTPSだけが通らない。電源の入れ直しで戻る）。
// 5分つづけて1回も通らなければ、自分で起動し直す。
static uint32_t lastHttpOk = 0;  // 最後にHTTP(S)が通った時刻
static uint8_t  wdBoots = 0;     // 見張りによる起動し直しが続いた回数（通れば0に戻す）
static uint32_t lastMrecv = 0;   // 最後にM値を取れた時刻
// 情報源のサーバ。GET /m → "score N flag"。
// 目(WROVER 192.168.0.202:80)か、クラウドの脳(443=HTTPS・パスに/spiritが付く)。シリアルsrcで切替。
static String srcIP = "192.168.0.202";   // 既定は目。クラウド脳なら `src arigato-3ipecjbnha-an.a.run.app 443`
static uint16_t srcPort = 80;
static String eyeIP = "192.168.0.202";   // 目のLANアドレス（在室を直接伝える相手・`eye <ip>`で変更可）
WiFiUDP mUdp;                            // 無線コマンド口（UDP 5006・netCmdで使用）

// 目へLAN直で在室/不在を伝える（0.3秒級・クラウドを待たない）。失敗しても本体を止めない
static void eyeNotify(bool occ) {
    if (WiFi.status() != WL_CONNECTED) return;
    WiFiClient c;
    c.setTimeout(800);
    if (!c.connect(eyeIP.c_str(), 80, 300)) return;   // 接続300msで諦める（目が不在でも本体を待たせない）
    c.print(String("GET /presence?state=") + (occ ? "occupied" : "empty") +
            " HTTP/1.0\r\nHost: eye\r\nConnection: close\r\n\r\n");
    uint32_t t0 = millis();
    while (c.connected() && millis() - t0 < 800) { while (c.available()) c.read(); delay(2); }
    c.stop();
}
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
    lastHttpOk = millis();                       // 返事が来た＝道は通っている（見張り用）
    String b = resp.substring(i + 4); b.trim();
    strncpy(body, b.c_str(), bodysz - 1); body[bodysz - 1] = 0;
    return b.length() > 0;
}
// クラウドで作った日本語の声を取りに行って、そのまま流す（2026-08-31）。
// 一言そのものを喋らせる。母音の合成音とは別系統で、字幕と同じ言葉が耳で届く。
// 半端な1バイトを持ち越した回数（2026-09-24）。stat に出せば「砂嵐がほんとうに
// 起きていたか」の証拠になる。0でも直った証拠にはならない（たまたま奇数が
// 来なかっただけのことがある）。0より大きい値が出たときだけが証拠。
static uint32_t oddCarries = 0;

static bool speakCloud() {
    if (srcPort != 443 || WiFi.status() != WL_CONNECTED) return false;
    WiFiClientSecure tls;
    tls.setInsecure();
    tls.setTimeout(8000);
    if (!tls.connect(srcIP.c_str(), 443)) return false;
    tls.print("GET /spirit/voice.pcm HTTP/1.0\r\nHost: ");
    tls.print(srcIP);
    tls.print("\r\nConnection: close\r\n\r\n");
    uint32_t t0 = millis();
    String line;
    bool body = false;
    while (!body && millis() - t0 < 8000) {
        if (!tls.available()) { if (!tls.connected()) break; delay(5); continue; }
        char ch = (char)tls.read();
        if (ch == '\n') {
            line.trim();
            if (line.length() == 0) body = true;
            line = "";
        } else if (ch != '\r') line += ch;
    }
    if (!body) { tls.stop(); return false; }
    uint8_t buf[514];                              // +2＝前回の半端な1バイトを頭に置く余地
    size_t total = 0;
    t0 = millis();
    // 声の大きさで口を動かす（2026-09-23）。512バイト＝16ミリ秒ぶんなので、
    // 6回ためして（約100ミリ秒）から動かす。人の口の速さに近く、絵の描き直しも減る。
    long acc = 0; int accN = 0;
    // 2026-09-24：本人「砂嵐がすごくて何を言っているか分からなかった」。
    // 音の部品（ESP_I2S.cpp 1227〜1231行）は、渡された長さが2バイトに満たないと
    // 黙って捨てて0を返す。声は2バイトで1つの数（16ビット）なので、通信から
    // 1バイトだけ届いた瞬間にその1バイトが消え、**以後ずっと上の桁と下の桁が
    // 入れ替わったまま鳴る＝砂嵐**になる。その再生が終わるまで直らない。
    // （2バイト以上なら部品の中で書き切るので、奇数でもずれない。捨てられるのは
    //   ちょうど1バイトのときだけ。）
    // 机の上で真似た結果：10回中7回が壊れ（食い違い1.0〜94.6%）、
    // 余りを持ち越す形にすると10回とも0%。
    int odd = 0;                                   // 持ち越した半端な1バイト（0 か 1）
    mouthBegin();
    while (millis() - t0 < 20000) {
        int avail = tls.available();
        if (avail > 0) {
            int n = tls.read(buf + odd, min(avail, 512));
            if (n > 0) {
                n += odd;                          // 持ち越しを頭に足した長さ
                int even = n & ~1;                 // 流すのは偶数ぶんだけ
                for (int i = 0; i + 1 < even; i += 2) {
                    int16_t s = (int16_t)((uint16_t)buf[i] | ((uint16_t)buf[i + 1] << 8));
                    acc += (s < 0) ? -(long)s : (long)s;
                }
                accN += even / 2;
                if (even) i2s.write(buf, even);    // 音を送ったあとの合間に描く（途切れさせない）
                odd = n - even;                    // 余りは次へ
                if (odd) { buf[0] = buf[even]; oddCarries++; }
                if (accN >= 256 * 6) {
                    mouthSet(mouthLevelOf(acc / accN));
                    acc = 0; accN = 0;
                }
                total += n; t0 = millis();
            }
        } else if (!tls.connected()) break;
        else delay(5);
    }
    mouthEnd();
    tls.stop();
    Serial.print("VOICE bytes="); Serial.println(total);
    return total > 1000;
}

static bool voiceOnce = false;   // murコマンドの1回だけ発声許可


// 情報源へHTTP(S) GETしてバイナリ本文をbufへ。戻り=本文バイト数(失敗は-1)
static int httpGetBin(const char *path, uint8_t *buf, int maxlen) {
    if (WiFi.status() != WL_CONNECTED) return -1;
    WiFiClient plain;
    WiFiClientSecure tls;
    Client *c;
    bool https = (srcPort == 443);
    if (https) {
        tls.setInsecure();
        tls.setTimeout(6000);
        if (!tls.connect(srcIP.c_str(), 443)) return -1;
        c = &tls;
    } else {
        if (!plain.connect(srcIP.c_str(), srcPort)) return -1;
        c = &plain;
    }
    c->print("GET ");
    if (https) c->print("/spirit");
    c->print(path);
    c->print(" HTTP/1.0\r\nHost: ");
    c->print(srcIP);
    c->print("\r\nConnection: close\r\n\r\n");
    // ヘッダを1行ずつ読み飛ばして本文へ
    uint32_t t0 = millis();
    String line;
    while (millis() - t0 < 8000) {
        if (!c->available()) { if (!c->connected()) break; delay(5); continue; }
        char ch = (char)c->read();
        if (ch == '\n') {
            line.trim();
            if (line.length() == 0) goto body;   // 空行＝ヘッダ終わり
            line = "";
        } else if (ch != '\r') line += ch;
    }
    c->stop();
    return -1;
body:
    int n = 0;
    t0 = millis();
    while (millis() - t0 < 10000 && n < maxlen) {
        int avail = c->available();
        if (avail > 0) {
            int k = c->read(buf + n, min(avail, maxlen - n));
            if (k > 0) { n += k; t0 = millis(); }
        } else if (!c->connected()) break;
        else delay(5);
    }
    c->stop();
    return n;
}
// 世話イベント（定義は下・serialPcmから使うため前方宣言）
Preferences prefs;
static uint32_t careCount = 0;
static float peakM = 0.0f;
static uint32_t lastCareAt = 0;
static uint32_t lastMotion = 0, lastNotice = 0;
static bool sleeping = false;
static void onM(float mv);
// ---- 名前を聞いている間（2026-09-23 本人の決定）----
// 「録音中は表情と『？』だけ、答えたあとは声を続ける」。表情はここ、声はクラウド側。
static const uint32_t LISTEN_MAX_MS = 25000;   // 合図の「切り」が落ちても、これで自分から戻る
static uint32_t listenUntil = 0;               // 0＝聞いていない
static bool     listenDrawn = false;           // 考えている顔をもう描いたか
static uint32_t nextQBob = 0;                  // 次に「？」を上下させる時刻
static int      qDown = 0;                     // 「？」の位置（0＝上 1＝下）
static inline bool listening() {
    return listenUntil != 0 && (int32_t)(millis() - listenUntil) < 0;
}

// ---- 撤去期に消す（2026-09-24 本人の決定・12/7〜12/20）----
// 0＝ふつう　1＝画面だけ消す　2＝画面と声を消す。`/spirit/m` の6つめで受け取る。
// 「声も消すかどうか」は12月に決めるので、両方作って選ぶだけにしてある。
// **C3 は抜かない。**人感も世話の判定もこの中にあるので、抜くと比べる相手そのものが消える。
// 記憶に残すのは、起動し直してから最初の問い合わせまでの数秒に顔が出ないようにするため。
static int g_hide = 0;
static inline bool hideFace()  { return g_hide >= 1; }
static inline bool hideVoice() { return g_hide >= 2; }

// ---- しょんぼり顔は「場所の状態」で決める（2026-09-24 本人の決定・(c)）----
// 前は「散らかったまま5分たつと」しょんぼりになる作りで、**同じ散らかり具合でも
// 時間がたっただけで顔が悪くなっていた**。それは不足の表示ではなく催促に近い。
// いまは散らかり具合そのものだけを見る。さらに**人が居る間は変えない**（料理の最中に
// 顔が曇ると、その人を責めることになるため）。人が居なくなってから次の値で塗り替える。
static const float M_MESSY = 0.30f;     // クラウドの M_HI と同じ
static bool faceSad = false;            // いま出している気分（人が居る間は固定）
static bool blanked = false;            // 撤去期に画面をもう黒く塗ったか

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
    else if (cmd.startsWith("listen")) {                    // listen 1／listen 0（名前を聞いている間）
        // 橋渡しが無線で送る（同じ家の中なので0.2〜1秒）。クラウドからはここへ直接届かない。
        // 無線の合図は届かないことがあるので、「切り」が落ちても LISTEN_MAX_MS で自分から戻る。
        int v = 1;
        sscanf(cmd.c_str(), "listen %d", &v);
        listenUntil = v ? millis() + LISTEN_MAX_MS : 0;
        // 返事はこの関数の最後で「OK <命令>」が付く。ここで足すと二重になる
    }
    else if (cmd.startsWith("hide")) {                      // hide 0|1|2（試験用）
        // 本番の持ち主はクラウド（`/m` の6つめ）。ここは手元で見え方を確かめるための口で、
        // 次の問い合わせ（10秒以内）でクラウドの値に戻される。
        int v = 0;
        sscanf(cmd.c_str(), "hide %d", &v);
        if (v >= 0 && v <= 2) { g_hide = v; havePrev = false; }
    }
    else if (cmd.startsWith("m ")) {                        // m 0.8 … M値を手で注入（テスト）
        float mv;
        if (sscanf(cmd.c_str(), "m %f", &mv) == 1) onM(mv);
    }
    else if (cmd.startsWith("n ")) {                        // n 0.6 … 放置度Nを手で注入（テスト）
        float nv;
        if (sscanf(cmd.c_str(), "n %f", &nv) == 1) g_N = nv;
    }
    else if (cmd.startsWith("stage ")) {                    // stage 4 … 段階を手で入れる（テスト）
        // 目の前に人が居ないとクラウドは段階0しか返さないので、机の上で
        // 5段階を試すための口（2026-09-19）。実際の受け取りは loop（変数がそこにある）。
        // 人が居ることにはしない（2026-09-21。偽の来訪を記録に混ぜないため）。
        int sv;
        if (sscanf(cmd.c_str(), "stage %d", &sv) == 1 && sv >= 0 && sv <= 4) {
            ctlStage = sv;
            out += "STAGE " + String(sv) + "\n";
        }
    }
    else if (cmd == "quiet on")  QUIET = true;
    else if (cmd == "quiet off") QUIET = false;
    else if (cmd.startsWith("eye ")) {                     // eye 192.168.0.202 … 目のLANアドレス変更
        char ip[40] = {0};
        if (sscanf(cmd.c_str(), "eye %39s", ip) == 1) {
            eyeIP = ip;
            prefs.putString("eyeIP", eyeIP);
            out += "EYE " + eyeIP + "\n";
        }
    }
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
        out += "M " + String(g_M, 3) + " N " + String(g_N, 3) + " STAGE " + String(g_stage) + "\n";
        out += "NET ok " + String((millis() - lastHttpOk) / 1000) + "s ago wd " + String(wdBoots) + "\n";
        out += "CARE " + String(careCount) + "\n";
        out += String("QUIET ") + (QUIET ? "on" : "off") + "\n";
        // 声の半端な1バイトを持ち越した回数（2026-09-24・研究トークC の直し）。
        // **0より大きく出れば、砂嵐が実際に起きていた証拠**になる。0は証拠にならない
        // （その再生でたまたま半端が来なかっただけ）。電源を入れ直すと0に戻る。
        out += String("ODD ") + String(oddCarries) + "\n";
        out += String("HIDE ") + String(g_hide)
             + (g_hide == 0 ? " （ふつう）" : g_hide == 1 ? " （画面だけ消す）" : " （画面と声を消す）")
             + String("  MOOD ") + (faceSad ? "しょんぼり" : "ふだん") + "\n";
        out += String("LISTEN ") + (listening() ? "on" : "off")
             + (listening() ? " " + String((listenUntil - millis()) / 1000) + "s left" : "") + "\n";
        // 人感の生死を無線から見る（2026-09-10：手を振っても「!」が出ないと報告あり）
        out += String("PIR ") + (pirNow() ? "HIGH" : "LOW")
             + " lastMotion " + String((millis() - lastMotion) / 1000) + "s ago\n";
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
            if (hideVoice()) {                 // 撤去期（画面と声を消す）は試験用の口からも鳴らさない
                out += "HIDE 2 のため鳴らしません\n";
                return out;
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
static const uint32_t PRESENCE_GAP_MS = 90000;                  // これだけ気配が絶えたら滞在おわり
// ※人感の死角（カウンター・調理位置）で20秒だと在室中に「不在」誤判定→巡回撮影が走った(8/30)。90秒に延長
static const int      VOICE_BUDGET    = 5;                      // 1回の滞在で声を出すのは最大5回（2026-09-10 本人：1分に1回・5分まで）
static bool     inEpisode = false;             // 今この場に人がいる一続き
static uint32_t episodeStart = 0;              // その滞在が始まった時刻
static int      voiceUsed = 0;                 // その滞在で声を出した回数
static bool     closeGreeted = false;          // その滞在で、なついている人への喜びをもう出したか
static int      joyToTell = -1;                // 喜んだ段階をクラウドへ知らせる待ち（-1＝なし）。stage コマンドでは立てない

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
static bool announceArrival = false;          // 来訪の瞬間、クラウドへ即報告する印
static void updatePresence(uint32_t now) {
    bool sensed = (now > PIR_WARMUP_MS) && pirNow();
    if (sensed) {
        if (!inEpisode) {                     // 新しい来訪
            inEpisode = true;
            episodeStart = now;
            voiceUsed = 0;
            closeGreeted = false;
            pendingScene = 1;                // 「!」（絵だけ・音は出さない）
            announceArrival = true;           // 8秒の定期を待たず即「occupied」を届ける
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
    prefs.begin("spirit", false);
    wdBoots = prefs.getUChar("wd", 0);         // 見張りが起こし直した起動なら1以上
    // 撤去期かどうかは、誕生の絵と音より**先に**読む。あとで読むと、起き直すたびに
    // 撤去中のキッチンで顔と音が一度だけ出てしまう（2週間のあいだ必ず何度か起き直す）。
    g_hide = prefs.getUChar("hide", 0);
    if (g_hide > 2) g_hide = 0;
    bool hush = QUIET || wdBoots > 0 || hideVoice();  // 見張りの起動し直しでは音を出さない（夜中に鳴らさない）
    if (!hush) chimeBoot();
    if (hideFace()) {                          // 撤去期：誕生の絵も出さず、真っ黒のまま始める
        static const uint8_t BLACK0[2] = {0x00, 0x00};
        fillRect(0, 240, BLACK0);
        havePrev = false;
        blanked = true;
    } else {
        playAnim(anim_hatch, 1);               // 誕生（絵はいつも通り）
        if (!hush) melodyHatch();
    }
    lastMotion = millis();
    nextMurmur = millis() + 3000;
    careCount = prefs.getUInt("care", 0);
    srcIP = prefs.getString("srcIP", srcIP);          // 情報源(目/脳)を記憶から復元
    srcPort = prefs.getUShort("srcPort", srcPort);
    eyeIP = prefs.getString("eyeIP", eyeIP);          // 目のLANアドレスも復元
    // 情報源(目/クラウド脳)との接続: WiFiに参加。繋がらなくても本体は動く
    WiFi.mode(WIFI_STA);
    WiFi.setHostname("spirit-c3");
    WiFi.begin(WIFI_SSID, WIFI_PASS);
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
        ArduinoOTA.setPassword(OTA_PASS);      // 同じWiFiに居ても合言葉なしでは書き込めない
        ArduinoOTA.onStart([]() { otaBusy = true; });
        ArduinoOTA.onEnd([]() { otaBusy = false; });
        ArduinoOTA.onError([](ota_error_t) { otaBusy = false; });
        ArduinoOTA.begin();
        WiFi.setSleep(false);                  // 省電力うたた寝をやめて応答を機敏に（電源はUSB給電なので問題なし）
        mUdp.stop();
        mUdp.begin(5006);                      // WiFi確立後に無線コマンド口を開き直す（接続前のbindは不発になることがある）
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
    if (ctlStage >= 0) {                       // stage コマンド（机上で5段階を試すため）
        // 2026-09-21：**人が居ることにしない。**
        // 前の版は lastMotion と inEpisode を書き換えていたので、試験のたびに
        // C3 がクラウドへ「在室」と報告し、**偽の来訪が研究の記録に混ざっていた**
        // （9/19 20:56〜21:03 の7回がそれ）。観察期間に使うと数字が狂う。
        // 本物の滞在の「1回だけ」の数え（closeGreeted）にも触らない。
        // 試験なので毎回出す。90秒あけて滞在を切る必要もなくなった。
        g_stage = ctlStage; ctlStage = -1;
        stageHold = millis() + 60000;          // 1分は上書きしない（stat で読めるように）
        if (g_stage >= 3) pendingScene = 2;    // なついている／べったり → 喜ぶ
    }
    uint32_t now = millis();

    // 撤去期（2026-09-24）：**絵と音を別々に止める。**
    //   1＝画面だけ消す → 音はふだんどおり（喜びの短い曲も鳴る）
    //   2＝画面と声を消す → どちらも出ない
    // 12月にどちらを選んでも、そのとおりに動くようにしておく（あとで焼き直せないため）。
    // 数えるのは止めない（喜びの回数・世話の数え・在室の報告はそのまま）。
    if (pendingScene == 1) {                   // 人が来た → 「!」の絵だけ（音なし・通過でうるさくしない）
        pendingScene = 0;
        sleeping = false;
        if (!hideFace()) playAnim(anim_notice, 1);
        return;
    }
    if (pendingScene == 2) {                   // 喜び
        pendingScene = 0;
        sleeping = false;
        if (!hideVoice()) melodyHappy();       // 画面だけ消すときは、喜びの曲は鳴らす
        if (!hideFace())  playAnim(anim_happy, 1);
        return;
    }
    if (pendingScene == 3) { pendingScene = 0; if (!hideFace()) playAnim(anim_sad, 1); return; }
    if (pendingScene == 4) { pendingScene = 0; if (!hideFace()) playAnim(anim_sleep, 2); return; }
    if (pendingScene == 5) {
        pendingScene = 0;
        if (!hideFace()) playAnim(anim_hatch, 1);
        if (!QUIET && !hideVoice()) melodyHatch();
        return;
    }

    if (!sleeping && now - lastMotion > SLEEP_AFTER_MS) sleeping = true;
    // 2026-09-19：眠りの絵は、クラウドとのやりとりの「あと」で出す（下）。
    // 以前はここで return していたので、眠っている間（10分気配なし〜）は /m も /presence も
    // 取りに行かず、クラウドからは「C3が止まった」と見えていた。

    if (now >= nextMurmur) {                  // 独り言（声だけ・滞在中は上限つき）
        // 字幕はやめた（2026-09-03）。声で届くなら、言葉を読ませる必要がない。
        // 読ませると相手は画面を見にいく。地霊は見るものではなく、居るもの。
        winIdx = (winIdx + 1) % N_WINS;        // 声の抑揚の選択に今も使っている
        bool staying = inEpisode && (now - episodeStart >= STAY_MS);   // 通過でなく居続けている
        if (!QUIET && !hideVoice() && staying && voiceUsed < VOICE_BUDGET) {
            // クラウドの日本語だけで喋る。届かなければ黙る。
            // 以前は届かないとあつ森語で鳴いていたが、クラウドが「いまは黙る」と
            // 返すたびに鳴いてしまい、意味のない音になっていた（2026-09-10 本人「いらない」）。
            speakCloud();
            voiceUsed++;
        } else if (voiceOnce) {                // murコマンドの強制発声（テスト用・上限外）
            voiceOnce = false;
            // 撤去期に「画面と声を消す」を選んでいる間は、試験用の口からも音を出さない。
            // ここを開けておくと、12月に誰かが試したひと声が対照期間に混ざり、
            // しかも記録に残らない（2026-09-24）。
            if (hideVoice()) return;
            speakCloud();
        }
        // 何を・いつ鳴らすかはクラウドが決める（1分に1回・滞在の最初の5分）。
        // ここは1分おきに取りに行くだけ。12秒おきだった頃は、3回の上限を
        // 最初の36秒で使い切っていた。
        nextMurmur = millis() + 60000;
    }

    // 来訪の瞬間は定期を待たず即報告（目へLAN直0.3秒級＋クラウドへ1〜2秒）
    if (announceArrival) {
        announceArrival = false;
        eyeNotify(true);                         // まず目（撮影を即止める）
        char t[8];
        httpGet("/presence?state=occupied", t, sizeof t);   // 記録用にクラウドへも
        nextBeat = now + 8000;
    }
    // 在室/不在の定期報告（8秒ごと・目とクラウド両方へ）
    if (now >= nextBeat) {
        nextBeat = now + 8000;
        bool occ = (now - lastMotion) < 90000;   // 死角で途切れても90秒は在室扱い（撮影の誤発火防止）
        eyeNotify(occ);
        char t[8];
        httpGet(occ ? "/presence?state=occupied" : "/presence?state=empty", t, sizeof t);
    }
    // 目からM・Nを取りに行く（10秒ごと）。"M N flag" を受けてonMNへ
    if (now >= nextPoll) {
        nextPoll = now + 10000;
        char body[48];
        // 起動して最初の1回は、なぜ起動したかを添える（on＝電源／wd＝見張り）。クラウドが記録に残す
        // 喜んだあとの1回は、その段階を添える（joy・2026-09-21）。誰に喜んだかはクラウドが知っている
        static bool bootTold = false;
        char path[24];
        if (!bootTold)          snprintf(path, sizeof path, "/m?boot=%s", wdBoots ? "wd" : "on");
        else if (joyToTell > 0) snprintf(path, sizeof path, "/m?joy=%d", joyToTell);
        else                    strcpy(path, "/m");
        if (httpGet(path, body, sizeof body)) {
            if (bootTold) joyToTell = -1;          // 届いた → 喜んだ知らせは済んだ（起動の知らせと同時には送らない）
            bootTold = true;
            if (wdBoots) { wdBoots = 0; prefs.putUChar("wd", 0); }   // 通った → 見張りの回数を戻す
            float m, n; int f, s = 0, li = -1, hd = -1;
            int got = sscanf(body, "%f %f %d %d %d %d", &m, &n, &f, &s, &li, &hd);
            if (got >= 2) onMN(m, n);
            // 6つめ＝撤去期に何を消すか（2026-09-24）。0=ふつう 1=画面だけ 2=画面と声。
            // ここは入りにも切りにも使う。手元の命令ではなくクラウドが持ち主なので、
            // 起動し直しても10秒で正しい状態に戻る（2週間のあいだ必ず何度か起き直すため）。
            if (got >= 6 && hd >= 0 && hd <= 2 && hd != g_hide) {
                g_hide = hd;
                prefs.putUChar("hide", (uint8_t)hd);   // 起動直後の数秒も顔を出さないため
                havePrev = false;                       // 戻したときに画面ぜんぶを描き直す
            }
            // 5つめ＝いま名前を聞いているか（2026-09-23）。速さは無線の合図に任せ、
            // ここは「入りの合図が届かなかったとき」の直し。最大10秒遅れるが、出遅れは埋まる。
            //
            // **切るのには使わない**（12:08 の実機で分かった）。クラウドは無線の合図を知らないので、
            // 10秒後の問い合わせで必ず「聞いていない」と答え、せっかく入った状態を打ち消していた。
            // 名前を聞く一往復は30秒〜1分かかるので、それでは顔が途中で戻ってしまう。
            // 切るのは無線の `listen 0` と、25秒の自動戻しに任せる。
            if (got >= 5 && li > 0 && !listening()) listenUntil = millis() + LISTEN_MAX_MS;
            if ((int32_t)(millis() - stageHold) >= 0)      // 手入れ中（1分）は上書きしない
                g_stage = (got >= 4 && s >= 0 && s <= 4) ? s : 0;
            // 段階の出し分け（まず1つ・2026-09-19）：なついている人（段階3以上）だと分かったら、
            // その滞在で1回だけ喜ぶ。顔が分かるのは来てから少しあとなので、「!」とは別に出る。
            if (inEpisode && !closeGreeted && g_stage >= 3) {
                closeGreeted = true;
                pendingScene = 2;
                joyToTell = g_stage;               // 次の問い合わせでクラウドへ知らせる（本物のときだけ）
            }
        }
    }
    // 見張り：5分つづけて通らなければ起動し直す。3回つづけて駄目なら、次からは1時間おき
    //（クラウドや家のWi-Fiが長く落ちている間、5分おきに起動し直しつづけないため）
    if (millis() - lastHttpOk > (wdBoots < 3 ? 300000UL : 3600000UL)) {
        if (wdBoots < 250) prefs.putUChar("wd", wdBoots + 1);
        Serial.println("WATCHDOG RESTART");
        delay(100);
        ESP.restart();
    }

    // 撤去期（2026-09-24）：画面を真っ黒にして、絵は一切出さない。
    // ここより上（見回りの報告・在室・世話の数え・顔の問い合わせ）は**そのまま動いている**。
    // 眠りや考えている顔より先に見るので、撤去中はどの顔も出ない。
    if (hideFace()) {
        static const uint8_t BLACK[2] = {0x00, 0x00};
        if (havePrev || !blanked) { fillRect(0, 240, BLACK); havePrev = false; blanked = true; }
        delay(20);
        return;
    }
    blanked = false;

    // 名前を聞いている間（2026-09-23）：絵を1コマで止めて、考えている顔と「？」を出す。
    // 絵を止めるのは、コマが動くと目の位置も動いて、描き替える場所が決まらないため。
    // 眠りより先に見る（聞いている最中に眠った顔にならないように）。
    if (listening()) {
        uint32_t t = millis();
        // 喋っている最中は描き直さない（口の動きと取り合って、ちらつくため）
        if (!mouthOn && (!listenDrawn || (int32_t)(t - nextQBob) >= 0)) {
            const uint8_t *p = anim_idle + 1;          // 待ち受けのコマ0
            const uint8_t *pal = p + 2, *grid = p + 14;
            qDown = listenDrawn ? (qDown ? 0 : 1) : 0;  // 「？」を上下にゆらす
            buildThinking(grid, pal, qDown);
            if (!havePrev) fillRect(0, 240, pal);
            drawGrid(gWork, pal, false);
            listenDrawn = true;
            nextQBob = t + 300;
        }
        delay(10);
        return;
    }
    if (listenDrawn) {                          // 聞きおわった → ふつうの絵に戻す
        listenDrawn = false;
        havePrev = false;                       // 次の1枚で画面ぜんぶを描き直す
    }

    if (sleeping) {                            // 眠り（長い無人）
        playAnim(anim_sleep, 1, checkInterrupt);
        return;
    }
    // 気分（2026-09-24 本人の決定）：**いまの散らかり具合だけ**で決める。時計は見ない。
    // 人が居る間は塗り替えない（料理の最中に顔が曇ると、その人を責めることになる）。
    if (!inEpisode) faceSad = (g_M >= M_MESSY);
    if (faceSad) playAnim(anim_sad, 1, checkInterrupt);
    else         playAnim(anim_idle, 1, checkInterrupt);
}
