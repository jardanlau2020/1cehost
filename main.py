#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""1cehost（dash.icehost.pl）自动续期 —— 已迁移到 renew-kit。

公共部分（结果语义 / 报告排版 / TG 通知 / 退出码 / 环境变量读取）交给 renewkit，
本文件只保留 1cehost 自己的业务：Cookie 注入 → 过 CF 盾 → 密码登录兜底 →
点「ADD 6 HOURS VALIDITY」→ 用到期时间差验证真的加了 6 小时。

迁移带来的行为变化（每一条都是实盘日志推出来的，不是拍脑袋）：

1. 「未到窗口」不再靠猜。实盘（#237 / #239）显示：窗口没开时续期按钮
   **照样在页面上**，真正的信号是**点击之后**弹出的红框。所以点击前只用
   语义明确的强特征把关，宽松特征留到点击后作佐证 —— 漏判只是多点一次
   （面板会再弹红框，无害），误判却会让脚本永远不点，服务器到期被
   suspend（不可逆）。方向上宁可多点是刻意的。

2. 到期时间读不到时**照点**。按钮在不在不是窗口信号，把「读不到到期时间」
   当成「窗口没开」会直接漏点（就是上面那条的后果）。读不到就点，事后用
   UNKNOWN 如实上报。注意这条与 aclclouds 的取舍相反：那边按钮语义模糊，
   读不到剩余天数就不点；这边按钮语义明确，不点才是错。

3. 跳过时**不发 TG**。本仓是每小时 cron，窗口没开是常态，一天 48 条垃圾
   消息没人受得了。只有 续期成功 / 已达上限 / 结果未确认 / 真失败 才打扰人。

4. 截图不再「发完就删」。原实现发完 TG 立刻 os.remove，导致同一次运行的
   upload-artifact 永远找不到文件（if-no-files-found: ignore 静默吞掉），
   等于排障产物一直是空的。现在截图留在工作目录，交给 action 上传。

5. 退出码收敛成两档：0 正常（含跳过、上游抖动）、1 真失败需人工。
   原来是 1/2/3 三种散落的 sys.exit，而 workflow 只看「非零」，分不出轻重，
   也没法把「上游 5xx」和「Cookie 失效」区分开。

6. WAF / CF 拦截单独识别。面板靠 session cookie **存在与否**放行（不验有效性），
   所以「WAF - Block」= Cookie 缺失或失效 → FAILED，要人换 ICEHOST_COOKIES；
   Cloudflare 挑战页 → TRANSIENT，下次排程重试即可。

用法（环境变量）：
    ICEHOST_SERVER_URL                面板地址（必填）
    ICEHOST_COOKIES                   Cookie：JSON 数组 / JSON 对象 / Cookie 头文本
    ICEHOST_EMAIL / ICEHOST_PASSWORD  Cookie 失效时的兜底登录
    ICEHOST_ACCOUNT_NAME              账号标签，只影响通知里的显示名
    PROXY_SERVER                      可选，如 socks5://127.0.0.1:1080
    TG_BOT_TOKEN / TG_CHAT_ID         可选
    DRY_RUN=1                         只检查不点击
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from renewkit import Outcome, RenewReport
from renewkit import env
from renewkit import notify
from renewkit.report import shorten

SERVICE = "1cehost"
DEFAULT_DOMAIN = "dash.icehost.pl"

SERVER_URL = env.get("ICEHOST_SERVER_URL")
COOKIES_RAW = env.get("ICEHOST_COOKIES")
EMAIL = env.get("ICEHOST_EMAIL")
PASSWORD = env.get("ICEHOST_PASSWORD")
ACCOUNT = env.get("ICEHOST_ACCOUNT_NAME")
PROXY_SERVER = env.get("PROXY_SERVER")
DRY_RUN = env.dry_run()

#: 面板一次续期 +6 小时；容差给到 5~9 小时，既容得下服务端取整/时区抖动，
#: 又能把「时间没变」「跳了几天」这类异常排除掉。
EXTEND_MIN_S = env.get_int("ICEHOST_EXTEND_MIN_S", 5 * 3600)
EXTEND_MAX_S = env.get_int("ICEHOST_EXTEND_MAX_S", 9 * 3600)

