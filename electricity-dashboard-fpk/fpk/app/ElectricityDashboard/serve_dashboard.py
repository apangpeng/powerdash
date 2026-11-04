# -*- coding: utf-8 -*-
"""用电总览看板 · 本地服务 + 网页一键登录

运行:
  python serve_dashboard.py

然后浏览器打开:  http://127.0.0.1:8776/

功能:
  - 把 electricity-dashboard 目录作为静态站点提供（index.html / data.js 等）
  - 页面右上角「登录」按钮调 /api/login 与 /api/code：
      1) 手机号/密码 -> 保存到 config.json -> 运行 fetch_sgcc.py --send-sms
      2) 若需短信验证 -> 页面出现验证码输入 -> /api/code 运行 fetch_sgcc.py --sms <验证码>
  - 成功后 data.js 更新，页面刷新即显示真实数据，无需手动编辑任何文件

安全:
  - 默认监听 "::"（IPv4 + IPv6 双栈），局域网内可直接访问
    http://<NAS_IP>:8776/；请勿将该端口暴露到公网。
  - 密码保存在本目录 config.json（明文），token 保存在 token.json，勿外传。
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path(__file__).resolve().parent
# 监听地址：默认 "::" 双栈（IPv4 + IPv6 都可达），可用 DASHBOARD_HOST 覆盖
HOST = os.environ.get("DASHBOARD_HOST", "::")
PORT = int(os.environ.get("DASHBOARD_PORT", "8776") or 8776)
# 静态页面根目录：优先环境变量；否则 fpk 结构里页面在 ../ui（index.cgi 同款），
# 源码直跑时 index.html 与脚本同目录，自动探测
def _find_static_dir() -> Path:
    env_dir = os.environ.get("DASHBOARD_STATIC_DIR")
    if env_dir:
        return Path(env_dir)
    for cand in (BASE, BASE.parent / "ui"):
        if (cand / "index.html").is_file():
            return cand
    return BASE

STATIC_DIR = _find_static_dir()
# 运行时数据目录（config.json/token.json/data.js 等）；容器中可挂载卷持久化
DATA_DIR = Path(os.environ.get("DASHBOARD_DATA_DIR", str(BASE)))
DATA_DIR.mkdir(parents=True, exist_ok=True)

_busy = {"flag": False}

APP_VERSION = "1.4.1"

# ------------------------------------------------------------ 访问口令
# 页面/数据/接口统一鉴权：口令正确后发 HttpOnly 会话 cookie（7 天）。
# 防爆破：同一 IP 连续错 5 次锁 15 分钟，锁期内直接拒绝，解锁后错 2 次再锁 30 分钟（翻倍）。
# v1.2.6：会话持久化到数据目录 pd_sessions.json（只存 token 哈希），应用重启后
# 浏览器会话仍有效，无需重新输入口令。
ACCESS_FREE = {"/api/access", "/api/access-set", "/api/status", "/access.html", "/favicon.ico"}
SESSION_DAYS = 7
LOCK_BASE = {"max": 5, "minutes": 15, "relock": 2}
SESSION_FILE = DATA_DIR / "pd_sessions.json"
_sessions: dict[str, float] = {}        # sha256(token) -> 过期时间戳
_fails: dict[str, list] = {}            # ip -> [连续失败次数, 锁定截止时间戳]
_lock_mutex = threading.Lock()


def _hash_tok(tok: str) -> str:
    return hashlib.sha256(tok.encode("utf-8")).hexdigest()


def _load_sessions() -> None:
    """启动时从磁盘恢复会话（跳过已过期项）。"""
    global _sessions
    loaded: dict[str, float] = {}
    try:
        if SESSION_FILE.exists():
            raw = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
            now = time.time()
            for k, v in raw.items():
                try:
                    if float(v) > now:
                        loaded[k] = float(v)
                except (TypeError, ValueError):
                    pass
    except Exception:
        loaded = {}
    _sessions = loaded


def _save_sessions() -> None:
    """把未过期会话写回磁盘（顺带清理过期项）。"""
    with _lock_mutex:
        now = time.time()
        keep = {k: v for k, v in _sessions.items() if v > now}
        _sessions.clear()
        _sessions.update(keep)
        try:
            SESSION_FILE.write_text(json.dumps(keep), encoding="utf-8")
        except Exception:  # noqa: BLE001（磁盘不可写时仅内存生效）
            pass


def access_password() -> str:
    cfg = load_config()
    return str(cfg.get("access_password", "") or "").strip()


def save_access_password(pw: str) -> None:
    cfg_file = DATA_DIR / "config.json"
    cfg = {}
    if cfg_file.exists():
        try:
            cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    cfg["access_password"] = pw
    cfg_file.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def _issue_session() -> str:
    tok = secrets.token_urlsafe(24)
    with _lock_mutex:
        _sessions[_hash_tok(tok)] = time.time() + SESSION_DAYS * 86400
    _save_sessions()
    return tok


def _check_lock(ip: str) -> int:
    """返回剩余锁定秒数；0 表示未锁定。"""
    with _lock_mutex:
        rec = _fails.get(ip)
        if not rec:
            return 0
        until = rec[1]
        if until and until > time.time():
            return int(until - time.time())
        if until:  # 锁已到期：重置计数
            _fails.pop(ip, None)
        return 0


def _record_fail(ip: str) -> int:
    """记录一次失败；返回锁定剩余秒数（>0 表示本次触发锁定）。"""
    with _lock_mutex:
        rec = _fails.get(ip) or [0, 0]
        rec[0] += 1
        if rec[0] >= LOCK_BASE["max"]:
            lock_min = LOCK_BASE["minutes"] * (1 << (rec[0] // LOCK_BASE["max"] - 1))
            rec[1] = time.time() + lock_min * 60
            rec[0] = 0
            left = lock_min * 60
        else:
            left = 0
        _fails[ip] = rec
        return left


def _clear_fails(ip: str) -> None:
    with _lock_mutex:
        _fails.pop(ip, None)


def run_script(args: list[str]) -> dict:
    """运行 fetch_sgcc，返回 {exit, output}。

    fnOS 应用沙箱可能禁止 spawn 子进程（表现为 PermissionError:
    [Errno 13] Permission denied: ''，exec 环节被拒），这里改为在同一进程内
    import 获取模块、在线程 + asyncio 里运行，彻底绕开子进程创建。
    """
    import asyncio
    import contextlib
    import io
    import threading
    from argparse import Namespace

    if str(BASE) not in sys.path:
        sys.path.insert(0, str(BASE))
    try:
        import fetch_sgcc  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001（依赖缺失、模块损坏等）
        return {"exit": -3, "output": f"获取模块加载失败：{exc}"}

    # 将命令行参数映射为 fetch_sgcc.run 需要的 Namespace
    ns = Namespace(days=0, dump=False, sms="", send_sms=False)
    if "--send-sms" in args:
        ns.send_sms = True
    if "--dump" in args:
        ns.dump = True
    if "--days" in args:
        i = args.index("--days")
        if i + 1 < len(args):
            ns.days = int(args[i + 1])
    if "--sms" in args:
        i = args.index("--sms")
        if i + 1 < len(args):
            ns.sms = args[i + 1]

    result: dict = {}

    def _worker() -> None:
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):

                async def _run_with_timeout() -> int:
                    task = asyncio.ensure_future(fetch_sgcc.run(ns))
                    try:
                        return await asyncio.wait_for(task, timeout=120)
                    except asyncio.TimeoutError:
                        task.cancel()
                        return -1

                code = asyncio.run(_run_with_timeout())
            result["exit"] = code
            output = buf.getvalue()
            result["output"] = "\n".join(
                line for line in output.splitlines() if line.strip()
            )[-2000:]
        except fetch_sgcc.StateGridError as exc:
            result["exit"] = 30
            result["output"] = f"接口错误：{exc}"
        except Exception as exc:  # noqa: BLE001
            result["exit"] = 31
            result["output"] = f"未预期错误：{exc}"

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout=130)
    if t.is_alive():
        return {"exit": -1, "output": "获取超时（120 秒），请稍后重试。"}
    return result


def save_config(phone: str, password: str) -> None:
    cfg_file = DATA_DIR / "config.json"
    cfg = {}
    if cfg_file.exists():
        try:
            cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    cfg["phone"] = phone
    cfg["password"] = password
    cfg_file.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def load_config() -> dict:
    cfg_file = DATA_DIR / "config.json"
    if not cfg_file.exists():
        return {}
    try:
        return json.loads(cfg_file.read_text(encoding="utf-8"))
    except Exception:
        return {}


def config_filled() -> bool:
    cfg = load_config()
    return bool(cfg.get("phone") and cfg.get("password"))


def token_valid() -> bool:
    """国网登录 token（token.json）是否仍有效：expires_at 距今 >5 分钟视为有效。"""
    token_file = DATA_DIR / "token.json"
    if not token_file.exists():
        return False
    try:
        obj = json.loads(token_file.read_text(encoding="utf-8"))
        expires_at = float(obj.get("expires_at") or 0)
        return expires_at > time.time() + 300
    except Exception:
        return False


# ------------------------------------------------------------ 每日定时刷新
# v1.2.4：每天早上 refresh_hour（默认 8）点用现有 token 静默获取一次。
# 只复用已有登录会话（async_ensure_login 在 token 有效时不重新登录、
# 不顶手机 App 会话）；token 失效/未登录时跳过当天，等用户网页登录。
# 配置：config.json 的 daily_refresh（缺省 true 开启）与 refresh_hour（缺省 8）。

def _daily_refresh_cfg() -> tuple[bool, int]:
    cfg = load_config()
    enabled = bool(cfg.get("daily_refresh", True))
    try:
        hour = int(cfg.get("refresh_hour", 8))
    except (TypeError, ValueError):
        hour = 8
    if not 0 <= hour <= 23:
        hour = 8
    return enabled, hour


def _start_daily_refresh() -> None:
    import threading

    def _loop() -> None:
        last_day: str | None = None
        while True:
            try:
                now = time.localtime()
                enabled, hour = _daily_refresh_cfg()
                day = time.strftime("%Y-%m-%d", now)
                if enabled and last_day != day and now.tm_hour == hour and not _busy["flag"]:
                    last_day = day
                    if token_valid():
                        _busy["flag"] = True
                        try:
                            result = run_script(["--send-sms"])
                            if result.get("exit") == 0:
                                print(f"[daily-refresh] {day} {hour:02d}:00 获取成功")
                                _dispatch_after_fetch(day, _load_dashboard_obj())
                            elif result.get("exit") == 40:
                                # token_valid 已拦住，理论上不会走到发短信
                                print(f"[daily-refresh] {day} {hour:02d}:00 需要短信验证，已跳过")
                            else:
                                print(f"[daily-refresh] {day} {hour:02d}:00 获取失败 exit={result.get('exit')}")
                        finally:
                            _busy["flag"] = False
                    else:
                        # 登录态失效：跳过获取并邮件提醒（等用户网页登录）
                        print(f"[daily-refresh] {day} {hour:02d}:00 登录态失效，跳过获取")
                        send_daily_mail(None, "【用电总览】自动更新中断")
            except Exception as exc:  # noqa: BLE001
                print(f"[daily-refresh] 定时任务异常：{exc}")
            time.sleep(30)

    threading.Thread(target=_loop, daemon=True, name="daily-refresh").start()


# ------------------------------------------------------------ 邮件推送（v1.2.5）
# 每日获取成功后把看板摘要发到邮箱（QQ 邮箱默认 smtp.qq.com:465，用「授权码」登录）。
# 配置：config.json 的 push 块：
#   "push": {
#     "channel": "mail",
#     "smtp_host": "smtp.qq.com",
#     "smtp_port": 465,
#     "smtp_user": "你的QQ号@qq.com",
#     "smtp_pass": "QQ邮箱SMTP授权码",
#     "mail_to": "收件邮箱（可同发件）"
#   }
# 配置缺失/发送失败只打日志，不影响看板服务。

def _load_dashboard_obj() -> dict | None:
    """读取最新获取的 data.js 数据对象（与 /api/status 同源）。"""
    data_js = DATA_DIR / "data.js"
    if not data_js.exists():
        return None
    try:
        text = data_js.read_text(encoding="utf-8")
        start = text.find("window.DASHBOARD_DATA = ")
        if start < 0:
            return None
        return json.loads(text[start + len("window.DASHBOARD_DATA = "):].rstrip().rstrip(";"))
    except Exception:
        return None


# ------------------------------------------------------------ 授权码混淆存储（v1.3.0）
# config.json 里的 smtp_pass 不再明文保存：存为 "obf:<base64异或>"。
# 旧版明文配置自动兼容：读取时无前缀视为明文直接使用；下次保存自动转混淆。

_OBF_KEY = 0x5A
_OBF_PREFIX = "obf:"


def _obfuscate(v: str) -> str:
    if not v:
        return ""
    import base64
    return _OBF_PREFIX + base64.b64encode(bytes(b ^ _OBF_KEY for b in v.encode("utf-8"))).decode("ascii")


def _deobfuscate(v: str) -> str:
    if not v:
        return ""
    if not v.startswith(_OBF_PREFIX):
        return v  # 旧版明文兼容
    import base64
    try:
        raw = bytes(b ^ _OBF_KEY for b in base64.b64decode(v[len(_OBF_PREFIX):]))
        return raw.decode("utf-8")
    except Exception:
        return ""


def _push_cfg() -> dict:
    """运行时推送配置（smtp_pass 已解混淆为明文）；未配置返回 {}。"""
    cfg = load_config()
    push = (cfg.get("push") or {}) if isinstance(cfg.get("push"), dict) else {}
    if push.get("channel") != "mail":
        return {}
    if not (push.get("smtp_user") and push.get("smtp_pass") and push.get("mail_to")):
        return {}
    push = dict(push)
    push["smtp_pass"] = _deobfuscate(push["smtp_pass"])
    return push


def _push_freq() -> str:
    p = _push_cfg()
    return p.get("freq") if p else ""


def _num(v) -> str:
    if v is None:
        return "—"
    try:
        n = round(float(v), 2)
        return ("%.2f" % n).rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return str(v)


def build_mail_text(obj: dict) -> str:
    kpis = {k["label"]: k for k in (obj.get("kpis") or [])}
    summary = {s["k"]: s for s in (obj.get("summary") or [])}
    stats = {s["k"]: s for s in (obj.get("stats") or [])}
    tier = obj.get("tier")
    m = obj.get("meta") or {}

    lines = [f"【用电总览】{m.get('asOf', '--')} 更新"]
    lines.append("-" * 22)
    mk = kpis.get("本月用电")
    if mk and mk.get("value") is not None:
        lines.append(f"本月用电：{_num(mk['value'])} kWh（{mk.get('trend', '')}）")
    lk = kpis.get("最新日用电")
    if lk and lk.get("value") is not None:
        lines.append(f"最新日用电：{_num(lk['value'])} kWh（{lk.get('trend', '')}）")
    bk = kpis.get("账户余额")
    if bk and bk.get("value") is not None:
        lines.append(f"账户余额：¥{_num(bk['value'])}（{bk.get('trend', '')}）")
    if "日均用电" in summary:
        lines.append(f"日均用电：{_num(summary['日均用电']['v'])} 度")
    if "预计月末" in summary:
        lines.append(f"预计月末：{_num(summary['预计月末']['v'])} 度")
    if "上月用电" in stats:
        lines.append(f"上月用电：{_num(stats['上月用电']['v'])} kWh")
    if "上月电费" in stats:
        lines.append(f"上月电费：¥{_num(stats['上月电费']['v'])} ")
    if "本年电费" in stats:
        lines.append(f"本年电费：¥{_num(stats['本年电费']['v'])}")
    if tier:
        lines.append("-" * 22)
        lines.append(f"阶梯：{tier.get('stage_label', '')}")
        caps = tier.get("caps") or []
        if len(caps) == 2:
            lines.append(f"边界：一档 0-{_num(caps[0])} / 二档 {_num(caps[0])}-{_num(caps[1])} / 三档 {_num(caps[1])}+")
        lines.append(f"年累计：{_num(tier.get('accum'))} kWh")
        split = tier.get("usage_split") or []
        fees = tier.get("fees") or []
        parts = []
        if len(split) >= 2 and split[1]:
            parts.append(f"二档 {_num(split[1])} kWh")
        if len(split) >= 3 and split[2]:
            parts.append(f"三档 {_num(split[2])} kWh")
        if parts and tier.get("month_usage") is not None:
            lines.append(f"上月账单 {_num(tier['month_usage'])} kWh = " + " + ".join(parts))
        fee_parts = []
        if len(fees) >= 1:
            fee_parts.append(f"基础 {_num(fees[0])}")
        if len(fees) >= 2:
            fee_parts.append(f"二档加价 {_num(fees[1])}")
        if len(fees) >= 3:
            fee_parts.append(f"三档加价 {_num(fees[2])}")
        if len(fees) >= 4:
            fee_parts.append(f"合计 {_num(fees[3])}")
        if fee_parts:
            lines.append("电费： " + " / ".join(fee_parts) + " 元")
    lines.append("-" * 22)
    lines.append("数据来自网上国网，发布延迟 1~2 天属正常，仅供个人参考。")
    return "\n".join(lines)


def send_daily_mail(obj: dict | None, subject: str = "【用电总览】每日更新") -> bool:
    push = _push_cfg()
    if not push:
        return False
    if obj is None:
        text = (
            "今日自动获取未执行：网上国网登录态已失效。\n"
            "请打开看板页面，右上角重新登录账号，恢复后自动更新将继续。"
        )
    else:
        text = build_mail_text(obj) + _ai_block(obj, "daily")
    return _send_mail(push, subject, text)


def build_weekly_mail_text(obj: dict) -> str:
    """周报：近 7 天合计/日均/环比 + 本月/余额/上月账单/阶梯。"""
    daily = (obj.get("charts") or {}).get("daily") or {}
    labels = daily.get("labels") or []
    values = daily.get("values") or []
    n = min(7, len(values))
    cur = values[-n:] if n else []
    prev = values[-2 * n:-n] if len(values) >= 2 * n else []
    cur_sum = sum(v for v in cur if v is not None)
    prev_sum = sum(v for v in prev if v is not None)
    cur_avg = (cur_sum / n) if n else None
    prev_avg = (prev_sum / n) if prev else None
    kpis = {k["label"]: k for k in (obj.get("kpis") or [])}
    stats = {s["k"]: s for s in (obj.get("stats") or [])}
    tier = obj.get("tier")

    days_txt = f"{labels[-n]} ~ {labels[-1]}" if labels and n else "-- ~ --"
    lines = [f"【用电总览】周报 {days_txt}"]
    lines.append("-" * 22)
    if n:
        lines.append(f"近 {n} 天用电合计：{_num(cur_sum)} kWh")
        if prev_avg:
            pct = (cur_avg - prev_avg) / prev_avg * 100
            lines.append(f"日均：{_num(cur_avg)} kWh（前 {n} 天日均 {_num(prev_avg)} kWh，环比 {_num(pct)}%）")
        else:
            lines.append(f"日均：{_num(cur_avg)} kWh")
    mk = kpis.get("本月用电")
    if mk and mk.get("value") is not None:
        lines.append(f"本月用电：{_num(mk['value'])} kWh（{mk.get('trend', '')}）")
    bk = kpis.get("账户余额")
    if bk and bk.get("value") is not None:
        lines.append(f"账户余额：¥{_num(bk['value'])}（{bk.get('trend', '')}）")
    if "上月用电" in stats and "上月电费" in stats:
        lines.append(f"上月账单：{_num(stats['上月用电']['v'])} kWh / ¥{_num(stats['上月电费']['v'])}")
    if tier:
        lines.append("-" * 22)
        lines.append(f"阶梯：{tier.get('stage_label', '')}（年累计 {_num(tier.get('accum'))} kWh）")
    lines.append("-" * 22)
    lines.append("数据来自网上国网，发布延迟 1~2 天属正常，仅供个人参考。")
    return "\n".join(lines)


def _balance_of(obj: dict | None):
    """取当前余额（kpis 账户余额优先，其次小结当前余额）。"""
    if not obj:
        return None
    for k in (obj.get("kpis") or []):
        if k.get("label") == "账户余额" and k.get("value") is not None:
            return k["value"]
    for s in (obj.get("summary") or []):
        if s.get("k") == "当前余额" and s.get("v") is not None:
            return s["v"]
    return None


_alert_sent: set[str] = set()   # 已发过余额告警的日期（每天最多一封）


def send_weekly_mail(obj: dict) -> bool:
    push = _push_cfg()
    if not push:
        return False
    return _send_mail(push, "【用电总览】每周更新", build_weekly_mail_text(obj) + _ai_block(obj, "weekly"))


def send_alert_mail(bal, th, day: str) -> bool:
    push = _push_cfg()
    if not push:
        return False
    text = (
        f"账户当前余额 ¥{_num(bal)} 已低于阈值 ¥{_num(th)}。\n"
        "为避免欠费断电，请及时通过网上国网 App 或本页缴费。\n\n"
        "（此提醒每天最多一封；可在页面右上角 ⚙ 设置中调整或关闭）"
    )
    if _send_mail(push, "【用电总览】余额不足提醒", text):
        _alert_sent.add(day)
        return True
    return False


def _dispatch_after_fetch(day: str, obj: dict | None) -> None:
    """定时获取成功后按推送频率分发邮件；登录失效提醒由调用方另行发送。"""
    freq = _push_freq() or "weekly"
    if freq == "daily":
        send_daily_mail(obj)
    elif freq == "weekly":
        if time.localtime().tm_wday == 0:   # 周一
            send_weekly_mail(obj)
    else:  # alert：不发定期摘要，仅余额阈值告警
        if obj is None:
            return
        p = _push_cfg()
        th = 0.0
        if p:
            try:
                th = float(p.get("balance_threshold") or 0)
            except (TypeError, ValueError):
                th = 0.0
        if th > 0 and day not in _alert_sent:
            bal = _balance_of(obj)
            if bal is not None and float(bal) < th:
                send_alert_mail(bal, th, day)


def _send_mail(push: dict, subject: str, text: str) -> bool:
    """按 push 配置发送一封邮件；失败只记录，不影响服务。"""
    try:
        import smtplib
        from email.header import Header
        from email.mime.text import MIMEText

        msg = MIMEText(text, "plain", "utf-8")
        msg["Subject"] = Header(subject, "utf-8")
        msg["From"] = push["smtp_user"]
        msg["To"] = push["mail_to"]
        smtp = smtplib.SMTP_SSL(
            push.get("smtp_host", "smtp.qq.com"),
            int(push.get("smtp_port", 465) or 465),
            timeout=15,
        )
        smtp.login(push["smtp_user"], push["smtp_pass"])
        smtp.sendmail(push["smtp_user"], [push["mail_to"]], msg.as_string())
        smtp.quit()
        print(f"[mail-push] 邮件已发送：{subject}")
        return True
    except Exception as exc:  # noqa: BLE001（配置错误/网络失败只记录，不影响服务）
        print(f"[mail-push] 邮件发送失败：{exc}")
        return False


# ---- 推送配置读写（供设置界面 /api/push-get|set|test 使用）----

def _save_push(push: dict) -> None:
    cfg = load_config()
    cfg["push"] = push
    cfg_file = DATA_DIR / "config.json"
    cfg_file.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def _push_public(push: dict) -> dict:
    """返回给前端的脱敏配置：授权码只回「是否已设置」。"""
    return {
        "channel": "mail",
        "smtp_host": push.get("smtp_host", ""),
        "smtp_port": push.get("smtp_port", 465),
        "smtp_user": push.get("smtp_user", ""),
        "smtp_pass_set": bool(push.get("smtp_pass")),
        "mail_to": push.get("mail_to", ""),
        "freq": push.get("freq") or "weekly",
        "balance_threshold": push.get("balance_threshold", 0),
    }


def _normalize_push(data: dict) -> dict:
    """前端提交 -> 存储形式 push 配置。

    smtp_pass：前端明文新值 -> 混淆后存储；留空 -> 保持 config 原值（已是混淆）。
    freq：daily/weekly/alert，缺省 weekly。balance_threshold：余额告警阈值，0 关闭。
    """
    cfg = load_config()
    cur = (cfg.get("push") or {}) if isinstance(cfg.get("push"), dict) else {}
    try:
        port = int(data.get("smtp_port") or cur.get("smtp_port", 465) or 465)
    except (TypeError, ValueError):
        port = 465
    if not 1 <= port <= 65535:
        port = 465
    freq = str(data.get("freq") or cur.get("freq", "weekly") or "weekly").strip()
    if freq not in ("daily", "weekly", "alert"):
        freq = "weekly"
    try:
        th = float(data.get("balance_threshold", 0) or 0)
    except (TypeError, ValueError):
        th = 0
    if th < 0:
        th = 0
    new_pass = str(data.get("smtp_pass") or "").strip()
    stored_pass = _obfuscate(new_pass) if new_pass else str(cur.get("smtp_pass", ""))
    return {
        "channel": "mail",
        "smtp_host": str(data.get("smtp_host") or "").strip() or str(cur.get("smtp_host", "smtp.qq.com")),
        "smtp_port": port,
        "smtp_user": str(data.get("smtp_user") or "").strip() or str(cur.get("smtp_user", "")),
        "smtp_pass": stored_pass,
        "mail_to": str(data.get("mail_to") or "").strip() or str(cur.get("mail_to", "")),
        "freq": freq,
        "balance_threshold": th,
    }


# ------------------------------------------------------------ AI 用电解读（v1.4.0）
# 方案 A：把聚合用电数据（电量/账单/余额/阶梯）交给云端 LLM 生成自然语言解读，
# 附加到现有周报/日报邮件末尾。只上传用电数据，config.json 里的国网账号密码、
# 访问口令、SMTP 授权码一律不出 NAS。OpenAI 兼容 chat/completions，urllib 直连，
# 无新增依赖；AI 故障只记日志，不影响邮件与看板。

_AI_PROVIDERS = {
    "deepseek": ("https://api.deepseek.com/v1", "deepseek-chat"),
    "qwen":     ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus"),
    "doubao":   ("https://ark.cn-beijing.volces.com/api/v3", ""),
    "custom":   ("", ""),
}


def _ai_cfg() -> dict:
    """运行时 AI 配置（api_key 已解混淆）；未启用/缺 key 返回 {}。"""
    cfg = load_config()
    ai = (cfg.get("ai") or {}) if isinstance(cfg.get("ai"), dict) else {}
    if not ai.get("enabled") or not ai.get("api_key"):
        return {}
    ai = dict(ai)
    ai["api_key"] = _deobfuscate(ai["api_key"])
    return ai


def _ai_public(ai: dict) -> dict:
    """返回给前端的脱敏配置：API Key 只回「是否已设置」。"""
    return {
        "enabled": bool(ai.get("enabled")),
        "provider": ai.get("provider") or "deepseek",
        "base_url": ai.get("base_url") or "",
        "model": ai.get("model") or "",
        "api_key_set": bool(ai.get("api_key")),
    }


def _normalize_ai(data: dict) -> dict:
    """前端提交 -> 存储形式 ai 配置。

    api_key：前端明文新值 -> 混淆后存储；留空 -> 保持 config 原值（已是混淆）。
    provider 决定默认 base_url/model；custom 时手动填。
    """
    cfg = load_config()
    cur = (cfg.get("ai") or {}) if isinstance(cfg.get("ai"), dict) else {}
    provider = str(data.get("provider") or cur.get("provider", "deepseek") or "deepseek").strip()
    if provider not in _AI_PROVIDERS:
        provider = "deepseek"
    base, dmodel = _AI_PROVIDERS[provider]
    base_url = str(data.get("base_url") or "").strip() or str(cur.get("base_url", "") or "").strip()
    if provider != "custom":
        base_url = base
    model = str(data.get("model") or "").strip() or str(cur.get("model", "") or "").strip() or dmodel
    new_key = str(data.get("api_key") or "").strip()
    stored_key = _obfuscate(new_key) if new_key else str(cur.get("api_key", ""))
    enabled = data.get("enabled", cur.get("enabled", False))
    if not isinstance(enabled, bool):
        enabled = bool(cur.get("enabled", False))
    return {
        "enabled": enabled,
        "provider": provider,
        "base_url": base_url,
        "model": model,
        "api_key": stored_key,
    }


def _save_ai(ai: dict) -> None:
    cfg = load_config()
    cfg["ai"] = ai
    cfg_file = DATA_DIR / "config.json"
    cfg_file.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def _build_ai_prompt(obj: dict, mode: str) -> str:
    """把看板数据压缩成纯数字事实（不发任何凭据）。"""
    kpis = {k["label"]: k for k in (obj.get("kpis") or [])}
    summary = {s["k"]: s for s in (obj.get("summary") or [])}
    stats = {s["k"]: s for s in (obj.get("stats") or [])}
    tier = obj.get("tier")
    m = obj.get("meta") or {}
    parts = [f"数据日期：{m.get('asOf', '--')}", f"模式：{'周报' if mode == 'weekly' else '日报'}"]

    def _add(key: str, src: dict, unit: str = "") -> None:
        if key in src and src[key].get("v") is not None:
            parts.append(f"{key}：{_num(src[key]['v'])}{unit}")

    for k, u in (("本月用电", "kWh"), ("日均用电", "kWh"), ("预计月末", "kWh"), ("当前余额", "元")):
        _add(k, summary, u)
    for k, u in (("上月用电", "kWh"), ("上月电费", "元"), ("本年电费", "元")):
        _add(k, stats, u)
    if "本月用电" in kpis and kpis["本月用电"].get("value") is not None:
        parts.append(f"本月用电：{_num(kpis['本月用电']['value'])} kWh（{kpis['本月用电'].get('trend', '')}）")
    if "账户余额" in kpis and kpis["账户余额"].get("value") is not None:
        parts.append(f"账户余额：¥{_num(kpis['账户余额']['value'])}（{kpis['账户余额'].get('trend', '')}）")
    if tier:
        parts.append(f"阶梯档位：{tier.get('stage_label', '')}（年累计 {_num(tier.get('accum'))} kWh）")
    daily = (obj.get("charts") or {}).get("daily") or {}
    labels = daily.get("labels") or []
    values = daily.get("values") or []
    if labels and values:
        # v1.4.1：无读数(null)的日子标注「无数据」，不得当 0 用电传给模型
        recent = [f"{l}:无数据" if v is None else f"{l}:{_num(v)}kWh" for l, v in zip(labels[-7:], values[-7:])]
        if recent:
            parts.append("近7天每日用电：" + "，".join(recent))
    return (
        "\n".join(parts)
        + "\n\n请用 120~220 字给出中文解读：1) 总体情况 2) 与前期相比的变化 3) 值得注意或异常的点 4) 一句实用的省电建议（如峰谷错峰）。标注为“无数据”的日期表示当天无读数（上游未返回），不是真实用电为 0，请勿将其解读为用电异常或据此推断原因。只依据上述数据作答，不要编造。"
    )


def _ai_call(ai: dict, obj: dict, mode: str) -> str:
    """调用云端 LLM（OpenAI 兼容 /chat/completions）；失败返回空串。"""
    if not (ai.get("base_url") and ai.get("model") and ai.get("api_key")):
        return ""
    prompt = _build_ai_prompt(obj, mode)
    try:
        import json as _json
        import urllib.request

        payload = _json.dumps({
            "model": ai["model"],
            "messages": [
                {"role": "system", "content": "你是专业的家庭用电数据分析助手。只依据给定的数据作答，不编造；回答用简体中文，简洁客观。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.4,
            "max_tokens": 600,
        }).encode("utf-8")
        req = urllib.request.Request(
            ai["base_url"].rstrip("/") + "/chat/completions",
            data=payload,
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + ai["api_key"]},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=40) as resp:
            body = resp.read().decode("utf-8")
        data = _json.loads(body)
        text = str(((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
        print(f"[ai-push] {mode} AI 解读生成成功（{len(text)} 字）")
        return text
    except Exception as exc:  # noqa: BLE001（AI 故障只记录，不影响邮件）
        print(f"[ai-push] {mode} AI 解读失败：{exc}")
        return ""


def ai_analyze(obj: dict | None, mode: str) -> str:
    if not obj:
        return ""
    return _ai_call(_ai_cfg(), obj, mode)


def _ai_block(obj: dict | None, mode: str) -> str:
    """邮件正文追加的 AI 解读段；未启用/失败返回空串。"""
    text = ai_analyze(obj, mode)
    if not text:
        return ""
    return "\n\n【AI 解读】\n" + text


class Handler(BaseHTTPRequestHandler):
    server_version = "ElectricDashboard/1.0"

    # ------------------------------------------------------------ 工具

    def _send(self, code: int, body: dict | str, ctype: str = "application/json; charset=utf-8", cookie: str | None = None) -> None:
        data = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
        payload = data.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(payload)

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _cookie_val(self, name: str) -> str:
        raw = self.headers.get("Cookie", "")
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == name:
                return v
        return ""

    def _auth_ok(self) -> bool:
        tok = self._cookie_val("pd_session")
        if not tok:
            return False
        key = _hash_tok(tok)
        exp = _sessions.get(key)
        if exp is None:
            return False
        if exp < time.time():
            with _lock_mutex:
                _sessions.pop(key, None)
            _save_sessions()
            return False
        return True

    def _client_ip(self) -> str:
        return str(self.client_address[0] if self.client_address else "?")

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        try:
            return json.loads(raw)
        except Exception:
            return {}

    def log_message(self, fmt, *args):  # 不打印密码等敏感信息，只打基本请求行
        pass

    # ------------------------------------------------------------ 路由

    def do_OPTIONS(self):  # noqa: N802
        self._send(200, {})

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        # 口令状态（未设置/已设置），口令页与首页共用；未认证只回裁剪字段
        if path in ("/api/status", "/api/status/"):
            cfg = load_config()
            data_js = DATA_DIR / "data.js"
            mock = True
            as_of = None
            if data_js.exists():
                try:
                    text = data_js.read_text(encoding="utf-8")
                    start = text.find("window.DASHBOARD_DATA = ")
                    if start >= 0:
                        obj = json.loads(text[start + len("window.DASHBOARD_DATA = "):].rstrip().rstrip(";"))
                        mock = bool(obj.get("meta", {}).get("mock"))
                        as_of = obj.get("meta", {}).get("asOf")
                except Exception:
                    pass
            body = {
                "server": "ok",
                "version": APP_VERSION,
                "access_set": bool(access_password()),
                "mock": mock,
                "asOf": as_of,
                "busy": _busy["flag"],
                # 登录状态（供顶栏按钮状态化）：账号是否已配置、账号尾号、
                # 国网 token 是否仍有效。均为低敏信息，未认证也返回。
                "config_filled": config_filled(),
                "phone_masked": (cfg.get("phone", "")[:3] + "****") if cfg.get("phone") else "",
                "token_valid": token_valid(),
            }
            self._send(200, body)
            return
        # 访问口令门：除口令页与 API 外，所有页面/data.js 都要会话
        if path not in ACCESS_FREE and not self._auth_ok():
            if path.startswith("/api/"):
                self._send(401, {"status": "error", "message": "未授权：需要访问口令。"})
            else:
                self._redirect("/access.html")
            return
        # 设置类 API 需口令会话（口令门已在上面拦过）
        if path in ("/api/push-get", "/api/push-get/"):
            self._handle_push_get()
            return
        if path in ("/api/ai-get", "/api/ai-get/"):
            self._handle_ai_get()
            return
        # 静态文件；data.js 为运行时数据，优先取数据目录（获取脚本产物），
        # 之后才回退安装目录的静态包。
        # 未抓到过时退回包内的示例数据。
        path = STATIC_DIR
        rel = self.path.lstrip("/").split("?")[0]
        if rel == "data.js":
            runtime = DATA_DIR / "data.js"
            path = runtime if runtime.is_file() else (STATIC_DIR / "data.js")
        elif self.path not in ("/", "/index.html"):
            candidate = (STATIC_DIR / rel).resolve()
            if not str(candidate).startswith(str(STATIC_DIR.resolve())):
                self._send(404, "not found", "text/plain; charset=utf-8")
                return
            path = candidate
        else:
            path = STATIC_DIR / "index.html"
        if not path.is_file():
            self._send(404, "not found", "text/plain; charset=utf-8")
            return
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".png": "image/png",
            ".md": "text/plain; charset=utf-8",
        }.get(path.suffix.lower(), "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(path.stat().st_size))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with open(path, "rb") as f:
            self.wfile.write(f.read())

    def do_POST(self):  # noqa: N802
        path = self.path.split("?")[0]
        if path in ("/api/access", "/api/access/"):
            self._handle_access()
            return
        if path in ("/api/access-set", "/api/access-set/"):
            self._handle_access_set()
            return
        if not self._auth_ok():
            self._send(401, {"status": "error", "message": "未授权：需要访问口令。"})
            return
        if path in ("/api/push-set", "/api/push-set/"):
            self._handle_push_set()
            return
        if path in ("/api/push-test", "/api/push-test/"):
            self._handle_push_test()
            return
        if path in ("/api/ai-set", "/api/ai-set/"):
            self._handle_ai_set()
            return
        if path in ("/api/ai-test", "/api/ai-test/"):
            self._handle_ai_test()
            return
        if _busy["flag"]:
            self._send(409, {"status": "error", "message": "已有获取任务在执行，请稍候。"})
            return
        if path in ("/api/login", "/api/login/"):
            self._handle_login()
            return
        if path in ("/api/code", "/api/code/"):
            self._handle_code()
            return
        self._send(404, {"status": "error", "message": "unknown api"})

    def _handle_access(self) -> None:
        data = self._read_json()
        pw = str(data.get("password", ""))
        ip = self._client_ip()
        left = _check_lock(ip)
        if left > 0:
            self._send(429, {"status": "error", "message": f"尝试次数过多，已锁定，请 {left // 60 + 1} 分钟后再试。"})
            return
        stored = access_password()
        if not stored:
            self._send(400, {"status": "error", "message": "访问口令尚未设置，请先完成首次设置。"})
            return
        if secrets.compare_digest(pw, stored):
            _clear_fails(ip)
            tok = _issue_session()
            self._send(200, {"status": "ok", "message": "口令正确。"},
                       cookie=f"pd_session={tok}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_DAYS * 86400}")
            return
        locked = _record_fail(ip)
        if locked > 0:
            self._send(429, {"status": "error", "message": f"口令错误次数过多，已锁定，请 {locked // 60 + 1} 分钟后再试。"})
        else:
            self._send(403, {"status": "error", "message": "口令错误，请重试。"})

    def _handle_access_set(self) -> None:
        if access_password():
            self._send(403, {"status": "error", "message": "访问口令已设置，不可重复设置。"})
            return
        data = self._read_json()
        pw = str(data.get("password", ""))
        if len(pw) < 8:
            self._send(400, {"status": "error", "message": "口令至少 8 位，请重新输入。"})
            return
        save_access_password(pw)
        ip = self._client_ip()
        _clear_fails(ip)
        tok = _issue_session()
        self._send(200, {"status": "ok", "message": "访问口令已设置。"},
                   cookie=f"pd_session={tok}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_DAYS * 86400}")

    def _handle_push_get(self) -> None:
        cfg = load_config()
        push = (cfg.get("push") or {}) if isinstance(cfg.get("push"), dict) else {}
        self._send(200, {"status": "ok", "push": _push_public(push)})

    def _handle_push_set(self) -> None:
        data = self._read_json()
        push = _normalize_push(data)
        _save_push(push)
        self._send(200, {"status": "ok", "message": "推送配置已保存，立即生效。", "push": _push_public(push)})

    def _handle_push_test(self) -> None:
        data = self._read_json()
        store = _normalize_push(data)
        p = dict(store)
        p["smtp_pass"] = _deobfuscate(store["smtp_pass"])   # 测试要用明文
        if not (p.get("smtp_user") and p.get("smtp_pass") and p.get("mail_to")):
            self._send(400, {"status": "error", "message": "请先填写完整的发件邮箱、授权码与收件邮箱。"})
            return
        text = (
            "【用电总览】测试邮件\n"
            "如果你收到这封邮件，说明邮件推送配置正确。\n"
            "之后会按你选择的频率（每天/每周/仅异常）自动发送用电摘要。"
        )
        ok = _send_mail(p, "【用电总览】测试邮件", text)
        if ok:
            self._send(200, {"status": "ok", "message": "测试邮件已发送，请查收。"})
        else:
            self._send(502, {"status": "error", "message": "发送失败，请检查发件邮箱、授权码与端口（NAS 日志 [mail-push] 行有详细原因）。"})

    def _handle_ai_get(self) -> None:
        cfg = load_config()
        ai = (cfg.get("ai") or {}) if isinstance(cfg.get("ai"), dict) else {}
        self._send(200, {"status": "ok", "ai": _ai_public(ai)})

    def _handle_ai_set(self) -> None:
        data = self._read_json()
        ai = _normalize_ai(data)
        _save_ai(ai)
        self._send(200, {"status": "ok", "message": "AI 解读配置已保存，立即生效。", "ai": _ai_public(ai)})

    def _handle_ai_test(self) -> None:
        data = self._read_json()
        store = _normalize_ai(data)
        key = _deobfuscate(store.get("api_key", ""))
        if not key:
            self._send(400, {"status": "error", "message": "请先填写 API Key。"})
            return
        obj = _load_dashboard_obj()
        if not obj:
            self._send(400, {"status": "error", "message": "暂无看板数据（data.js），请先完成国网账号登录获取。"})
            return
        ai = dict(store)
        ai["api_key"] = key
        text = _ai_call(ai, obj, "daily")
        if not text:
            self._send(502, {"status": "error", "message": "AI 解读生成失败，请检查 API Key/模型/网络（NAS 日志 [ai-push] 行有详细原因）。"})
            return
        self._send(200, {"status": "ok", "message": "AI 解读生成成功。", "analysis": text})

    def _handle_login(self) -> None:
        data = self._read_json()
        phone = str(data.get("phone", "")).strip()
        password = str(data.get("password", ""))
        if not phone or not password:
            self._send(400, {"status": "error", "message": "手机号或密码为空。"})
            return
        save_config(phone, password)
        _busy["flag"] = True
        try:
            result = run_script(["--send-sms"])
        finally:
            _busy["flag"] = False
        if result["exit"] == 0:
            self._send(200, {"status": "ok", "message": "登录成功，数据已更新。", "exit": 0})
        elif result["exit"] == 40:
            self._send(200, {"status": "need_sms", "message": "已发送短信验证码，请输入 6 位验证码。", "exit": 40})
        else:
            self._send(200, {"status": "error", "message": "登录/获取失败", "exit": result["exit"], "output": result["output"]})

    def _handle_code(self) -> None:
        data = self._read_json()
        code = str(data.get("code", "")).strip()
        if not code:
            self._send(400, {"status": "error", "message": "验证码为空。"})
            return
        if not config_filled():
            self._send(400, {"status": "error", "message": "尚未提交手机号/密码，请重新登录。"})
            return
        _busy["flag"] = True
        try:
            result = run_script(["--sms", code])
        finally:
            _busy["flag"] = False
        if result["exit"] == 0:
            self._send(200, {"status": "ok", "message": "登录成功，数据已更新。", "exit": 0})
        else:
            self._send(200, {"status": "error", "message": "验证码校验失败", "exit": result["exit"], "output": result["output"]})


class DualStackHTTPServer(ThreadingHTTPServer):
    """IPv6 双栈监听：一个 socket 同时收 IPv4 与 IPv6（Linux 默认 IPV6_V6ONLY=0）。"""

    address_family = socket.AF_INET6

    def server_bind(self):
        try:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except OSError:
            pass
        super().server_bind()


def main() -> None:
    _load_sessions()
    try:
        server = DualStackHTTPServer((HOST, PORT), Handler)
    except OSError:
        # 环境无 IPv6 时回退 IPv4 全接口
        server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    _start_daily_refresh()
    print("=" * 56)
    print(" 用电总览看板 · 本地服务已启动")
    print(" 局域网访问:  http://<NAS_IP>:8776/   (IPv4 / IPv6 均可)")
    if access_password():
        print(" 访问口令已设置：打开页面需输入口令")
    else:
        print(" 访问口令未设置：首次打开页面时设置（8 位以上）")
    print(" 页面右上角点「登录」即可完成网上国网对接")
    _, refresh_hour = _daily_refresh_cfg()
    print(f" 每日 {refresh_hour:02d}:00 自动获取一次（token 有效时静默更新）")
    if _push_cfg():
        freq = _push_freq()
        freq_txt = {"daily": "每天", "weekly": "每周一", "alert": "仅异常提醒（余额不足/登录失效）"}.get(freq, "每周一")
        print(f" 邮件推送已配置：{freq_txt}发送摘要；登录失效随时提醒")
    else:
        print(" 邮件推送未配置：config.json 的 push 块填好 SMTP 后启用")
    _ai = _ai_cfg()
    if _ai:
        print(f" AI 用电解读已开启：{_ai.get('provider')} / {_ai.get('model')}（附加在周报/日报邮件末尾）")
    else:
        print(" AI 用电解读未开启：设置界面填写 API Key 后启用（可选，仅上传用电数据）")
    print(" 按 Ctrl+C 停止服务")
    print("=" * 56)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止。")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
