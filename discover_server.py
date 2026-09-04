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

    # 1) JS 挖 DOM:搵「Serwer testowy」所在元素嘅 outerHTML
    print("=== JS 搵 Serwer testowy DOM ===")
    info = sb.execute_script("""
      const results = [];
      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      while (walker.nextNode()) {
        const t = walker.currentNode.textContent.trim();
        if (t && t.includes('Serwer testowy')) {
          let el = walker.currentNode.parentElement;
          let chain = [];
          for (let i = 0; i < 5 && el; i++) {
            chain.push(el.tagName + (el.className ? '.' + String(el.className).slice(0,50) : '') + (el.id ? '#' + el.id : ''));
            el = el.parentElement;
          }
          results.push({text: t.slice(0,100), chain: chain.join(' < '), html: walker.currentNode.parentElement.outerHTML.slice(0,500)});
        }
      }
      return results.slice(0,5);
    """)
    for r in info:
        print("TEXT:", r.get("text"))
        print("CHAIN:", r.get("chain"))
        print("HTML:", r.get("html"))
        print("---")

    # 2) 挖所有 onclick / data-* 有 server 痕跡嘅元素
    print("=== data-server / onclick 特搜 ===")
    hits = sb.execute_script("""
      const out = [];
      document.querySelectorAll('[data-server-id],[data-id],[data-url],[onclick],[href*="server"],[href*="edit"]').forEach(el=>{
        out.push({
          tag: el.tagName,
          href: el.getAttribute('href') || '',
          onclick: (el.getAttribute('onclick')||'').slice(0,120),
          data: JSON.stringify(Object.fromEntries([...el.attributes].filter(a=>a.name.startsWith('data-')).map(a=>[a.name,a.value]))).slice(0,150),
          text: (el.textContent||'').trim().slice(0,60)
        });
      });
      return out.slice(0,25);
    """)
    for h in hits:
        print(h)

    # 3) XPath 精準撳 SHOW MY SERVERS
    for label in ["SHOW MY SERVERS", "SHOW ASSIGNED SERVERS"]:
        try:
            el = sb.find_element(f"xpath://*[contains(normalize-space(text()),'{label}')]", timeout=5)
            print(f"=== XPath 撳 {label} ===")
            el.click()
            sb.sleep(5)
            print("URL:", sb.get_current_url())
            body = sb.get_text("body") or ""
            for l in body.splitlines():
                s = l.strip()
                if s: print("  ", s[:180])
        except Exception as e:
            print(f"XPath {label} fail:", str(e)[:100])

    # 4) 連埋全部 a[href] 睇有冇 servers/xxx
    print("=== 全部 a[href] ===")
    hrefs = sb.execute_script(
        "return Array.from(document.querySelectorAll('a[href]')).map(a=>a.getAttribute('href')).filter(h=>h);")
    for h in sorted(set(hrefs)):
        if 'server' in h.lower() or h.count('/') >= 2:
            print("  ", h)

    sb.save_screenshot("discover_screenshot.png")