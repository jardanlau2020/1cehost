import os
import json
import urllib.parse
import re
from seleniumbase import SB

COOKIES = os.getenv("ICEHOST_COOKIES", "").strip()
if not COOKIES:
    print("DISCOVER_RESULT=NO_COOKIES")
    raise SystemExit

with SB(uc=True, xvfb=True) as sb:
    sb.uc_open_with_reconnect("https://dash.icehost.pl/", reconnect_time=8)
    sb.sleep(6)

    # 注入 cookie
    token_value = COOKIES
    if "icehostpl_session=" in token_value:
        token_value = token_value.split("icehostpl_session=")[1].split(";")[0]
    elif "XSRF-TOKEN=" in token_value:
        token_value = token_value.split("XSRF-TOKEN=")[1].split(";")[0]
    token_value = token_value.strip()

    for name in ["icehostpl_session", "XSRF-TOKEN"]:
        sb.add_cookie({
            "name": name,
            "value": urllib.parse.unquote(token_value),
            "domain": "dash.icehost.pl",
            "path": "/",
            "secure": True,
        })
    sb.refresh()
    sb.sleep(6)

    print("=== 基本信息 ===")
    print("TITLE:", sb.get_page_title() or "(none)")
    print("URL:", sb.get_current_url())

    src = sb.get_page_source()
    print("HTML 長度:", len(src))
    print("有 logout 文字:", "logout" in src.lower() or "wyloguj" in src.lower())
    print("有登入表單:", 'type="email"' in src or 'name="email"' in src or 'name="password"' in src)

    # 搵所有 link
    print("=== 全部 a[href] ===")
    hrefs = set()
    for a in sb.find_elements("a[href]"):
        h = a.get_attribute("href")
        if h and h.startswith("http"):
            hrefs.add(h)
        elif h and h.startswith("/"):
            hrefs.add("https://dash.icehost.pl" + h)
        elif h:
            hrefs.add(h)
    for h in sorted(hrefs):
        print("  A:", h)

    # 搵 form action
    print("=== 全部 form ===")
    for f in sb.find_elements("form"):
        act = f.get_attribute("action")
        print("  FORM action:", act)

    # 搵含 server / host / renew / prolong / 6 嘅元素文字
    print("=== 相關文字節點 ===")
    body_text = sb.get_text("body") or ""
    for kw in ["dodaj", "add", "prolong", "przedłuż", "przedluz", "server", "renew", "6 godzin", "6 hour", "servers"]:
        matches = [l.strip() for l in body_text.splitlines() if kw.lower() in l.lower()]
        for m in matches[:5]:
            print(f"  [{kw}] {m[:120]}")

    # 嘗試幾個常風路徑
    print("=== 嘗試直接訪問候選路徑 ===")
    for path in ["/servers", "/server", "/home", "/panel", "/dashboard", "/servers/"]:
        try:
            sb.open("https://dash.icehost.pl" + path)
            sb.sleep(3)
            print(f"  GET {path} → {sb.get_current_url()[:100]} | title={sb.get_page_title()[:60]}")
        except Exception as e:
            print(f"  GET {path} → ERR {str(e)[:80]}")

    sb.save_screenshot("discover_screenshot.png")