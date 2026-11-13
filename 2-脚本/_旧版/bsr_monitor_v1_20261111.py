# -*- coding: utf-8 -*-
"""
Amazon BSR 榜单每日监控（Running Boards / US 站）
------------------------------------------------
功能：每日抓取指定 browse node 的 Top N ASIN，落 SQLite 快照，
      与上一个快照日对比，输出：新进入榜单 / 掉出榜单 / 排名大幅变动。

数据源（--source）：
  keepa : 官方推荐，稳定。需环境变量 KEEPA_API_KEY。
  html  : 亚马逊前台 Best Sellers 页面解析，免费但只能拿到部分名次且易反爬。

用法：
  set KEEPA_API_KEY=xxxx
  python bsr_monitor.py --source keepa --node 15736621 --top 100
  python bsr_monitor.py --source html  --node 15736621 --top 60

节点参考（US）：
  15736611 = Automotive > Exterior Accessories > Running Boards & Steps（父类目）
  15736621 = ... > Running Boards（叶子，通常用它）
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sqlite3
import sys
import time
from datetime import date
from html import unescape
from pathlib import Path

try:
    import requests
except ImportError:
    requests = None

# ---------------------------------------------------------------- 常量
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
KEEPA_DOMAIN = {"US": 1, "UK": 2, "DE": 3, "FR": 4, "JP": 5, "CA": 6, "IT": 8, "ES": 9, "MX": 10}

# 前台页面正则
RE_RANK = re.compile(r'class="[^"]*zg-bdg-text[^"]*"\s*>\s*#(\d+)\s*<')
RE_ASIN = re.compile(r'data-asin="([A-Z0-9]{10})"')
RE_TITLE = re.compile(r'<img alt="([^"]{5,400})"')
RE_PRICE = re.compile(r'_cDEzb_p13n-sc-price_[^"]*">\s*\$([\d,]+\.\d{2})')


# ---------------------------------------------------------------- 采集层
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
        except Exception as e:            # 网络抖动，退避重试
            last = f"{type(e).__name__}: {e}"
        time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"请求失败 {url} -> {last}")


def fetch_keepa(node: str, top: int, domain: str = "US") -> list[dict]:
    """Keepa best sellers：返回按销量排序的 ASIN 列表（不含标题价格）。"""
    key = os.environ.get("KEEPA_API_KEY", "").strip()
    if not key:
        raise RuntimeError("未设置 KEEPA_API_KEY 环境变量")
    r = _get("https://api.keepa.com/bestsellers",
             {"key": key, "domain": KEEPA_DOMAIN.get(domain, 1), "category": node})
    payload = r.json()
    if payload.get("error"):
        raise RuntimeError(f"Keepa 返回错误: {payload['error']}（tokensLeft={payload.get('tokensLeft')}）")
    asins = (payload.get("bestSellersList") or {}).get("asinList") or []
    if not asins:
        raise RuntimeError(f"Keepa 无该节点列表: node={node} domain={domain}")
    return [{"rank": i + 1, "asin": a, "title": "", "price": ""} for i, a in enumerate(asins[:top])]


def _parse_bestsellers_html(html: str) -> list[dict]:
    """按文档位置把 #排名 绑定到其后的第一个 data-asin，避免分块正则失效。"""
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
    """前台 Best Sellers 页面解析。每页实际只渲染约 30 条，名次会有缺口。"""
    all_rows, seen = [], set()
    for pg in range(1, pages + 1):
        url = f"https://www.amazon.com/gp/bestsellers/automotive/{node}?pg={pg}"
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


def fetch_browser(node: str, top: int, pages: int = 2, pause: float = 2.0) -> list[dict]:
    """Playwright 渲染模式：滚动触发懒加载后可取满每页 50 条，两页即 Top100。
    缺点：单次约 20~40 秒，依赖本机 Chromium。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError("缺少 playwright，请执行 pip install playwright && playwright install chromium")

    rows, seen = [], set()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(locale="en-US", user_agent=HEADERS["User-Agent"],
                                  viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        for no in range(1, pages + 1):
            url = f"https://www.amazon.com/gp/bestsellers/automotive/{node}?pg={no}"
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
    rank      INTEGER NOT NULL,
    asin      TEXT NOT NULL,
    title     TEXT,
    price     TEXT,
    source    TEXT,
    PRIMARY KEY (snap_date, node, asin)
);
CREATE INDEX IF NOT EXISTS idx_snap ON snapshot (node, snap_date);
"""


