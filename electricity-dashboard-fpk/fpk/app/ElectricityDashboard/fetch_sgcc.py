# -*- coding: utf-8 -*-
"""网上国网用电数据抓取脚本 -> 生成 data.js（用电总览看板数据源）

用法（命令行）:
  python fetch_sgcc.py               # 正常抓取并更新 data.js（首次会提示输入短信验证码）
  python fetch_sgcc.py --days 40     # 指定日用电查询天数（1~90，默认取 config.json 的 days）
  python fetch_sgcc.py --dump        # 只抓取原始响应到 debug/ 目录，不写 data.js
  python fetch_sgcc.py --send-sms    # 只触发登录：token 有效则直接抓取；需要验证则发送短信
                                     #   并把 codeKey 存到 pending_sms.json，退出码 40
  python fetch_sgcc.py --sms 123456  # 用短信验证码完成登录并抓取（配合 --send-sms 的 codeKey）

用途（配合网页登录）:
  serve_dashboard.py 会依次调用 --send-sms 与 --sms <验证码>，
  在浏览器「登录」弹窗中完成配置，无需手动编辑 config.json。

前置（见 README.md）:
  1) pip install -r requirements.txt
  2) tools/sgcc_protocol/ 协议目录必须存在（复制自 MIT 开源项目
     github.com/stevenjoezhang/hass-state-grid ）

说明:
  - 登录态(token)保存在 token.json，有效期约 15 天，过期自动重新登录。
  - 国家电网日用电数据一般延迟 1~2 天发布，属正常。
  - 国网每天登录次数有限，连续失败请间隔一段时间再试。
  - 本脚本仅用于读取你自己的账户数据，勿共享账号与 token 文件。

退出码:
  0  成功（已更新 data.js）
  2  配置缺失（config.json 未填手机号/密码）
  10 安全校验未通过（RK008，需要官方 App 验证后重试）
  11 登录失败（账号密码错误等）
  12 网络/接口错误
  20 账户列表为空（未绑定户号）
  30 接口业务错误
  31 未预期错误
  40 已发送短信验证码，等待输入（--send-sms 模式）
"""

from __future__ import annotations

import argparse
import asyncio
import calendar
import json
import os
import shutil
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent
TOOLS_PKG = BASE / "tools"
# 运行时数据目录（config/token/data.js 等落在此处）。fnOS 下由 cmd/main
# 注入 DASHBOARD_DATA_DIR=TRIM_PKGVAR（应用数据目录，可写、升级保留）；
# 本地未设置时默认与脚本同目录（保持原行为）。
DATA_DIR = Path(os.environ.get("DASHBOARD_DATA_DIR", str(BASE)))
DATA_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_FILE = DATA_DIR / "config.json"
DEVICE_STATE_FILE = DATA_DIR / "device_state.json"
TOKEN_FILE = DATA_DIR / "token.json"
PENDING_SMS_FILE = DATA_DIR / "pending_sms.json"
DATA_JS = DATA_DIR / "data.js"
DEBUG_DIR = DATA_DIR / "debug"

sys.path.insert(0, str(TOOLS_PKG))
try:
    from sgcc_protocol.api import (
        StateGridAppApi,
        StateGridAuthenticationError,
        StateGridDeviceVerificationRequired,
        StateGridError,
        StateGridInteractiveChallengeRequired,
    )
    from sgcc_protocol.models import LoginSession
    from sgcc_protocol.synthetic_device import build_device_profile, create_device_state
    import aiohttp
except ImportError as exc:  # pragma: no cover
    print("缺少协议库或依赖：", exc)
    print("请先执行: pip install -r requirements.txt")
    print("并确认 tools/sgcc_protocol/ 目录存在。")
    sys.exit(1)


EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_RK008 = 10
EXIT_AUTH = 11
EXIT_NETWORK = 12
EXIT_NO_ACCOUNT = 20
EXIT_API = 30
EXIT_UNEXPECTED = 31
EXIT_SMS_SENT = 40


# ---------------------------------------------------------------- 配置与状态

