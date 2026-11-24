#!/usr/bin/env python3
"""m-ws.cc 自动续期（纯浏览器点击，不发任何站点 API 请求）。

流程：Discord token 登录 -> 打开控制台 -> 点 Renew -> 若弹 Turnstile 点 checkbox
     -> 等页面提示 -> 从 DOM 读剩余时长 -> 推送 Telegram。
"""

import json
import os
import re
import time
import traceback

import requests
from DrissionPage import ChromiumOptions, ChromiumPage
from DrissionPage.common import Settings

Settings.raise_when_ele_not_found = False
Settings.raise_when_wait_failed = False

SITE = "https://cloud.m-ws.cc"
DASH = f"{SITE}/dash"   # 工作台；SITE 根路径是营销首页，没有续期按钮

# 站点是三语 UI（ja / zh / en），关键词全部覆盖
LOGIN_BTN = ("使用 Discord 登录", "Discord でログイン", "Login with Discord")
AUTH_BTN = ("認可", "授权", "授權", "Authorize", "允许", "允許")
RENEW_BTN = ("续期", "延長", "Renew")

# 剩余时长：卡片显示 "Sleep in 165h"；也兼容下面几种写法
PAT_HOURS = [
    re.compile(r"Sleep\s*in\s*(\d+)\s*h", re.I),
    re.compile(r"(?:剩余|残り)\s*(\d+)\s*[天日]\s*(\d+)\s*h"),
    re.compile(r"(\d+)d\s*(\d+)h\s*(?:left|残り)"),
    re.compile(r"(?:剩余|残り)\s*(\d+)\s*h"),
    re.compile(r"Sleep\s*in\s*(\d+)\s*(?:d|日|天)\s*(\d+)?\s*h?", re.I),
]


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def clean_secret(name: str) -> str:
    raw = os.environ.get(name) or ""
    return re.sub(r"\s+", "", raw).strip("'\"")


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #

def tg_send(text: str) -> None:
    token, chat = clean_secret("TG_BOT_TOKEN"), clean_secret("TG_CHAT_ID")
    if not token or not chat:
        log("⚠️ TG 未配置，跳过推送")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": text, "disable_web_page_preview": True},
            timeout=30,
        )
        log(f"▶ TG 文本推送 {r.status_code}")
        if r.status_code != 200:
            log(r.text[:300])
    except Exception as exc:
        log(f"⚠️ TG 文本推送失败: {exc}")


def tg_send_photo(path: str, caption: str) -> None:
    token, chat = clean_secret("TG_BOT_TOKEN"), clean_secret("TG_CHAT_ID")
    if not token or not chat or not os.path.exists(path):
        return
    try:
        with open(path, "rb") as fp:
            r = requests.post(
                f"https://api.telegram.org/bot{token}/sendPhoto",
                data={"chat_id": chat, "caption": caption[:1000]},
                files={"photo": fp},
                timeout=60,
            )
        log(f"▶ TG 图片推送 {r.status_code}")
    except Exception as exc:
        log(f"⚠️ TG 图片推送失败: {exc}")


# --------------------------------------------------------------------------- #
# 浏览器
# --------------------------------------------------------------------------- #

def configure_proxy(co) -> None:
    """DrissionPage 的 set_proxy 不支持 socks5，改用 Chrome 原生的 --proxy-server。

    setup_proxy.sh 会同时监听 1080(socks5) 和 1081(http)，优先走 http 入站。
    """
    proxy = (os.environ.get("PROXY_SERVER") or "").strip()
    if (os.environ.get("IS_PROXY") or "").lower() != "true" or not proxy:
        log("▶ 直连（未启用代理）")
        return

    m = re.match(r"socks5?://([^:/]+):(\d+)", proxy)
    if m:
        host = m.group(1)
        http_proxy = f"http://{host}:1081"
        co.set_argument(f"--proxy-server={http_proxy}")
        log(f"▶ 已挂代理: {http_proxy}（由 {proxy} 映射到 http 入站）")
    else:
        co.set_argument(f"--proxy-server={proxy}")
        log(f"▶ 已挂代理: {proxy}")


