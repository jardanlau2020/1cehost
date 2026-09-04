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

def dump_body(tag):
    print(f"=== {tag} body ===")
    body = sb.get_text("body") or ""
    for l in body.splitlines():
        s = l.strip()
        if s:
            print("  ", s[:180])

def search_keywords(tag):
    print(f"=== {tag} 關鍵字掃描 ===")
    src = sb.get_page_source()
    for kw in ["dodaj", "add 6", "add6", "przedłuż", "przedluz", "extend", "renew",
               "6 godzin", "6h", "godzin", "expiration", "expiry", "rok", "year"]:
        for m in re.finditer(kw, src, re.IGNORECASE):
            s = max(0, m.start()-60)
            e = min(len(src), m.end()+60)
            snippet = src[s:e].replace("\n", " ")
            snippet = re.sub(r"<[^>]+>", " ", snippet)
            print(f"  [{kw}] ...{snippet.strip()[:130]}...")
            break

with SB(uc=True, xvfb=True) as sb:
    sb.uc_open_with_reconnect("https://dash.icehost.pl/", reconnect_time=8)
    sb.sleep(6)
    send_cookies(sb)
    sb.refresh()
    sb.sleep(8)

    dump_body("首頁")
    search_keywords("首頁")

    # 撳入 server card(text=Serwer testowy,唔用 exact link text)
    for selector in [
        "xpath://p[contains(text(),'Serwer testowy')]",
        "xpath://*[contains(text(),'Serwer testowy')]",
        "css:p.hrkiAy",
    ]:
        try:
            el = sb.find_element(selector, timeout=5)
            print(f"=== 撳 {selector} ===")
            el.click()
            sb.sleep(6)
            print("URL:", sb.get_current_url())
            break
        except Exception as e:
            print(f"撳 {selector} fail: {str(e)[:80]}")

    dump_body("撳入 server 後")
    search_keywords("撳入 server 後")

    # 撳 Server Settings
    try:
        el = sb.find_element("text=Server Settings", timeout=5)
        print("=== 撳 Server Settings ===")
        el.click()
        sb.sleep(5)
        dump_body("Server Settings")
        search_keywords("Server Settings")
    except Exception as e:
        print("撳 Server Settings fail:", str(e)[:120])

    sb.save_screenshot("discover_screenshot.png")