def load_config() -> dict:
    if not CONFIG_FILE.exists():
        print(f"缺少配置文件 {CONFIG_FILE}，请先创建并填写手机号/密码。")
        sys.exit(EXIT_CONFIG)
    cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    if not cfg.get("phone") or not cfg.get("password"):
        print("config.json 中 phone / password 为空，请先填写你的网上国网登录信息。")
        sys.exit(EXIT_CONFIG)
    # 阶梯电价配置（缺省按辽宁居民口径；政策调价可改 config.json 的 tier_prices）
    tp = cfg.get("tier_prices") or {}
    cfg["tier_prices"] = {
        "base": float(tp.get("base", 0.5)),
        "tier2_add": float(tp.get("tier2_add", 0.05)),
        "tier3_add": float(tp.get("tier3_add", 0.3)),
    }
    return cfg


def load_or_create_device_state() -> dict:
    if DEVICE_STATE_FILE.exists():
        return json.loads(DEVICE_STATE_FILE.read_text(encoding="utf-8"))
    return create_device_state()


def save_device_state(state: dict) -> None:
    DEVICE_STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_login_session() -> LoginSession | None:
    if not TOKEN_FILE.exists():
        return None
    try:
        return LoginSession.from_dict(json.loads(TOKEN_FILE.read_text(encoding="utf-8")))
    except Exception:
        return None