def build_page() -> ChromiumPage:
    co = ChromiumOptions()
    for path in ("/usr/bin/google-chrome", "/usr/bin/google-chrome-stable", "/usr/bin/chromium"):
        if os.path.exists(path):
            co.set_browser_path(path)
            log(f"▶ 浏览器: {path}")
            break

    co.set_argument("--no-sandbox")
    co.set_argument("--disable-dev-shm-usage")
    co.set_argument("--disable-gpu")
    co.set_argument("--window-size=1440,900")
    co.set_argument("--lang=zh-CN")
    # Turnstile 对自动化标志很敏感，尽量抹掉
    co.set_argument("--disable-blink-features=AutomationControlled")
    co.set_argument("--disable-features=IsolateOrigins,site-per-process")

    configure_proxy(co)

    page = ChromiumPage(co)
    page.set.timeouts(base=10, page_load=90, script=30)
    return page


def check_egress(page) -> None:
    """打印出口 IP，代理失效时一眼能看出来。"""
    try:
        page.get("https://api.ipify.org/?format=text")
        ip = (page.ele("tag:body", timeout=5).text or "").strip()
        log(f"▶ 出口 IP: {ip[:40]}")
    except Exception as exc:
        log(f"⚠️ 出口 IP 检测失败: {exc}")


def wait_ready(page, seconds=3) -> None:
    try:
        page.wait.doc_loaded(timeout=30)
    except Exception:
        pass
    time.sleep(seconds)


def find_clickable(page, keywords, tags=("button", "a"), max_len=40):
    """按可见文本找可点击元素。"""
    lowered = [k.lower() for k in keywords]
    for tag in tags:
        for ele in page.eles(f"tag:{tag}"):
            try:
                txt = (ele.text or "").strip()
            except Exception:
                continue
            if not txt or len(txt) > max_len:
                continue
            low = txt.lower()
            if not any(k in low for k in lowered):
                continue
            try:
                if not ele.states.is_enabled or not ele.states.is_displayed:
                    continue
            except Exception:
                pass
            return ele
    return None


def page_has_text(page, words, tags=("button", "a", "div", "p", "span", "h1", "h2")) -> bool:
    return find_clickable(page, words, tags=tags, max_len=200) is not None


def read_timer(page):
    """从页面文本里抓剩余时长，返回小时数；抓不到返回 None。

    卡片实际显示的是 "Sleep in 165h"（日文 "Sleep in" 相同），
    另外也兼容 "剩余 6天 23h" / "残り 6日 23h" / "6d 23h left"。
    """
    try:
        body = page.ele("tag:body", timeout=1)
        text = body.text if body else ""
    except Exception:
        return None
    for pat in PAT_HOURS:
        m = pat.search(text)
        if not m:
            continue
        groups = m.groups()
        if len(groups) == 2:
            return int(groups[0]) * 24 + int(groups[1])
        return int(groups[0])
    return None


def timer_label(hours) -> str:
    if hours is None:
        return "未读到"
    return f"剩余约 {hours // 24}天 {hours % 24}h（{hours}h）"


# --------------------------------------------------------------------------- #
# 登录（全程页面操作）
# --------------------------------------------------------------------------- #

def set_local_storage(page, key: str, value: str) -> bool:
    """通过 CDP 的 DOMStorage 直写 localStorage。

    Discord 的登录页把 Storage API 从页面 JS 上下文里屏蔽了
    （typeof localStorage === 'undefined'），所以不能在页面里跑
    localStorage.setItem，必须走 CDP 写存储层。
    """
    try:
        page.run_cdp("DOMStorage.enable")
        frame_id = page.run_cdp("Page.getFrameTree")["frameTree"]["frame"]["id"]
        storage_key = page.run_cdp("Storage.getStorageKeyForFrame", frameId=frame_id)["storageKey"]
        page.run_cdp(
            "DOMStorage.setDOMStorageItem",
            storageId={"storageKey": storage_key, "isLocalStorage": True},
            key=key,
            value=value,
        )
        got = page.run_cdp(
            "DOMStorage.getDOMStorageItems",
            storageId={"storageKey": storage_key, "isLocalStorage": True},
        )
        entries = got.get("entries") or []
        ok = any(k == key for k, _ in entries)
        log(f"▶ localStorage 写入 {key}: {'成功' if ok else '未确认'} (storageKey={storage_key})")
        return ok
    except Exception as exc:
        log(f"⚠️ CDP 写 localStorage 失败: {str(exc)[:200]}")
        return False


