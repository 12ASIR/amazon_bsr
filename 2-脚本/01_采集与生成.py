# -*- coding: utf-8 -*-
"""
Amazon BSR 榜单每日监控（Running Boards / US 站）
================================================================
功能：每日抓取指定 browse node 的 Top N ASIN，落 SQLite 快照，
      与同一数据源的上一个快照日对比，输出：新进入 / 掉出 / 排名大幅变动。

数据源（--source）：
  browser      : 亚马逊前台 Best Sellers 页面 Playwright 渲染采集（可拿满 Top100，约 30~60s，主用）
  html         : 前台页面 requests 轻量解析（仅约 58 条且缺号，易反爬，备用）
  第三方 API（卖家精灵 / Keepa）链路已移除，脚本内不再保留任何 API 采集分支；
  历史代码见 2-脚本\_旧版\ 下存档。

站点与配送邮编：
  固定采集亚马逊美国站 www.amazon.com；配送邮编由配置 delivery_zip 指定（默认 10001 / 纽约）。
  browser 模式在开抓前用前台「配送至」弹窗把位置锁到该邮编，并回读页面校验是否生效，
  避免价格随亚马逊默认配送位置漂移；html 备源为静态请求、不锁邮编（已在文档中标注）。

用法示例（在项目根目录执行）：
  python 启动.py --source browser
  python 2-脚本\01_采集与生成.py --source browser
  python 2-脚本\01_采集与生成.py --source browser --node 15736621 --top 100
  python 2-脚本\01_采集与生成.py --rebuild-only      只读库重建图表与看板，不联网

节点参考（US）：
  15736611 = Automotive > Exterior Accessories > Running Boards & Steps（父类目）
  15736621 = ... > Running Boards（叶子，通常用它）
================================================================
"""
from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import re
import sqlite3
import sys
import time
from datetime import date, datetime
from html import unescape
from pathlib import Path
from string import Template

try:
    import requests
except ImportError:
    requests = None

# ---------------- 项目骨架路径常量（三层极简结构：1-数据源 / 2-脚本 / 3-输出） ----------------
# 本脚本固定位于  项目根\2-脚本\  下，BASE 指向项目根。
SCRIPT_DIR = Path(__file__).resolve().parent          # …\AM BSR每日监控\2-脚本
BASE = SCRIPT_DIR.parent                              # …\AM BSR每日监控

CONFIG_PATH = SCRIPT_DIR / "监控配置.json"              # 配置随脚本同目录
DB_PATH = BASE / "1-数据源" / "bsr_history.db"          # 唯一数据源（SQLite 快照库）
OUT_DIR = BASE / "3-输出"                              # 全部产物根目录
LOG_DIR = SCRIPT_DIR / "logs"                          # 运行日志

# 3-输出 下的四个分类子目录（集中定义，改版只需改这里）
DETAIL_SUB = "明细"          # 每日完整榜单明细，一天一个日期子目录
NEW_SUB = "新增名单"          # 新进入清单 + 品牌汇总 CSV
CHART_SUB = "可视化"          # 新增进入名单 PNG
BOARD_SUB = "看板"            # 单页看板 index.html

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# ---------------- 站点 / 配送位置常量 ----------------
# 前台采集固定为亚马逊美国站；配送邮编用于锁定价格口径（默认 10001 = 纽约曼哈顿）。
AMZ_SITE = "https://www.amazon.com"
# 商品详情页前缀：ASIN 名单每条补上可直达链接
ASIN_URL_BASE = "https://www.amazon.com/dp/"
DEFAULT_ZIP = "10001"
# 最近一次 browser 采集的邮编锁定结果（供主流程打印与文档核对，不参与落库）
ZIP_STATE: dict = {}

# 前台页面正则
RE_RANK = re.compile(r'class="[^"]*zg-bdg-text[^"]*"\s*>\s*#(\d+)\s*<')
RE_ASIN = re.compile(r'data-asin="([A-Z0-9]{10})"')
RE_TITLE = re.compile(r'<img alt="([^"]{5,400})"')
RE_PRICE = re.compile(r'_cDEzb_p13n-sc-price_[^"]*">\s*\$([\d,]+\.\d{2})')


# ---------------------------------------------------------------- 通用
def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[warn] 监控配置.json 解析失败，已忽略: {e}", file=sys.stderr)
    return {}


def _get(url, params=None, timeout=30, retries=3):
    if requests is None:
        raise RuntimeError("缺少 requests 库，请先 pip install requests")
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
            if r.status_code == 200:
                return r
            last = f"HTTP {r.status_code}"
            if r.status_code == 400:      # 前台翻页越界，不必重试
                break
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
        time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"请求失败 {url} -> {last}")


# ---------------------------------------------------------------- 前台 HTML（轻量）
def _parse_bestsellers_html(html: str) -> list[dict]:
    marks = [(m.start(), 0, m.group(1)) for m in RE_RANK.finditer(html)]
    marks += [(m.start(), 1, m.group(1)) for m in RE_ASIN.finditer(html)]
    marks.sort()
    rows, pending = [], None
    for pos, kind, val in marks:
        if kind == 0:
            pending = int(val)
        elif pending is not None:
            seg = html[pos:pos + 5000]
            ti, pr = RE_TITLE.search(seg), RE_PRICE.search(seg)
            rows.append({"rank": pending, "asin": val,
                         "title": (unescape(ti.group(1)[:200]) if ti else ""),
                         "price": (pr.group(1) if pr else "")})
            pending = None
    return rows


def fetch_html(node: str, top: int, pages: int = 4, pause: float = 2.5) -> list[dict]:
    """requests 轻量备源。注意：该模式无法锁定配送邮编，价格取亚马逊默认位置口径。"""
    all_rows, seen = [], set()
    for pg in range(1, pages + 1):
        url = f"{AMZ_SITE}/gp/bestsellers/automotive/{node}?pg={pg}"
        try:
            html = _get(url, timeout=30).text
        except RuntimeError:
            break
        for row in _parse_bestsellers_html(html):
            if row["asin"] not in seen:
                seen.add(row["asin"])
                all_rows.append(row)
        time.sleep(pause)
    all_rows.sort(key=lambda x: x["rank"])
    return all_rows[:top]


# ---------------------------------------------------------------- 配送邮编锁定（美国站）
def lock_delivery_zip(page, zipcode: str) -> dict:
    """把亚马逊前台配送位置锁定到指定邮编（美国站），并回读页面确认是否生效。

    做法：进首页 → 点顶部「配送至」入口 → 弹窗内填邮编 → 提交 → 必要时点确认 → 回读
    #glow-ingress-line2 文案，命中邮编即认定锁定成功。任何一步失败都不抛错，只如实记录
    失败环节与回读文案，交由主流程打印告知（避免锁邮编失败连带整次采集失败）。
    返回：{"zip", "before", "actual", "locked", "method"}
    """
    info = {"zip": zipcode, "before": "", "actual": "", "locked": False, "method": ""}
    if not zipcode:
        info["method"] = "未配置 delivery_zip，跳过锁定"
        return info
    try:
        page.goto(f"{AMZ_SITE}/", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(1800)
        lbl = page.query_selector("#glow-ingress-line2")
        info["before"] = (lbl.inner_text().strip() if lbl else "")

        opened = False
        for sel in ("#nav-global-location-popover-link", "#glow-ingress-block", "#glow-ingress-line2"):
            try:
                page.click(sel, timeout=6000)
                opened = True
                break
            except Exception:
                continue
        if not opened:
            info["method"] = "未找到「配送至」入口"
            return info

        box = None
        for sel in ("#GLUXZipUpdateInput", "input[name='zipCode']", "#GLUXZipUpdateInput_0",
                    "input[type='text'][maxlength='5']"):
            try:
                box = page.wait_for_selector(sel, timeout=8000)
                if box:
                    break
            except Exception:
                continue
        if not box:
            info["method"] = "未找到邮编输入框"
            return info

        box.fill("")
        box.type(zipcode, delay=70)
        applied = False
        for sel in ("#GLUXZipUpdate input", "#GLUXZipUpdate",
                    "input[aria-labelledby='GLUXZipUpdate-announce']", "span#GLUXZipUpdate"):
            try:
                page.click(sel, timeout=6000)
                applied = True
                break
            except Exception:
                continue
        if not applied:
            box.press("Enter")
        page.wait_for_timeout(2600)

        # 部分会话会二次弹「确认 / 继续」按钮
        for sel in ("#GLUXConfirmClose", "button[name='glowDoneButton']",
                    "#GLUXConfirmClose-announce", "button.a-button-text[name='glowDoneButton']"):
            try:
                page.click(sel, timeout=2500)
                page.wait_for_timeout(1200)
                break
            except Exception:
                continue
        page.wait_for_timeout(1200)

        lbl = page.query_selector("#glow-ingress-line2")
        info["actual"] = (lbl.inner_text().strip() if lbl else "")
        info["locked"] = zipcode in info["actual"]
        info["method"] = "前台「配送至」弹窗"
    except Exception as e:
        info["method"] = f"锁定过程异常 {type(e).__name__}: {e}"
    return info


# ---------------------------------------------------------------- 前台浏览器渲染
def fetch_browser(node: str, top: int, pages: int = 2, pause: float = 2.0,
                  zipcode: str = DEFAULT_ZIP) -> list[dict]:
    """Playwright 渲染模式：先锁定配送邮编，再滚动触发懒加载取满每页 50 条，两页即 Top100。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError("缺少 playwright，请执行 pip install playwright && playwright install chromium")

    rows, seen = [], set()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(locale="en-US", user_agent=HEADERS["User-Agent"],
                                  viewport={"width": 1440, "height": 900})
        # 语言 / 站点固定美国站
        try:
            ctx.add_cookies([{"name": "lc-main", "value": "en_US", "domain": ".amazon.com", "path": "/"}])
        except Exception:
            pass
        page = ctx.new_page()

        ZIP_STATE.clear()
        ZIP_STATE.update(lock_delivery_zip(page, zipcode))
        page.wait_for_timeout(1200)

        for no in range(1, pages + 1):
            url = f"{AMZ_SITE}/gp/bestsellers/automotive/{node}?pg={no}"
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                time.sleep(2.5)
                for _ in range(12):                 # 滚动触发懒加载
                    page.mouse.wheel(0, 2000)
                    time.sleep(0.6)
                time.sleep(1.5)
            except Exception as e:
                print(f"[warn] 第 {no} 页加载失败: {type(e).__name__}", file=sys.stderr)
                continue

            for card in page.query_selector_all("div[id^='p13n-asin-index-']"):
                badge = card.query_selector(".zg-bdg-text")
                node_asin = card.query_selector("[data-asin]")
                if not badge or not node_asin:
                    continue
                asin = node_asin.get_attribute("data-asin") or ""
                if not re.fullmatch(r"[A-Z0-9]{10}", asin) or asin in seen:
                    continue
                rank = int(re.sub(r"\D", "", badge.inner_text()) or 0)
                img = card.query_selector("img")
                price_el = card.query_selector("[class*='p13n-sc-price']")
                rating_el = card.query_selector(".a-icon-alt")
                seen.add(asin)
                rows.append({
                    "rank": rank,
                    "asin": asin,
                    "title": unescape(img.get_attribute("alt") or "") if img else "",
                    "price": (re.sub(r"[^\d.]", "", price_el.inner_text()) if price_el else ""),
                    "rating": (re.search(r"([\d.]+) out of 5", rating_el.inner_text()).group(1)
                               if rating_el and re.search(r"([\d.]+) out of 5", rating_el.inner_text()) else ""),
                    "reviews": (card.inner_text().split("\n")[3].strip().replace(",", "")
                                if len(card.inner_text().split("\n")) > 3 else ""),
                })
            time.sleep(pause)
        browser.close()

    rows.sort(key=lambda x: x["rank"])
    return rows[:top]


# ---------------------------------------------------------------- 存储层
DDL = """
CREATE TABLE IF NOT EXISTS snapshot (
    snap_date TEXT NOT NULL,
    node      TEXT NOT NULL,
    source    TEXT NOT NULL DEFAULT '',
    rank      INTEGER NOT NULL,
    asin      TEXT NOT NULL,
    title     TEXT,
    price     TEXT,
    rating    TEXT,
    reviews   TEXT,
    PRIMARY KEY (snap_date, node, source, asin)
);
CREATE INDEX IF NOT EXISTS idx_snap ON snapshot (node, source, snap_date);
"""


def db_open(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    # 老库（主键不含 source）平滑迁移，避免多数据源同日冲突
    exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='snapshot'").fetchone()
    if exists:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(snapshot)")]
        if "source" not in cols:
            conn.executescript("ALTER TABLE snapshot RENAME TO snapshot_bak;")
            conn.executescript(DDL)
            conn.execute("""INSERT INTO snapshot (snap_date,node,source,rank,asin,title,price)
                            SELECT snap_date,node,COALESCE(source,'browser'),rank,asin,title,price
                            FROM snapshot_bak""")
            conn.execute("DROP TABLE snapshot_bak")
            conn.commit()
            print("[migrate] 旧库已升级为多数据源结构（source 参与主键）")
            cols = [r[1] for r in conn.execute("PRAGMA table_info(snapshot)")]
        for extra in ("rating", "reviews"):
            if extra not in cols:
                conn.execute(f"ALTER TABLE snapshot ADD COLUMN {extra} TEXT")
                print(f"[migrate] 补齐字段 {extra}")
        conn.commit()
    conn.executescript(DDL)
    return conn


def last_snap_date(conn, node: str, source: str, before: str) -> str | None:
    cur = conn.execute(
        "SELECT MAX(snap_date) FROM snapshot WHERE node=? AND source=? AND snap_date<?",
        (node, source, before))
    return cur.fetchone()[0]


def save_snapshot(conn, node: str, source: str, snap_date: str, rows: list[dict]):
    conn.execute("DELETE FROM snapshot WHERE snap_date=? AND node=? AND source=?",
                 (snap_date, node, source))
    conn.executemany(
        """INSERT INTO snapshot (snap_date,node,source,rank,asin,title,price,rating,reviews)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        [(snap_date, node, source, r["rank"], r["asin"], r.get("title", ""),
          r.get("price", ""), r.get("rating", ""), r.get("reviews", "")) for r in rows])
    conn.commit()