def save_login_session(api: StateGridAppApi) -> None:
    if api.login_session is None:
        return
    TOKEN_FILE.write_text(
        json.dumps(api.login_session.as_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def save_pending_code_key(code_key: str, phone: str) -> None:
    PENDING_SMS_FILE.write_text(
        json.dumps({
            "codeKey": code_key,
            "phone": phone,
            "created": datetime.now().isoformat(timespec="seconds"),
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_pending_code_key() -> str:
    if not PENDING_SMS_FILE.exists():
        return ""
    try:
        return str(json.loads(PENDING_SMS_FILE.read_text(encoding="utf-8")).get("codeKey", ""))
    except Exception:
        return ""


def clear_pending_code_key() -> None:
    if PENDING_SMS_FILE.exists():
        try:
            PENDING_SMS_FILE.unlink()
        except OSError:
            pass


def log(msg: str) -> None:
    print(datetime.now().strftime("%H:%M:%S"), msg)


# ---------------------------------------------------------------- 登录流程

async def login_step(api: StateGridAppApi, args) -> int:
    """确保已登录。返回退出码：EXIT_OK 成功；EXIT_SMS_SENT 已发短信待验证；其余为失败码。"""
    try:
        await api.async_ensure_login()
        return EXIT_OK
    except StateGridDeviceVerificationRequired:
        code_key = load_pending_code_key() if args.sms else ""
        if code_key:
            await api.async_login(verification_code=args.sms, code_key=code_key)
            clear_pending_code_key()
            return EXIT_OK
        log("服务端要求新设备验证，正在向注册手机号发送短信验证码 ...")
        code_key = await api.async_send_device_verification_sms()
        if args.send_sms:
            save_pending_code_key(code_key, api.username)
            log("短信验证码已发送（已暂存 codeKey，等待网页输入验证码）")
            return EXIT_SMS_SENT
        code = args.sms or input("请输入收到的 6 位短信验证码: ").strip()
        await api.async_login(verification_code=code, code_key=code_key)
        return EXIT_OK
    except StateGridInteractiveChallengeRequired:
        log("RK008：服务端安全校验未通过，需要额外验证。")
        log("建议：先在官方「网上国网」App 中完成登录验证后，隔一段时间再试。")
        return EXIT_RK008
    except StateGridAuthenticationError as exc:
        log(f"登录失败（{exc.code} {exc.message}）")
        if exc.code in ("-200", "-201"):
            log("凭证已失效，已尝试重新登录但未成功，请检查账号密码。")
        return EXIT_AUTH
    except StateGridError as exc:
        log(f"登录失败：{exc}")
        return EXIT_NETWORK


# ---------------------------------------------------------------- 数据映射

def _r2(v):
    return None if v is None else round(float(v), 2)


def build_dashboard_data(account, readings, total, yearly, balance, as_of: date, cfg: dict | None = None) -> dict:
    today = as_of
    # 接口返回的日用电为倒序（最新日期在前），先按日期升序，保证
    # 「最新一条」与图表时间轴（左旧右新）正确。
    readings = sorted(readings, key=lambda r: r.day)
    month_readings = [
        r for r in readings
        if r.day.year == today.year and r.day.month == today.month
    ]
    month_total = _r2(
        sum(r.usage for r in month_readings if r.usage is not None) or None
    )
    latest = next((r for r in reversed(readings) if r.usage is not None), None)
    latest_bill = yearly.bills[-1] if yearly.bills else None

    # 阶梯档位与拆分：按年累计电量 + 阶梯边界（firstRefPq=一档上限，
    # secondRefPq=二档区间长度），与 App 账单口径一致（levelStdCode 不是档位号）
    tier_code = ""
    tier_accum = None
    tier_breakdown = None
    if yearly.raw:
        for item in yearly.raw.get("list") or []:
            if latest_bill and str(item.get("ym", "")) == latest_bill.month.strftime("%Y%m"):
                tier_code = str(item.get("levelStdCode", "") or "")
                try:
                    tier_accum = float(item.get("levelAccuPq"))
                except (TypeError, ValueError):
                    tier_accum = None
                try:
                    first_cap = float(item.get("firstRefPq"))
                    second_span = float(item.get("secondRefPq"))
                except (TypeError, ValueError):
                    first_cap = second_span = None
                break
        if first_cap is not None and second_span is not None and tier_accum is not None:
            second_cap = first_cap + second_span
            month_usage_bill = latest_bill.usage if latest_bill else None
            prev_accum = tier_accum - (month_usage_bill or 0.0)
            prices = (cfg or {}).get("tier_prices") or {}
            base_p = float(prices.get("base", 0.5))
            add2 = float(prices.get("tier2_add", 0.05))
            add3 = float(prices.get("tier3_add", 0.3))
            def _seg(lo, hi) -> float:
                return max(0.0, min(tier_accum, hi) - max(prev_accum, lo))
            t1_usage = _seg(0.0, first_cap)
            t2_usage = _seg(first_cap, second_cap)
            t3_usage = _seg(second_cap, tier_accum)
            base_fee = _r2((month_usage_bill or 0.0) * base_p)
            add2_fee = _r2(t2_usage * add2)
            add3_fee = _r2(t3_usage * add3)
            if tier_accum > second_cap:
                stage = 3
            elif tier_accum > first_cap:
                stage = 2
            else:
                stage = 1
            tier_breakdown = {
                "stage": stage,
                "stage_label": "第" + "一二三"[stage - 1] + "阶梯",
                "accum": _r2(tier_accum),
                "caps": [_r2(first_cap), _r2(second_cap)],
                "usage_split": [_r2(t1_usage), _r2(t2_usage), _r2(t3_usage)],
                "fees": [base_fee, add2_fee, add3_fee, _r2(base_fee + add2_fee + add3_fee)],
            }
    TIER_NAMES = {"1": "第一档", "2": "第二档", "3": "第三档"}
    tier_label = TIER_NAMES.get(tier_code, "") or (f"第{tier_code}档" if tier_code else "")

    daily_avg = None
    est_eom = None
    latest_day = latest.day if latest else today
    if month_total is not None and latest_day.day > 0:
        daily_avg = round(month_total / latest_day.day, 1)
        est_eom = round(daily_avg * calendar.monthrange(today.year, today.month)[1])

    balance_val = balance.balance if balance else None
    amount_due = balance.amount_due if balance else None
    balance_trend = "预付费账户余额" if (balance and balance.cons_type == "1") else "后付费账户应交金额"
    if balance_val is None and amount_due is not None:
        balance_trend = "后付费账户应交金额"
    # 预付费账户：上月结余 = 预存余额（上期结转）；后付费账户无此概念
    prev_balance = balance.prepay_balance if balance and balance.cons_type == "1" else None

    month_bar = 0
    if est_eom and month_total is not None:
        month_bar = max(0, min(100, int(month_total / est_eom * 100)))

    kpis = [
        {
            "label": "本月用电",
            "value": month_total,
            "unit": "kWh",
            "trend": f"截至 {latest_day.strftime('%m-%d')} 累计",
            "bar": month_bar,
            "masked": False,
        },
        {
            "label": "最新日用电",
            "value": latest.usage if latest else None,
            "unit": "kWh",
            "trend": f"数据日期 {latest_day.strftime('%m-%d')}（上游延迟1~2天）" if latest else "暂无数据",
            "bar": 0,
            "masked": False,
        },
        {
            "label": "账户余额",
            "value": balance_val if balance_val is not None else amount_due,
            "unit": "元",
            "trend": balance_trend,
            "bar": 0,
            "masked": False,
        },
    ]

    summary = [
        {"k": "日均用电", "v": daily_avg, "unit": "度", "masked": False},
        {"k": "预计月末", "v": est_eom, "unit": "度", "masked": False},
        {"k": "上月结余", "v": prev_balance, "unit": "元", "masked": False},
        {"k": "当前余额", "v": balance_val if balance_val is not None else amount_due, "unit": "元", "masked": False},
    ]

    stats = [
        {"k": "上月用电", "v": _r2(latest_bill.usage) if latest_bill else None, "unit": "kWh", "masked": False},
        {"k": "上月电费", "v": _r2(latest_bill.charge) if latest_bill else None, "unit": "元", "masked": False},
        {"k": "本年电费", "v": _r2(yearly.charge), "unit": "元", "masked": False},
    ]

    details = []
    if tier_breakdown:
        bd = tier_breakdown
        details.append({"k": "基础电费", "v": bd["fees"][0], "unit": "元", "masked": False})
        for label, usage, fee in (
            ("二档加价", bd["usage_split"][1], bd["fees"][1]),
            ("三档加价", bd["usage_split"][2], bd["fees"][2]),
        ):
            if usage and usage > 0:
                u_show = int(usage) if float(usage).is_integer() else usage
                details.append({"k": f"{label}（{u_show} kWh）", "v": fee, "unit": "元", "masked": False})
    elif tier_label:
        tier_v = tier_label
        if tier_accum is not None:
            acc_show = int(tier_accum) if float(tier_accum).is_integer() else tier_accum
            tier_v = f"{tier_label} · 年累计 {acc_show} kWh"
        details.append({"k": "电费档位", "v": tier_v, "unit": "", "masked": False})

    daily_labels = [r.day.strftime("%m/%d") for r in readings]
    # v1.4.1：无读数的日期保留 null（勿填 0），前端显示为缺口，AI 解读标注「无数据」
    daily_values = [r.usage for r in readings]

    charts = {
        "daily": {"labels": daily_labels, "values": daily_values},
        "tou": {
            "labels": daily_labels,
            "jian": [r.tip for r in readings],
            "feng": [r.peak for r in readings],
            "ping": [r.flat for r in readings],
            "gu": [r.valley for r in readings],
        },
        "monthly": {
            "labels": [b.month.strftime("%Y-%m") for b in yearly.bills],
            "energy": [b.usage or 0 for b in yearly.bills],
            "fee": [b.charge or 0 for b in yearly.bills],
        },
    }

    return {
        "meta": {
            "title": "用电总览",
            "asOf": today.isoformat(),
            "accountLabel": account.masked_number or account.name,
            "mock": False,
        },
        "kpis": kpis,
        "summary": summary,
        "stats": stats,
        "details": details,
        "tier": (
            {
                "stage": bd["stage"],
                "stage_label": bd["stage_label"],
                "accum": bd["accum"],
                "caps": bd["caps"],
                "usage_split": bd["usage_split"],
                "fees": bd["fees"],
                "month_usage": _r2(latest_bill.usage) if latest_bill else None,
            }
            if tier_breakdown
            else None
        ),
        "charts": charts,
    }


def write_data_js(obj: dict) -> None:
    header = (
        "/* 用电总览看板 - 数据文件（由 fetch_sgcc.py 自动生成）\n"
        " * 结构说明见 README.md；手动修改会被下次抓取覆盖。\n"
        " */\n"
    )
    payload = "window.DASHBOARD_DATA = " + json.dumps(obj, ensure_ascii=False, indent=2) + ";\n"
    tmp = DATA_JS.with_suffix(".js.tmp")
    tmp.write_text(header + payload, encoding="utf-8")
    if DATA_JS.exists():
        shutil.copy2(DATA_JS, DATA_JS.with_suffix(".js.bak"))
    os.replace(str(tmp), str(DATA_JS))
    log("已更新 data.js（旧版本备份为 data.js.bak）")


def dump_raw(name: str, payload) -> None:
    DEBUG_DIR.mkdir(exist_ok=True)
    target = DEBUG_DIR / name
    if hasattr(payload, "as_dict"):
        payload = payload.as_dict()
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    log(f"已保存原始响应到 {target.name}")


# ---------------------------------------------------------------- 主流程

async def fetch_and_write(api: StateGridAppApi, cfg: dict, args) -> int:
    days = max(1, min(90, args.days or int(cfg.get("days", 30))))
    accounts = api.accounts
    if not accounts:
        log("账户列表为空：请确认已在网上国网绑定用电户号。")
        return EXIT_NO_ACCOUNT
    idx = int(cfg.get("account_index", 0) or 0)
    account = accounts[idx] if idx < len(accounts) else accounts[0]
    log(f"使用户号 {account.masked_number}（{account.name}）")

    end = date.today()
    start = end - timedelta(days=days - 1)
    log(f"查询日用电量 {start} ~ {end}")
    readings, total = await api.async_query_daily_usage(account, start, end)
    log(f"日用电记录 {len(readings)} 条，区间合计 {total} kWh")

    log(f"查询 {end.year} 年月度账单")
    yearly = await api.async_query_monthly_bills(account, end.year)
    if yearly.bills:
        b = yearly.bills[-1]
        log(f"最近出账月 {b.month.strftime('%Y-%m')}: {b.usage} kWh / {b.charge} 元")

    log("查询账户余额")
    balance = await api.async_query_account_balance(account)
    if balance:
        log(f"余额: {balance.balance} 元（应交: {balance.amount_due} 元）")

    if args.dump:
        dump_raw("readings.json", [r.as_dict() for r in readings])
        dump_raw(
            "monthly_bills.json",
            {
                "year": yearly.year,
                "usage": yearly.usage,
                "charge": yearly.charge,
                "bills": [b.as_dict() for b in yearly.bills],
            },
        )
        dump_raw("monthly_bills_raw.json", yearly.raw if getattr(yearly, "raw", None) else {})
        dump_raw(
            "balance.json",
            {
                "cons_type": balance.cons_type,
                "sum_money": balance.sum_money,
                "account_balance": balance.account_balance,
                "prepay_balance": balance.prepay_balance,
                "history_owe": balance.history_owe,
                "penalty": balance.penalty,
                "date": balance.date,
                "balance": balance.balance,
            }
            if balance
            else {},
        )
        dump_raw("balance_raw.json", balance.raw if balance and getattr(balance, "raw", None) else {})
        log("--dump 模式：未更新 data.js（原始响应在 debug/，阶梯档位调试用）")
    else:
        data = build_dashboard_data(account, readings, total, yearly, balance, as_of=end, cfg=cfg)
        write_data_js(data)
        log("完成")
    return EXIT_OK


async def run(args) -> int:
    cfg = load_config()

    state = load_or_create_device_state()
    profile, state = build_device_profile(
        state,
        province=str(cfg.get("province", "") or ""),
        city=str(cfg.get("city", "") or ""),
        region=str(cfg.get("region", "") or ""),
    )
    save_device_state(state)

    saved = load_login_session()
    # 国网网关 28630 端口使用自签名证书，公共 CA 无法校验；
    # 目标地址固定且流量仍为 TLS 加密，故禁用证书校验以完成连接。
    connector = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(connector=connector) as http:
        api = StateGridAppApi(
            http,
            username=cfg["phone"],
            password=cfg["password"],
            profile=profile,
            login_session=saved,
        )

        rc = await login_step(api, args)
        if rc != EXIT_OK:
            return rc
        save_login_session(api)
        log("登录成功")

        return await fetch_and_write(api, cfg, args)


def main() -> None:
    parser = argparse.ArgumentParser(description="网上国网用电数据 -> data.js")
    parser.add_argument("--days", type=int, default=0, help="日用电查询天数 1~90")
    parser.add_argument("--dump", action="store_true", help="只保存原始响应，不更新 data.js")
    parser.add_argument("--sms", type=str, default="", help="短信验证码")
    parser.add_argument("--send-sms", action="store_true", help="发送短信验证码后退出（供网页登录流程调用）")
    args = parser.parse_args()
    try:
        code = asyncio.run(run(args))
    except StateGridError as exc:
        log(f"接口错误：{exc}")
        code = EXIT_API
    except Exception as exc:  # noqa: BLE001
        log(f"未预期错误：{exc}")
        code = EXIT_UNEXPECTED
    sys.exit(code)


if __name__ == "__main__":
    main()