def discord_login(page, token: str) -> bool:
    if not token:
        log("❌ 缺少 DISCORD_TOKEN")
        return False

    log("▶ 注入 Discord token...")
    page.get("https://discord.com/login")
    wait_ready(page, 3)

    # 确认确实停在 discord.com 上
    for _ in range(20):
        try:
            if "discord.com" in str(page.run_js("return location.origin;")) and \
               str(page.run_js("return document.readyState;")) == "complete":
                break
        except Exception:
            pass
        time.sleep(1)
    else:
        log(f"❌ Discord 登录页未加载，URL: {page.url}")
        return False

    if not set_local_storage(page, "token", json.dumps(token)):
        log("❌ token 写入失败")
        return False

    page.refresh()
    wait_ready(page, 3)

    for _ in range(30):
        time.sleep(1)
        if "/channels/" in page.url:
            log("✅ Discord 已登录（跳转客户端）")
            return True
        if page.ele('div[class*="app_"]', timeout=0.2):
            log("✅ Discord 已登录")
            return True

    log(f"❌ Discord 登录失败，URL: {page.url}")
    return False


def click_discord_authorize(page, timeout=90) -> bool:
    log("▶ 等待 Discord OAuth 授权页...")
    deadline = time.time() + 45
    while time.time() < deadline:
        if "oauth2/authorize" in page.url:
            break
        time.sleep(1)
    else:
        log(f"❌ 未进入授权页，URL: {page.url}")
        return False

    log(f"▶ 授权页: {page.url[:110]}")
    wait_ready(page, 2)

    deadline = time.time() + timeout
    while time.time() < deadline:
        # 授权按钮常在折叠区下方，滚到底
        page.run_js(
            "(() => {document.querySelectorAll('*').forEach(e => {"
            "if (e.scrollHeight > e.clientHeight) e.scrollTop = e.scrollHeight;"
            "}); window.scrollTo(0, document.body.scrollHeight);})()"
        )
        time.sleep(0.6)

        btn = find_clickable(page, AUTH_BTN, tags=("button", "div", "span"), max_len=30)
        if btn:
            try:
                btn.click()
                log(f"✅ 已点击授权: {(btn.text or '').strip()[:20]}")
                time.sleep(2)
                return True
            except Exception as exc:
                log(f"⚠️ 点击授权失败({exc})，重试")
        time.sleep(1)

    log("❌ 无法点击授权按钮")
    return False


def site_login(page, token: str) -> bool:
    if not discord_login(page, token):
        return False

    log("▶ 点击站点登录入口...")
    page.get(f"{SITE}/login")
    wait_ready(page, 3)

    # 登录入口本身就是 <a href="/api/auth/login">，点击它，不直接拼地址
    login_link = find_clickable(page, LOGIN_BTN, tags=("a", "button"), max_len=40)
    if login_link:
        href = ""
        try:
            href = login_link.attr("href") or ""
        except Exception:
            pass
        try:
            login_link.click()
            log(f"▶ 已点击登录入口: {(login_link.text or '').strip()[:24]}")
        except Exception:
            # React 重渲染会把句柄冲掉，直接走它指向的地址
            target = href or f"{SITE}/api/auth/login"
            log(f"▶ 登录入口句柄失效，直接访问 {target}")
            page.get(target)
    else:
        log("⚠️ 未找到登录入口，直接访问")
        page.get(f"{SITE}/api/auth/login")

    if not click_discord_authorize(page):
        return False

    # 等回调落回站点
    deadline = time.time() + 90
    while time.time() < deadline:
        time.sleep(1)
        if page.url.startswith(SITE) and "/login" not in page.url:
            break
    wait_ready(page, 4)
    log(f"▶ 落地页面: {page.url}")

    # 纯 DOM 判定：登录页按钮消失 + 出现续期/添加 Bot 之类元素
    for _ in range(20):
        if page_has_text(page, LOGIN_BTN, tags=("a", "button", "div")):
            time.sleep(1)
            continue
        if find_clickable(page, RENEW_BTN, tags=("button",), max_len=24):
            log("✅ 站点登录成功（已看到续期按钮）")
            return True
        if page_has_text(page, ("添加 Bot", "Bot を追加", "Add Bot"), tags=("button", "div", "span")):
            log("✅ 站点登录成功（已看到添加 Bot）")
            return True
        time.sleep(1)

    log("❌ 站点登录状态未确认")
    return False


# --------------------------------------------------------------------------- #
# Turnstile
# --------------------------------------------------------------------------- #

TURNSTILE_IFRAME = 'css:iframe[src^="https://challenges.cloudflare.com"]'


def has_turnstile(page) -> bool:
    return bool(page.ele(TURNSTILE_IFRAME, timeout=0.2))


