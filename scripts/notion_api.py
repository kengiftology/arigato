# -*- coding: utf-8 -*-
"""Notion のタスクDBを読み書きする小さな口（2026-09-24）。

鍵は ~/.arigato/notion.env（NOTION_TOKEN=...）を先に見る。
無ければ research-os の中の既存スクリプトから読む（当面の互換）。鍵は画面に出さない。
"""
import json
import os
import pathlib
import re
import urllib.request

TASK_DB = "3e4a51ac-655f-8112-aca6-def9695cabcd"   # 10/5までのタスク（大中小）
CASE_DB = "3d0a51ac-655f-810a-ba25-e8b7b24e3a59"   # 事例DB


def token() -> str:
    env = pathlib.Path.home() / ".arigato" / "notion.env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("NOTION_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"')
    src = pathlib.Path.home() / "research-os/thesis/references/notion_shohyo.py"
    return re.search(r'TOKEN\s*=\s*"([^"]+)"', src.read_text(encoding="utf-8")).group(1)


def api(path: str, method: str = "GET", body=None):
    req = urllib.request.Request(
        "https://api.notion.com/v1/" + path, method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + token(),
                 "Notion-Version": "2022-06-28",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def text_of(prop) -> str:
    return "".join(t["plain_text"] for t in (prop.get("rich_text") or prop.get("title") or []))


def sel(prop):
    return (prop.get("select") or {}).get("name")


def tasks() -> list:
    out, cur = [], None
    while True:
        body = {"page_size": 100}
        if cur:
            body["start_cursor"] = cur
        d = api(f"databases/{TASK_DB}/query", "POST", body)
        for r in d["results"]:
            p = r["properties"]
            out.append({
                "id": r["id"],
                "num": text_of(p["番号"]),
                "name": text_of(p["名前"]),
                "why": text_of(p["なぜ要るか"]),
                "level": sel(p["階層"]),
                "group": sel(p["大項目"]),
                "state": sel(p["状態"]),
                "who": sel(p["担当"]),
                "must": p["凍結前必須"]["checkbox"],
                "due": (p["期限"].get("date") or {}).get("start"),
            })
        cur = d.get("next_cursor")
        if not d.get("has_more"):
            break
    out.sort(key=lambda t: [int(x) if x.isdigit() else 0 for x in t["num"].split(".")])
    return out


def add_task(num, name, who, due, level="小", group=None, why="", must=False):
    props = {
        "名前": {"title": [{"text": {"content": name}}]},
        "番号": {"rich_text": [{"text": {"content": num}}]},
        "階層": {"select": {"name": level}},
        "状態": {"select": {"name": "未着手"}},
        "期限": {"date": {"start": due}},
        "凍結前必須": {"checkbox": must},
    }
    if who:
        props["担当"] = {"select": {"name": who}}
    if group:
        props["大項目"] = {"select": {"name": group}}
    if why:
        props["なぜ要るか"] = {"rich_text": [{"text": {"content": why}}]}
    return api("pages", "POST", {"parent": {"database_id": TASK_DB}, "properties": props})


def set_state(page_id, state):
    return api("pages/" + page_id, "PATCH", {"properties": {"状態": {"select": {"name": state}}}})