# ---------------------------------------------------------------- 对比层
def diff(conn, node: str, source: str, today: str) -> dict:
    prev_date = last_snap_date(conn, node, source, today)
    if not prev_date:
        return {"prev_date": None, "new_in": [], "dropped": [], "moved": []}

    cur = {a: r for a, r in conn.execute(
        "SELECT asin, rank FROM snapshot WHERE node=? AND source=? AND snap_date=?",
        (node, source, today))}
    old = {a: r for a, r in conn.execute(
        "SELECT asin, rank FROM snapshot WHERE node=? AND source=? AND snap_date=?",
        (node, source, prev_date))}

    new_in = sorted(({"asin": a, "rank": cur[a]} for a in cur.keys() - old.keys()),
                    key=lambda x: x["rank"])
    dropped = sorted(({"asin": a, "rank": old[a]} for a in old.keys() - cur.keys()),
                     key=lambda x: x["rank"])
    moved = []
    for a in cur.keys() & old.keys():
        d = old[a] - cur[a]          # 正数=排名前进
        if abs(d) >= 10:
            moved.append({"asin": a, "prev": old[a], "now": cur[a], "delta": d})
    moved.sort(key=lambda x: -abs(x["delta"]))
    return {"prev_date": prev_date, "new_in": new_in, "dropped": dropped, "moved": moved}