#: 点击后等红框渲染的时间，以及刷新后等 SPA 重新拉数据的时间
CLICK_WAIT_S = env.get_int("ICEHOST_CLICK_WAIT_S", 5)
REFRESH_WAIT_S = env.get_int("ICEHOST_REFRESH_WAIT_S", 5)

SHOT_DIR = env.get("ICEHOST_SHOT_DIR", ".") or "."
SHOT_NAME = "run_screenshot.png"

#: 续期按钮：兼容 ADD 6 HOURS VALIDITY / Dodaj 6 godzin / add 6
#: XPath 里 translate() 把小写化做进表达式，所以大小写怎么写都能命中
RENEW_BTN_XPATH = (
    "//*[not(*) and ("
    "contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'add 6 hours')"
    " or contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'dodaj 6')"
    " or contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'add 6')"
    ")]"
)


# ────────────────────────── 纯逻辑（不碰浏览器，可离线单测） ──────────────────────────

#: Cookie 里必须有这两条：session 是 WAF 的「放行牌」，XSRF 是 XHR 的 CSRF 凭据
REQUIRED_COOKIES = ("icehostpl_session", "XSRF-TOKEN")


def parse_cookies(raw: str, *, domain: str = DEFAULT_DOMAIN) -> list[dict]:
    """把 Secret 里的 Cookie 解析成 SeleniumBase ``add_cookie`` 能吃的 dict 列表。

    支持三种写法（实测三种都有人填）：
      · JSON 数组   ``[{"name": "icehostpl_session", "value": "..."}, ...]``
      · JSON 对象   ``{"cookies": [...]}``，或直接 ``{"icehostpl_session": "...", ...}``
      · Cookie 头   ``icehostpl_session=...; XSRF-TOKEN=...``

    解析结果必须同时含 session 与 XSRF，否则抛 :class:`ValueError` 并说明缺了哪个
    —— 原实现在缺 XSRF 时报「Cookie 缺少: XSRF-TOKEN」，比笼统的「格式错误」好定位。
    """
    text = (raw or "").strip()
    if not text:
        raise ValueError("Cookie 为空")

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = None

    entries: list | None
    if isinstance(data, list):
        entries = data
    elif isinstance(data, dict) and isinstance(data.get("cookies"), list):
        entries = data["cookies"]
    elif isinstance(data, dict):
        # 顶层直接当 name -> value（手抄 JSON 最常见）
        entries = [{"name": k, "value": v} for k, v in data.items()
                   if isinstance(v, (str, int, float))]
    else:
        entries = None

    pairs: dict[str, str] = {}
    if entries is not None:
        for c in entries:
            if isinstance(c, dict) and c.get("name"):
                pairs[str(c["name"])] = str(c.get("value", ""))
    else:
        for part in text.split(";"):
            if "=" in part:
                name, value = part.strip().split("=", 1)
                pairs[name.strip()] = value.strip()

    missing = [n for n in REQUIRED_COOKIES if not pairs.get(n)]
    if missing:
        raise ValueError("Cookie 缺少: " + ", ".join(missing))

    return [
        {
            "name": name,
            # Laravel 的 XSRF-TOKEN 是 base64 再 URL 编码，Cookie 头里抄来的一定是
            # 编码态；unquote 对不含 %xx 的字符串本来就是恒等操作，所以统一解一次
            # 既照顾了 Cookie 头来源，也不会破坏已经解码过的 JSON 来源。
            "value": urllib.parse.unquote(pairs[name]),
            "domain": domain,
            "path": "/",
            "secure": True,
        }
        for name in REQUIRED_COOKIES
    ]