def db_open(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(DDL)
    return conn


def last_snap_date(conn, node: str, before: str) -> str | None:
    cur = conn.execute(
        "SELECT MAX(snap_date) FROM snapshot WHERE node=? AND snap_date<?", (node, before))
    return cur.fetchone()[0]


def save_snapshot(conn, node: str, snap_date: str, rows: list[dict], source: str):
    conn.execute("DELETE FROM snapshot WHERE snap_date=? AND node=?", (snap_date, node))
    conn.executemany(
        "INSERT INTO snapshot (snap_date,node,rank,asin,title,price,source) VALUES (?,?,?,?,?,?,?)",
        [(snap_date, node, r["rank"], r["asin"], r.get("title", ""), r.get("price", ""), source)
         for r in rows])
    conn.commit()


# ---------------------------------------------------------------- 对比层
def diff(conn, node: str, today: str) -> dict:
    prev_date = last_snap_date(conn, node, today)
    if not prev_date:
        return {"prev_date": None, "new_in": [], "dropped": [], "moved": []}

    cur = {a: r for a, r in conn.execute(
        "SELECT asin, rank FROM snapshot WHERE node=? AND snap_date=?", (node, today))}
    old = {a: r for a, r in conn.execute(
        "SELECT asin, rank FROM snapshot WHERE node=? AND snap_date=?", (node, prev_date))}

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


def print_report(node, today, rows, res, source):
    print("=" * 62)
    print(f"节点 {node} | 快照日 {today} | 数据源 {source} | 抓到 {len(rows)} 条")
    if res["prev_date"] is None:
        print("首次运行，已建立基线快照。下次运行开始输出新进入清单。")
        print("=" * 62)
        return
    print(f"对比基准：{res['prev_date']}")
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


# ---------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser(description="Amazon BSR 每日监控")
    ap.add_argument("--source", choices=["browser", "keepa", "html"], default="browser",
                    help="browser=前台页面渲染采集（默认，可拿满Top100）；keepa=官方API；html=轻量抓取仅约60条")
    ap.add_argument("--node", default="15736621", help="browse node id，默认 Running Boards(US)")
    ap.add_argument("--domain", default="US")
    ap.add_argument("--top", type=int, default=100)
    ap.add_argument("--pages", type=int, default=2, help="翻页数，browser/html 模式每页 50/约30 条")
    ap.add_argument("--db", default=str(Path(__file__).with_name("bsr_history.db")))
    ap.add_argument("--outdir", default=str(Path(__file__).with_name("reports")))
    ap.add_argument("--date", default=date.today().strftime("%Y-%m-%d"))
    args = ap.parse_args()

    outdir = Path(args.outdir)
    if args.source == "keepa":
        rows = fetch_keepa(args.node, args.top, args.domain)
    elif args.source == "browser":
        rows = fetch_browser(args.node, args.top, args.pages)
    else:
        rows = fetch_html(args.node, args.top, args.pages)
    if not rows:
        print("未采集到数据，终止。", file=sys.stderr)
        return 1

    conn = db_open(Path(args.db))
    save_snapshot(conn, args.node, args.date, rows, args.source)
    res = diff(conn, args.node, args.date)

    tag = args.date.replace("-", "")
    snap_csv = outdir / f"bsr_snapshot_{args.node}_{tag}.csv"
    cols = ["rank", "asin", "title", "price"]
    if any("rating" in r for r in rows):
        cols += ["rating", "reviews"]
    write_csv(snap_csv, cols, [[r.get(c, "") for c in cols] for r in rows])

    if res["prev_date"]:
        new_csv = outdir / f"bsr_new_entrants_{args.node}_{tag}.csv"
        write_csv(new_csv, ["asin", "rank_today", "title", "price"],
                  [[x["asin"], x["rank"],
                    next((r.get("title", "") for r in rows if r["asin"] == x["asin"]), ""),
                    next((r.get("price", "") for r in rows if r["asin"] == x["asin"]), "")]
                   for x in res["new_in"]])
        print(f"明细已输出：{snap_csv}")
        print(f"新增清单：{new_csv}")

    print_report(args.node, args.date, rows, res, args.source)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