# ---------------------------------------------------------------- 输出层
def write_csv(path: Path, header: list[str], rows: list[list]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


# ---------------------------------------------------------------- 品牌解析层
# 通用/规格类词：出现在标题首位时说明该标题没有品牌前缀（白牌或配件）
_GENERIC_HEADS = {
    "set", "sets", "pair", "pairs", "kit", "kits", "running", "side", "sides",
    "step", "steps", "nerf", "board", "boards", "aluminum", "aluminium",
    "stainless", "steel", "black", "left", "right", "front", "rear", "driver",
    "passenger", "power", "universal", "replacement", "new", "two", "three",
    "four", "one", "full", "crew", "quad", "double", "regular", "extended",
    "inch", "inches", "for", "fits", "fit", "compatible", "oem",
}
# 常见品牌后缀词：与首词合并成一个品牌名
_BRAND_SUFFIX = {
    "auto", "autos", "star", "dynasty", "autoparts", "works", "gear", "tech",
    "line", "max", "pro", "usa", "racing", "accessories", "offroad", "motors",
    "industries", "group", "custom", "design", "designs",
}
# 品牌类型 -> 颜色 / 中文标签
_TYPE_COLOR = {"brand": "#3f7fd0", "white": "#8a9099",
               "parts": "#e08b3c", "bparts": "#e08b3c"}
_TYPE_LABEL = {"brand": "品牌", "white": "白牌",
               "parts": "配件", "bparts": "品牌配件"}
# 判定为「配件/OEM」的关键词
_PARTS_HINTS = ("motor assembly", "motor", "end cap", "step pad", "replacement",
                "bracket", "hardware", "repair", "oem", "mounting", "clip")
# 品牌名截断边界：产品词 / 车型连接词
_CUT_RE = re.compile(
    r"\b(?:running\s+boards?|side\s+steps?|side\s+bars?|step\s+bars?|nerf\s+bars?|"
    r"boards?|steps?|compatible|compatiable|fits?|for|with)\b|\(|\||,|&", re.I)


def parse_brand(title: str) -> dict:
    """从商品标题解析品牌。返回 {name, key, type}，type ∈ brand/white/parts。"""
    t = (title or "").strip()
    m = _CUT_RE.search(t)
    head = t[:m.start()] if m else t[:45]
    # 必须从词首开始取 token，避免 "68461581AA" 里抠出 "AA" 当品牌
    toks = re.findall(r"(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9&'\-\.]*", head)
    toks = [x.strip(".-'") for x in toks if x.strip(".-'")]
    # 配件关键词只在标题前段判断，避免后文 "with Mounting Brackets" 误伤整板商品
    is_parts = any(h in t[:65].lower() for h in _PARTS_HINTS)

    if not toks or toks[0].lower() in _GENERIC_HEADS:
        return {"name": "配件/OEM" if is_parts else "白牌/无品牌",
                "key": "__parts__" if is_parts else "__white__",
                "type": "parts" if is_parts else "white"}

    name = toks[0]
    if len(toks) > 1 and toks[1].lower() in _BRAND_SUFFIX:
        name = f"{toks[0]} {toks[1]}"
    if is_parts:
        # 有品牌的配件（如 XX End Cap / Motor Assembly / Step Pad），不与整板混排
        return {"name": name, "key": toks[0].lower() + "__bparts", "type": "bparts"}
    return {"name": name, "key": toks[0].lower(), "type": "brand"}


def build_brand_map(rows: list[dict]) -> dict[str, dict]:
    """两遍解析品牌：
    1) 用标题首个有效词定品牌（白牌/配件按关键词）；
    2) 对「白牌」条目再回捞：品牌名出现在标题 "|" 之后（如 xx Compatible for ... | AUTOSAVER88 ...）时归该品牌。
    """
    first = {r["asin"]: parse_brand(r.get("title", "")) for r in rows}
    vocab = {b["name"].lower(): b["name"] for b in first.values() if b["type"] == "brand"}
    if not vocab:
        return first
    pats = [(re.compile(r"(?<![a-z0-9])" + re.escape(k) + r"(?![a-z0-9])"), v)
            for k, v in sorted(vocab.items(), key=lambda x: -len(x[0]))]
    for r in rows:
        if first[r["asin"]]["type"] != "white":
            continue
        t = r.get("title", "") or ""
        if "|" not in t:
            continue
        tail = t.split("|", 1)[1].lower()
        for pat, real in pats:
            if pat.search(tail):
                first[r["asin"]] = {"name": real, "key": real.lower(), "type": "brand"}
                break
    return first


def group_by_brand(items: list[dict], brand_of: dict) -> list[dict]:
    """按品牌分组：组内按名次升序，组间按各组最佳名次升序。"""
    groups: dict[str, dict] = {}
    for it in items:
        b = brand_of.get(it["asin"], {"name": "未知", "key": "__unknown__", "type": "white"})
        g = groups.setdefault(b["key"], {"name": b["name"], "type": b["type"], "items": []})
        g["items"].append(it)
    for g in groups.values():
        g["items"].sort(key=lambda x: x["rank"])
        g["best"] = g["items"][0]["rank"]
        g["count"] = len(g["items"])
    return sorted(groups.values(), key=lambda g: (g["best"], g["name"]))


def _to_float(v):
    if v is None:
        return None
    s = str(v).replace("$", "").replace(",", "").replace("￥", "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def render_new_entrants(new_in: list[dict], rows: list[dict], today: str, node: str,
                        source: str, out_png: Path, dropped_n: int = 0,
                        top_show: int = 60, dropped_items: list[dict] | None = None) -> Path | None:
    """新增进入名单可视化（按品牌分组）：
    左：品牌分组分层条（组内按名次）；右上：品牌进入/掉出对比；右下：价格带分布。
    """
    if not new_in:
        return None

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager as fm
    from matplotlib.patches import Patch

    avail = {f.name for f in fm.fontManager.ttflist}
    for cand in ("Microsoft YaHei", "SimHei", "DengXian", "SimSun",
                 "Noto Sans CJK SC", "Noto Sans SC", "WenQuanYi Zen Hei", "DejaVu Sans"):
        if cand in avail:
            plt.rcParams["font.sans-serif"] = [cand]
            break
    plt.rcParams["axes.unicode_minus"] = False

    info = {r["asin"]: r for r in rows}
    data = sorted(new_in, key=lambda x: x["rank"])
    brand_of = build_brand_map(rows)
    groups = group_by_brand(data, brand_of)

    # 超过上限时按「品牌威胁度」截断：先保品牌组，再保白牌/配件，组内按名次
    budget, kept = top_show, []
    for g in groups:
        if budget <= 0:
            break
        take = min(len(g["items"]), budget)
        kept.append({**g, "items": g["items"][:take], "shown_full": take == len(g["items"])})
        budget -= take

    def _fill(t, rank):
        base = _TYPE_COLOR.get(t, "#3f7fd0")
        return base, 0.45 + 0.55 * (1 - rank / 100)

    n_row = sum(1 + len(g["items"]) for g in kept) + len(kept) * 0.5
    fig = plt.figure(figsize=(16.5, max(7.5, 0.36 * n_row + 3.0)), dpi=150)
    gs = fig.add_gridspec(2, 2, width_ratios=[2.5, 1.0], height_ratios=[1.15, 1.0],
                          wspace=0.26, hspace=0.30)

    # ================= 左：按品牌分组的名次明细
    ax1 = fig.add_subplot(gs[:, 0])
    y = 0.0
    tick_y, tick_lb = [], []
    for g in kept:
        prices = [_to_float(info.get(x["asin"], {}).get("price", "")) for x in g["items"]]
        vp = [p for p in prices if p is not None]
        avgp = f"${sum(vp) / len(vp):.0f}" if vp else "无价"
        ax1.axhspan(y - 0.46, y + 0.46, color="#eef1f5", zorder=0)
        ax1.text(0.6, y, f"{g['name']}", fontsize=10, fontweight="bold",
                 color="#1f2937", va="center", zorder=3)
        ax1.text(24, y, f"{len(g['items'])} 条 · 最佳 #{g['best']} · 均价 {avgp}",
                 fontsize=8.6, color="#4b5563", va="center", zorder=3)
        y += 1.0
        for it, p in zip(g["items"], prices):
            base, alpha = _fill(g["type"], it["rank"])
            ax1.barh(y, 101 - it["rank"], height=0.66, color=base, alpha=alpha,
                     edgecolor="white", linewidth=0.5, zorder=2)
            rev = str(info.get(it["asin"], {}).get("reviews", "") or "").strip()
            tail = (f"${p:.2f}" if p is not None else "价格缺失")
            if rev:
                tail += f" · {rev}评"
            ax1.text(101 - it["rank"] + 1.2, y, tail, va="center", fontsize=7.4,
                     color="#4b5563", zorder=3)
            tick_y.append(y)
            tick_lb.append(f"#{it['rank']}  {it['asin']}")
            y += 1.0
        y += 0.5

    ax1.set_yticks(tick_y)
    ax1.set_yticklabels(tick_lb, fontsize=8.2, family="Consolas")
    ax1.set_ylim(-0.9, y)
    ax1.invert_yaxis()
    ax1.set_xlim(0, 124)
    ax1.set_xticks([0, 20, 40, 60, 80, 100])
    ax1.set_xlabel("名次优势（101 − 今日名次，条越长排名越靠前）", fontsize=9)
    ax1.set_title("新进入 ASIN 明细（按品牌分组，组内按名次）", fontsize=11, pad=8,
                  loc="left", fontweight="bold")
    ax1.grid(axis="x", linestyle=":", alpha=0.30)
    ax1.set_axisbelow(True)
    for s in ("top", "right"):
        ax1.spines[s].set_visible(False)
    ax1.tick_params(axis="y", length=0)
    ax1.legend(handles=[Patch(color="#3f7fd0", label="品牌商品"),
                        Patch(color="#8a9099", label="白牌/无品牌"),
                        Patch(color="#e08b3c", label="配件/OEM（含品牌配件）")],
               loc="upper center", bbox_to_anchor=(0.5, -0.055), ncol=3,
               fontsize=8.8, frameon=False)

    # ================= 右上：品牌进入 / 掉出对比
    ax2 = fig.add_subplot(gs[0, 1])
    drop_list = dropped_items or []
    drop_brand = build_brand_map(drop_list) if drop_list else {}
    cnt_in, cnt_out, name_of, type_of = {}, {}, {}, {}
    for d in new_in:
        b = brand_of[d["asin"]]
        cnt_in[b["key"]] = cnt_in.get(b["key"], 0) + 1
        name_of.setdefault(b["key"], b["name"])
        type_of.setdefault(b["key"], b["type"])
    for d in drop_list:
        b = drop_brand[d["asin"]]
        cnt_out[b["key"]] = cnt_out.get(b["key"], 0) + 1
        name_of.setdefault(b["key"], b["name"])
        type_of.setdefault(b["key"], b["type"])
    keys = sorted(set(cnt_in) | set(cnt_out),
                  key=lambda k: (-(cnt_in.get(k, 0) + cnt_out.get(k, 0)), -cnt_in.get(k, 0)))[:12]
    ys = list(range(len(keys)))
    ax2.barh(ys, [cnt_in.get(k, 0) for k in keys], height=0.60,
             color=[_TYPE_COLOR.get(type_of[k], "#3f7fd0")
                    for k in keys], alpha=0.90, edgecolor="white", linewidth=0.5,
             label="新进入")
    ax2.barh(ys, [-cnt_out.get(k, 0) for k in keys], height=0.60,
             color="#cf6a6a", alpha=0.72, edgecolor="white", linewidth=0.5,
             label="掉出")
    ax2.set_yticks(ys)
    ax2.set_yticklabels([name_of[k] for k in keys], fontsize=8.4)
    mx = max([cnt_in.get(k, 0) for k in keys] + [cnt_out.get(k, 0) for k in keys] + [2])
    ax2.set_xlim(-mx - 1.6, mx + 1.9)
    ax2.set_xticks(range(-mx, mx + 1, max(1, mx // 3 or 1)))
    ax2.set_xticklabels([str(abs(v)) for v in ax2.get_xticks()], fontsize=8)
    ax2.axvline(0, color="#8b95a1", linewidth=0.9)
    ax2.set_xlabel("← 掉出条数    |    新进入条数 →", fontsize=8.6)
    ax2.set_title("品牌进入 / 掉出对比（看谁在真扩张）", fontsize=10, pad=7,
                  loc="left", fontweight="bold")
    for yy, k in zip(ys, keys):
        i_, o_ = cnt_in.get(k, 0), cnt_out.get(k, 0)
        if i_:
            ax2.text(i_ + 0.12, yy, f"+{i_}", va="center", fontsize=7.8, color="#2f6bb5")
        if o_:
            ax2.text(-o_ - 0.12, yy, f"-{o_}", va="center", ha="right",
                     fontsize=7.8, color="#b04a4a")
    ax2.grid(axis="x", linestyle=":", alpha=0.28)
    ax2.set_axisbelow(True)
    ax2.tick_params(axis="y", length=0)
    ax2.legend(fontsize=8, frameon=False, loc="lower right")
    for s in ("top", "right"):
        ax2.spines[s].set_visible(False)

    # ================= 右下：价格带分布
    ax3 = fig.add_subplot(gs[1, 1])
    allp = [_to_float(info.get(d["asin"], {}).get("price", "")) for d in data]
    valid = [p for p in allp if p is not None]
    if valid:
        bins = min(12, max(4, len(valid) // 3))
        ax3.hist(valid, bins=bins, color="#3f7fd0", alpha=0.80, edgecolor="white")
        avg = sum(valid) / len(valid)
        ax3.axvline(avg, color="#c0392b", linestyle="--", linewidth=1.2)
        ax3.annotate(f"均价 ${avg:.0f}", xy=(avg, ax3.get_ylim()[1]),
                     xytext=(2, -3), textcoords="offset points", fontsize=8.2,
                     color="#c0392b", va="top", ha="left")
        ax3.set_xlabel("价格（USD）", fontsize=8.8)
        ax3.set_ylabel("ASIN 数量", fontsize=8.8)
        ax3.set_title(f"新进入价格带（有效 {len(valid)}/{len(data)}）", fontsize=10,
                      pad=7, loc="left", fontweight="bold")
        ax3.grid(axis="y", linestyle=":", alpha=0.30)
        ax3.set_axisbelow(True)
        for s in ("top", "right"):
            ax3.spines[s].set_visible(False)
    else:
        ax3.text(0.5, 0.5, "价格数据缺失", ha="center", va="center",
                 transform=ax3.transAxes, fontsize=11, color="#888888")
        ax3.set_xticks([])
        ax3.set_yticks([])

    n_brand = sum(1 for d in data if brand_of[d["asin"]]["type"] == "brand")
    n_white = sum(1 for d in data if brand_of[d["asin"]]["type"] == "white")
    n_parts = sum(1 for d in data if brand_of[d["asin"]]["type"] == "parts")
    fig.suptitle(
        f"Amazon BSR 新增进入名单（按品牌分组）  |  节点 {node}  |  {today}  |  数据源 {source}\n"
        f"新增 {len(data)} 条（品牌 {n_brand} / 白牌 {n_white} / 配件 {n_parts}，涉及 "
        f"{len(groups)} 个品牌组）  ·  掉出 {dropped_n} 条  ·  本图展示 {len(tick_y)} 条",
        fontsize=12.5, fontweight="bold", y=0.995)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return out_png


def brand_summary(new_in: list[dict], rows: list[dict]) -> list[list]:
    """品牌维度汇总：品牌 / 新增条数 / 最佳名次 / 均价 / 价格中位 / 平均评分数。"""
    info = {r["asin"]: r for r in rows}
    brand_of = build_brand_map(rows)
    out = []
    for g in group_by_brand(new_in, brand_of):
        ps = sorted(p for p in (_to_float(info.get(x["asin"], {}).get("price", ""))
                                for x in g["items"]) if p is not None)
        rs = [_to_float(info.get(x["asin"], {}).get("reviews", "")) for x in g["items"]]
        rs = [r for r in rs if r is not None]
        med = (ps[len(ps) // 2] if len(ps) % 2 else (ps[len(ps) // 2 - 1] + ps[len(ps) // 2]) / 2) if ps else ""
        out.append([
            g["name"], _TYPE_LABEL.get(g["type"], g["type"]),
            len(g["items"]), g["best"],
            round(sum(ps) / len(ps), 2) if ps else "",
            round(med, 2) if med != "" else "",
            round(sum(rs) / len(rs), 0) if rs else "",
            " / ".join(f"#{x['rank']} {x['asin']}" for x in g["items"][:6]),
        ])
    return out


def print_report(node, today, rows, res, source):
    print("=" * 62)
    print(f"节点 {node} | 快照日 {today} | 数据源 {source} | 抓到 {len(rows)} 条")
    if rows:
        ranks = sorted(r["rank"] for r in rows)
        print(f"名次范围 #{ranks[0]} ~ #{ranks[-1]} | 去重 ASIN {len({r['asin'] for r in rows})} 个")
    if res["prev_date"] is None:
        print("首次运行，已建立基线快照。下次运行开始输出新进入清单。")
        print("=" * 62)
        return
    print(f"对比基准：{res['prev_date']}（同数据源）")
    print(f"新进入 {len(res['new_in'])} 个 / 掉出 {len(res['dropped'])} 个 / 大幅变动 {len(res['moved'])} 个")
    if res["new_in"]:
        print("\n[新进入榜单]")
        for x in res["new_in"][:20]:
            print(f"  #{x['rank']:<4} {x['asin']}")
    if res["dropped"]:
        print("\n[掉出榜单]")
        for x in res["dropped"][:20]:
            print(f"  原#{x['rank']:<4} {x['asin']}")
    if res["moved"]:
        print("\n[排名变动≥10]")
        for x in res["moved"][:10]:
            arrow = "↑" if x["delta"] > 0 else "↓"
            print(f"  {x['asin']}  #{x['prev']} -> #{x['now']}  {arrow}{abs(x['delta'])}")
    print("=" * 62)


# ---------------------------------------------------------------- 增强图表层（份额趋势 / 桑基 / 四象限）
# 三张图全部由 1-数据源 历史快照与 3-输出 真实明细推导，不做任何虚构补数。
SANKEY_NEW = "__new__"            # 左列虚拟节点：上一快照日不在榜内
SANKEY_DROP = "__drop__"          # 右列虚拟节点：今日已掉出榜单
SANKEY_NEW_LABEL = "新进入（榜外）"
SANKEY_DROP_LABEL = "掉出榜外"
SHARE_MAX_DAYS = 30
SHARE_TOP_BRANDS = 8
CHART_DPI = 132
_PALETTE = ["#3f7fd0", "#e08b3c", "#5aa469", "#c0564f", "#8a6fbf",
            "#2f9ea8", "#b8912f", "#7b8896", "#9d8ec7", "#cf7fa3"]


def _plt():
    """懒加载 matplotlib 并统一中文字体，返回 pyplot。"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager as fm
    avail = {f.name for f in fm.fontManager.ttflist}
    for cand in ("Microsoft YaHei", "SimHei", "DengXian", "SimSun", "Noto Sans CJK SC",
                 "Noto Sans SC", "WenQuanYi Zen Hei", "DejaVu Sans"):
        if cand in avail:
            plt.rcParams["font.sans-serif"] = [cand]
            break
    plt.rcParams["axes.unicode_minus"] = False
    return plt


def db_snapshot_dates(conn, node: str, source: str) -> list[str]:
    """库内「节点 + 数据源」的全部快照日（升序）。"""
    cur = conn.execute("SELECT DISTINCT snap_date FROM snapshot WHERE node=? AND source=? ORDER BY snap_date",
                       (node, source))
    return [r[0] for r in cur]


def db_day_rows(conn, node: str, source: str, snap_date: str) -> list[dict]:
    """按快照日读回榜单明细（字段口径与前台采集一致）。"""
    cur = conn.execute(
        "SELECT rank, asin, title, price, rating, reviews FROM snapshot "
        "WHERE snap_date=? AND node=? AND source=? ORDER BY rank", (snap_date, node, source))
    return [{"rank": r[0], "asin": r[1], "title": r[2] or "", "price": r[3] or "",
             "rating": r[4] or "", "reviews": r[5] or ""} for r in cur]


def _median(vals: list[float]) -> float:
    xs = sorted(vals)
    n = len(xs)
    if not n:
        return 0.0
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2.0


# ---------------- ① 品牌份额时间序列 ----------------
def build_share_series(conn, node: str, source: str, max_days: int = SHARE_MAX_DAYS,
                       top_brands: int = SHARE_TOP_BRANDS) -> dict:
    """逐快照日统计各品牌上榜条数占比（%），取份额峰值最高的若干品牌，其余合并为「其他品牌」。"""
    dates = db_snapshot_dates(conn, node, source)[-max_days:]
    per_day = []
    for d in dates:
        rows = db_day_rows(conn, node, source, d)
        if not rows:
            continue
        brand_of = build_brand_map(rows)
        cnt: dict[str, int] = {}
        for r in rows:
            nm = (brand_of.get(r["asin"]) or {}).get("name") or "未知"
            cnt[nm] = cnt.get(nm, 0) + 1
        per_day.append({"date": d, "counts": cnt, "total": len(rows)})
    if not per_day:
        return {"dates": [], "series": [], "totals": []}
    peak: dict[str, float] = {}
    for p in per_day:
        tot = max(1, p["total"])
        for nm, c in p["counts"].items():
            peak[nm] = max(peak.get(nm, 0.0), c / tot)
    keep = [nm for nm, _ in sorted(peak.items(), key=lambda x: (-x[1], x[0]))[:top_brands]]
    series, covered = [], [0.0] * len(per_day)
    for nm in keep:
        vals = []
        for i, p in enumerate(per_day):
            v = round(100.0 * p["counts"].get(nm, 0) / max(1, p["total"]), 2)
            vals.append(v)
            covered[i] += v
        series.append({"name": nm, "share": vals})
    rest = [round(max(0.0, 100.0 - c), 2) for c in covered]
    if any(v > 0.05 for v in rest):
        series.append({"name": "其他品牌", "share": rest})
    return {"dates": [p["date"] for p in per_day], "series": series,
            "totals": [p["total"] for p in per_day]}


def render_brand_share_trend(conn, node: str, source: str, out_png: Path,
                             max_days: int = SHARE_MAX_DAYS) -> Path | None:
    """品牌上榜份额时间序列折线图（多快照日），末尾标注最新份额。"""
    data = build_share_series(conn, node, source, max_days)
    dates = data["dates"]
    if not dates:
        print("[份额趋势] 库内无快照记录，跳过出图", file=sys.stderr)
        return None
    plt = _plt()
    xs = list(range(len(dates)))
    n_ser = len(data["series"])
    fig, ax = plt.subplots(figsize=(11.4, 4.7), dpi=CHART_DPI)
    for i, sv in enumerate(data["series"]):
        is_other = sv["name"] == "其他品牌"
        c = "#b9c0c9" if is_other else _PALETTE[i % len(_PALETTE)]
        ax.plot(xs, sv["share"], marker="o", markersize=4.2 if not is_other else 3.0,
                linewidth=1.2 if is_other else 1.9, color=c, label=sv["name"],
                alpha=0.55 if is_other else 0.95, zorder=2 if not is_other else 1)
        ax.annotate(f"{sv['share'][-1]:.1f}%", xy=(xs[-1], sv["share"][-1]), xytext=(7, 0),
                    textcoords="offset points", fontsize=8.2, color=c, va="center")
    ax.set_xticks(xs)
    ax.set_xticklabels([d[5:] for d in dates], fontsize=9, rotation=0)
    ax.set_xlabel("快照日（月-日）", fontsize=9.5)
    ax.set_ylabel("上榜份额（%）", fontsize=9.5)
    top_max = max(max(s["share"]) for s in data["series"])
    ax.set_ylim(0, min(100.0, max(8.0, top_max * 1.3)))
    ax.set_xlim(-0.35, len(dates) - 0.35)
    ax.grid(axis="y", linestyle=":", linewidth=0.8, alpha=0.4)
    ax.set_axisbelow(True)
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    ax.tick_params(labelsize=9)
    ax.legend(fontsize=8.3, ncol=min(5, max(1, n_ser)), frameon=False,
              loc="upper center", bbox_to_anchor=(0.5, -0.19))
    ax.set_title(f"品牌上榜份额时间序列 · 节点 {node} · 数据源 {source} · "
                 f"{dates[0]} ~ {dates[-1]}（{len(dates)} 个快照日，日榜 Top{max(data['totals'])} 口径）",
                 fontsize=10.8, loc="left", fontweight="bold", pad=8)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(out_png, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return out_png


# ---------------- ② 进入 / 掉出 桑基图 ----------------
def compute_brand_flows(prev_rows: list[dict], cur_rows: list[dict]) -> tuple[dict, dict, dict]:
    """按 ASIN 追踪「上一快照日品牌 → 今日品牌」的上榜流转，并补上 榜外→榜内 / 榜内→榜外。"""
    pb, cb = build_brand_map(prev_rows), build_brand_map(cur_rows)
    prev = {r["asin"]: (pb.get(r["asin"]) or {}).get("name") or "未知" for r in prev_rows}
    cur = {r["asin"]: (cb.get(r["asin"]) or {}).get("name") or "未知" for r in cur_rows}
    flows: dict[tuple, int] = {}
    for a in prev.keys() & cur.keys():
        flows[(prev[a], cur[a])] = flows.get((prev[a], cur[a]), 0) + 1
    for a in prev.keys() - cur.keys():
        flows[(prev[a], SANKEY_DROP)] = flows.get((prev[a], SANKEY_DROP), 0) + 1
    for a in cur.keys() - prev.keys():
        flows[(SANKEY_NEW, cur[a])] = flows.get((SANKEY_NEW, cur[a]), 0) + 1
    return flows, prev, cur


def _merge_small_nodes(flows: dict, side: str, keep_n: int) -> dict:
    """把某一列上的长尾品牌并成「其他品牌」，避免节点过多不可读。"""
    side = side.lower()
    totals: dict[str, int] = {}
    for (l, r), v in flows.items():
        k = l if side == "left" else r
        totals[k] = totals.get(k, 0) + v
    virtual = {SANKEY_NEW, SANKEY_DROP}
    real = sorted([x for x in totals.items() if x[0] not in virtual], key=lambda x: (-x[1], x[0]))
    keep = {k for k, _ in real[:keep_n]}
    merged_name = "其他品牌（上方）" if side == "left" else "其他品牌（今日）"
    out: dict[tuple, int] = {}
    for (l, r), v in flows.items():
        k = l if side == "left" else r
        nk = k if (k in keep or k in virtual) else merged_name
        nl = nk if side == "left" else l
        nr = r if side == "left" else nk
        out[(nl, nr)] = out.get((nl, nr), 0) + v
    return out


def render_brand_sankey(prev_rows: list[dict], cur_rows: list[dict], prev_date: str, today: str,
                        node: str, source: str, out_png: Path, keep_each: int = 9) -> Path | None:
    """进入 / 掉出 桑基图：左列=上一快照日品牌（含「新进入」），右列=今日品牌（含「掉出榜外」）。"""
    if not prev_rows or not cur_rows:
        print("[桑基] 缺少可比的两个快照日，跳过出图", file=sys.stderr)
        return None
    flows, prev_map, cur_map = compute_brand_flows(prev_rows, cur_rows)
    if not flows:
        print("[桑基] 无可绘制的流转关系，跳过出图", file=sys.stderr)
        return None
    flows = _merge_small_nodes(_merge_small_nodes(flows, "left", keep_each), "right", keep_each)

    left_tot: dict[str, int] = {}
    right_tot: dict[str, int] = {}
    for (l, r), v in flows.items():
        left_tot[l] = left_tot.get(l, 0) + v
        right_tot[r] = right_tot.get(r, 0) + v
    left_nodes = sorted(left_tot.items(), key=lambda x: (x[0] == SANKEY_NEW, -x[1], x[0]))
    right_nodes = sorted(right_tot.items(), key=lambda x: (x[0] == SANKEY_DROP, -x[1], x[0]))

    from matplotlib.path import Path as MplPath
    from matplotlib.patches import PathPatch, Rectangle
    plt = _plt()
    total = float(sum(flows.values()))
    H = 1.0
    gap_l = H * 0.014 if len(left_nodes) > 1 else 0.0
    gap_r = H * 0.014 if len(right_nodes) > 1 else 0.0
    scale_l = (H - gap_l * (len(left_nodes) - 1)) / total if len(left_nodes) > 1 else H / total
    scale_r = (H - gap_r * (len(right_nodes) - 1)) / total if len(right_nodes) > 1 else H / total

    def layout(nodes, g, sc):
        pos, y = {}, 0.0
        for k, v in nodes:
            h = v * sc
            pos[k] = [y, y + h]
            y += h + g
        return pos

    lpos = layout(left_nodes, gap_l, scale_l)
    rpos = layout(right_nodes, gap_r, scale_r)

    fig_h = max(5.6, min(11.0, 0.30 * max(len(left_nodes), len(right_nodes)) + 3.4))
    fig, ax = plt.subplots(figsize=(11.8, fig_h), dpi=CHART_DPI)
    ax.set_xlim(0, 1)
    # 上下留白给两行标题与末节点标签，避免标题压住首个节点、底节点被裁切
    ax.set_ylim(H * 1.085, -H * 0.075)
    ax.axis("off")

    left_color = {}
    for i, (nm, _) in enumerate(left_nodes):
        left_color[nm] = "#4f9d69" if nm == SANKEY_NEW else _PALETTE[i % len(_PALETTE)]

    # 节点条
    for nm, v in left_nodes:
        y0, y1 = lpos[nm]
        ax.add_patch(Rectangle((0.088, y0), 0.014, max(y1 - y0, 0.002),
                               facecolor=left_color[nm], edgecolor="white", linewidth=0.7, zorder=3))
        ax.text(0.080, (y0 + y1) / 2, f"{nm[:16]}  {v}", ha="right", va="center",
                fontsize=8.6, color="#26313d", zorder=4)
    for nm, v in right_nodes:
        y0, y1 = rpos[nm]
        c = "#b04a42" if nm == SANKEY_DROP else "#465a70"
        ax.add_patch(Rectangle((0.898, y0), 0.014, max(y1 - y0, 0.002),
                               facecolor=c, edgecolor="white", linewidth=0.7, zorder=3))
        ax.text(0.920, (y0 + y1) / 2, f"{nm[:16]}  {v}", ha="left", va="center",
                fontsize=8.6, color="#26313d", zorder=4)

    # 缎带（按左节点分组，沿右节点顺序排布，避免交叉）
    order_r = {nm: i for i, (nm, _) in enumerate(right_nodes)}
    cur_l = {nm: lpos[nm][0] for nm, _ in left_nodes}
    cur_r = {nm: rpos[nm][0] for nm, _ in right_nodes}
    for (l, r), v in sorted(flows.items(), key=lambda kv: (order_r.get(kv[0][1], 99), kv[0][0])):
        hl, hr = v * scale_l, v * scale_r
        y0a, y0b = cur_l[l], cur_l[l] + hl
        y1a, y1b = cur_r[r], cur_r[r] + hr
        cur_l[l] += hl
        cur_r[r] += hr
        alpha = 0.30 if r == SANKEY_DROP else (0.30 if l == SANKEY_NEW else 0.46)
        if l == SANKEY_NEW and r == SANKEY_DROP:
            continue
        x0, x1 = 0.104, 0.896
        xm = (x0 + x1) / 2
        verts = [(x0, y0a), (xm, y0a), (xm, y1a), (x1, y1a),
                 (x1, y1b), (xm, y1b), (xm, y0b), (x0, y0b), (x0, y0a)]
        codes = [MplPath.MOVETO, MplPath.CURVE4, MplPath.CURVE4, MplPath.CURVE4,
                 MplPath.LINETO, MplPath.CURVE4, MplPath.CURVE4, MplPath.CURVE4, MplPath.CLOSEPOLY]
        ax.add_patch(PathPatch(MplPath(verts, codes), facecolor=left_color[l],
                               edgecolor="none", alpha=alpha, zorder=2))

    n_new = sum(v for (l, r), v in flows.items() if l == SANKEY_NEW)
    n_drop = sum(v for (l, r), v in flows.items() if r == SANKEY_DROP)
    ax.set_title(f"进入 / 掉出 桑基图 · {prev_date} → {today} · 节点 {node} · 数据源 {source}\n"
                 f"新进入 {n_new} 个（左），掉出 {n_drop} 个（右），两日共同在榜 {int(total) - n_new - n_drop} 个；"
                 f"细带为同品牌内 ASIN 换位",
                 fontsize=10.6, loc="left", fontweight="bold", pad=10)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(out_png, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return out_png


# ---------------- ③ 价格 - 评论 四象限散点 ----------------
def render_price_review_quadrant(rows: list[dict], today: str, node: str, source: str,
                                 out_png: Path) -> Path | None:
    """价格（X）与评论数（Y）四象限散点：分割线取中位数，点色区分品牌/配件/白牌，点大小随名次。"""
    brand_of = build_brand_map(rows)
    pts = []
    for r in rows:
        p, rv = _to_float(r.get("price")), _to_float(r.get("reviews"))
        if p is None or rv is None or p <= 0 or rv <= 0:
            continue
        b = brand_of.get(r["asin"]) or {"name": "未知", "type": "white"}
        pts.append({"p": p, "rv": rv, "asin": r["asin"], "rank": r["rank"],
                    "name": b.get("name") or "未知", "type": b.get("type") or "white"})
    if len(pts) < 4:
        print(f"[四象限] 有效价格/评论点仅 {len(pts)} 个（<4），跳过出图", file=sys.stderr)
        return None

    plt = _plt()
    xs = [x["p"] for x in pts]
    ys = [x["rv"] for x in pts]
    med_x, med_y = _median(xs), _median(ys)
    use_log = (max(ys) / max(1.0, min(ys))) > 60

    fig, ax = plt.subplots(figsize=(11.2, 6.0), dpi=CHART_DPI)
    style = {"brand": ("#3f7fd0", "品牌"), "bparts": ("#e08b3c", "品牌配件"), "white": ("#98a2b3", "白牌/无品牌")}
    for t, (c, lb) in style.items():
        sel = [x for x in pts if x["type"] == t]
        if not sel:
            continue
        ax.scatter([x["p"] for x in sel], [x["rv"] for x in sel],
                   s=[26 + 78 * max(0.0, 1 - x["rank"] / 110.0) for x in sel],
                   c=c, alpha=0.72, edgecolors="white", linewidths=0.7, label=f"{lb}（{len(sel)}）", zorder=3)

    ax.axvline(med_x, color="#8a94a3", linestyle="--", linewidth=1.0, alpha=0.85, zorder=1)
    ax.axhline(med_y, color="#8a94a3", linestyle="--", linewidth=1.0, alpha=0.85, zorder=1)
    ax.text(0.012, 0.975, "高价 · 高评论\n（头部垄断区）", transform=ax.transAxes, fontsize=8.6,
            color="#4a5665", va="top", ha="left")
    ax.text(0.988, 0.975, "低价 · 高评论\n（性价比护城河）", transform=ax.transAxes, fontsize=8.6,
            color="#2f7d51", va="top", ha="right")
    ax.text(0.012, 0.028, "高价 · 低评论\n（新品溢价区）", transform=ax.transAxes, fontsize=8.6,
            color="#8a6532", va="bottom", ha="left")
    ax.text(0.988, 0.028, "低价 · 低评论\n（价格战/尾部）", transform=ax.transAxes, fontsize=8.6,
            color="#8b4a45", va="bottom", ha="right")

    # 标注：名次最靠前的 4 个 + 评论数最高的 2 个（控制数量，避免标签互相压盖）
    marks = sorted(pts, key=lambda x: (x["rv"] > med_y, x["p"] < med_x, x["rank"]))[:4]
    seen = {x["asin"] for x in marks}
    marks += [x for x in sorted(pts, key=lambda x: -x["rv"])[:2] if x["asin"] not in seen]
    for i, x in enumerate(marks[:6]):
        off = (7, 7) if i % 2 == 0 else (-7, -13)      # 交替偏移，减少相邻标签重叠
        ax.annotate(f"#{x['rank']} {x['name'][:10]}", xy=(x["p"], x["rv"]), xytext=off,
                    textcoords="offset points", fontsize=7.4, color="#3c4757",
                    ha="left" if i % 2 == 0 else "right",
                    bbox=dict(boxstyle="round,pad=0.18", fc="white", ec="#dbe2ec", lw=0.6, alpha=0.9),
                    arrowprops=dict(arrowstyle="-", color="#c9d2de", lw=0.5,
                                    shrinkA=0, shrinkB=2) if i % 2 else None,
                    zorder=5)

    if use_log:
        ax.set_yscale("log")
    ax.set_xlabel(f"售价（USD，中位数 ${med_x:,.0f}）", fontsize=9.8)
    ax.set_ylabel("评论数（条" + ("，对数轴）" if use_log else "）"), fontsize=9.8)
    ax.grid(linestyle=":", linewidth=0.8, alpha=0.35)
    ax.set_axisbelow(True)
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    ax.tick_params(labelsize=9)
    ax.legend(fontsize=8.6, frameon=False, loc="upper center", bbox_to_anchor=(0.5, -0.12), ncol=3)
    ax.set_title(f"价格 - 评论 四象限散点 · {today} · 节点 {node} · 数据源 {source}"
                 f"（{len(pts)} 个有效点，分割线=中位数：${med_x:,.0f} / {med_y:,.0f} 条）",
                 fontsize=10.8, loc="left", fontweight="bold", pad=8)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(out_png, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return out_png


# ---------------------------------------------------------------- 网页看板层
# 单文件自包含看板：把 3-输出 下真实 CSV / PNG 汇总成一页 index.html，
# 图片以 base64 内嵌（内嵌前剥离 PNG 文本元数据块），不引用任何外部资源。
_PNG_TEXT_CHUNKS = {b"tEXt", b"zTXt", b"iTXt"}
_PNG_SIG = b"\x89PNG\r\n\x1a\n"
# 增强图表（份额趋势除外，只嵌一张）内嵌总预算：超出后不再内嵌，仅在页面给出磁盘路径，
# 避免多日多图把单文件看板撑到几十 MB、手机端打不开。
CHART_EMBED_BUDGET = 7 * 1024 * 1024


def strip_png_text(data: bytes) -> bytes:
    """剥离 PNG 的 tEXt / zTXt / iTXt 文本元数据块，其余数据块（含像素数据）原样保留。"""
    if not data.startswith(_PNG_SIG):
        return data
    out, pos, n = bytearray(_PNG_SIG), len(_PNG_SIG), len(data)
    while pos + 12 <= n:
        length = int.from_bytes(data[pos:pos + 4], "big")
        ctype = data[pos + 4:pos + 8]
        end = pos + 12 + length          # 4 长度 + 4 类型 + 数据 + 4 CRC
        if end > n:
            break
        if ctype not in _PNG_TEXT_CHUNKS:
            out += data[pos:end]
        pos = end
    return bytes(out)


def read_csv_dicts(path: Path) -> list[dict]:
    """读取 CSV 为字典列表（容错：文件不存在返回空列表）。"""
    if not path.exists():
        return []
    try:
        with open(path, "r", newline="", encoding="utf-8-sig") as f:
            return [dict(r) for r in csv.DictReader(f)]
    except Exception as e:
        print(f"[warn] 读取 CSV 失败 {path}: {type(e).__name__}: {e}", file=sys.stderr)
        return []


def png_data_url(path: Path, stat: dict, budget: int | None = None) -> str:
    """把 PNG 转成 data URL（先剥离文本元数据块）。stat 用于累计字节统计。

    budget 不为空时，累计内嵌字节超过该预算即返回空串（页面改为展示磁盘路径），
    保证 index.html 体积可控、移动端仍能打开。
    """
    if not path.exists():
        return ""
    try:
        raw = path.read_bytes()
    except Exception as e:
        print(f"[warn] 读取 PNG 失败 {path}: {type(e).__name__}: {e}", file=sys.stderr)
        return ""
    clean = strip_png_text(raw)
    if budget is not None and stat.get("chart_bytes", 0) + len(clean) > budget:
        stat["png_skip"] = stat.get("png_skip", 0) + 1
        return ""
    stat["png_raw"] = stat.get("png_raw", 0) + len(raw)
    stat["png_clean"] = stat.get("png_clean", 0) + len(clean)
    stat["png_n"] = stat.get("png_n", 0) + 1
    if budget is not None:
        stat["chart_bytes"] = stat.get("chart_bytes", 0) + len(clean)
    return "data:image/png;base64," + base64.b64encode(clean).decode("ascii")


def collect_dashboard_days(outdir: Path, node: str, source: str, max_days: int) -> tuple[list[dict], dict, list[str]]:
    """扫描 3-输出 目录，按「快照日 + 数据源」收集看板所需数据。

    返回 (days, png_stat, older_dates)；days 按日期倒序、同日当前数据源优先。
    一律读取磁盘上已生成的真实文件，不做任何推算或补数。
    """
    stat: dict = {}
    detail_root = outdir / DETAIL_SUB
    new_dir = outdir / NEW_SUB
    chart_dir = outdir / CHART_SUB
    entries: list[dict] = []
    if not detail_root.exists():
        return [], stat, []

    for date_dir in [p for p in detail_root.iterdir() if p.is_dir()]:
        tag = date_dir.name.replace("-", "")
        pattern = f"bsr_snapshot_{node}_{tag}_*.csv"
        for csv_path in date_dir.glob(pattern):
            src = csv_path.stem.rsplit("_", 1)[-1]
            new_path = new_dir / f"bsr_new_entrants_{node}_{tag}_{src}.csv"
            sum_path = new_dir / f"bsr_brand_summary_{node}_{tag}_{src}.csv"
            png_path = chart_dir / f"bsr_new_entrants_{node}_{tag}_{src}.png"
            sankey_path = chart_dir / f"bsr_sankey_{node}_{tag}_{src}.png"
            quad_path = chart_dir / f"bsr_quadrant_{node}_{tag}_{src}.png"
            detail = read_csv_dicts(csv_path)
            if not detail:
                continue
            entries.append({
                "date": date_dir.name,
                "source": src,
                "detail": detail,
                "new": read_csv_dicts(new_path),
                "summary": read_csv_dicts(sum_path),
                "image": png_data_url(png_path, stat),
                "sankey": png_data_url(sankey_path, stat, CHART_EMBED_BUDGET),
                "quadrant": png_data_url(quad_path, stat, CHART_EMBED_BUDGET),
                "detail_file": str(csv_path),
                "new_file": str(new_path) if new_path.exists() else "",
                "sum_file": str(sum_path) if sum_path.exists() else "",
                "image_file": str(png_path) if png_path.exists() else "",
                "sankey_file": str(sankey_path) if sankey_path.exists() else "",
                "quadrant_file": str(quad_path) if quad_path.exists() else "",
            })

    # 日期倒序；同日把本次运行的数据源排到最前
    entries.sort(key=lambda e: (e["date"], 1 if e["source"] == source else 0), reverse=True)
    # max_days<=0 表示内嵌全部已有快照日（历史全量回溯）
    keep = int(max_days)
    if keep <= 0:
        keep = len(entries)
    keep = max(1, keep)
    older = sorted({e["date"] for e in entries[keep:]}, reverse=True)
    return entries[:keep], stat, older


def latest_trend_png(outdir: Path, node: str, source: str) -> Path | None:
    """取 3-输出/可视化 下最新的品牌份额趋势图（文件名带最新快照日 tag）。"""
    chart_dir = outdir / CHART_SUB
    if not chart_dir.exists():
        return None
    cands = sorted(chart_dir.glob(f"bsr_brand_share_{node}_*_{source}.png"))
    return cands[-1] if cands else None


def render_dashboard(outdir: Path, node: str, source: str, max_days: int = 0) -> Path | None:
    """生成单页自包含看板 3-输出/看板/index.html。"""
    days, stat, older = collect_dashboard_days(outdir, node, source, max_days)
    if not days:
        return None
    trend_path = latest_trend_png(outdir, node, source)
    trend_url = png_data_url(trend_path, stat) if trend_path else ""
    payload = {
        "meta": {
            "node": node,
            "source": source,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "max_days": max_days,
            "shown": len(days),
            "older": older[:12],
            "trend": {"image": trend_url, "file": str(trend_path) if trend_path else ""},
        },
        "days": days,
    }
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = _DASHBOARD_TPL.safe_substitute(
        NODE=node,
        SOURCE=source,
        GENERATED=payload["meta"]["generated_at"],
        LATEST=days[0]["date"],
        EARLIEST=days[-1]["date"],
        MAXDAYS=("全部" if int(max_days) <= 0 else str(max_days)),
        SHOWN=str(len(days)),
        DATA=blob,
    )
    out = outdir / BOARD_SUB / "index.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    if stat.get("png_n"):
        print(f"[看板] PNG 元数据剥离：{stat['png_n']} 张，"
              f"{stat['png_raw'] / 1024:.0f} KB -> {stat['png_clean'] / 1024:.0f} KB")
    return out


_DASHBOARD_TPL = Template(r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light">
<title>BSR 每日监控看板 · 节点 $NODE</title>
<style>
:root{--bg:#f4f6fa;--card:#fff;--ink:#1e2733;--muted:#6b7684;--line:#e2e8f0;
--blue:#3f7fd0;--orange:#e08b3c;--gray:#8a9099;--red:#c0564f;--head:#1d2b3a;
--wrap:1240px;--pad-x:clamp(16px,2.2vw,32px)}
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{background:var(--bg);color:var(--ink);
font:14px/1.55 -apple-system,"Segoe UI","Microsoft YaHei","PingFang SC","Hiragino Sans GB","Heiti SC",sans-serif;
-webkit-text-size-adjust:100%}
.topbar{position:sticky;top:0;z-index:30;background:var(--head);color:#fff;
padding:12px max(var(--pad-x),calc((100vw - var(--wrap)) / 2 + var(--pad-x)));
display:flex;flex-wrap:wrap;gap:10px 18px;align-items:center;
justify-content:space-between;box-shadow:0 2px 10px rgba(0,0,0,.16)}
.topbar .ttl{font-size:17px;font-weight:700;letter-spacing:.3px}
.topbar .sub{font-size:12px;color:#b9c6d6;margin-top:2px}
.ctrl{display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-size:12px;color:#cdd8e5}
.ctrl select{font:13px inherit;padding:5px 9px;border-radius:6px;border:1px solid #3c4f66;
background:#26374a;color:#fff;min-width:190px}
.ctrl .gen{color:#93a5b9}
.ctrl input{font:12px inherit;padding:5px 9px;border-radius:6px;border:1px solid #3c4f66;
background:#26374a;color:#fff;width:190px}
.ctrl input::placeholder{color:#8fa2b8}
main{max-width:var(--wrap);margin:0 auto;padding:18px var(--pad-x) 10px}
.kpis{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:12px;margin-bottom:14px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:11px 13px;
box-shadow:0 1px 2px rgba(16,24,40,.04)}
.k-lb{font-size:12px;color:var(--muted)}
.k-v{font-size:21px;font-weight:700;margin:3px 0 1px;letter-spacing:.2px}
.k-sub{font-size:11px;color:#94a0ae}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px 16px;
margin-bottom:14px;box-shadow:0 1px 2px rgba(16,24,40,.04)}
.card-hd{display:flex;flex-wrap:wrap;gap:8px 14px;align-items:baseline;justify-content:space-between;
margin-bottom:10px;border-bottom:1px solid var(--line);padding-bottom:9px}
.card-hd h2{font-size:15px;margin:0;letter-spacing:.2px}
.hint{font-size:12px;color:var(--muted)}
.hint input{font:12px inherit;padding:4px 8px;border:1px solid #cfd8e3;border-radius:6px;width:170px}
.tbl-wrap{overflow:auto;max-height:560px;border:1px solid var(--line);border-radius:8px}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{padding:7px 9px;text-align:left;border-bottom:1px solid #eef1f6;vertical-align:top}
th{position:sticky;top:0;background:#eef2f8;font-weight:600;font-size:12px;color:#42505f;z-index:2;
white-space:nowrap}
tbody tr:hover{background:#f8fbff}
a.asl{color:var(--blue);text-decoration:none;border-bottom:1px dotted rgba(63,127,208,.55);
word-break:break-all}
a.asl:hover{color:#2b5f9e;border-bottom-style:solid}
.sep{color:var(--muted)}
td.num,th.num{text-align:right;white-space:nowrap}
td.mono,.mono{font-family:Consolas,"Courier New",monospace;font-size:12.5px}
td.tt{min-width:320px;max-width:640px}
.small{font-size:11.5px;color:var(--muted);word-break:break-all}
.bdg{display:inline-block;font-size:11px;padding:1px 7px;border-radius:10px;color:#fff;
background:var(--blue);white-space:nowrap}
.bdg.brand{background:var(--blue)}
.bdg.white{background:var(--gray)}
.bdg.parts,.bdg.bparts{background:var(--orange)}
.rk{display:inline-block;min-width:34px;text-align:center;font-family:Consolas,monospace;
font-size:12px;padding:1px 5px;border-radius:5px;background:#eef2f8;color:#42505f}
.rk.mid{background:#fdf1e3;color:#a8641c}
.rk.top{background:#e6f0fb;color:#2467ad;font-weight:700}
.tag{font-size:11.5px;color:var(--muted);background:#f2f5f9;border:1px solid var(--line);
border-radius:10px;padding:1px 7px}
.grp{border:1px solid var(--line);border-radius:8px;margin-bottom:10px;overflow:hidden;background:#fff}
.grp-hd{display:flex;flex-wrap:wrap;gap:6px 8px;align-items:center;background:#f7f9fc;
padding:9px 12px;font-size:13px;line-height:1.5;cursor:pointer}
.grp-hd b{font-size:13.5px;font-weight:700;line-height:1.45;min-width:0;flex:1 1 auto;
overflow-wrap:anywhere;word-break:break-word}
.grp-hd .bdg,.grp-hd .tag{flex:0 0 auto}
details.grp>summary{list-style:none;outline:none}
details.grp>summary::-webkit-details-marker{display:none}
details.grp>summary::before{content:"▸";color:#7b8896;font-size:11px;margin-right:2px;flex:0 0 auto}
details.grp[open]>summary::before{content:"▾"}
.bar-tools{display:flex;gap:8px;margin-bottom:10px}
.mini{font:12px inherit;padding:4px 10px;border:1px solid #cfd8e3;background:#fff;border-radius:6px;
color:#42505f;cursor:pointer}
.mini:hover{background:#f2f6fb;border-color:#a9bccf}
.tabs{display:flex;gap:8px;margin-bottom:10px;flex-wrap:wrap}
.tab{font:13px inherit;padding:6px 14px;border:1px solid #cfd8e3;background:#fff;border-radius:16px;
color:#42505f;cursor:pointer}
.tab.on{background:var(--blue);border-color:var(--blue);color:#fff;font-weight:600}
img.chart{display:block;width:100%;height:auto;border:1px solid var(--line);border-radius:8px;
background:#fff}
.empty{color:var(--muted);font-size:13px;padding:12px 2px;margin:0}
.foot{font-size:11.5px;color:var(--muted);margin:8px 0 0;word-break:break-all}
.click{cursor:pointer}
tr.click.on{background:#e8f1fc}
tr.click.on td{border-bottom-color:#d5e5f8}
.page-foot{max-width:var(--wrap);margin:0 auto;padding:4px var(--pad-x) 26px;font-size:11.5px;
color:#8794a3;line-height:1.7}
@media(max-width:1080px){.kpis{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media(max-width:720px){
main{padding:12px 12px 6px}
.topbar{padding:10px 12px}
.topbar .ttl{font-size:15px}
.ctrl select{min-width:150px}
.kpis{grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}
.k-v{font-size:18px}
.card{padding:11px 11px 13px}
td.tt{min-width:220px;max-width:300px;font-size:12.5px}
th,td{padding:6px 7px}
.hint input{width:120px}
.page-foot{padding:4px 12px 20px}
.grp-hd{padding:9px 10px;gap:5px 6px;font-size:12px;line-height:1.55}
.grp-hd b{font-size:13px;line-height:1.5;flex:1 1 auto}
.grp-hd .tag{font-size:11px;padding:1px 6px;line-height:1.45}
.grp-hd .bdg{font-size:10.5px;padding:1px 6px}
}
@media(max-width:430px){
.grp-hd{gap:5px;padding:9px 9px}
.grp-hd .tag{font-size:10.5px;padding:1px 5px}
}
</style>
</head>
<body>
<header class="topbar">
  <div>
    <div class="ttl">Amazon BSR 每日监控看板</div>
    <div class="sub">Running Boards（US）· 节点 $NODE · 数据源 $SOURCE · 当前快照日 <span id="hdDate">$LATEST</span></div>
  </div>
  <div class="ctrl">
    <label for="dateSel">历史日期</label>
    <select id="dateSel" aria-label="历史日期切换"></select>
    <label for="q">过滤</label>
    <input id="q" type="search" placeholder="ASIN / 品牌 / 标题" aria-label="关键词过滤">
    <span class="gen">页面生成 $GENERATED</span>
  </div>
</header>
<main>
  <section class="kpis" id="kpis"></section>

  <section class="card">
    <div class="card-hd"><h2>① 新增名单（按品牌分组）</h2><span class="hint" id="newHint"></span></div>
    <div id="newBody"></div>
  </section>

  <section class="card">
    <div class="card-hd"><h2>② 品牌维度汇总（新增口径）</h2><span class="hint">来自 bsr_brand_summary CSV</span></div>
    <div class="tbl-wrap"><table id="sumTbl"></table></div>
  </section>

  <section class="card">
    <div class="card-hd"><h2>③ 可视化图（新增进入名单）</h2><span class="hint">PNG 剥离 tEXt 后 base64 内嵌</span></div>
    <div id="imgBody"></div>
  </section>

  <section class="card">
    <div class="card-hd"><h2>④ 品牌份额时间序列</h2><span class="hint" id="trendHint">跨快照日汇总</span></div>
    <div id="trendBody"></div>
  </section>

  <section class="card">
    <div class="card-hd"><h2>⑤ 进入 / 掉出 桑基图</h2><span class="hint" id="sankeyHint"></span></div>
    <div id="sankeyBody"></div>
  </section>

  <section class="card">
    <div class="card-hd"><h2>⑥ 价格 - 评论 四象限</h2><span class="hint" id="quadHint"></span></div>
    <div id="quadBody"></div>
  </section>

  <section class="card">
    <div class="card-hd">
      <h2>⑦ 当日明细（品牌分组 / 逐行表）</h2>
      <span class="hint" id="detHint"></span>
    </div>
    <div class="tabs">
      <button class="tab on" data-view="brand" type="button">品牌分组视图</button>
      <button class="tab" data-view="table" type="button">逐行明细表</button>
    </div>
    <div id="detailBody"></div>
  </section>

  <section class="card">
    <div class="card-hd"><h2>⑧ 历史日期切换</h2><span class="hint">本页内嵌快照日：$MAXDAYS（点击任一行即切换日期）</span></div>
    <div class="tbl-wrap"><table id="histTbl"></table></div>
    <p class="foot" id="histFoot"></p>
  </section>
</main>
<footer class="page-foot">
  页面数据全部读取自项目 3-输出 目录内真实 CSV / PNG，不含任何硬编码样例；单文件自包含，不引用外部 CDN / 字体 / JS / CSS，断网双击即可打开。<br>
  内嵌范围 $EARLIEST ~ $LATEST · 共 $SHOWN 个快照日 · 更早日期的明细与图片仍在 3-输出\明细、3-输出\新增名单、3-输出\可视化 内。
</footer>
<script id="bsr-data" type="application/json">$DATA</script>
<script>
(function () {
  var D = JSON.parse(document.getElementById('bsr-data').textContent);
  var DAYS = D.days || [];
  var CUR = 0, VIEW = 'brand', Q = '';
  var TC = { '品牌': 'brand', '白牌': 'white', '配件': 'parts', '品牌配件': 'bparts' };

  function esc(s) {
    return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
  function num(v) {
    if (v == null) { return null; }
    var t = String(v).replace(/[^0-9.\-]/g, '');
    if (!t) { return null; }
    var f = parseFloat(t);
    return isNaN(f) ? null : f;
  }
  function money(v) { var f = num(v); return f == null ? '—' : '$' + f.toFixed(2); }
  function mean(a) {
    if (!a.length) { return null; }
    var s = 0, i;
    for (i = 0; i < a.length; i++) { s += a[i]; }
    return s / a.length;
  }
  function plist(rows) {
    var out = [];
    (rows || []).forEach(function (r) { var f = num(r.price); if (f != null) { out.push(f); } });
    return out;
  }
  var DP = 'https://www.amazon.com/dp/';
  function asinA(a) {
    var s = String(a || '').trim();
    if (!s) { return '—'; }
    var u = (s.indexOf('http') === 0) ? s : (DP + encodeURIComponent(s));
    return '<a class="asl mono" href="' + u + '" target="_blank" rel="noopener noreferrer">' + esc(s) + '</a>';
  }
  function asinList(v) {
    var arr = String(v || '').split(/[,;\s]+/).filter(function (x) { return x; });
    if (!arr.length) { return '—'; }
    return arr.map(asinA).join('<span class="sep">, </span>');
  }
  function bdg(t) { return '<span class="bdg ' + (TC[t] || 'brand') + '">' + esc(t || '未知') + '</span>'; }
  function bdgOpt(t) { return t ? bdg(t) : ''; }
  function rk(r) {
    var n = parseInt(r, 10) || 0;
    var c = (n > 0 && n <= 10) ? ' top' : (n <= 30 ? ' mid' : '');
    return '<span class="rk' + c + '">#' + esc(r) + '</span>';
  }
  function hit(r) {
    if (!Q) { return true; }
    var s = ((r.asin || '') + ' ' + (r.brand || '') + ' ' + (r.title || '')).toLowerCase();
    return s.indexOf(Q) >= 0;
  }
  function groups(rows, rankKey) {
    var map = {}, order = [];
    rows.forEach(function (r) {
      var k = r.brand || '未知';
      if (!map[k]) { map[k] = { name: k, type: r.brand_type || '', rows: [] }; order.push(k); }
      map[k].rows.push(r);
    });
    var gs = order.map(function (k) { return map[k]; });
    gs.forEach(function (g) {
      g.rows.sort(function (a, b) {
        return (parseInt(a[rankKey], 10) || 999) - (parseInt(b[rankKey], 10) || 999);
      });
      g.count = g.rows.length;
      g.best = parseInt(g.rows[0][rankKey], 10) || 999;
      g.avg = mean(plist(g.rows));
    });
    gs.sort(function (a, b) { return a.best - b.best; });
    return gs;
  }

  function renderKpis(day) {
    var det = day.detail || [], nw = day.new || [];
    var ps = plist(det), bset = {}, nset = {};
    det.forEach(function (r) { bset[r.brand || '未知'] = 1; });
    nw.forEach(function (r) { nset[r.brand || '未知'] = 1; });
    var items = [
      ['榜单条数', det.length, '明细 CSV 实际行数'],
      ['品牌组数', Object.keys(bset).length, '按 brand 列归组'],
      ['新增进入', nw.length, '相对上一快照日'],
      ['新增涉及品牌', Object.keys(nset).length, '含白牌 / 配件组'],
      ['榜单均价', ps.length ? '$' + mean(ps).toFixed(2) : '—', '有效价格 ' + ps.length + ' / ' + det.length],
      ['价格区间', ps.length ? ('$' + Math.min.apply(null, ps).toFixed(0) + ' ~ $' + Math.max.apply(null, ps).toFixed(0)) : '—', '最低 ~ 最高']
    ];
    var h = '';
    items.forEach(function (it) {
      h += '<div class="kpi"><div class="k-lb">' + esc(it[0]) + '</div><div class="k-v">' +
        esc(String(it[1])) + '</div><div class="k-sub">' + esc(it[2]) + '</div></div>';
    });
    document.getElementById('kpis').innerHTML = h;
  }

  function rowTable(rows, rankKey) {
    var h = '<div class="tbl-wrap"><table><thead><tr><th>名次</th><th>ASIN</th><th>标题</th>' +
      '<th class="num">价格</th><th class="num">评分</th><th class="num">评分数</th></tr></thead><tbody>';
    rows.forEach(function (r) {
      h += '<tr><td>' + rk(r[rankKey]) + '</td><td>' + asinA(r.link || r.asin) + '</td>' +
        '<td class="tt" title="' + esc(r.title) + '">' + esc(r.title) + '</td>' +
        '<td class="num">' + money(r.price) + '</td>' +
        '<td class="num">' + esc(r.rating || '—') + '</td>' +
        '<td class="num">' + esc(r.reviews || '—') + '</td></tr>';
    });
    return h + '</tbody></table></div>';
  }

  function renderNew(day) {
    var all = day.new || [];
    var wrap = document.getElementById('newBody');
    document.getElementById('newHint').textContent = all.length ? ('共 ' + all.length + ' 条 · 组内按今日名次') : '';
    if (!all.length) {
      wrap.innerHTML = '<p class="empty">该快照日没有新增进入记录（首次建立基线快照时不产出新增名单 CSV，属正常）。</p>';
      return;
    }
    var rows = all.filter(hit);
    if (!rows.length) {
      wrap.innerHTML = '<p class="empty">没有匹配「' + esc(Q) + '」的新增条目。</p>';
      return;
    }
    var h = '';
    groups(rows, 'rank_today').forEach(function (g) {
      h += '<div class="grp"><div class="grp-hd">' + bdg(g.type) + '<b>' + esc(g.name) + '</b>' +
        '<span class="tag">' + g.count + ' 条</span><span class="tag">最佳 ' + rk(g.best) + '</span>' +
        '<span class="tag">均价 ' + (g.avg == null ? '—' : '$' + g.avg.toFixed(2)) + '</span></div>' +
        rowTable(g.rows, 'rank_today') + '</div>';
    });
    wrap.innerHTML = h;
  }

  function renderSum(day) {
    var head = '<thead><tr><th>品牌</th><th>类型</th><th class="num">新增条数</th><th class="num">最佳名次</th>' +
      '<th class="num">均价</th><th class="num">价格中位</th><th class="num">平均评分数</th><th>ASIN 明细</th></tr></thead>';
    var rows = (day.summary || []).filter(hit);
    var tbl = document.getElementById('sumTbl');
    if (!rows.length) {
      tbl.innerHTML = head + '<tbody><tr><td colspan="8" class="empty">该快照日无品牌汇总数据。</td></tr></tbody>';
      return;
    }
    var b = '';
    rows.forEach(function (r) {
      b += '<tr><td><b>' + esc(r.brand) + '</b></td><td>' + bdg(r.brand_type) + '</td>' +
        '<td class="num">' + esc(r.new_count) + '</td><td class="num">' + rk(r.best_rank) + '</td>' +
        '<td class="num">' + money(r.avg_price) + '</td><td class="num">' + money(r.median_price) + '</td>' +
        '<td class="num">' + esc(r.avg_reviews || '—') + '</td>' +
        '<td class="small">' + asinList(r.asin_list) + '</td></tr>';
    });
    tbl.innerHTML = head + '<tbody>' + b + '</tbody>';
  }

  function renderImg(day) {
    var wrap = document.getElementById('imgBody');
    if (day.image) {
      wrap.innerHTML = '<img class="chart" alt="新增进入名单可视化" src="' + day.image + '">' +
        '<p class="foot">原始文件：<span class="mono">' + esc(day.image_file) +
        '</span>（内嵌前已剥离 PNG 的 tEXt / zTXt / iTXt 文本块，像素数据未改动）</p>';
    } else {
      wrap.innerHTML = '<p class="empty">该快照日没有可视化 PNG（无新增进入时不出图，或图文件缺失）。</p>';
    }
  }

  function chartBlock(el, url, file, alt, emptyMsg) {
    if (url) {
      el.innerHTML = '<img class="chart" alt="' + esc(alt) + '" src="' + url + '">' +
        '<p class="foot">原始文件：<span class="mono">' + esc(file || '（未落盘）') +
        '</span>（内嵌前已剥离 PNG 文本块）</p>';
      return;
    }
    if (file) {
      el.innerHTML = '<p class="empty">' + esc(emptyMsg) + '（图片未内嵌：单文件体积预算已用满）</p>' +
        '<p class="foot">原始文件：<span class="mono">' + esc(file) + '</span></p>';
      return;
    }
    el.innerHTML = '<p class="empty">' + esc(emptyMsg) + '</p>';
  }

  function renderTrend() {
    var m = (D.meta && D.meta.trend) ? D.meta.trend : {};
    chartBlock(document.getElementById('trendBody'), m.image, m.file, '品牌份额时间序列',
      '尚无品牌份额趋势图（至少需 1 个快照日，运行一次后自动出图）。');
    document.getElementById('trendHint').textContent = (m.image || m.file)
      ? ('跨 ' + ((D.meta && D.meta.shown) || 0) + '+ 个快照日汇总 · 不随顶部日期切换')
      : '暂无数据';
  }

  function renderSankey(day) {
    var has = day.sankey || day.sankey_file;
    chartBlock(document.getElementById('sankeyBody'), day.sankey, day.sankey_file, '进入掉出桑基图',
      '该快照日没有桑基图（首个快照日无对比基准，或该日图文件缺失）。');
    document.getElementById('sankeyHint').textContent = has
      ? ('上一快照日 → ' + day.date + ' · 左列昨日品牌 / 右列今日品牌')
      : '无对比基准，无法生成';
  }

  function renderQuad(day) {
    var has = day.quadrant || day.quadrant_file;
    chartBlock(document.getElementById('quadBody'), day.quadrant, day.quadrant_file, '价格评论四象限',
      '该快照日没有四象限图（有效价格 / 评论点不足 4 个，或图文件缺失）。');
    document.getElementById('quadHint').textContent = has
      ? (day.date + ' · 分割线取当日中位数 · 点大小随名次')
      : '有效数据点不足';
  }

  function openAll(v) {
    var els = document.getElementById('detailBody').querySelectorAll('details');
    for (var i = 0; i < els.length; i++) { els[i].open = v; }
  }

  function renderDetail(day) {
    var body = document.getElementById('detailBody');
    var all = day.detail || [];
    var rows = all.filter(hit);
    document.getElementById('detHint').textContent = Q
      ? ('过滤后 ' + rows.length + ' / ' + all.length + ' 条')
      : ('共 ' + all.length + ' 条 · 指标卡为全量口径');
    if (!rows.length) {
      body.innerHTML = '<p class="empty">没有匹配的明细行。</p>';
      return;
    }
    if (VIEW === 'table') {
      body.innerHTML = rowTable(rows, 'rank');
      return;
    }
    var h = '<div class="bar-tools"><button class="mini" id="expAll" type="button">全部展开</button>' +
      '<button class="mini" id="colAll" type="button">全部收起</button></div>';
    groups(rows, 'rank').forEach(function (g) {
      h += '<details class="grp" open><summary class="grp-hd">' + bdgOpt(g.type) + '<b>' + esc(g.name) +
        '</b><span class="tag">' + g.count + ' 条</span><span class="tag">最佳 ' + rk(g.best) + '</span>' +
        '<span class="tag">均价 ' + (g.avg == null ? '—' : '$' + g.avg.toFixed(2)) + '</span></summary>' +
        rowTable(g.rows, 'rank') + '</details>';
    });
    body.innerHTML = h;
    document.getElementById('expAll').onclick = function () { openAll(true); };
    document.getElementById('colAll').onclick = function () { openAll(false); };
  }

  function renderHist() {
    var head = '<thead><tr><th>日期</th><th>数据源</th><th class="num">榜单条数</th>' +
      '<th class="num">新进入</th><th class="num">品牌组</th><th class="num">图</th></tr></thead>';
    var b = '';
    DAYS.forEach(function (d, i) {
      var bs = {}, cn = 0;
      (d.detail || []).forEach(function (r) {
        var k = r.brand || '未知';
        if (!bs[k]) { bs[k] = 1; cn++; }
      });
      b += '<tr class="click' + (i === CUR ? ' on' : '') + '" data-i="' + i + '">' +
        '<td><b>' + esc(d.date) + '</b>' + (i === 0 ? ' <span class="tag">最新</span>' : '') + '</td>' +
        '<td>' + esc(d.source) + '</td><td class="num">' + (d.detail || []).length + '</td>' +
        '<td class="num">' + (d.new || []).length + '</td><td class="num">' + cn + '</td>' +
        '<td class="num">' + (d.image ? '有' : '—') + '</td></tr>';
    });
    var tbl = document.getElementById('histTbl');
    tbl.innerHTML = head + '<tbody>' + b + '</tbody>';
    var trs = tbl.querySelectorAll('tr.click');
    for (var j = 0; j < trs.length; j++) {
      trs[j].onclick = function () {
        CUR = parseInt(this.getAttribute('data-i'), 10) || 0;
        document.getElementById('dateSel').value = String(CUR);
        render();
        window.scrollTo(0, 0);
      };
    }
    var older = (D.meta && D.meta.older) ? D.meta.older : [];
    document.getElementById('histFoot').innerHTML = older.length
      ? ('另有更早快照日未内嵌：' + esc(older.join('、')) + '（明细与图片仍保留在 3-输出 对应目录内）')
      : '已内嵌当前可用的全部快照日。';
  }

  function render() {
    if (!DAYS.length) {
      document.getElementById('kpis').innerHTML = '<p class="empty">未找到任何快照数据。</p>';
      return;
    }
    document.getElementById('hdDate').textContent = DAYS[CUR].date;
    renderKpis(DAYS[CUR]);
    renderNew(DAYS[CUR]);
    renderSum(DAYS[CUR]);
    renderImg(DAYS[CUR]);
    renderTrend();
    renderSankey(DAYS[CUR]);
    renderQuad(DAYS[CUR]);
    renderDetail(DAYS[CUR]);
    renderHist();
  }

  var sel = document.getElementById('dateSel');
  DAYS.forEach(function (d, i) {
    var o = document.createElement('option');
    o.value = String(i);
    o.textContent = d.date + ' · ' + d.source + (i === 0 ? '（最新）' : '');
    sel.appendChild(o);
  });
  sel.onchange = function () {
    CUR = parseInt(this.value, 10) || 0;
    render();
  };
  var tabs = document.querySelectorAll('.tab');
  for (var t = 0; t < tabs.length; t++) {
    tabs[t].onclick = function () {
      VIEW = this.getAttribute('data-view');
      for (var k = 0; k < tabs.length; k++) {
        tabs[k].className = 'tab' + (tabs[k] === this ? ' on' : '');
      }
      if (DAYS.length) { renderDetail(DAYS[CUR]); }
    };
  }
  document.getElementById('q').oninput = function () {
    Q = this.value.trim().toLowerCase();
    if (DAYS.length) {
      renderKpis(DAYS[CUR]);
      renderNew(DAYS[CUR]);
      renderSum(DAYS[CUR]);
      renderDetail(DAYS[CUR]);
    }
  };
  render();
})();
</script>
</body>
</html>
""")


# ---------------------------------------------------------------- 主流程
def main():
    cfg = load_config()
    ap = argparse.ArgumentParser(description="Amazon BSR 每日监控")
    ap.add_argument("--source", choices=["browser", "html"], default="browser",
                    help="browser=亚马逊前台渲染(默认，锁定邮编 10001)；html=亚马逊前台轻量备源")
    ap.add_argument("--node", default=cfg.get("node", "15736621"), help="browse node id")
    ap.add_argument("--domain", default=cfg.get("marketplace", "US"))
    ap.add_argument("--top", type=int, default=int(cfg.get("top", 100)))
    ap.add_argument("--pages", type=int, default=2, help="browser/html 模式翻页数")
    ap.add_argument("--zipcode", default=str(cfg.get("delivery_zip", DEFAULT_ZIP)),
                    help=f"美国站配送邮编（默认 {DEFAULT_ZIP}，仅 browser 模式生效）")
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--outdir", default=str(OUT_DIR))
    ap.add_argument("--date", default=date.today().strftime("%Y-%m-%d"))
    ap.add_argument("--html-days", type=int, default=0,
                    help="网页看板内嵌的历史快照日数量（含当日；0 或负数=内嵌全部已有日期，默认 0）")
    ap.add_argument("--no-html", action="store_true",
                    help="只出 CSV/PNG，不生成网页看板 index.html")
    ap.add_argument("--rebuild-only", action="store_true",
                    help="只读 1-数据源\\bsr_history.db 重建图表与看板，不发起任何采集（离线验证用）")
    args = ap.parse_args()

    outdir = Path(args.outdir)
    t0 = time.time()
    conn = db_open(Path(args.db))

    if args.rebuild_only:
        # 离线重建：完全不发起网络请求，只用库内既有快照重算图表与看板
        dates = db_snapshot_dates(conn, args.node, args.source)
        if not dates:
            print(f"[重建] 库内没有 {args.node} / {args.source} 的快照记录，无法重建。", file=sys.stderr)
            conn.close()
            return 1
        use_date = args.date if args.date in dates else dates[-1]
        rows = db_day_rows(conn, args.node, args.source, use_date)
        if not rows:
            print(f"[重建] 快照日 {use_date} 无明细记录，无法重建。", file=sys.stderr)
            conn.close()
            return 1
        args.date = use_date
        cost = 0.0
        print(f"[重建] 仅读库重建，未发起任何采集：快照日 {use_date}，{len(rows)} 条，"
              f"库内共 {len(dates)} 个快照日")
    else:
        if args.source == "browser":
            rows = fetch_browser(args.node, args.top, args.pages, zipcode=args.zipcode)
        else:
            rows = fetch_html(args.node, args.top, args.pages)
        cost = time.time() - t0

        if not rows:
            print("未采集到数据，终止。", file=sys.stderr)
            conn.close()
            return 1

        if args.source == "browser":
            print(f"[邮编] 目标 {args.zipcode} · 回读「{ZIP_STATE.get('actual') or '未取到' }」· "
                  f"{'已锁定' if ZIP_STATE.get('locked') else '未确认锁定'} · {ZIP_STATE.get('method')}")
        elif args.source == "html":
            print("[邮编] 轻量备源（requests）无法携带前台配送邮编，价格按亚马逊默认位置口径；"
                  "需要 10001 口径请使用 --source browser")
        save_snapshot(conn, args.node, args.source, args.date, rows)

    res = diff(conn, args.node, args.source, args.date)

    tag = args.date.replace("-", "")
    detail_dir = outdir / DETAIL_SUB / args.date
    new_dir = outdir / NEW_SUB
    chart_dir = outdir / CHART_SUB

    brand_of = build_brand_map(rows)

    def _cell(r, c):
        if c == "brand":
            return brand_of.get(r["asin"], {}).get("name", "")
        if c == "brand_type":
            return _TYPE_LABEL.get(brand_of.get(r["asin"], {}).get("type", ""), "")
        if c == "link":
            asin = (r.get("asin") or "").strip()
            return (ASIN_URL_BASE + asin) if asin else ""
        return r.get(c, "")

    if args.rebuild_only:
        pass          # 重建模式不改写任何已有 CSV / PNG，只重出图表与看板
    else:
        snap_csv = detail_dir / f"bsr_snapshot_{args.node}_{tag}_{args.source}.csv"
        cols = ["rank", "asin", "link", "brand", "title", "price"]
        if any(r.get("rating") for r in rows):
            cols += ["rating", "reviews"]
        write_csv(snap_csv, cols, [[_cell(r, c) for c in cols] for r in rows])
        print(f"[明细] {snap_csv}")

        if res["prev_date"]:
            new_csv = new_dir / f"bsr_new_entrants_{args.node}_{tag}_{args.source}.csv"
            ncols = ["brand", "brand_type", "rank_today", "asin", "link", "title", "price"]
            if any(r.get("rating") for r in rows):
                ncols += ["rating", "reviews"]
            info = {r["asin"]: r for r in rows}
            # 按品牌分组 → 组内按名次 → 组间按最佳名次，CSV 行序即要求的排序
            ordered = [x for g in group_by_brand(res["new_in"], brand_of) for x in g["items"]]
            write_csv(new_csv, ncols,
                      [[_cell(info.get(x["asin"], {"asin": x["asin"]}), c)
                        if c != "rank_today" else x["rank"] for c in ncols]
                       for x in ordered])
            print(f"[新增名单] {new_csv}（{len(res['new_in'])} 条，已按品牌分组+名次排序）")

            sum_csv = new_dir / f"bsr_brand_summary_{args.node}_{tag}_{args.source}.csv"
            write_csv(sum_csv, ["brand", "brand_type", "new_count", "best_rank",
                                "avg_price", "median_price", "avg_reviews", "asin_list"],
                      brand_summary(res["new_in"], rows))
            print(f"[品牌汇总] {sum_csv}")

            png = chart_dir / f"bsr_new_entrants_{args.node}_{tag}_{args.source}.png"
            prev_titles = {r0: r1 for r0, r1 in conn.execute(
                "SELECT asin, title FROM snapshot WHERE snap_date=? AND node=? AND source=?",
                (res["prev_date"], args.node, args.source))}
            drop_items = [{"asin": x["asin"], "rank": x["rank"],
                           "title": prev_titles.get(x["asin"], "")} for x in res["dropped"]]
            got = render_new_entrants(res["new_in"], rows, args.date, args.node,
                                      args.source, png, len(res["dropped"]),
                                      dropped_items=drop_items)
            print(f"[可视化] {got}" if got else "[可视化] 今日无新增进入，跳过出图")

    # ---------------- 增强图表：份额趋势 / 桑基 / 四象限 ----------------
    chart_dir2 = outdir / CHART_SUB
    share_png = chart_dir2 / f"bsr_brand_share_{args.node}_{tag}_{args.source}.png"
    try:
        got = render_brand_share_trend(conn, args.node, args.source, share_png)
        print(f"[份额趋势] {got}" if got else "[份额趋势] 快照日不足，跳过出图")
    except Exception as e:
        print(f"[warn] 份额趋势出图失败 {type(e).__name__}: {e}", file=sys.stderr)

    sankey_png = chart_dir2 / f"bsr_sankey_{args.node}_{tag}_{args.source}.png"
    prev_rows_for_sankey = (db_day_rows(conn, args.node, args.source, res["prev_date"])
                            if res.get("prev_date") else [])
    try:
        got = render_brand_sankey(prev_rows_for_sankey, rows, res.get("prev_date") or "",
                                  args.date, args.node, args.source, sankey_png)
        print(f"[桑基图] {got}" if got else "[桑基图] 缺少上一快照日，跳过出图")
    except Exception as e:
        print(f"[warn] 桑基图出图失败 {type(e).__name__}: {e}", file=sys.stderr)

    quad_png = chart_dir2 / f"bsr_quadrant_{args.node}_{tag}_{args.source}.png"
    try:
        got = render_price_review_quadrant(rows, args.date, args.node, args.source, quad_png)
        print(f"[四象限] {got}" if got else "[四象限] 有效数据点不足，跳过出图")
    except Exception as e:
        print(f"[warn] 四象限出图失败 {type(e).__name__}: {e}", file=sys.stderr)

    if not args.no_html:
        page = render_dashboard(outdir, args.node, args.source, args.html_days)
        if page:
            size_mb = page.stat().st_size / 1024 / 1024
            _scope = "全部已有快照日" if args.html_days <= 0 else f"最近 {args.html_days} 个快照日"
            print(f"[看板] {page}（内嵌{_scope}，{size_mb:.2f} MB）")
        else:
            print("[看板] 未找到可用明细 CSV，跳过生成")

    print(f"采集耗时 {cost:.1f}s")
    print_report(args.node, args.date, rows, res, args.source)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
