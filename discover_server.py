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

with SB(uc=True, xvfb=True) as sb:
    sb.uc_open_with_reconnect("https://dash.icehost.pl/", reconnect_time=8)
    sb.sleep(6)
    send_cookies(sb)
    sb.refresh()
    sb.sleep(8)

    # JS 直接撳 #list 內嘅 server card(揾含「Serwer」嘅 p 卡)
    clicked = sb.execute_script("""
      const list = document.getElementById('list');
      if (!list) return 'NO_LIST';
      const p = Array.from(list.querySelectorAll('p')).find(x => x.textContent.includes('Serwer'));
      if (!p) return 'NO_SERVER_P';
      // 向上搵可點擊祖先(article/div/a)
      let el = p;
      for (let i=0; i<4; i++) { el = el.parentElement; if (!el) break; }
      el.click();
      return 'CLICKED:' + el.tagName + '.' + (el.className||'').toString().slice(0,60);
    """)
    print("JS CLICK:", clicked)
    sb.sleep(7)

    print("URL:", sb.get_current_url())
    src = sb.get_page_source()
    print("HTML 長度:", len(src))

    # 完整 body
    print("=== body ===")
    body = sb.get_text("body") or ""
    for l in body.splitlines():
        s = l.strip()
        if s:
            print("  ", s[:180])

    # 搜尋續期關鍵字
    print("=== 續期關鍵字掃描 ===")
    for kw in ["dodaj", "add 6", "add6", "przedłuż", "przedluz", "extend", "renew",
               "6 godzin", "no expiration", "expiration", "godzin", "Konto", "plan"]:
        ms = list(re.finditer(kw, src, re.IGNORECASE))
        if ms:
            m = ms[0]
            s = max(0, m.start()-80); e = min(len(src), m.end()+80)
            snippet = re.sub(r"<[^>]+>", " ", src[s:e]).strip()
            print(f"  [{kw}] x{len(ms)}: ...{snippet[:150]}...")

    # 按鈕/連結列表
    print("=== buttons ===")
    for b in sb.find_elements("button"):
        t = (b.text or "").strip()
        if t and len(t) < 100:
            print("  BTN:", t)
    print("=== 全部 a ===")
    for a in sb.find_elements("a"):
        t = (a.text or "").strip()
        if t and len(t) < 80:
            print("  A:", t)

    sb.save_screenshot("discover_screenshot.png")