import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.getenv("ICEHOST_API_BASE", "https://dash.icehost.pl")
TOKEN = os.getenv("ICEHOST_API_TOKEN", "")
ACCOUNT = os.getenv("ICEHOST_ACCOUNT_NAME", "IceHost")


def get(path):
    req = urllib.request.Request(
        BASE + path,
        headers={
            "Authorization": "Bearer " + TOKEN,
            "Accept": "application/json",
            "User-Agent": "icehost-renew-api-probe/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            body = r.read()
            try:
                j = json.loads(body)
                return r.status, json.dumps(j, ensure_ascii=False)[:1500]
            except Exception:
                return r.status, body[:500].decode(errors="replace")
    except urllib.error.HTTPError as e:
        b = e.read()[:300].decode(errors="replace")
        return e.code, b
    except Exception as e:
        return -1, f"ERR {type(e).__name__}: {e}"


def main():
    ok = True
    paths = ["/api/client", "/api/client/servers"]
    for p in paths:
        code, body = get(p)
        print(f"[{ACCOUNT}] {p} -> {code}")
        print(body[:1500])
        if code >= 400:
            ok = False
    print(f"probe_done ok={ok}")
    sys.exit(0 if ok else 2)


if __name__ == "__main__":
    main()