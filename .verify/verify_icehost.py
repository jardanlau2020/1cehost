#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""1cehost 续期脚本（renew-kit 迁移）验收 harness。

不碰真浏览器、不发真网络请求，全部用替身。三块：

  [A] 纯逻辑单测 —— Cookie 解析 / 到期时间解析 / 续期差值 / 红框特征 /
      页面分类 / 登录态判定 / 结果决策 / 通知门控 / multipart 拼装
  [B] run_browser() 场景矩阵 —— 浏览器层换成假实现，逐场景断言 Outcome 与
      「到底点没点续期按钮」。重点守三条不变量：
        · 读不到到期时间 → 照点（按钮在不在不是窗口信号），事后报 UNKNOWN
        · 点击后出现红框 → SKIPPED，且**不发** TG
        · WAF/CF 拦截 → 绝不进入续期流程
  [C] 静态与一致性 —— 旧实现的坑不得回归（散落的 exit 2/3、发完 TG 就删截图、
      硬编码 /tmp）；workflow 与 README 必须和代码对得上

用法：
    python .verify/verify_icehost.py            # 自动找 renew-kit
    RENEWKIT_PATH=/path/to/renew-kit python .verify/verify_icehost.py
退出码 0 = 全绿。
"""
from __future__ import annotations

import ast
import importlib.util
import io
import os
import re
import shutil
import subprocess
import sys
import tokenize
import types
from contextlib import redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent                                   # _sync/1cehost
SCRIPT = ROOT / "main.py"
WORKFLOW = ROOT / ".github" / "workflows" / "direct-renew.yml"
PROXY_SH = ROOT / "scripts" / "setup_proxy.sh"
README = ROOT / "README.md"

#: 仓库里实际存在的 workflow（probe / warp-probe / discover 已在远端确认存在，
#: 本地只 stage 了 direct-renew.yml，所以分两个集合来校验 README 的引用）
REMOTE_WORKFLOWS = {"direct-renew.yml", "probe.yml", "warp-probe.yml", "discover.yml"}
STAGED_WORKFLOWS = {"direct-renew.yml"}

#: 有默认值的调参项，不要求 workflow 一定传
OPTIONAL_ENV = {"ICEHOST_EXTEND_MIN_S", "ICEHOST_EXTEND_MAX_S", "ICEHOST_CLICK_WAIT_S",
                "ICEHOST_REFRESH_WAIT_S", "ICEHOST_SHOT_DIR"}

#: 迁移前的原始判据，用来证明语义没被偷偷改掉
ORIGINAL_LIMIT_KEYWORDS = ("Nie możesz przedłużyć", "niedawno to zrobiłeś",
                           "kolejne 6 godziny", "cannot extend", "recently",
                           "next 6 hours")
ORIGINAL_EXTEND_WINDOW = (18000, 32400)          # 5h ~ 9h，与老实现一致


# ----------------------------------------------------------------- 基础设施

class Checks:
    def __init__(self) -> None:
        self.ok = 0
        self.fails: list[str] = []
        self.skips: list[str] = []

    def section(self, title: str) -> None:
        print(f"\n{title}")

    def check(self, name: str, cond: bool, extra: str = "") -> bool:
        if cond:
            self.ok += 1
            print(f"  \u2705 {name}")
        else:
            tag = f"  [{extra}]" if extra else ""
            self.fails.append(name + tag)
            print(f"  \u274c {name}{tag}")
        return bool(cond)

    def eq(self, name: str, got, want) -> bool:
        return self.check(name, got == want, f"got={got!r} want={want!r}")

    def skip(self, name: str, why: str) -> None:
        self.skips.append(name)
        print(f"  \u26aa SKIP {name} — {why}")

    def report(self) -> int:
        print("\n" + "=" * 62)
        total = self.ok + len(self.fails)
        if self.fails:
            print(f"\u274c {len(self.fails)}/{total} 项失败")
            for f in self.fails:
                print(f"   - {f}")
        else:
            print(f"\u2705 全部通过（{self.ok} 项）"
                  + (f"，{len(self.skips)} 项跳过" if self.skips else ""))
        return 1 if self.fails else 0


def strip_docstrings(src: str) -> str:
    """把所有 docstring 挖空（保留行号），用于「代码里不许出现 X」这类断言。

    不这么做会被解释性注释误伤：本脚本的 docstring 里特意写了「原来用
    sys.exit(1/2/3)」「原实现发完 TG 就 os.remove 截图」来说明为什么不那么做，
    直接全文匹配就会把它们当成违规。
    """
    tree = ast.parse(src)
    lines = src.splitlines()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef,
                                 ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        body = getattr(node, "body", None) or []
        if body and isinstance(body[0], ast.Expr) \
                and isinstance(body[0].value, ast.Constant) \
                and isinstance(body[0].value.value, str):
            doc = body[0]
            for i in range(doc.lineno - 1, doc.end_lineno):
                lines[i] = ""
    return "\n".join(lines)


def strip_comments(src: str) -> str:
    """去掉 ``#`` 注释（保留行号）。

    和 strip_docstrings 配合使用：本脚本的注释里会写「别改成 `[^0-9]{0,40}`」
    这类反面教材，不挖掉就会被「代码里不许出现 X」的断言误判成违规。
    """
    try:
        comments = [tok for tok in tokenize.generate_tokens(io.StringIO(src).readline)
                    if tok.type == tokenize.COMMENT]
    except tokenize.TokenError:
        return src
    lines = src.splitlines()
    for tok in comments:
        row, col = tok.start
        lines[row - 1] = lines[row - 1][:col]
    return "\n".join(lines)


def find_renewkit() -> Path | None:
    """优先用本地源码（开发时与 renew-kit 同工作区），找不到就退回已安装的包。"""
    override = os.environ.get("RENEWKIT_PATH")
    if override:
        return Path(override)
    for cand in (ROOT.parents[1] / "renew-kit",
                 ROOT.parent / "renew-kit",
                 Path.home() / "renew-kit"):
        if (cand / "renewkit" / "__init__.py").is_file():
            return cand
    return None


def install_stubs() -> bool:
    """本机没有 requests 时塞一个最小替身，好让 renewkit 能被 import。

    renewkit.http 顶层 `import requests`，但本 harness 只用到
    outcome / report / env / notify，不会真的发请求。返回是否装了替身。
    """
    if importlib.util.find_spec("requests") is not None:
        return False

    req = types.ModuleType("requests")

    class RequestException(Exception):
        pass

    class Session:
        def __init__(self, *a, **k):
            self.headers = {}

    req.RequestException = RequestException
    req.Session = Session
    req.Response = type("Response", (), {})
    sys.modules["requests"] = req

    adapters = types.ModuleType("requests.adapters")

    class HTTPAdapter:
        def __init__(self, *a, **k):
            pass

    adapters.HTTPAdapter = HTTPAdapter
    req.adapters = adapters
    sys.modules["requests.adapters"] = adapters

    u3 = types.ModuleType("urllib3")
    sys.modules["urllib3"] = u3
    u3util = types.ModuleType("urllib3.util")
    sys.modules["urllib3.util"] = u3util
    u3retry = types.ModuleType("urllib3.util.retry")

    class Retry:
        def __init__(self, *a, **k):
            pass

    u3retry.Retry = Retry
    sys.modules["urllib3.util.retry"] = u3retry
    return True


def load_renewkit(renewkit: Path | None) -> None:
    if renewkit is not None:
        sys.path.insert(0, str(renewkit))
    else:
        try:
            import renewkit  # noqa: F401
        except ImportError:
            raise SystemExit(
                "找不到 renewkit：先 `pip install renewkit`，"
                "或用 RENEWKIT_PATH 指向 renew-kit 源码目录")


#: 影响脚本行为的全部环境变量 —— 每次加载模块前先清干净，免得上一个场景串味
MANAGED_ENV = (
    "ICEHOST_SERVER_URL", "ICEHOST_COOKIES", "ICEHOST_EMAIL", "ICEHOST_PASSWORD",
    "ICEHOST_ACCOUNT_NAME", "PROXY_SERVER", "DRY_RUN",
    "TG_BOT_TOKEN", "TG_CHAT_ID", "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID",
    "ICEHOST_EXTEND_MIN_S", "ICEHOST_EXTEND_MAX_S", "ICEHOST_SHOT_DIR",
)

_load_counter = [0]


def load_main(**envs):
    """按给定环境变量重新加载 main.py。

    模块级常量是在 import 时读的，所以要换环境就得重新 exec 一遍；
    用递增的模块名保证每次都真的重跑，而不是拿 sys.modules 里的缓存。
    """
    for key in MANAGED_ENV:
        os.environ.pop(key, None)
    for key, value in envs.items():
        if value is not None:
            os.environ[key] = str(value)
    _load_counter[0] += 1
    name = f"icehost_main_{_load_counter[0]}"
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# ------------------------------------------------------- [B] 浏览器层替身

class FakeDriver:
    """只提供 _password_login 用到的两个 cookie 操作。"""

    def __init__(self, cookies=None):
        self._cookies = list(cookies if cookies is not None else [
            {"name": "icehostpl_session", "value": "s"},
            {"name": "XSRF-TOKEN", "value": "x"},
        ])
        self.deleted: list[str] = []

    def get_cookies(self):
        return list(self._cookies)

    def delete_cookie(self, name):
        self.deleted.append(name)
        self._cookies = [c for c in self._cookies if c["name"] != name]


class FakeSB:
    """最小 SeleniumBase 替身。

    用一串「页面快照」按调用顺序推进：``open``（第二次起）、``refresh``、``click``
    各推进一格，其余调用只是记录。这样就能把「点击前 / 点击后 / 刷新后」三种页面
    状态分开喂进去 —— 真实分支靠人工点不出来。
    """

    def __init__(self, pages=None, **kwargs):
        self.pages = list(pages or [{}])
        self.kwargs = kwargs
        self._idx = 0
        self._opened = False
        self._url = ""
        self.driver = FakeDriver()
        self.calls: list = []
        self.clicks: list[str] = []
        self.cookies_added: list[dict] = []
        self.screenshots: list[str] = []

    # -- 生命周期
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def _advance(self):
        if self._idx < len(self.pages) - 1:
            self._idx += 1

    def _cur(self) -> dict:
        return self.pages[self._idx]

    # -- 被脚本调用的接口
    def uc_open_with_reconnect(self, url, reconnect_time=0):
        self.calls.append("open")
        if self._opened:
            self._advance()
        self._opened = True
        self._url = url

    def refresh(self):
        self.calls.append("refresh")
        self._advance()

    def click(self, selector):
        self.calls.append("click")
        self.clicks.append(selector)
        self._advance()

    def sleep(self, seconds):
        pass

    def get_page_source(self) -> str:
        return self._cur().get("source", "")

    def get_current_url(self) -> str:
        return self._cur().get("url") or self._url

    def is_element_visible(self, selector) -> bool:
        if selector == "input[type='password']":
            return bool(self._cur().get("password_visible"))
        return False

    def wait_for_element_visible(self, selector, timeout=0):
        if self._cur().get("no_button"):
            raise Exception("element not visible after %ss" % timeout)
        return True

    def find_elements(self, selector):
        return []

    def save_screenshot(self, path):
        self.screenshots.append(path)
        if self._cur().get("screenshot_raises"):
            raise OSError("no display")

    def uc_gui_click_captcha(self):
        self.calls.append("captcha")

    def add_cookie(self, cookie):
        self.cookies_added.append(cookie)

    def update_text(self, selector, text):
        self.calls.append("update_text")

    def press_keys(self, selector, keys):
        self.calls.append("press_keys")


class SBFactory:
    """冒充 `seleniumbase.SB`：每次构造都返回同一个 FakeSB 实例。"""

    def __init__(self, instance: FakeSB):
        self.instance = instance
        self.kwargs_seen: list[dict] = []

    def __call__(self, **kwargs):
        self.kwargs_seen.append(kwargs)
        return self.instance


def install_fake_seleniumbase(factory) -> None:
    mod = types.ModuleType("seleniumbase")
    mod.SB = factory
    sys.modules["seleniumbase"] = mod


def renew_clicks(sb: FakeSB, mod) -> int:
    """只数续期按钮上的点击 —— 密码登录也会 click，不能混进来。"""
    return sum(1 for sel in sb.clicks if sel == mod.RENEW_BTN_XPATH)


#: 一个「窗口开着」的服务器页：有按钮、有到期时间、没有红框
DASH_OK = """
<div><h3>EXPIRATION DATE</h3><span>2026-10-02 12:03</span>
<button>ADD 6 HOURS VALIDITY</button></div>
"""
DASH_AFTER = DASH_OK.replace("2026-10-02 12:03", "2026-10-02 18:03")
DASH_LIMITED = DASH_OK + '<div class="alert">Nie możesz przedłużyć serwera, zrobiłeś to niedawno</div>'
LOGIN_PAGE = '<form><input name="username"><input type="password"></form>Zaloguj się'


def scenario(mod, *, pages, **sb_kwargs):
    """跑一次 run_browser()，返回 (RunResult, FakeSB, 日志)。"""
    fake = FakeSB(pages=pages, **sb_kwargs)
    factory = SBFactory(fake)
    install_fake_seleniumbase(factory)
    buf = io.StringIO()
    with redirect_stdout(buf):
        result = mod.run_browser()
    return result, fake, buf.getvalue()


# ----------------------------------------------------------------- 主流程

def main() -> int:
    c = Checks()
    stubbed = install_stubs()
    renewkit = find_renewkit()
    load_renewkit(renewkit)

    src = SCRIPT.read_text(encoding="utf-8")
    code = strip_comments(strip_docstrings(src))

    # ─────────────────────────── [A] 语法 / 导入
    c.section("[A] 语法与导入")
    try:
        ast.parse(src)
        c.check("main.py 语法正确", True)
    except SyntaxError as exc:
        c.check("main.py 语法正确", False, str(exc))
        return c.report()

    mod = load_main(ICEHOST_SERVER_URL="https://dash.icehost.pl/servers",
                    ICEHOST_COOKIES="icehostpl_session=a; XSRF-TOKEN=b",
                    TG_BOT_TOKEN="t", TG_CHAT_ID="c")
    c.check("能加载 main.py", hasattr(mod, "main"))
    c.check("用上了 renewkit 的 Outcome", hasattr(mod.Outcome, "RENEWED"))
    c.check("用上了 renewkit 的 RenewReport", hasattr(mod, "RenewReport"))
    if stubbed:
        c.skip("requests 替身", "本机没装 requests，已注入最小替身")

    # ─────────────────────────── [B1] Cookie 解析
    c.section("[B1] parse_cookies：三种写法都要吃")
    pc = mod.parse_cookies

    got = pc('[{"name":"icehostpl_session","value":"abc"},{"name":"XSRF-TOKEN","value":"d%2Fe"}]')
    c.eq("JSON 数组 → 两条 cookie", [g["name"] for g in got], ["icehostpl_session", "XSRF-TOKEN"])
    c.eq("  XSRF 值做了 URL 解码", got[1]["value"], "d/e")
    c.eq("  域名默认 dash.icehost.pl", got[0]["domain"], "dash.icehost.pl")
    c.check("  secure=True", all(g["secure"] for g in got))

    got = pc('{"cookies":[{"name":"icehostpl_session","value":"a"},{"name":"XSRF-TOKEN","value":"b"}]}')
    c.eq("JSON 对象包一层 cookies → 两条", len(got), 2)

    got = pc('{"icehostpl_session":"a","XSRF-TOKEN":"b"}')
    c.eq("JSON 对象顶层 name->value → 两条", len(got), 2)

    got = pc("icehostpl_session=a; XSRF-TOKEN=b")
    c.eq("Cookie 头文本 → 两条", [g["name"] for g in got], ["icehostpl_session", "XSRF-TOKEN"])
    c.eq("  取到 session 值", got[0]["value"], "a")

    got = pc("icehostpl_session=a; XSRF-TOKEN=b; other=c")
    c.eq("多余字段被忽略", len(got), 2)

    for bad, want in [("", "Cookie 为空"),
                      ("icehostpl_session=a", "Cookie 缺少: XSRF-TOKEN"),
                      ("XSRF-TOKEN=b", "Cookie 缺少: icehostpl_session"),
                      ("[]", "Cookie 缺少: icehostpl_session, XSRF-TOKEN")]:
        try:
            pc(bad)
            c.check(f"坏输入被拒（{bad[:20]!r}）", False, "没抛异常")
        except ValueError as exc:
            c.eq(f"坏输入被拒（{bad[:20]!r}）", str(exc), want)

    # ─────────────────────────── [B2] 到期时间解析
    c.section("[B2] 到期时间解析")
    fe, pd, fx = mod.find_expiry, mod.parse_expiry_dt, mod.fmt_exp

    c.eq("英文标签", fe('<td>EXPIRATION DATE</td><td>2026-10-02 18:03</td>'), "2026-10-02 18:03")
    c.eq("波兰语标签", fe('<td>Data wygaśnięcia</td><td>2026-10-02 18:03</td>'), "2026-10-02 18:03")
    c.eq("中文标签", fe('<td>到期日</td><td>2026-10-02 18:03</td>'), "2026-10-02 18:03")
    c.eq("ISO 的 T 分隔", fe('expiry 2026-10-02T18:03'), "2026-10-02T18:03")
    c.eq("带秒", fe('expiry 2026-10-02 18:03:45'), "2026-10-02 18:03:45")
    # 回归：标签与时间之间夹着带数字的 HTML 时也要读得到。
    # 老实现用 [^0-9]{0,40} 作填充，`</h3>` 里那个 3 就把匹配卡死了 ——
    # 页面换个标签名就静默读不到到期时间，而这正是「该不该点」的依据。
    c.eq("标签用 <h3> 包着（中间有数字）",
         fe('<h3>EXPIRATION DATE</h3><span>2026-10-02 18:03</span>'), "2026-10-02 18:03")
    c.eq("中间夹着 Bootstrap 栅格类名",
         fe('<div class="col-3">EXPIRATION DATE</div><div>2026-10-02 18:03</div>'),
         "2026-10-02 18:03")
    c.eq("取的是紧跟标签的那个时间（不是页面上更晚的）",
         fe('expiry 2026-10-02 18:03 ... 2026-11-01 09:00'), "2026-10-02 18:03")
    c.check("没有标签 → None", fe('<td>2026-10-02 18:03</td>') is None)
    c.check("空串 → None", fe("") is None)
    c.check("None → None", fe(None) is None)

    c.check("解析 datetime", pd("2026-10-02 18:03") is not None)
    c.check("解析 T 分隔", pd("2026-10-02T18:03") is not None)
    c.check("解析带秒", pd("2026-10-02 18:03:45") is not None)
    c.check("解析只到日", pd("2026-10-02") is not None)
    c.check("垃圾串 → None", pd("n/a") is None)

    c.eq("fmt 带时间", fx("2026-10-02 18:03"), "10-02 18:03")
    c.eq("fmt 只到日", fx("2026-10-02"), "10-02")
    c.eq("fmt 读不到", fx(None), "讀唔到")

    # ─────────────────────────── [B3] 续期差值
    c.section("[B3] extend_ok：+6h 才算真续上")
    ek = mod.extend_ok
    c.check("正好 +6h", ek("2026-10-02 12:03", "2026-10-02 18:03"))
    c.check("+5h 下界", ek("2026-10-02 12:03", "2026-10-02 17:03"))
    c.check("+9h 上界", ek("2026-10-02 12:03", "2026-10-02 21:03"))
    c.check("+4h59m 差一点 → 否", not ek("2026-10-02 12:03", "2026-10-02 17:02"))
    c.check("+9h01m 超了 → 否", not ek("2026-10-02 12:03", "2026-10-02 21:04"))
    c.check("时间没变 → 否", not ek("2026-10-02 12:03", "2026-10-02 12:03"))
    c.check("跳了几天 → 否", not ek("2026-10-02 12:03", "2026-10-05 12:03"))
    c.check("倒退了 → 否", not ek("2026-10-02 12:03", "2026-10-02 06:03"))
    c.check("读不到 before → 否", not ek(None, "2026-10-02 18:03"))
    c.check("读不到 after → 否", not ek("2026-10-02 12:03", None))
    c.eq("容差窗口与老实现一致", (mod.EXTEND_MIN_S, mod.EXTEND_MAX_S), ORIGINAL_EXTEND_WINDOW)

    # ─────────────────────────── [B4] 红框特征（强弱两档）
    c.section("[B4] has_limit_notice：点击前只用强特征")
    hl = mod.has_limit_notice

    c.check("强特征命中（波兰语）", hl("Nie możesz przedłużyć serwera", strong_only=True))
    c.check("强特征命中（英文）", hl("You cannot extend right now", strong_only=True))
    c.check("强特征大小写不敏感", hl("NIE MOŻESZ PRZEDŁUŻYĆ", strong_only=True))
    c.check("弱特征在点击后命中", hl("try again in the next 6 hours"))
    c.check("弱特征在点击前**不**命中", not hl("try again in the next 6 hours", strong_only=True))
    c.check("'recently' 只在点击后算数", hl("you did this recently")
            and not hl("you did this recently", strong_only=True))
    c.check("普通页面不命中", not hl(DASH_OK, strong_only=True))
    c.check("空串不命中", not hl("", strong_only=True))

    for kw in ORIGINAL_LIMIT_KEYWORDS:
        c.check(f"老判据仍然认得（{kw}）", hl(f"...{kw}..."))

    # ─────────────────────────── [B5] 页面分类
    c.section("[B5] classify_page：WAF / CF / 正常")
    cp = mod.classify_page

    # has_proxy 是**必填** kwarg：忘了传要直接 TypeError，不许静默走错分支。
    # （上一版这里没有这个参数，于是「代理没起来」和「Cookie 废了」被压成同一个
    #   FAILED —— 实盘 #241 就是这么每小时刷一条红。）
    try:
        cp("blocked on our WAF")
        c.check("has_proxy 必填（漏传直接报错）", False, "竟然没抛异常")
    except TypeError:
        c.check("has_proxy 必填（漏传直接报错）", True)

    c.eq("走代理 + WAF → FAILED", cp("blocked on our WAF", has_proxy=True)[0],
         mod.Outcome.FAILED)
    c.eq("WAF - Block → FAILED", cp("WAF - Block", has_proxy=True)[0], mod.Outcome.FAILED)
    c.eq("Connection Blocked → FAILED", cp("Connection Blocked", has_proxy=True)[0],
         mod.Outcome.FAILED)
    c.check("  走代理那档说明提到换 Cookie",
            "ICEHOST_COOKIES" in cp("blocked on our WAF", has_proxy=True)[1])

    # 实盘 #241：代理没起来 → 直连出口被 WAF 拦。出口脏了不是业务失败，
    # 标红就是一天 24 条噪音 → TRANSIENT（exit 0、静默、下个小时重试）。
    c.eq("直连 + WAF → TRANSIENT（不标红）", cp("blocked on our WAF", has_proxy=False)[0],
         mod.Outcome.TRANSIENT)
    c.check("  直连那档指向代理、不提 Cookie",
            "代理" in cp("blocked on our WAF", has_proxy=False)[1]
            and "ICEHOST_COOKIES" not in cp("blocked on our WAF", has_proxy=False)[1])

    c.eq("CF 挑战页 → TRANSIENT", cp("Just a moment...", has_proxy=True)[0],
         mod.Outcome.TRANSIENT)
    c.eq("cf-turnstile → TRANSIENT", cp('<div class="cf-turnstile">', has_proxy=True)[0],
         mod.Outcome.TRANSIENT)
    c.eq("正常页 → None", cp(DASH_OK, has_proxy=True)[0], None)
    c.eq("空串 → None", cp("", has_proxy=True)[0], None)
    c.eq("WAF 优先于 CF", cp("Just a moment ... blocked on our WAF", has_proxy=True)[0],
         mod.Outcome.FAILED)

    # ─────────────────────────── [B6] 登录态
    c.section("[B6] looks_logged_out")
    llo = mod.looks_logged_out

    c.check("URL 带 login", llo("https://dash.icehost.pl/auth/login", "", False))
    c.check("有密码框", llo("https://dash.icehost.pl/servers", "", True))
    c.check("文案命中 Zaloguj", llo("https://dash.icehost.pl/", "Zaloguj się", False))
    c.check("文案大小写不敏感", llo("https://dash.icehost.pl/", "SIGN IN", False))
    c.check("已登录页 → 否", not llo("https://dash.icehost.pl/servers", DASH_OK, False))
    c.check("登录页文本命中 Log in", llo("https://dash.icehost.pl/", "Please Log in", False))

    # ─────────────────────────── [B7] 决策矩阵
    c.section("[B7] decide：点击前后的观测 → 结论")
    dc = mod.decide
    O = mod.Outcome

    cases = [
        ("点击前已有红框", dict(before="2026-10-02 12:03", after="2026-10-02 12:03",
                                limited_before=True), O.SKIPPED),
        ("正常 +6h", dict(before="2026-10-02 12:03", after="2026-10-02 18:03"), O.RENEWED),
        ("点击后红框", dict(before="2026-10-02 12:03", after="2026-10-02 12:03",
                            limited_after=True), O.SKIPPED),
        ("时间没变（无红框）", dict(before="2026-10-02 12:03", after="2026-10-02 12:03"), O.SKIPPED),
        ("时间跳了几天", dict(before="2026-10-02 12:03", after="2026-10-05 12:03"), O.UNKNOWN),
        ("读不到 before", dict(before=None, after="2026-10-02 18:03"), O.UNKNOWN),
        ("读不到 after", dict(before="2026-10-02 12:03", after=None), O.UNKNOWN),
        ("两个都读不到", dict(before=None, after=None), O.UNKNOWN),
        ("+6h 且点击后有红框（矛盾）→ 以数值为准", dict(
            before="2026-10-02 12:03", after="2026-10-02 18:03", limited_after=True), O.RENEWED),
    ]
    for label, kwargs, want in cases:
        got, detail = dc(**kwargs)
        c.eq(f"{label} → {want.value}", got, want)

    c.eq("点击后红框的说明带原因",
         dc(before="2026-10-02 12:03", after="2026-10-02 12:03", limited_after=True)[1],
         "未到续期窗口（点击后弹出限制提示）")
    c.check("只有 FAILED 才让 job 红", not O.UNKNOWN.is_error and not O.SKIPPED.is_error
            and O.FAILED.is_error)

    # ─────────────────────────── [B8] 通知门控
    c.section("[B8] should_notify：跳过时静默（本仓每小时跑）")
    sn = mod.should_notify
    RR = mod.RenewReport

    def rep(*outcomes):
        r = RR(service="1cehost")
        for o in outcomes:
            r.add("t", o)
        return r

    c.check("RENEWED → 发", sn(rep(O.RENEWED)))
    c.check("FAILED → 发", sn(rep(O.FAILED)))
    c.check("UNKNOWN → 发", sn(rep(O.UNKNOWN)))
    c.check("ALREADY_MAX → 发", sn(rep(O.ALREADY_MAX)))
    c.check("SKIPPED → 静默", not sn(rep(O.SKIPPED)))
    c.check("TRANSIENT → 静默", not sn(rep(O.TRANSIENT)))
    c.check("SKIPPED + TRANSIENT → 静默", not sn(rep(O.SKIPPED, O.TRANSIENT)))
    c.check("SKIPPED + RENEWED → 发", sn(rep(O.SKIPPED, O.RENEWED)))
    c.check("空报告 → 静默", not sn(rep()))

    # ─────────────────────────── [B9] multipart
    c.section("[B9] multipart_body")
    body = mod.multipart_body({"chat_id": "123", "caption": "hi"}, "photo", "shot.png",
                              b"\x89PNG-data", "BOUND")
    c.check("开头是 boundary", body.startswith(b"--BOUND\r\n"))
    c.check("chat_id 字段在", b'name="chat_id"\r\n\r\n123\r\n' in body)
    c.check("caption 字段在", b'name="caption"\r\n\r\nhi\r\n' in body)
    c.check("文件字段带 filename", b'name="photo"; filename="shot.png"' in body)
    c.check("Content-Type 是 image/png", b"Content-Type: image/png\r\n\r\n" in body)
    c.check("文件内容原样带进去", b"\x89PNG-data" in body)
    c.check("结尾是结束 boundary", body.endswith(b"--BOUND--\r\n"))

    # ─────────────────────────── [B10] run_browser 场景矩阵
    c.section("[B10] run_browser 场景矩阵（假浏览器）")
    BASE_ENV = dict(ICEHOST_SERVER_URL="https://dash.icehost.pl/servers",
                    ICEHOST_EMAIL="a@b.c", ICEHOST_PASSWORD="pw",
                    ICEHOST_ACCOUNT_NAME="01", TG_BOT_TOKEN="t", TG_CHAT_ID="c")
    COOKIE_ENV = dict(BASE_ENV, ICEHOST_COOKIES="icehostpl_session=a; XSRF-TOKEN=b")

    # 1) 正常续期
    m = load_main(**COOKIE_ENV)
    r, sb, out = scenario(m, pages=[
        {"source": DASH_OK, "url": "https://dash.icehost.pl/servers"},
        {"source": DASH_OK},
        {"source": DASH_AFTER},
    ])
    c.eq("正常续期 → RENEWED", r.outcome, m.Outcome.RENEWED)
    c.eq("  点了 1 次续期按钮", renew_clicks(sb, m), 1)
    c.eq("  注入 2 条 cookie", len(sb.cookies_added), 2)
    c.check("  截图落在工作目录（不是 /tmp）",
            all("/tmp" not in p for p in sb.screenshots))
    c.check("  日志打了续期前后的时间", "12:03" in out and "18:03" in out)

    # 2) 窗口未开：点击后弹红框（实盘 #237/#239 的真实路径）
    r, sb, out = scenario(m, pages=[
        {"source": DASH_OK, "url": "https://dash.icehost.pl/servers"},
        {"source": DASH_OK},
        {"source": DASH_LIMITED},
    ])
    c.eq("点击后红框 → SKIPPED", r.outcome, m.Outcome.SKIPPED)
    c.eq("  仍然点了 1 次（按钮在不在不是窗口信号）", renew_clicks(sb, m), 1)
    c.check("  说明写明是点击后弹的", "点击后" in r.detail)

    # 3) 点击前就有限制提示（强特征）→ 不点
    r, sb, _ = scenario(m, pages=[
        {"source": DASH_LIMITED, "url": "https://dash.icehost.pl/servers"},
    ])
    c.eq("点击前有强红框 → SKIPPED", r.outcome, m.Outcome.SKIPPED)
    c.eq("  一次都没点", renew_clicks(sb, m), 0)

    # 4) 到期时间读不到 → 照点，事后 UNKNOWN
    plain = "<div><button>ADD 6 HOURS VALIDITY</button></div>"
    r, sb, _ = scenario(m, pages=[
        {"source": plain, "url": "https://dash.icehost.pl/servers"},
        {"source": plain},
        {"source": plain},
    ])
    c.eq("读不到到期时间 → UNKNOWN", r.outcome, m.Outcome.UNKNOWN)
    c.eq("  照点 1 次（漏点会被 suspend，不可逆）", renew_clicks(sb, m), 1)

    # 5) dry-run → 不点
    m_dry = load_main(DRY_RUN="1", **COOKIE_ENV)
    r, sb, _ = scenario(m_dry, pages=[
        {"source": DASH_OK, "url": "https://dash.icehost.pl/servers"},
        {"source": DASH_OK},
    ])
    c.eq("dry-run → SKIPPED", r.outcome, m_dry.Outcome.SKIPPED)
    c.eq("  dry-run 不点", renew_clicks(sb, m_dry), 0)

    # 6) ★ 回归实盘 #241：首次裸访吃 WAF - Block，但 Cookie 注入后放行了。
    #    上一版在**注入 Cookie 之前**就判 WAF，于是代理和 Cookie 都好好的也照样红，
    #    一次都没点到。判定点必须在注入之后 —— 这里首页给 WAF、注入后给正常页。
    m2 = load_main(**COOKIE_ENV)
    r, sb, _ = scenario(m2, pages=[
        {"source": "Connection Blocked - blocked on our WAF",
         "url": "https://dash.icehost.pl/"},
        {"source": DASH_OK, "url": "https://dash.icehost.pl/servers"},
        {"source": DASH_AFTER},
    ])
    c.eq("裸访被拦但注入后放行 → 照常 RENEWED", r.outcome, m2.Outcome.RENEWED)
    c.eq("  确实注入了 cookie（没在注入前退出）", len(sb.cookies_added), 2)
    c.eq("  确实点了 1 次续期", renew_clicks(sb, m2), 1)

    # 6b) 注入 Cookie 后**仍然**被拦 + 直连出口 → TRANSIENT（#241 的确切结论）
    r, sb, _ = scenario(m2, pages=[
        {"source": "Connection Blocked - blocked on our WAF",
         "url": "https://dash.icehost.pl/"},
    ])
    c.eq("直连 + 注入后仍被拦 → TRANSIENT", r.outcome, m2.Outcome.TRANSIENT)
    c.eq("  没点过", renew_clicks(sb, m2), 0)
    c.check("  说明指向代理没生效", "代理" in r.detail)

    # 6c) 走了代理还被拦 → 出口是干净的，只剩 cookie 废了 → FAILED 要人换
    m_waf_proxy = load_main(PROXY_SERVER="socks5://127.0.0.1:1080", **COOKIE_ENV)
    r, sb, _ = scenario(m_waf_proxy, pages=[
        {"source": "blocked on our WAF", "url": "https://dash.icehost.pl/"},
    ])
    c.eq("走代理 + 注入后仍被拦 → FAILED", r.outcome, m_waf_proxy.Outcome.FAILED)
    c.check("  说明点名换 ICEHOST_COOKIES", "ICEHOST_COOKIES" in r.detail)
    c.eq("  没点过", renew_clicks(sb, m_waf_proxy), 0)

    # 7) CF 挑战页 → TRANSIENT
    r, sb, _ = scenario(m2, pages=[
        {"source": "<title>Just a moment...</title>", "url": "https://dash.icehost.pl/"},
    ])
    c.eq("CF 挑战页 → TRANSIENT", r.outcome, m2.Outcome.TRANSIENT)
    c.eq("  没点过", renew_clicks(sb, m2), 0)

    # 8) 找不到续期按钮 → FAILED
    #    注意：按钮是在「注入 Cookie 后的那一页」上找的（pages[1]），不是首页
    r, sb, out = scenario(m2, pages=[
        {"source": DASH_OK, "url": "https://dash.icehost.pl/servers"},
        {"source": DASH_OK, "no_button": True},
    ])
    c.eq("找不到按钮 → FAILED", r.outcome, m2.Outcome.FAILED)
    c.eq("  没点过", renew_clicks(sb, m2), 0)
    c.check("  说明提到 ADD 6 HOURS", "ADD 6 HOURS" in r.detail)

    # 9) 被停权（按钮消失 + Suspended）
    r, sb, _ = scenario(m2, pages=[
        {"source": DASH_OK, "url": "https://dash.icehost.pl/servers"},
        {"source": DASH_OK + "<div>Suspended</div>", "no_button": True},
    ])
    c.eq("被停权 → FAILED", r.outcome, m2.Outcome.FAILED)
    c.check("  说明点名停权", "停权" in r.detail)

    # 10) Cookie 失效 + 密码登录成功 → 继续续期
    login_pages = [
        {"source": LOGIN_PAGE, "url": "https://dash.icehost.pl/auth/login"},
        {"source": LOGIN_PAGE, "url": "https://dash.icehost.pl/auth/login"},
        {"source": LOGIN_PAGE, "url": "https://dash.icehost.pl/auth/login",
         "password_visible": True},
        {"source": DASH_OK, "url": "https://dash.icehost.pl/servers"},     # 提交后
        {"source": DASH_OK, "url": "https://dash.icehost.pl/servers"},     # 重新打开
        {"source": DASH_AFTER, "url": "https://dash.icehost.pl/servers"},  # 刷新后
    ]
    r, sb, out = scenario(m2, pages=login_pages)
    c.eq("Cookie 失效 → 密码登录成功 → RENEWED", r.outcome, m2.Outcome.RENEWED)
    c.eq("  续期按钮点了 1 次", renew_clicks(sb, m2), 1)
    c.check("  删掉了 XSRF-TOKEN", "XSRF-TOKEN" in sb.driver.deleted)
    c.check("  保留 session cookie 作 WAF 放行牌",
            "icehostpl_session" not in sb.driver.deleted)
    c.check("  日志确认走了密码登录", "密码登录成功" in out)

    # 11) Cookie 失效 + 没配密码 → FAILED，不点
    m3 = load_main(ICEHOST_SERVER_URL="https://dash.icehost.pl/servers",
                   ICEHOST_COOKIES="icehostpl_session=a; XSRF-TOKEN=b")
    r, sb, _ = scenario(m3, pages=[
        {"source": LOGIN_PAGE, "url": "https://dash.icehost.pl/auth/login"},
        {"source": LOGIN_PAGE, "url": "https://dash.icehost.pl/auth/login"},
    ])
    c.eq("Cookie 失效且无密码 → FAILED", r.outcome, m3.Outcome.FAILED)
    c.eq("  没点过", renew_clicks(sb, m3), 0)
    c.check("  说明提示更新 ICEHOST_COOKIES", "ICEHOST_COOKIES" in r.detail)

    # 12) 登录表单没渲染 → FAILED
    r, sb, _ = scenario(m2, pages=[
        {"source": LOGIN_PAGE, "url": "https://dash.icehost.pl/auth/login"},
        {"source": LOGIN_PAGE, "url": "https://dash.icehost.pl/auth/login"},
        {"source": "<div>empty</div>", "url": "https://dash.icehost.pl/auth/login"},
    ])
    c.eq("登录表单没渲染 → FAILED", r.outcome, m2.Outcome.FAILED)
    c.check("  说明点名表单没渲染", "表单没渲染" in r.detail)

    # 13) 代理参数透传
    m_proxy = load_main(PROXY_SERVER="socks5://127.0.0.1:1080", **COOKIE_ENV)
    fake = FakeSB(pages=[{"source": DASH_OK, "url": "https://dash.icehost.pl/servers"},
                         {"source": DASH_OK}, {"source": DASH_AFTER}])
    factory = SBFactory(fake)
    install_fake_seleniumbase(factory)
    with redirect_stdout(io.StringIO()):
        m_proxy.run_browser()
    c.eq("PROXY_SERVER 透传给 SB", factory.kwargs_seen[0].get("proxy"),
         "socks5://127.0.0.1:1080")
    c.check("uc=True 保留（过 CF 盾靠它）", factory.kwargs_seen[0].get("uc") is True)

    # ─────────────────────────── [B11] main() 入口
    c.section("[B11] main()：缺凭据与退出码")
    m_nourl = load_main(ICEHOST_COOKIES="icehostpl_session=a; XSRF-TOKEN=b")
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = m_nourl.main()
    c.eq("缺 ICEHOST_SERVER_URL → 退出码 1", rc, 1)
    c.check("  说明点名缺哪个变量", "ICEHOST_SERVER_URL" in buf.getvalue())

    m_nocred = load_main(ICEHOST_SERVER_URL="https://dash.icehost.pl/servers")
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = m_nocred.main()
    c.eq("完全没凭据 → 退出码 1", rc, 1)
    c.check("  说明点名两个选择", "ICEHOST_COOKIES" in buf.getvalue()
            and "ICEHOST_EMAIL" in buf.getvalue())

    # Cookie 坏 + 有密码 → 不该直接判死，应该继续走浏览器
    m_badcookie = load_main(ICEHOST_SERVER_URL="https://dash.icehost.pl/servers",
                            ICEHOST_COOKIES="not-a-cookie", ICEHOST_EMAIL="a@b.c",
                            ICEHOST_PASSWORD="pw")
    fake = FakeSB(pages=[{"source": DASH_OK, "url": "https://dash.icehost.pl/servers"},
                         {"source": DASH_OK}, {"source": DASH_AFTER}])
    install_fake_seleniumbase(SBFactory(fake))
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = m_badcookie.main()
    c.eq("Cookie 坏了但有密码 → 仍能续期（退出码 0）", rc, 0)
    c.check("  日志说明 Cookie 不可用", "Cookie 不可用" in buf.getvalue())

    # SKIPPED 时不该发 TG
    m_quiet = load_main(**COOKIE_ENV)
    fake = FakeSB(pages=[{"source": DASH_LIMITED, "url": "https://dash.icehost.pl/servers"}])
    install_fake_seleniumbase(SBFactory(fake))
    sent: list[str] = []
    orig_send = sys.modules["renewkit.notify"].send
    sys.modules["renewkit.notify"].send = lambda text, **k: sent.append(text) or True
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = m_quiet.main()
    finally:
        sys.modules["renewkit.notify"].send = orig_send
    c.eq("SKIPPED → 退出码 0", rc, 0)
    c.eq("SKIPPED → 一条 TG 都不发", len(sent), 0)

    # ★ 回归实盘 #241：直连出口被 WAF 拦 → 必须 exit 0 且一条 TG 都不发。
    #    上一版这里是 exit 1 + 一条「请更新 ICEHOST_COOKIES」，而 Cookie 其实是好的。
    m_trans = load_main(**COOKIE_ENV)
    fake = FakeSB(pages=[{"source": "blocked on our WAF", "url": "https://dash.icehost.pl/"}])
    install_fake_seleniumbase(SBFactory(fake))
    sent_t: list[str] = []
    sys.modules["renewkit.notify"].send = lambda text, **k: sent_t.append(text) or True
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = m_trans.main()
    finally:
        sys.modules["renewkit.notify"].send = orig_send
    c.eq("直连被 WAF 拦 → 退出码 0（实盘 #241 的坑）", rc, 0)
    c.eq("  一条 TG 都不发（每小时 cron，不能刷屏）", len(sent_t), 0)
    c.check("  但日志里挂了 ::warning:: 注解指路",
            "::warning::" in buf.getvalue() and "NODE_LINK" in buf.getvalue())

    # FAILED 时要发。用「走代理仍被 WAF 拦」构造 —— 直连那档已经降级成 TRANSIENT 了。
    m_loud = load_main(PROXY_SERVER="socks5://127.0.0.1:1080", **COOKIE_ENV)
    fake = FakeSB(pages=[{"source": "blocked on our WAF", "url": "https://dash.icehost.pl/"}])
    install_fake_seleniumbase(SBFactory(fake))
    sent2: list[str] = []
    sys.modules["renewkit.notify"].send = lambda text, **k: sent2.append(text) or True
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = m_loud.main()
    finally:
        sys.modules["renewkit.notify"].send = orig_send
    c.eq("FAILED → 退出码 1", rc, 1)
    c.eq("FAILED → 发 1 条 TG", len(sent2), 1)
    c.check("  TG 正文含服务名", "1cehost" in sent2[0])

    # 浏览器/驱动起不来 → main() 得兜住，不能把 traceback 甩给 workflow
    # （甩出去的话 action 只看到 "Process completed with exit code 1"，
    #  连是哪一步挂的都看不出来）
    class Boom:
        def __call__(self, **kwargs):
            raise RuntimeError("chrome not found")

    install_fake_seleniumbase(Boom())
    m_boom = load_main(**COOKIE_ENV)
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = m_boom.main()
    c.eq("浏览器起不来 → 退出码 1", rc, 1)
    c.check("  报告里带异常类型与原文", "RuntimeError: chrome not found" in buf.getvalue())

    # ─────────────────────────── [C1] 旧实现的坑
    c.section("[C1] 旧实现的坑不得回归")
    c.eq("全文件只有一处 sys.exit", code.count("sys.exit("), 1)
    c.check("且是 sys.exit(main())", "sys.exit(main())" in code)
    for bad in ("sys.exit(2)", "sys.exit(3)", "raise SystemExit(2)", "raise SystemExit(3)"):
        c.check(f"没有散落的 {bad}", bad not in code)
    c.check("不再发完 TG 就删截图（os.remove）", "os.remove(" not in code)
    c.check("不再硬编码 /tmp", "/tmp" not in code)
    c.check("脚本层不再直接 import requests（走 renewkit）", "import requests" not in code)
    c.check("不再硬编码 binary_location", "binary_location" not in code)
    c.check("过 CF 盾的能力保留（uc_gui_click_captcha）", "uc_gui_click_captcha" in code)
    c.check("密码登录保留「只删 XSRF」的修法",
            'delete_cookie' in code and "WAF 放行牌" in src)
    c.check("删 XSRF 只针对 XSRF-TOKEN",
            'cookie.get("name") == "XSRF-TOKEN"' in code)
    c.check("登录用 refresh 而不是新导航", "sb.refresh()" in code)
    c.check("seleniumbase 延迟导入（可离线单测）",
            "from seleniumbase import SB" in code
            and code.index("from seleniumbase import SB") > code.index("def run_browser"))
    c.check("到期时间正则不再用 [^0-9]{0,40} 那种被数字卡死的填充",
            "[^0-9]{0,40}" not in code and "不许出现另一个日期" in src)

    # ★ 实盘 #241 的坑：WAF/CF 判定必须在注入 Cookie 之后。
    #   用 AST 数调用点与行号，比字符串匹配抗重排。
    _tree = ast.parse(src)
    cp_calls = [n.lineno for n in ast.walk(_tree)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == "classify_page"]
    addcookie_calls = [n.lineno for n in ast.walk(_tree)
                       if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                       and n.func.attr == "add_cookie"]
    c.eq("classify_page 只有一处调用点", len(cp_calls), 1)
    c.check("add_cookie 确实有调用点", len(addcookie_calls) >= 1)
    c.check("WAF/CF 判定点在注入 Cookie 之后（实盘 #241 的坑）",
            bool(cp_calls) and bool(addcookie_calls) and min(cp_calls) > max(addcookie_calls),
            f"classify_page@{cp_calls} add_cookie@{addcookie_calls}")
    c.check("直连被 WAF 拦时挂 ::warning:: 注解（把「代理没起来」和「被拦」对上号）",
            "::warning::" in code)

    # ─────────────────────────── [C2] workflow ↔ 代码
    c.section("[C2] workflow 与代码一致")
    wf = WORKFLOW.read_text(encoding="utf-8")
    rd = README.read_text(encoding="utf-8")

    c.check("workflow 存在", WORKFLOW.is_file())
    c.check("script 指向 main.py", "script: main.py" in wf)
    c.check("主命令套 xvfb-run", "xvfb-run" in wf and "python3 main.py" in wf)
    c.check("装了 xvfb / fonts-noto-cjk", "xvfb" in wf and "fonts-noto-cjk" in wf)
    c.check("装了 seleniumbase", "seleniumbase" in wf)
    c.check("setup-command 指向 setup_proxy.sh", "scripts/setup_proxy.sh" in wf)
    c.check("setup_proxy.sh 存在", PROXY_SH.is_file())
    c.check("artifact 收 *.png 且与截图名对得上",
            "*.png" in wf and mod.SHOT_NAME.endswith(".png"))
    c.check("不再手写 setup-python（交给 action）", "actions/setup-python@" not in wf)
    c.check("不再手写 apt（交给 action）", "apt-get install" not in wf)
    c.check("保留每小时 cron", "'0 * * * *'" in wf)
    c.check("保留 2 账号矩阵", 'account: ["01", "02"]' in wf)
    c.check("保留 fail-fast: false", "fail-fast: false" in wf)
    c.check("加了并发锁（同账号不重叠）", "concurrency:" in wf)
    c.check("加了 dry_run 手动开关", "dry_run" in wf)

    env_names = set(re.findall(r"^\s{10}([A-Z][A-Z0-9_]+):", wf, re.M))
    for name in ("ICEHOST_SERVER_URL", "ICEHOST_COOKIES", "ICEHOST_EMAIL",
                 "ICEHOST_PASSWORD", "ICEHOST_ACCOUNT_NAME", "TG_BOT_TOKEN",
                 "TG_CHAT_ID", "DRY_RUN"):
        c.check(f"workflow 传了 {name}", name in env_names)
    c.eq("workflow 没漏传矩阵变量", sorted(env_names),
         sorted(["ICEHOST_ACCOUNT_NAME", "ICEHOST_SERVER_URL", "ICEHOST_COOKIES",
                 "ICEHOST_EMAIL", "ICEHOST_PASSWORD", "TG_BOT_TOKEN",
                 "TG_CHAT_ID", "DRY_RUN", "NODE_LINK"]))

    # ★ 实盘 #241：迁移时把原 workflow 代理 step 上的 NODE_LINK 丢了，
    #   上游 installer 取到空值 → 静默退直连 → 出口 IP 被面板 WAF 拦。
    c.check("workflow 传了 NODE_LINK（实盘 #241 漏的就是它）", "NODE_LINK" in env_names)
    c.check("NODE_LINK 接的是 secrets.NODE_LINK",
            "NODE_LINK: ${{ secrets.NODE_LINK }}" in wf)

    # 脚本读的 ICEHOST_* 凭据变量必须都在 workflow 里传了（防止加了变量忘了接线）
    read_vars = set(re.findall(r'env\.(?:get|get_int|get_list)\("(ICEHOST_[A-Z0-9_]+)"', code))
    read_vars -= OPTIONAL_ENV
    c.check("脚本读的 ICEHOST_* 凭据都在 workflow 里",
            read_vars <= env_names, f"缺 {sorted(read_vars - env_names)}")

    # ─────────────────────────── [C3] README ↔ 事实
    c.section("[C3] README 与仓库事实一致")
    mentioned = set(re.findall(r"([\w.-]+\.ya?ml)", rd))
    c.check("README 提到的 workflow 都真实存在",
            mentioned <= REMOTE_WORKFLOWS, f"多出来 {sorted(mentioned - REMOTE_WORKFLOWS)}")
    for name in sorted(STAGED_WORKFLOWS):
        c.check(f"本地 stage 的 {name} 在", (ROOT / ".github" / "workflows" / name).is_file())
    for f in (SCRIPT, PROXY_SH, WORKFLOW, HERE / "verify_icehost.py"):
        rel = f.relative_to(ROOT).as_posix()
        c.check(f"README 目录树里的 {rel} 存在", f.is_file())
    for token in ("ICEHOST_COOKIES", "ICEHOST_SERVER_URL", "TG_BOT_TOKEN", "TG_CHAT_ID",
                  "NODE_LINK", "0 * * * *", "RENEWED", "SKIPPED", "TRANSIENT", "UNKNOWN",
                  "FAILED", "renew-kit"):
        c.check(f"README 写了 {token}", token in rd)
    c.check("README 说明了跳过时静默", "静默" in rd)
    c.check("README 说明了按钮不是窗口信号", "照样在页面上" in rd or "不是窗口信号" in rd)
    c.check("README 解释了 WAF 与 session cookie 的关系",
            "WAF" in rd and "Cloudflare" in rd and "不验" in rd)
    c.check("README 说明了 WAF 判定必须在注入 Cookie 之后",
            "注入 Cookie 之后" in rd)
    c.check("README 说明了 WAF 按出口分两档",
            "走了代理仍被拦" in rd and "直连被拦" in rd)
    c.check("README 提醒 NODE_LINK 不配就退直连",
            "NODE_LINK" in rd and "退直连" in rd)

    # ─────────────────────────── [C4] 版本 pin 三方一致
    c.section("[C4] renew-kit 版本 pin 一致")
    # 真实路径长这样：jardanlau2020/renew-kit/.github/actions/renew@v0.4.2
    # 所以锚在 `actions/renew@` 上，而不是 `renew-kit@`
    pin_uses = re.search(r"actions/renew@([\w.]+)", wf)
    pin_ref = re.search(r"renewkit-ref:\s*([\w.]+)", wf)
    pin_readme = re.search(r"actions/renew@([\w.]+)", rd)
    pin_readme_ref = re.search(r"renewkit-ref:\s*`?([\w.]+)`?", rd)
    c.check("workflow 里有 uses 的 pin", pin_uses is not None)
    c.check("workflow 里有 renewkit-ref 的 pin", pin_ref is not None)
    c.check("README 里有 pin", pin_readme is not None)
    c.check("README 里有 renewkit-ref 的 pin", pin_readme_ref is not None)
    if pin_uses and pin_ref and pin_readme:
        c.eq("action 版本 == renewkit-ref", pin_uses.group(1), pin_ref.group(1))
        c.eq("action 版本 == README 里的 pin", pin_uses.group(1), pin_readme.group(1))
        c.check("pin 不是浮动的 main", pin_uses.group(1) != "main")
    if pin_ref and pin_readme_ref:
        c.eq("renewkit-ref 与 README 一致", pin_ref.group(1), pin_readme_ref.group(1))

    # ─────────────────────────── [C5] setup_proxy.sh
    c.section("[C5] setup_proxy.sh")
    sh_src = PROXY_SH.read_text(encoding="utf-8")
    c.check("走 GITHUB_ENV 而不是 export", "GITHUB_ENV" in sh_src)
    c.check("先验证出口再认定代理可用", "api.ipify.org" in sh_src)
    c.check("验证失败会退回直连（不中断）", "回退直连" in sh_src)
    c.check("没有 set -e（失败不该中断续期）", "set -e" not in sh_src)
    c.check("有重试", "for i in 1 2 3" in sh_src)
    # ★ 实盘 #241：NODE_LINK 空值 → 上游 installer 静默退直连。这里必须喊出来。
    c.check("检查 NODE_LINK 是否传入（实盘 #241 的兜底）",
            'NODE_LINK' in sh_src and '${NODE_LINK:-}' in sh_src)
    c.check("NODE_LINK 为空时打 ::warning:: 注解", "::warning::" in sh_src)
    c.check("NODE_LINK 为空时提示去看 workflow 的 env",
            "secrets.NODE_LINK" in sh_src)

    bash = os.environ.get("RENEWKIT_BASH") or shutil.which("bash")
    if bash is None:
        c.skip("setup_proxy.sh 语法检查", "本机没有 bash")
    else:
        probe = subprocess.run([bash, "-n"], input="echo ok\n",
                               capture_output=True, text=True, timeout=60)
        if probe.returncode != 0:
            c.skip("setup_proxy.sh 语法检查", f"bash 不可用（{bash}）: {probe.stderr.strip()}")
        else:
            proc = subprocess.run([bash, "-n"], input=sh_src,
                                  capture_output=True, text=True, timeout=60)
            c.check("setup_proxy.sh 语法正确", proc.returncode == 0,
                    (proc.stderr or "").strip())

    return c.report()


if __name__ == "__main__":
    sys.exit(main())
