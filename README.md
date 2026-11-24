# m-ws 自动续期

GitHub Actions 上跑的 m-ws.cc 自动续期：Discord token 登录 → 点 Renew → 过 Cloudflare Turnstile → 推送 Telegram。

## 需要的 Secrets

| Name | 说明 |
|---|---|
| `DISCORD_TOKEN` | Discord 用户 token（必填） |
| `TG_BOT_TOKEN` | Telegram Bot token（必填） |
| `TG_CHAT_ID` | 接收消息的 chat id（必填） |
| `NODE_LINK` | 代理节点链接，可选；不填则直连 |

## 文件

- `main.py` — 主流程
- `.github/workflows/renew.yml` — 定时触发 + Chrome/xvfb/代理安装
- `requirements.txt` — DrissionPage + requests

## 手动触发

Actions → `m-ws renew` → Run workflow。失败时会把截图发到 Telegram。