#: 到期时间的标签，中/英/波兰三种语言都见得到。
#:
#: 标签与时间之间用「最多 60 个字符、且中途不许出现另一个日期」的填充来兜住 ——
#: 这样取到的一定是紧跟标签的那个时间。别改成 `[^0-9]{0,40}`：那连 `<h3>` 里的
#: 那个 3 都会把匹配卡死（`</h3>` 第一个数字就是 3），页面换个标签名就静默读不到
#: 到期时间 —— 而这正是「该不该点」的依据。
_GAP = r"(?:(?!\d{4}-\d{2}-\d{2}).){0,60}"
_EXP_RE = re.compile(
    r"(EXPIRATION DATE|Data wygaśnięcia|到期日|expiry)" + _GAP +
    r"(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?)",
    re.IGNORECASE | re.DOTALL,
)


def find_expiry(page_source: str) -> str | None:
    """从页面源码里抠出到期时间，读不到返回 None。"""
    m = _EXP_RE.search(page_source or "")
    return m.group(2) if m else None


def parse_expiry_dt(value: str | None) -> datetime | None:
    """``2026-10-02 18:03`` / ``2026-10-02T18:03`` / 带秒 / 只到日 → datetime。"""
    if not value:
        return None
    s = str(value).strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def fmt_exp(value: str | None) -> str:
    """``2026-10-02 18:03`` → ``10-02 18:03``；读不到给「讀唔到」。"""
    if not value:
        return "讀唔到"
    text = str(value)
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})", text)
    if m:
        return f"{m.group(2)}-{m.group(3)} {m.group(4)}:{m.group(5)}"
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", text)
    if m:
        return f"{m.group(2)}-{m.group(3)}"
    return text[:16]


def extend_ok(before: str | None, after: str | None,
              *, lo: int = EXTEND_MIN_S, hi: int = EXTEND_MAX_S) -> bool:
    """续期前后到期时间差落在 ``[lo, hi]`` 秒内才算真续上。

    只看「变了没有」不够：面板出错也可能把时间改掉。卡住 5~9 小时这个窗口，
    既认得出正常的 +6h，也能把「没变」和「跳了几天」都排除。
    """
    a, b = parse_expiry_dt(before), parse_expiry_dt(after)
    if a is None or b is None:
        return False
    return lo <= (b - a).total_seconds() <= hi


#: 未到窗口时面板弹的红框文案。刻意分强弱两档：
#:  · STRONG 语义明确指向「不能续期」，点击前也用它把关
#:  · WEAK 里的词（recently / next 6 hours）可能出现在无关文案里，
#:    只在点击后当佐证 —— 误判的代价是永远不点，服务器被 suspend，不可逆
LIMIT_STRONG = (
    "nie możesz przedłużyć",
    "niedawno to zrobiłeś",
    "cannot extend",
)
LIMIT_WEAK = (
    "kolejne 6 godziny",
    "next 6 hours",
    "recently",
)


def has_limit_notice(page_source: str, *, strong_only: bool = False) -> bool:
    """页面是否提示「未到可续期时间」。默认含弱特征，点击前请传 strong_only=True。"""
    low = (page_source or "").lower()
    if any(k in low for k in LIMIT_STRONG):
        return True
    if strong_only:
        return False
    return any(k in low for k in LIMIT_WEAK)


#: 面板前面的 WAF / Cloudflare 特征（与 probe.py 的判据保持一致）
WAF_MARKERS = ("connection blocked", "blocked on our waf", "zablokowane", "waf - block")
CF_MARKERS = ("just a moment", "challenges.cloudflare.com", "cf-turnstile", "cf-chl")


def classify_page(page_source: str) -> tuple[Outcome | None, str]:
    """页面级拦截识别。命中返回 ``(结论, 说明)``，没命中返回 ``(None, "")``。

    WAF 放行只验 session cookie「存在」不验「有效」，所以「WAF - Block」等价于
    Cookie 没了 → FAILED 要人换；CF 挑战页只是没过了盾，换次排程可能就过 → TRANSIENT。
    """
    low = (page_source or "").lower()
    if any(m in low for m in WAF_MARKERS):
        return Outcome.FAILED, "面板 WAF 拦截出口（Cookie 缺失或失效，请更新 ICEHOST_COOKIES）"
    if any(m in low for m in CF_MARKERS):
        return Outcome.TRANSIENT, "Cloudflare 挑战页未过"
    return None, ""