def solve_turnstile(page, timeout=75) -> bool:
    """点掉 Turnstile 的交互式复选框，返回是否已点成功。

    实测要点（cloud.m-ws.cc 的 renew 弹窗）：
      - checkbox 在 iframe 的 shadow DOM 内，真正可点的是 <label>
      - 相对 input 的坐标点击无效；label.click() 通常 2~4 秒即通过
      - widget 会重渲染，元素句柄随时失效，必须每轮重新定位
      - 通过后页面可能立即重载，导致 "connection to the page has been
        disconnected"，这属于成功信号而不是失败
      - token 不会写进 input 的 value（永远为 None），所以本函数只负责点击，
        真正的结果由调用方用 renew 响应判定
    """
    log("🛡️ 出现 Turnstile，尝试点击 checkbox...")
    deadline = time.time() + timeout
    clicked = False

    while time.time() < deadline:
        # 页面重载/跳转后 iframe 会消失，代表已通过
        if not has_turnstile(page):
            if clicked:
                log("✅ Turnstile 已消失")
                return True
            time.sleep(1)
            continue

        if not clicked:
            time.sleep(2)  # 等 shadow DOM 渲染稳定
            try:
                frame = page.get_frame(TURNSTILE_IFRAME, timeout=8)
                if frame:
                    sr = frame.ele("tag:body").shadow_root
                    if sr:
                        label = sr.ele("css:label")
                        if label:
                            label.click()
                            clicked = True
                            log("▶ 已点击 Turnstile label")
                        else:
                            box = sr.ele("css:div.main-wrapper")
                            if box:
                                box.click.at(offset_x=30, offset_y=32)
                                clicked = True
                                log("▶ 已点击 main-wrapper（兜底）")
            except Exception as exc:
                msg = str(exc)
                # 页面已经重载：说明点击生效、验证已过
                if "disconnected" in msg.lower() or "无效" in msg or "失效" in msg:
                    log("✅ Turnstile 已通过（页面已刷新）")
                    return True
                log(f"⚠️ 点击 Turnstile 失败: {msg[:120]}")
        time.sleep(1)

    if clicked:
        log("⚠️ Turnstile 超时，但已点击过，交由续期响应判定")
    else:
        log("❌ Turnstile 未能点击")
    return clicked


# --------------------------------------------------------------------------- #
# 续期（纯点击）
# --------------------------------------------------------------------------- #

def dump_dom(page, tag: str) -> None:
    """DEBUG_DOM=1 时把关键 DOM 结构打到日志，用来校准选择器。"""
    if os.environ.get("DEBUG_DOM") != "1":
        return
    log(f"===== DOM DUMP [{tag}] url={page.url} =====")
    try:
        for b in page.eles("tag:button"):
            try:
                log(f"  BUTTON {repr((b.text or '').strip()[:40])} "
                    f"class={(b.attr('class') or '')[:60]}")
            except Exception:
                pass
        for a in page.eles("tag:a"):
            try:
                txt = (a.text or "").strip()
                if txt and len(txt) <= 40:
                    log(f"  A      {repr(txt)} href={(a.attr('href') or '')[:60]}")
            except Exception:
                pass
        # 含「Bot」的短文本节点，看卡片长什么样
        for ele in page.eles("tag:div"):
            try:
                txt = (ele.text or "").strip()
                if not txt or len(txt) > 60:
                    continue
                cls = (ele.attr("class") or "")
                if re.search(r"(Bot|ボット|机器人|剩余|残り|续期|延長)", txt):
                    log(f"  DIV    {repr(txt[:60])} class={cls[:70]}")
            except Exception:
                pass
    except Exception as exc:
        log(f"  dump 异常: {exc}")
    log(f"===== DOM DUMP END [{tag}] =====")


def locate_renew_button(page):
    """在 /dash 的 Bot 卡片上找「续期」按钮。

    真实结构：<button class="card-module__FrKoNG__btn card-module__FrKoNG__btnGhost">续期</button>
    """
    btn = find_clickable(page, RENEW_BTN, tags=("button",), max_len=24)
    if btn:
        log(f"✅ 找到续期按钮: {(btn.text or '').strip()}")
        return btn

    # 兜底：卡片里的 ghost 按钮
    for ele in page.eles("css:button[class*='card-module']"):
        try:
            txt = (ele.text or "").strip()
        except Exception:
            continue
        if any(k in txt for k in RENEW_BTN) or txt == "":
            # 空文本的是图标按钮，跳过
            if any(k in txt for k in RENEW_BTN):
                log(f"✅ 通过 class 找到续期按钮: {txt}")
                return ele

    page.get_screenshot(path=".", name="no_renew_button.png", full_page=True)
    log("❌ 找不到续期按钮")
    return None


