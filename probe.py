#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IceHost WAF / CF 盾探路腳本 —— 零憑據。
目的：確認 GitHub Actions 出口 IP 直連 dash.icehost.pl 到底係
  (a) WAF 國別/IP 封鎖（Connection Blocked）
  (b) CF Turnstile 挑戰（可用 uc_gui_click_captcha 過）
  (c) 正常登入頁（最好嘅情況）
唔需要任何 cookie / 帳密。
"""

import os
import sys
import time

TARGET = os.environ.get("PROBE_URL", "https://dash.icehost.pl/")


def probe_requests():
    """純 HTTP 層探測（唔用瀏覽器）"""
    import requests

    print("=" * 60)
    print("【一】純 HTTP 層探測")
    print("=" * 60)

    try:
        ip = requests.get("https://api.ipify.org", timeout=15).text.strip()
        print(f"📍 Runner 出口 IP: {ip}")
    except Exception as e:
        print(f"⚠️ 拿唔到出口 IP: {e}")

    try:
        r = requests.get(
            TARGET,
            timeout=25,
            allow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/140.0.0.0 Safari/537.36"
                )
            },
        )
        print(f"🌐 GET {TARGET}")
        print(f"   HTTP {r.status_code} | final URL: {r.url}")
        for h in ("server", "cf-mitigated", "cf-ray", "location"):
            if h in r.headers:
                print(f"   {h}: {r.headers[h]}")

        body = r.text or ""
        print(f"   body 長度: {len(body)}")

        markers = {
            "WAF 封鎖": ["Connection Blocked", "blocked on our WAF", "zablokowane"],
            "CF 挑戰": ["Just a moment", "challenges.cloudflare.com", "cf-turnstile"],
            "登入頁": ["type=\"email\"", "type='email'", "name=\"email\"", "password"],
        }
        for label, kws in markers.items():
            hit = [k for k in kws if k in body]
            if hit:
                print(f"   ▶ 命中【{label}】: {hit}")
    except Exception as e:
        print(f"❌ HTTP 探測失敗: {e}")


def probe_browser():
    """瀏覽器層探測（seleniumbase UC + xvfb，同真腳本一致）"""
    print()
    print("=" * 60)
    print("【二】瀏覽器層探測（seleniumbase UC 模式）")
    print("=" * 60)

    from seleniumbase import SB

    with SB(uc=True, xvfb=True) as sb:
        print(f"🌐 uc_open_with_reconnect: {TARGET}")
        sb.uc_open_with_reconnect(TARGET, reconnect_time=8)
        sb.sleep(6)

        print(f"   URL   : {sb.get_current_url()}")
        print(f"   TITLE : {sb.get_title()}")
        sb.save_screenshot("probe_01_first_load.png")

        src = sb.get_page_source() or ""
        print(f"   源碼長度: {len(src)}")

        if "Connection Blocked" in src or "blocked on our WAF" in src:
            print("   🔴 判定：WAF 直接封鎖出口 IP —— 直連唔通，必須換出口")
            return "WAF_BLOCKED"

        # 試過 Turnstile
        has_ts = "cf-turnstile" in src or "challenges.cloudflare.com" in src
        print(f"   Turnstile 存在: {has_ts}")
        if has_ts or "Just a moment" in (sb.get_title() or ""):
            print("   🖱️ 嘗試 uc_gui_click_captcha ...")
            try:
                sb.uc_gui_click_captcha()
                sb.sleep(10)
            except Exception as e:
                print(f"   ⚠️ uc_gui_click_captcha 異常: {e}")
            sb.save_screenshot("probe_02_after_captcha.png")
            print(f"   過盾後 URL   : {sb.get_current_url()}")
            print(f"   過盾後 TITLE : {sb.get_title()}")
            src = sb.get_page_source() or ""

        # 睇有無登入表單
        for sel in ('input[type="email"]', 'input[name="email"]',
                    'input[type="password"]', 'input[name="username"]'):
            try:
                if sb.is_element_present(sel):
                    print(f"   ✅ 見到登入欄位: {sel}")
            except Exception:
                pass

        sb.save_screenshot("probe_03_final.png")

        # 抽可見文字前 800 字，幫我讀懂頁面
        try:
            txt = sb.get_text("body")[:800]
            print("   ── 頁面可見文字 ──")
            for line in txt.splitlines():
                if line.strip():
                    print(f"     {line.strip()[:160]}")
        except Exception:
            pass

        return "OK"


if __name__ == "__main__":
    probe_requests()
    try:
        result = probe_browser()
        print()
        print(f"🏁 探路結論: {result}")
    except Exception as e:
        print(f"❌ 瀏覽器探測失敗: {e}")
        sys.exit(0)  # 探路唔算失敗，唔好令 workflow 紅
