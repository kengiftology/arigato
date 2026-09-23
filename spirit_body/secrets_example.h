// 合言葉のひな形（2026-09-23）。**このファイルは git に上がる。本物の値は書かない。**
//
// 本物は、どのトークの作業コピーからも同じ場所を見られるように、共通の場所に置く：
//     C:/Users/kengk/.arigato/spirit_secrets.h
// このファイルをそこへ写して、値を本物に書き換える。git には上がらない場所なので安全。
//
// 作り方（PowerShell・1行）：
//     Copy-Item spirit_body\secrets_example.h "$env:USERPROFILE\.arigato\spirit_secrets.h"
// そのあと、メモ帳などで開いて3つの値を書き換える。
//
// 2026-09-23 まで、ここの3つは同じ文字列がそのまま `spirit_body.ino` に書かれていて、
// 公開リポジトリから誰でも読めた。Wi-Fi・C3への書き込み・観察メモの3つが同じ合言葉だったので、
// 1つ見えたら3つとも開く状態だった。

#pragma once

#define WIFI_SSID "ここにWi-Fiの名前"
#define WIFI_PASS "ここにWi-Fiの合言葉"
#define OTA_PASS  "ここにC3へ書き込むときの合言葉"