LOGIN_MARKERS = ("zaloguj", "logowanie", "sign in", "log in",
                 "remember me", "zapamiętaj mnie", "forgot password")


def looks_logged_out(url: str, page_source: str, has_password_field: bool) -> bool:
    """Cookie 是否已失效。

    判据分两档：URL 带 login、页面上真的渲染出密码框 —— 这两条是硬证据；
    文案关键词只在 SPA 不换 URL 时兜底，所以放最后。
    """
    if "login" in (url or "").lower():
        return True
    if has_password_field:
        return True
    low = (page_source or "").lower()
    return any(m in low for m in LOGIN_MARKERS)


def decide(before: str | None, after: str | None, *,
           limited_before: bool = False, limited_after: bool = False,
           lo: int = EXTEND_MIN_S, hi: int = EXTEND_MAX_S) -> tuple[Outcome, str]:
    """点击前后的观测 → ``(结论, 说明)``。

    抽成纯函数是为了把「红框出现在点击前还是点击后」「到期时间读不到」
    「时间没变」「时间跳了几天」这些组合全都能离线测一遍 —— 浏览器里的分支
    靠人工点不出来。
    """
    if limited_before:
        return Outcome.SKIPPED, "未到续期窗口"
    if extend_ok(before, after, lo=lo, hi=hi):
        return Outcome.RENEWED, ""
    if limited_after:
        return Outcome.SKIPPED, "未到续期窗口（点击后弹出限制提示）"
    if before and after and before == after:
        return Outcome.SKIPPED, "到期时间没变，未到续期窗口"
    if before and after:
        return Outcome.UNKNOWN, f"到期时间异常：{fmt_exp(before)} → {fmt_exp(after)}"
    return Outcome.UNKNOWN, f"{fmt_exp(before)} → {fmt_exp(after)}，未能确认结果"


#: 静默的结果：这两种不打扰人（未到窗口是常态；上游抖动等下次排程即可）
QUIET_OUTCOMES = frozenset({Outcome.SKIPPED, Outcome.TRANSIENT})


def should_notify(report: RenewReport) -> bool:
    """本仓每小时跑一次，窗口没开就静默 —— 否则一天能刷 48 条 TG。"""
    return any(r.outcome not in QUIET_OUTCOMES for r in report.results)


def multipart_body(fields: dict[str, str], file_field: str, filename: str,
                   content: bytes, boundary: str) -> bytes:
    """拼一个 multipart/form-data 请求体（纯函数，方便单测）。"""
    out = bytearray()
    for key, value in fields.items():
        out += f"--{boundary}\r\n".encode()
        out += f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode()
        out += str(value).encode("utf-8") + b"\r\n"
    out += f"--{boundary}\r\n".encode()
    out += (f'Content-Disposition: form-data; name="{file_field}"; '
            f'filename="{filename}"\r\n').encode()
    out += b"Content-Type: image/png\r\n\r\n"
    out += content + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out)


def send_photo(path: str, caption: str) -> bool:
    """把截图发到 TG。失败只打印 —— 通知出问题绝不能影响续期结论。"""
    token, chat_id = notify.config()
    if not token or not chat_id:
        return False
    try:
        content = Path(path).read_bytes()
    except OSError as exc:
        print(f"   ⚠️ 读不到截图 {path}: {exc}", flush=True)
        return False

    boundary = "----renewkit" + uuid.uuid4().hex
    body = multipart_body({"chat_id": chat_id, "caption": caption},
                          "photo", Path(path).name, content, boundary)
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendPhoto",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            ok = bool(json.loads(resp.read().decode("utf-8", "replace")).get("ok"))
    except Exception as exc:
        print(f"   ⚠️ TG 截图发送异常: {exc}", flush=True)
        return False
    print("   📸 截图已发 TG" if ok else "   ⚠️ TG 截图被拒", flush=True)
    return ok


# ────────────────────────── 浏览器流程 ──────────────────────────