def do_renew(page):
    """点续期 -> 按需过 Turnstile -> 用站点自身发出的 renew 响应确认结果。

    判定只依赖浏览器自己产生的流量（listen 是监听，不发起请求），
    并用响应的 remaining_hours 作为权威结果。
    """
    before = read_timer(page)
    log(f"▶ 续期前: {timer_label(before)}")

    btn = locate_renew_button(page)
    if not btn:
        return False, "找不到续期按钮"

    # 只监听页面自己发出的续期请求
    page.listen.start("renew")
    log("▶ 点击续期...")
    try:
        btn.click()
    except Exception as exc:
        page.listen.stop()
        return False, f"点击失败: {exc}"

    # 处理可能弹出的 Turnstile；一旦 renew 响应到达就立即收工
    packet = None
    turnstile_handled = False
    for _ in range(60):
        packet = page.listen.wait(count=1, timeout=1)
        if packet:
            break
        if not turnstile_handled and has_turnstile(page):
            turnstile_handled = True
            solve_turnstile(page)

    if not packet:
        packet = page.listen.wait(count=1, timeout=60)
    page.listen.stop()

    after = read_timer(page)
    log(f"▶ 续期后: {timer_label(after)}")
    try:
        page.get_screenshot(path=".", name="after_renew.png", full_page=True)
    except Exception:
        log("⚠️ 续期后截图失败（页面可能正在重载）")

    if packet:
        status = int(getattr(packet.response, "status", 0) or 0)
        body = packet.response.body
        if isinstance(body, str):
            try:
                body = json.loads(body or "{}")
            except Exception:
                body = {}
        body = body or {}
        log(f"▶ renew 响应 {status}: {json.dumps(body, ensure_ascii=False)[:300]}")

        timer = body.get("timer") if isinstance(body, dict) else None
        if timer:
            hours = timer.get("remaining_hours")
            stop_at = timer.get("stop_at", "")
            detail = (f"续期前 {timer_label(before)}\n"
                      f"剩余 {hours}h\n到期 {stop_at}")
            if before is not None and hours is not None and hours < before:
                return False, detail + "\n（续期后时长反而减少，请确认）"
            return 200 <= status < 300, detail

        if 200 <= status < 300:
            return True, f"续期前 {timer_label(before)}\n续期后 {timer_label(after)}"
        return False, f"renew 返回 HTTP {status}"

    # 没抓到响应：退回 DOM 判定
    if after is not None and before is not None and after > before:
        return True, f"续期前 {timer_label(before)}\n续期后 {timer_label(after)}\n（未捕获响应，按计时器增长判定）"
    if after is not None and after >= 167:
        return True, f"续期后 {timer_label(after)}\n（计时器已接近满额）"

    page.get_screenshot(path=".", name="renew_no_response.png", full_page=True)
    return False, f"未捕获续期响应\n续期前 {timer_label(before)}\n续期后 {timer_label(after)}"


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def main() -> int:
    started = time.time()
    token = clean_secret("DISCORD_TOKEN")
    ok, detail = False, "未开始"
    page = None

    try:
        page = build_page()
        check_egress(page)
        page.get(SITE)
        wait_ready(page, 3)

        if not site_login(page, token):
            detail = "登录失败（Discord token 或 OAuth 授权）"
            page.get_screenshot(path=".", name="login_failed.png", full_page=True)
        else:
            # 登录后已经在 /dash；确保停在工作台，不要回到营销首页
            if "/dash" not in page.url:
                page.get(DASH)
                wait_ready(page, 5)
            dump_dom(page, "dash-before-renew")
            ok, detail = do_renew(page)
    except Exception:
        detail = "脚本异常:\n" + traceback.format_exc(limit=4)[-800:]
        log(detail)
        if page:
            try:
                page.get_screenshot(path=".", name="exception.png", full_page=True)
            except Exception:
                pass
    finally:
        cost = int(time.time() - started)
        head = "✅ m-ws 续期成功" if ok else "❌ m-ws 续期失败"
        tg_send(f"{head}\n\n{detail}\n\n耗时 {cost}s")
        if not ok:
            for name in ("turnstile_failed", "no_renew_button", "login_failed", "exception"):
                shot = f"{name}.png"
                if os.path.exists(shot):
                    tg_send_photo(shot, f"失败截图: {name}")
                    break
        if page:
            try:
                page.quit()
            except Exception:
                pass

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
