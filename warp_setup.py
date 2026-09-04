#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
warp_setup.py — 喺 GitHub Actions runner 起 WARP (wgcf + wireproxy) SOCKS5。
用法:python3 warp_setup.py
成功後 SOCKS5 聽 127.0.0.1:10808。
"""
import gzip
import os
import re
import shutil
import subprocess
import tarfile
import time
import urllib.request

WORK = "/tmp/warp"
SOCKS = "127.0.0.1:10808"


def run(cmd, **kw):
    print("+", " ".join(cmd))
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def download(url, dest):
    print(f"download {url}")
    with urllib.request.urlopen(url, timeout=60) as r:
        data = r.read()
    with open(dest, "wb") as f:
        f.write(data)
    print(f"  -> {dest} ({len(data)} bytes)")


def main():
    os.makedirs(WORK, exist_ok=True)
    os.chdir(WORK)

    # 1. wgcf
    wgcf = os.path.join(WORK, "wgcf")
    download(
        "https://github.com/ViRb3/wgcf/releases/latest/download/wgcf_2.2.28_linux_amd64",
        wgcf,
    )
    os.chmod(wgcf, 0o755)
    r = run([wgcf, "register", "--accept-tos"], input="\n")
    if r.returncode != 0:
        print("wgcf register 唔成功:", r.stdout[-400:], r.stderr[-400:])
    r = run([wgcf, "generate"])
    if r.returncode != 0:
        print("wgcf generate 唔成功:", r.stdout[-400:], r.stderr[-400:])
    conf = open("/tmp/wgcf-profile.conf").read()
    priv = re.search(r"PrivateKey\s*=\s*(\S+)", conf).group(1)
    peer = re.search(r"PublicKey\s*=\s*(\S+)", conf).group(1)
    endp = re.search(r"Endpoint\s*=\s*(\S+)", conf).group(1)
    print(f"wgcf: priv={priv[:6]}... peer={peer[:6]}... endp={endp}")

    # 2. wireproxy
    wp_tgz = os.path.join(WORK, "wireproxy.tar.gz")
    download(
        "https://github.com/pufferffish/wireproxy/releases/latest/download/wireproxy_linux_amd64.tar.gz",
        wp_tgz,
    )
    with tarfile.open(wp_tgz, "r:gz") as t:
        t.extractall(WORK)
    wp_bin = os.path.join(WORK, "wireproxy")
    if not os.path.exists(wp_bin):
        for f in os.listdir(WORK):
            if "wireproxy" in f:
                wp_bin = os.path.join(WORK, f)
    os.chmod(wp_bin, 0o755)
    print("wireproxy 二進制:", wp_bin)

    # 3. 寫 conf(用 python,唔使 heredoc 縮排問題)
    wp_conf = os.path.join(WORK, "wireproxy.conf")
    with open(wp_conf, "w") as f:
        f.write(
            f"""[Interface]
PrivateKey = {priv}
Address = 172.16.0.2/32, fd01:5ca1:ab1e:823e:e094::8888/128
DNS = 1.1.1.1

[Socks5]
BindAddress = {SOCKS}

[Peer]
PublicKey = {peer}
Endpoint = {endp}
PersistentKeepalive = 25
AllowedIPs = 0.0.0.0/0, ::/0
"""
        )
    print("conf:", wp_conf)

    # 4. 起 wireproxy
    proc = subprocess.Popen(
        [wp_bin, "-c", wp_conf],
        stdout=open("/tmp/wireproxy.log", "w"),
        stderr=subprocess.STDOUT,
    )
    time.sleep(4)
    print("--- wireproxy.log ---")
    try:
        print(open("/tmp/wireproxy.log").read()[-1000:])
    except FileNotFoundError:
        pass
    if proc.poll() is not None:
        print("wireproxy 已退出 code", proc.returncode)
        return

    # 5. 驗證 SOCKS5
    for i in range(3):
        try:
            r = subprocess.run(
                [
                    "curl",
                    "-s",
                    "--max-time",
                    "15",
                    "-x",
                    f"socks5h://{SOCKS}",
                    "https://api.ipify.org",
                ],
                capture_output=True,
                text=True,
                timeout=25,
            )
            ip = r.stdout.strip()
            print(f"WARP SOCKS5 出口 IP: {ip}")
            if ip:
                break
        except Exception as e:
            print(f"  第{i+1}次驗證失敗: {e}")
        time.sleep(3)


if __name__ == "__main__":
    main()