@dataclass
class RunResult:
    outcome: Outcome
    detail: str = ""
    expire: str | None = None
    shot: str | None = None
    extra: dict = field(default_factory=dict)


def _shoot(sb, path: str) -> str | None:
    """截图，失败就算了（排障产物而已，不该把续期拖挂）。"""
    try:
        sb.save_screenshot(path)
        return path
    except Exception as exc:
        print(f"   ⚠️ 截图失败: {exc}", flush=True)
        return None


def _visible(sb, selector: str) -> bool:
    try:
        return bool(sb.is_element_visible(selector))
    except Exception:
        return False


def _dump_page(sb) -> None:
    """找不到续期按钮时把现场 dump 出来，省得对着「失败」两个字干瞪眼。"""
    try:
        print(f"[DIAG] 当前 URL: {sb.get_current_url()}", flush=True)
        buttons = sb.find_elements(
            "button, a.btn, input[type=submit], a[href*='renew'], a[href*='extend']")
        print(f"[DIAG] 页面共 {len(buttons)} 个按钮/链接候选:", flush=True)
        for b in buttons[:40]:
            try:
                text = (b.text or b.get_attribute("value") or "").strip().replace("\n", " ")[:60]
                print(f"[DIAG]   <{b.tag_name}> {text!r} href={(b.get_attribute('href') or '')[:80]}",
                      flush=True)
            except Exception:
                pass
        source = sb.get_page_source() or ""
        visible = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", source, flags=re.S | re.I)
        visible = re.sub(r"<[^>]+>", " ", visible)
        visible = re.sub(r"\s+", " ", visible)
        print(f"[DIAG] 页面可见文字(前1200字): {visible[:1200]}", flush=True)
    except Exception as exc:
        print(f"[DIAG] dump 失败: {exc}", flush=True)


def _password_login(sb, shot: str) -> tuple[bool, str]:
    """Cookie 失效时的兜底：用账号密码登。

    两个坑都在这里绕开（实测出来的，不是推测）：
      · WAF 只验 session cookie「存在」不验「有效」。一旦没有 session cookie
        就整个 "WAF - Block"。所以要**保留** icehostpl_session（死值也是放行牌），
        只删 XSRF-TOKEN（死 token 是 "CSRF mismatch" 的元凶），让后端重新派一对。
      · 用当前页 refresh 而不是新导航到 /auth/login —— 新导航会被 WAF 当新访客
        直接拦掉，refresh 有概率过。
    """
    print("🔑 Cookie 失效，改用账号密码登录…", flush=True)
    try:
        for cookie in sb.driver.get_cookies():
            if cookie.get("name") == "XSRF-TOKEN":
                try:
                    sb.driver.delete_cookie(cookie["name"])
                except Exception:
                    pass
        print("   已移除 XSRF-TOKEN（保留 session cookie 作 WAF 放行牌）", flush=True)
    except Exception as exc:
        print(f"   移除 XSRF cookie 异常: {exc}", flush=True)

    try:
        sb.refresh()
        sb.sleep(10)
    except Exception as exc:
        print(f"   refresh 异常: {exc}", flush=True)

    # 表单没渲染就别盲填 —— 空页面上填不出登录，只会拿到一个看不懂的失败
    form_ready = False
    for _ in range(3):
        if _visible(sb, "input[type='password']"):
            form_ready = True
            break
        sb.sleep(5)
    if not form_ready:
        print(f"   登录表单未渲染。URL: {sb.get_current_url()}", flush=True)
        _dump_page(sb)
        return False, "登录页异常：表单没渲染（CF 盾未过或页面结构变动）"

    try:
        sb.uc_gui_click_captcha()
        sb.sleep(10)
    except Exception as exc:
        print(f"   登录页验证盾处理异常（可忽略）: {exc}", flush=True)

    # email 栏实测是 input[name='username'](type=text)，其余按常见写法兜底
    email_locators = ["input[name='username']", "input[type='text']",
                      "input[name='email']", "input[type='email']"]
    filled = False
    for loc in email_locators:
        try:
            sb.update_text(loc, EMAIL)
            print(f"   email 栏已填（{loc}）", flush=True)
            filled = True
            break
        except Exception:
            continue
    if not filled:
        try:
            for el in sb.find_elements("input"):
                itype = (el.get_attribute("type") or "text").lower()
                if itype in ("password", "checkbox", "radio", "hidden", "submit", "button", "file"):
                    continue
                sb.update_text(el, EMAIL)
                print(f"   通用 fallback 已填 email（name={el.get_attribute('name')!r}）", flush=True)
                filled = True
                break
        except Exception as exc:
            print(f"   通用 fallback 失败: {exc}", flush=True)
    if not filled:
        print("   ⚠️ 找不到 email 输入栏，只填密码（多半会失败）", flush=True)

    try:
        sb.update_text("input[type='password']", PASSWORD)
        sb.sleep(2)
        try:
            sb.click('button[type="submit"]')
        except Exception:
            try:
                sb.click('input[type="submit"]')
            except Exception:
                sb.press_keys('input[type="password"]', "\n")
        sb.sleep(15)
        _shoot(sb, shot)
    except Exception as exc:
        return False, f"密码登录过程异常：{shorten(str(exc), 100)}"

    url = sb.get_current_url()
    source = sb.get_page_source()
    if looks_logged_out(url, source, _visible(sb, "input[type='password']")):
        errors = re.findall(
            r"(?:alert|error|invalid|invalid-feedback|text-danger|danger|warning|form-text|message)"
            r"[^>]*>([^<]{4,120})", source or "", re.I)
        print(f"   登录后 URL: {url}", flush=True)
        print(f"   页面错误提示: {errors[:10]}", flush=True)
        return False, "密码登录后仍停在登录页（查 ICEHOST_EMAIL/PASSWORD 或人机验证）"
    return True, ""


