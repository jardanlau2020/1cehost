import os
import json
import urllib.parse
import re
from seleniumbase import SB

COOKIES = os.getenv("ICEHOST_COOKIES", "").strip()
if not COOKIES:
    print("DISCOVER_RESULT=NO_COOKIES")
    raise SystemExit

token_value = COOKIES
if "icehostpl_session=" in token_value:
    token_value = token_value.split("icehostpl_session=")[1].split(";")[0]
elif "XSRF-TOKEN=" in token_value:
    token_value = token_value.split("XSRF-TOKEN=")[1].split(";")[0]
token_value = urllib.parse.unquote(token_value.strip())

def send_cookies(br):
    for name in ["icehostpl_session", "XSRF-TOKEN"]:
        br.add_cookie({
            "name": name,
            "value": token_value,
            "domain": "dash.icehost.pl",
            "path": "/",
            "secure": True,
        })

def dump(body_tag, n=200):
    print(f"=== {body_tag} ===")
    body = sb.get_text("body") or ""
    for l in body.splitlines():
        s = l.strip()
        if s:
            print("  ", s[:200])

with SB(uc=True, xvfb=True) as sb:
    sb.uc_open_with_reconnect("https://dash.icehost.pl/", reconnect_time=8)
    sb.sleep(6)
    send_cookies(sb)
    sb.refresh()
    sb.sleep(8)

    print("STEP1 首頁 URL:", sb.get_current_url())
    dump("首頁 body")

    # 嘗試撳 SHOW MY SERVERS
    for label in ["SHOW MY SERVERS", "SHOW ASSIGNED SERVERS"]:
        try:
            el = sb.find_element(f"text={label}", timeout=4)
            print(f"STEP2 撳 {label}")
            el.click()
            sb.sleep(4)
            print("   URL:", sb.get_current_url())
            dump(f"撳完 {label} body")
        except Exception as e:
            print(f"STEP2 {label} 撳唔到: {str(e)[:100]}")

    # 嘗試撳 server 名 / 搵 server 卡片撳入去
    try:
        el = sb.find_element("text=Serwer testowy", timeout=5)
        print("STEP3 撳 server 名 Serwer testowy")
        el.click()
        sb.sleep(5)
        print("   URL:", sb.get_current_url())
        dump("撳入 server body")
    except Exception as e:
        print("STEP3 撳 server 名: ", str(e)[:120])

    # JS history 睇實際 SPA 路由
    try:
        hist = sb.execute_script("return window.location.href;")
        print("STEP4 location.href:", hist)
    except Exception as e:
        print("STEP4:", str(e)[:80])

    sb.save_screenshot("discover_screenshot.png")