# 1cehost

Scheduled keep-alive automation via GitHub Actions.

A headless browser (SeleniumBase) performs recurring renewals on a host
panel and posts results to a Telegram bot.

- `main.py` — renewal engine (Cookie-first, falls back to password login)
- `.github/workflows/` — `Auto Renew` (scheduled) plus manual probe jobs
- `probe.py`, `scripts/api_probe.py`, `discover_server.py` — manual diagnostics