def run_browser() -> RunResult:
    """跑一遍完整流程。返回结论，不抛异常（异常由调用方兜）。"""
    from seleniumbase import SB  # 延迟导入：没装 seleniumbase 时也能跑单测

    sb_kwargs: dict = {"uc": True, "xvfb": True}
    if PROXY_SERVER:
        print(f"🌐 使用代理: {PROXY_SERVER}", flush=True)
        sb_kwargs["proxy"] = PROXY_SERVER

    shot = os.path.join(SHOT_DIR, SHOT_NAME)

    with SB(**sb_kwargs) as sb:
        print(f"🌐 访问面板: {SERVER_URL}", flush=True)
        sb.uc_open_with_reconnect(SERVER_URL, reconnect_time=8)
        sb.sleep(5)

        # 1) 先看出口有没有被 WAF/CF 拦 —— 被拦时连登录页都到不了，后面全是噪声
        blocked, why = classify_page(sb.get_page_source())
        if blocked is not None:
            return RunResult(blocked, why, shot=_shoot(sb, shot))

        # 2) 注入 Cookie
        cookies: list[dict] = []
        if COOKIES_RAW:
            try:
                cookies = parse_cookies(COOKIES_RAW)
            except ValueError as exc:
                print(f"⚠️ Cookie 不可用（将只用账号密码登录）: {exc}", flush=True)
        if cookies:
            try:
                for c in cookies:
                    sb.add_cookie(c)
                print("🍪 Cookie 已注入，刷新应用…", flush=True)
                sb.refresh()
                sb.sleep(5)
            except Exception as exc:
                print(f"⚠️ 注入 Cookie 异常（改用密码登录）: {exc}", flush=True)

        # 3) 过 CF 盾：系统级物理点击，SeleniumBase UC 模式的看家本事
        _shoot(sb, shot)
        try:
            print("🖱️ 尝试物理点击 Cloudflare 人机验证…", flush=True)
            sb.uc_gui_click_captcha()
            sb.sleep(10)
            _shoot(sb, shot)
        except Exception as exc:
            print(f"ℹ️ 验证盾跳过或已处理: {exc}", flush=True)

        # 4) 登录状态判定
        blocked, why = classify_page(sb.get_page_source())
        if blocked is not None:
            return RunResult(blocked, why, shot=_shoot(sb, shot))

        if looks_logged_out(sb.get_current_url(), sb.get_page_source(),
                            _visible(sb, "input[type='password']")):
            if not (EMAIL and PASSWORD):
                return RunResult(
                    Outcome.FAILED,
                    "Cookie 已失效，且未配置账号密码（请更新 ICEHOST_COOKIES）",
                    shot=_shoot(sb, shot))
            ok, why = _password_login(sb, shot)
            if not ok:
                return RunResult(Outcome.FAILED, why, shot=shot)
            print("✅ 密码登录成功", flush=True)
            # 登录成功后回到面板首页，避免停在 /auth/login 上找按钮
            sb.uc_open_with_reconnect(SERVER_URL, reconnect_time=8)
            sb.sleep(5)
        else:
            print("✅ Cookie 有效，登录状态正常", flush=True)

        # 5) 未到窗口？这里只用强特征 —— 见文件头第 1 条
        source = sb.get_page_source()
        before = find_expiry(source)
        if has_limit_notice(source, strong_only=True):
            return RunResult(Outcome.SKIPPED, "未到续期窗口", expire=before,
                             shot=_shoot(sb, shot))
        print(f"📅 续期前到期时间: {fmt_exp(before)}", flush=True)

        if DRY_RUN:
            return RunResult(Outcome.SKIPPED, "dry-run：未真正点击续期", expire=before,
                             shot=_shoot(sb, shot))

        # 6) 点续期按钮
        try:
            print("🖱️ 等待续期按钮…", flush=True)
            sb.wait_for_element_visible(RENEW_BTN_XPATH, timeout=15)
        except Exception as exc:
            _dump_page(sb)
            suspended = "Suspended" in (sb.get_page_source() or "")
            detail = ("已被停权（Suspended），续期按钮消失，需人工进面板解封（72h 宽限期后删机）"
                      if suspended else
                      f"找不到续期按钮（ADD 6 HOURS）：{shorten(str(exc), 100)}")
            return RunResult(Outcome.FAILED, detail, expire=before, shot=_shoot(sb, shot))

        print("🖱️ 点击续期按钮…", flush=True)
        sb.click(RENEW_BTN_XPATH)
        sb.sleep(CLICK_WAIT_S)          # 等红框渲染，此时页面还没刷新
        _shoot(sb, shot)
        limited_after = has_limit_notice(sb.get_page_source())

        sb.refresh()                    # 刷新才能读到服务端更新后的到期时间
        sb.sleep(REFRESH_WAIT_S)
        source = sb.get_page_source()
        after = find_expiry(source)
        print(f"📅 续期后到期时间: {fmt_exp(after)}", flush=True)

        outcome, detail = decide(before, after, limited_after=limited_after)
        return RunResult(outcome, detail, expire=after or before, shot=_shoot(sb, shot))


# ────────────────────────── 入口 ──────────────────────────


def main() -> int:
    report = RenewReport(service=SERVICE)
    target = f"{SERVICE}（{ACCOUNT}）" if ACCOUNT else SERVICE

    if not SERVER_URL:
        report.add(target, Outcome.FAILED, detail="缺少 ICEHOST_SERVER_URL")
        return report.finish()

    has_fallback_login = bool(EMAIL and PASSWORD)
    if not COOKIES_RAW and not has_fallback_login:
        report.add(target, Outcome.FAILED,
                   detail="缺少 ICEHOST_COOKIES，且未配置 ICEHOST_EMAIL/PASSWORD")
        return report.finish()

    try:
        result = run_browser()
    except Exception as exc:            # 浏览器/驱动异常
        result = RunResult(Outcome.FAILED, f"{type(exc).__name__}: {shorten(str(exc), 140)}")

    report.add(target, result.outcome, expire=result.expire, detail=result.detail)

    if result.outcome is Outcome.FAILED and result.shot:
        send_photo(result.shot, f"📸 {target} · 排障截图")

    return report.finish(notify_tg=should_notify(report))


if __name__ == "__main__":
    sys.exit(main())
