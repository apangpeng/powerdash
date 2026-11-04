# -*- coding: utf-8 -*-
"""生成初始 data.js（示例数据）。运行: python gen_data.py
结构与 fetch_sgcc.py 输出保持一致，仅 mock 标记不同。
"""
import json
import math
from datetime import date, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent

labels = []
d = date(2026, 9, 26)
for _ in range(25):
    labels.append(str(d.month) + "/" + str(d.day))
    d += timedelta(days=1)


def seq(n, base, amp):
    return [round(base + math.sin(i / 3.1) * amp + (i % 5) * 0.8, 1) for i in range(n)]


daily = [12.4, 16.8, 22.1, 18.3, 25.6, 31.2, 27.4, 19.8, 15.2, 24.5,
         33.7, 29.1, 21.6, 26.8, 34.2, 38.5, 30.3, 17.9, 13.6, 20.4,
         28.7, 23.5, 18.2, 12.9, 8.4]

obj = {
    "meta": {
        "title": "用电总览",
        "asOf": "2026-10-24",
        "accountLabel": "国网8449",
        "mock": True,
    },
    "kpis": [
        {"label": "本月用电", "value": 336.76, "unit": "kWh", "trend": "距月末预计约 435 kWh", "bar": 77, "masked": False},
        {"label": "昨日用电", "value": 8.36, "unit": "kWh", "trend": "较近两周日均低约 40%", "bar": 22, "masked": False},
        {"label": "账户余额", "value": 1286.5, "unit": "元", "trend": "上月结余 ¥312.80*，余额充足", "bar": 64, "masked": True},
    ],
    "summary": [
        {"k": "日均用电", "v": 14.0, "unit": "度", "masked": False},
        {"k": "预计月末", "v": 435, "unit": "度", "masked": False},
        {"k": "上月结余", "v": 312.8, "unit": "元", "masked": True},
        {"k": "当前余额", "v": 1286.5, "unit": "元", "masked": True},
    ],
    "stats": [
        {"k": "上月用电", "v": 883.0, "unit": "kWh", "masked": False},
        {"k": "上月电费", "v": 676.57, "unit": "元", "masked": False},
        {"k": "本年电费", "v": 6182.7, "unit": "元", "masked": True},
    ],
    "details": [
        {"k": "账户余额", "v": 1286.5, "unit": "元", "masked": True},
        {"k": "本月用电量", "v": 336.76, "unit": "kWh", "masked": False},
        {"k": "昨日用电量", "v": 8.36, "unit": "kWh", "masked": False},
        {"k": "上月电量", "v": 883.0, "unit": "kWh", "masked": False},
        {"k": "上月电费", "v": 676.57, "unit": "元", "masked": False},
    ],
    "charts": {
        "daily": {"labels": labels, "values": daily},
        "tou": {
            "labels": labels,
            "jian": seq(25, 9, 4),
            "feng": seq(25, 18, 7),
            "ping": seq(25, 12, 5),
            "gu": seq(25, 6, 3),
        },
        "monthly": {
            "labels": ["2025-10", "2026-02", "2026-06", "2026-10"],
            "energy": [960.0, 780.7, 596.3, 410.0],
            "fee": [735.4, 598.1, 456.8, 314.1],
        },
    },
}

js = (
    "/* 用电总览看板 - 数据文件\n"
    " * 页面所有数字由此文件提供；可由 fetch_sgcc.py 自动更新，也可手动修改（兜底）。\n"
    " * 结构说明见 README.md。\n"
    " */\n"
    "window.DASHBOARD_DATA = " + json.dumps(obj, ensure_ascii=False, indent=2) + ";\n"
)

with open(BASE / "data.js", "w", encoding="utf-8") as f:
    f.write(js)

print("data.js written, chars:", len(js))
