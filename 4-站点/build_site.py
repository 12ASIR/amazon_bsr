# -*- coding: utf-8 -*-
"""
AM 监控中心站点生成器
------------------------------------------------
读取两个项目的本地数据，生成 GitHub Pages 三张页面：
  index.html        导航页
  bsr.html          BSR 新增 ASIN（简版）
  competitor.html   竞品动态（含进场动作归因）

数据源（默认读取桌面两个项目，路径不写死，可用参数覆盖）：
  A) BSR 项目  3-输出/新增名单/*.csv、3-输出/明细/<日期>/*.csv
  B) 竞品项目  1-数据源/<YYYYMMDD>/流量时光机_流量变化_<ASIN>_<YYYYMMDD>.xlsx
               2-脚本/asin_map.csv

用法：
  python build_site.py
  python build_site.py --bsr "D:\\path" --sif "D:\\path" --out "D:\\repo"
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
# 数据源默认取仓库同级目录（本仓库是从两个本地项目抽出的发布副本），
# 可用环境变量 BSR_SRC / SIF_SRC 或命令行 --bsr / --sif 覆盖
DEFAULT_BSR = Path(os.environ.get("BSR_SRC") or (BASE.parent.parent / "AM BSR每日监控"))
DEFAULT_SIF = Path(os.environ.get("SIF_SRC") or (BASE.parent.parent / "AM竞品数据-sif"))

AMZ_DP = "https://www.amazon.com/dp/{}"
DATE_DIR_RE = re.compile(r"^\d{8}$")
ASIN_RE = re.compile(r"(B0[A-Z0-9]{8})")


# ---------------------------------------------------------------- 工具
def to_num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if not s or s in {"-", "—"}:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", s.replace(",", ""))
    return float(m.group()) if m else None


def to_bool(v):
    s = str(v).strip().lower() if v is not None else ""
    return s in {"是", "true", "1", "y", "yes"}


def money(v):
    n = to_num(v)
    return round(n, 2) if n is not None else None


def short(s, n):
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


# ---------------------------------------------------------------- A) BSR 新增 ASIN
def load_bsr(base: Path):
    outdir = base / "3-输出"
    entrants_files = sorted((outdir / "新增名单").glob("bsr_new_entrants_*.csv"))
    detail_dirs = sorted(
        [d for d in (outdir / "明细").glob("*") if d.is_dir()])
    detail_files = []
    for d in detail_dirs:
        detail_files += sorted(d.glob("bsr_snapshot_*.csv"))

    rows = []
    src_date = None
    if entrants_files:
        f = entrants_files[-1]
        nums = re.findall(r"(20\d{6})", f.stem)
        if nums:
            d8 = nums[-1]
            src_date = "{}-{}-{}".format(d8[:4], d8[4:6], d8[6:])
        with open(f, encoding="utf-8-sig", newline="") as fh:
            for r in csv.DictReader(fh):
                asin = (r.get("asin") or "").strip()
                if not asin:
                    continue
                rows.append({
                    "asin": asin,
                    "brand": (r.get("brand") or "").strip(),
                    "brand_type": (r.get("brand_type") or "").strip(),
                    "rank": int(to_num(r.get("rank_today")) or 0),
                    "title": (r.get("title") or "").strip(),
                    "title_short": short(r.get("title"), 78),
                    "price": money(r.get("price")),
                    "rating": to_num(r.get("rating")),
                    "reviews": int(to_num(r.get("reviews")) or 0),
                    "url": AMZ_DP.format(asin),
                })
        rows.sort(key=lambda x: x["rank"])

    # 快照总量 / 上次对比
    snap = {"date": None, "count": 0, "avg_price": None}
    if detail_files:
        last = detail_files[-1]
        nums = re.findall(r"(20\d{6})", last.stem)
        if nums:
            d8 = nums[-1]
            snap["date"] = "{}-{}-{}".format(d8[:4], d8[4:6], d8[6:])
        prices = []
        cnt = 0
        with open(last, encoding="utf-8-sig", newline="") as fh:
            for r in csv.DictReader(fh):
                cnt += 1
                p = money(r.get("price"))
                if p:
                    prices.append(p)
        snap["count"] = cnt
        snap["avg_price"] = round(sum(prices) / len(prices), 2) if prices else None

    return {
        "node": "15736621",
        "node_name": "Automotive › Running Boards",
        "source_date": src_date,
        "snapshot": snap,
        "count": len(rows),
        "items": rows,
    }


# ---------------------------------------------------------------- B) 竞品动态
COLS = {
    "date": 1, "sub_bsr": 16, "main_bsr": 17, "sales30": 18,
    "final_price": 19, "ld_price": 20, "coupon": 21, "prime_price": 22, "bb_price": 23,
    "new_campaign": 24, "title_changed": 25, "image_changed": 26,
    "rating": 27, "reviews": 28, "sellers": 29,
    "organic_flow": 30, "sp_flow": 31, "sp_rec_flow": 32, "sb_flow": 33, "sbv_flow": 34,
    "organic_pct": 35, "sp_pct": 36, "sp_rec_pct": 37, "sb_pct": 38, "sbv_pct": 39,
}


def load_competitor(sif: Path, keep_days=7):
    amap = {}
    mp = sif / "2-脚本" / "asin_map.csv"
    if mp.exists():
        with open(mp, encoding="utf-8-sig", newline="") as fh:
            for r in csv.DictReader(fh):
                amap[(r.get("ASIN") or "").strip()] = {
                    "brand": (r.get("品牌") or "").strip(),
                    "model": (r.get("适配车型") or "").strip(),
                }

    srcroot = sif / "1-数据源"
    day_dirs = sorted([d for d in srcroot.glob("*") if d.is_dir() and DATE_DIR_RE.match(d.name)])
    per_asin = {}
    for d in day_dirs:
        for f in sorted(d.glob("*.xlsx")):
            m = ASIN_RE.search(f.name)
            if not m:
                continue
            asin = m.group(1)
            try:
                import openpyxl
                wb = openpyxl.load_workbook(f, data_only=True)
            except Exception as e:  # noqa: BLE001
                print(f"[warn] 跳过 {f.name}: {e}", file=sys.stderr)
                continue
            ws = wb.worksheets[0]
            rows = []
            for i, row in enumerate(ws.iter_rows(min_row=3, values_only=True)):
                if not row or len(row) < 40:
                    continue
                dv = row[COLS["date"]]
                if dv is None:
                    continue
                if isinstance(dv, datetime):
                    ds = dv.strftime("%Y-%m-%d")
                else:
                    s = str(dv).strip()
                    if not re.match(r"\d{4}-\d{2}-\d{2}", s):
                        continue
                    ds = s[:10]
                rows.append({
                    "date": ds,
                    "sub_bsr": to_num(row[COLS["sub_bsr"]]),
                    "main_bsr": to_num(row[COLS["main_bsr"]]),
                    "sales30": to_num(row[COLS["sales30"]]),
                    "bb_price": money(row[COLS["bb_price"]]),
                    "ld_price": money(row[COLS["ld_price"]]),
                    "coupon": money(row[COLS["coupon"]]),
                    "new_campaign": to_bool(row[COLS["new_campaign"]]),
                    "title_changed": to_bool(row[COLS["title_changed"]]),
                    "image_changed": to_bool(row[COLS["image_changed"]]),
                    "rating": to_num(row[COLS["rating"]]),
                    "reviews": to_num(row[COLS["reviews"]]),
                    "sellers": to_num(row[COLS["sellers"]]),
                    "organic_pct": to_num(row[COLS["organic_pct"]]),
                    "sp_pct": to_num(row[COLS["sp_pct"]]),
                    "sp_rec_pct": to_num(row[COLS["sp_rec_pct"]]),
                    "sb_pct": to_num(row[COLS["sb_pct"]]),
                    "sbv_pct": to_num(row[COLS["sbv_pct"]]),
                })
            wb.close()
            rows.sort(key=lambda x: x["date"])
            per_asin.setdefault(asin, []).extend(rows)

    items = []
    for asin, series in per_asin.items():
        # 同日期去重，保留最后一份
        dedup = {}
        for r in series:
            dedup[r["date"]] = r
        series = [dedup[k] for k in sorted(dedup)]
        if not series:
            continue
        window = series[-keep_days:]
        latest = window[-1]
        prev = window[-2] if len(window) > 1 else None

        # ---- 近 7 天动作事件
        events = []
        prices = [(r["date"], r["bb_price"]) for r in window if r["bb_price"] is not None]
        if len(prices) >= 2 and prices[0][1] != prices[-1][1]:
            delta = round(prices[-1][1] - prices[0][1], 2)
            events.append({"type": "price", "dir": "down" if delta < 0 else "up",
                           "text": f"价格 {prices[0][1]} → {prices[-1][1]}（{'+' if delta > 0 else ''}{delta}）"})
        for r in window:
            if r["title_changed"]:
                events.append({"type": "title", "dir": "act", "text": f"{r['date'][5:]} 改标题"})
            if r["image_changed"]:
                events.append({"type": "image", "dir": "act", "text": f"{r['date'][5:]} 改主图"})
            if r["new_campaign"]:
                events.append({"type": "ad", "dir": "act", "text": f"{r['date'][5:]} 新增广告活动"})
            if r["ld_price"] is not None:
                events.append({"type": "ld", "dir": "act", "text": f"{r['date'][5:]} 秒杀 ${r['ld_price']}"})
            if r["coupon"] is not None:
                events.append({"type": "coupon", "dir": "act", "text": f"{r['date'][5:]} 券 ${r['coupon']}"})

        # 排名变化
        sub_delta = None
        if prev and latest["sub_bsr"] and prev["sub_bsr"]:
            sub_delta = int(prev["sub_bsr"] - latest["sub_bsr"])  # 正数=上升
        ad_share = latest["sp_pct"]
        if ad_share is None and latest["sp_pct"] is None:
            ad_parts = [latest.get(k) for k in ("sp_pct", "sp_rec_pct", "sb_pct", "sbv_pct")]
            ad_share = round(sum(p for p in ad_parts if p), 4) if any(ad_parts) else None

        meta = amap.get(asin, {})
        items.append({
            "asin": asin,
            "brand": meta.get("brand", ""),
            "model": meta.get("model", ""),
            "url": AMZ_DP.format(asin),
            "latest": latest,
            "prev": prev,
            "sub_delta": sub_delta,
            "events": events[:12],
            "actions_today": [e["type"] for e in events if e["text"].startswith(latest["date"][5:])],
            "series": [{"d": r["date"][5:], "p": r["bb_price"], "c": r["coupon"],
                        "s": int(r["sub_bsr"]) if r["sub_bsr"] else None,
                        "ad": round((r["sp_pct"] or 0) * 100, 1)} for r in window],
        })

    items.sort(key=lambda x: (x["latest"]["sub_bsr"] or 9999))
    latest_date = max((r["date"] for s in per_asin.values() for r in s), default=None)
    return {
        "latest_date": latest_date,
        "tracked": len(items),
        "action_count": sum(1 for i in items if i["actions_today"]),
        "items": items,
    }


# ---------------------------------------------------------------- 模板
CSS = """
:root{--navy:#1F3864;--navy2:#2C4E85;--line:#DDE3EA;--bg:#EEF1F6;--ink:#202020;
--muted:#6B7A90;--down:#1B6B32;--downbg:#E4F4E8;--up:#B00020;--upbg:#FDE3E4;
--act:#9C6500;--actbg:#FFF4E0;--card:#fff;}
*{box-sizing:border-box}
body{margin:0;padding:22px 22px 48px;background:var(--bg);color:var(--ink);
font-family:"Microsoft YaHei","PingFang SC","Hiragino Sans GB",system-ui,sans-serif;font-size:13.5px;}
.wrap{max-width:1180px;margin:0 auto}
a{color:var(--navy2);text-decoration:none}
a:hover{text-decoration:underline}
.top{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;margin-bottom:4px}
h1{font-size:20px;color:var(--navy);margin:0;letter-spacing:.5px}
.badge{font-size:12px;color:#41556F;background:#fff;border:1px solid var(--line);
border-radius:99px;padding:3px 11px}
.sub{color:var(--muted);font-size:12.5px;margin:6px 0 18px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:20px 20px 18px;
box-shadow:0 1px 3px rgba(31,56,100,.07);display:flex;flex-direction:column;gap:12px;
transition:.15s;border-top:4px solid var(--navy)}
.card:hover{box-shadow:0 6px 18px rgba(31,56,100,.13);transform:translateY(-2px)}
.card h2{margin:0;font-size:16.5px;color:var(--navy)}
.card p{margin:0;color:var(--muted);font-size:12.5px;line-height:1.7}
.stat{display:flex;gap:18px;flex-wrap:wrap;margin:2px 0 2px}
.stat div{display:flex;flex-direction:column;gap:2px}
.stat b{font-size:21px;color:var(--navy);font-variant-numeric:tabular-nums;font-weight:700}
.stat span{font-size:11.5px;color:var(--muted)}
.btn{display:inline-block;background:var(--navy);color:#fff;padding:9px 18px;border-radius:6px;
font-weight:600;font-size:13px;align-self:flex-start}
.btn:hover{background:var(--navy2);text-decoration:none}
.btn.ghost{background:#fff;color:var(--navy);border:1px solid var(--navy)}
table{width:100%;border-collapse:separate;border-spacing:0;background:#fff;
border:1px solid var(--line);border-radius:8px;overflow:hidden;font-variant-numeric:tabular-nums}
thead th{background:var(--navy);color:#fff;font-size:12px;font-weight:600;padding:9px 10px;
text-align:left;white-space:nowrap}
tbody td{padding:9px 10px;border-top:1px solid var(--line);vertical-align:middle}
tbody tr:nth-child(even){background:#FAFBFD}
tbody tr:hover{background:#F0F5FC}
.mono{font-family:Consolas,"SF Mono",Menlo,monospace;font-size:12.5px;letter-spacing:.3px}
.t{color:#3A4A60;font-size:12.5px;max-width:520px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.num{text-align:right;font-variant-numeric:tabular-nums}
.tag{display:inline-block;font-size:11.5px;padding:2px 8px;border-radius:4px;margin:1px 3px 1px 0;
background:var(--actbg);color:var(--act);font-weight:600;white-space:nowrap}
.tag.down{background:var(--downbg);color:var(--down)}
.tag.up{background:var(--upbg);color:var(--up)}
.tag.none{background:#F0F2F5;color:#8A97A8;font-weight:400}
.toolbar{display:flex;gap:10px;align-items:center;margin:0 0 14px;flex-wrap:wrap}
.toolbar input{padding:8px 12px;border:1px solid #C9D4E3;border-radius:6px;font-size:13px;
min-width:240px;outline:none}
.toolbar input:focus{border-color:var(--navy2);box-shadow:0 0 0 2px #E4ECF8}
.toolbar .count{margin-left:auto;color:var(--muted);font-size:12.5px}
.cgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:14px}
.ccard{background:#fff;border:1px solid var(--line);border-radius:10px;padding:16px 16px 12px;
box-shadow:0 1px 3px rgba(31,56,100,.06)}
.ccard .hd{display:flex;justify-content:space-between;align-items:flex-start;gap:8px;margin-bottom:10px}
.ccard .brand{font-weight:700;color:var(--navy);font-size:14.5px}
.ccard .meta{color:var(--muted);font-size:11.5px;margin-top:2px}
.kv{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin:10px 0 10px}
.kv div{background:#F7F9FC;border:1px solid #E8EDF4;border-radius:6px;padding:7px 8px}
.kv b{display:block;font-size:14px;color:var(--navy);font-variant-numeric:tabular-nums}
.kv span{font-size:11px;color:var(--muted)}
.mini{width:100%;border:1px solid #E8EDF4;border-radius:6px;font-size:11.5px;overflow:hidden}
.mini th{background:#F0F4FA;color:#41556F;font-weight:600;padding:5px 6px;text-align:center;font-size:11px}
.mini td{padding:4px 6px;text-align:center;border-top:1px solid #EEF2F7;font-variant-numeric:tabular-nums}
.note{background:#FFFDF5;border:1px solid #F0E4C0;border-left:4px solid #D9A922;border-radius:8px;
padding:13px 16px;color:#5C4A18;font-size:12.5px;line-height:1.85;margin-top:22px}
.note b{color:#7A5B00}
footer{margin-top:26px;color:#93A0B2;font-size:12px;text-align:center}
.empty{background:#fff;border:1px dashed #C9D4E3;border-radius:10px;padding:44px;text-align:center;
color:var(--muted)}
""".strip()

HEAD = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>{css}</style>
</head>
<body><div class="wrap">
"""


def page(title, css, body):
    return (HEAD.format(title=title, css=css) + body +
            "\n</div></body>\n</html>\n")


def build_index(bsr, comp):
    b_date = bsr.get("source_date") or "—"
    c_date = comp.get("latest_date") or "—"
    body = f"""
<div class="top"><h1>AM 竞品监控中心</h1>
<span class="badge">BSR 数据 {b_date}</span><span class="badge">竞品数据 {c_date}</span></div>
<div class="sub">亚马逊美国站 · Automotive › Running Boards（节点 {bsr['node']}）· 每日自动采集</div>

<div class="grid">
  <div class="card">
    <h2>BSR 每日监控</h2>
    <p>Top100 榜单每天对比，只列出今天新进榜的 ASIN，点开直接去亚马逊看商品。</p>
    <div class="stat">
      <div><b>{bsr['count']}</b><span>今日新进榜</span></div>
      <div><b>{bsr['snapshot']['count'] or '—'}</b><span>榜单监控数</span></div>
      <div><b>{'$' + str(bsr['snapshot']['avg_price']) if bsr['snapshot']['avg_price'] else '—'}</b><span>榜内均价</span></div>
    </div>
    <a class="btn" href="bsr.html">进入 BSR 页 →</a>
  </div>

  <div class="card">
    <h2>竞品动态</h2>
    <p>监控名单内竞品的价格、优惠券、秒杀、排名与流量结构变化，标注当期动作。</p>
    <div class="stat">
      <div><b>{comp['tracked']}</b><span>监控竞品</span></div>
      <div><b>{comp['action_count']}</b><span>今日有动作</span></div>
      <div><b>{comp['latest_date'] or '—'}</b><span>数据日期</span></div>
    </div>
    <a class="btn" href="competitor.html">进入竞品页 →</a>
  </div>
</div>

<div class="sub" style="margin-top:24px">
  另：<a href="dashboard.html">原有完整看板</a> · <span style="color:#A8B4C4">保留备查，日常不必进</span>
</div>

<footer>数据源：亚马逊前台 BSR 榜单 + 卖家精灵「流量时光机」· 页面为静态快照，随每日采集重新生成</footer>
"""
    return page("AM 竞品监控中心", CSS, body)


def build_bsr(bsr):
    rows = []
    for i, it in enumerate(bsr["items"], 1):
        stars = f'{it["rating"]:.1f}' if it["rating"] else "—"
        rows.append(
            f'<tr><td class="num">{it["rank"]}</td>'
            f'<td><a class="mono" href="{it["url"]}" target="_blank" rel="noopener">{it["asin"]}</a></td>'
            f'<td class="t" title="{it["title"]}">{it["title_short"]}</td>'
            f'<td>{it["brand"] or "—"}</td>'
            f'<td class="num">{"$" + str(it["price"]) if it["price"] else "—"}</td>'
            f'<td class="num">{stars} / {it["reviews"]}</td>'
            f'<td><a class="btn" style="padding:5px 12px;font-size:12px" href="{it["url"]}" '
            f'target="_blank" rel="noopener">亚马逊 ↗</a></td></tr>')
    body = f"""<div class="top"><h1>BSR 新增 ASIN</h1>
<span class="badge">数据日期 {bsr.get('source_date') or '—'}</span></div>
<div class="sub">节点 {bsr['node']} · {bsr['node_name']} · 今日新进榜 {bsr['count']} 个 ·
<a href="index.html">← 返回导航</a></div>

<div class="toolbar">
  <input id="q" type="search" placeholder="搜索 ASIN / 品牌 / 标题关键词">
  <span class="count" id="cnt"></span>
</div>

<table id="tb">
<thead><tr><th style="width:56px">排名</th><th style="width:118px">ASIN</th>
<th>商品标题</th><th style="width:112px">品牌</th><th style="width:82px">价格</th>
<th style="width:112px">评分/评论</th><th style="width:104px">链接</th></tr></thead>
<tbody>
{''.join(rows) if rows else '<tr><td colspan="7" style="text-align:center;padding:30px;color:#8A97A8">暂无新增 ASIN 数据</td></tr>'}
</tbody></table>

<footer>点击 ASIN 或「亚马逊 ↗」直接打开对应商品页 · 数据随每日采集更新</footer>

<script>
(function(){{
  var q=document.getElementById('q'),cnt=document.getElementById('cnt'),
      trs=Array.prototype.slice.call(document.querySelectorAll('#tb tbody tr'));
  function upd(){{
    var v=q.value.trim().toLowerCase(),n=0;
    trs.forEach(function(tr){{
      var hit=!v||tr.innerText.toLowerCase().indexOf(v)>-1;
      tr.style.display=hit?'':'none'; if(hit)n++;
    }});
    cnt.textContent='显示 '+n+' / '+trs.length;
  }}
  q.addEventListener('input',upd); upd();
}})();
</script>
"""
    return page("BSR 新增 ASIN", CSS, body)


def build_competitor(comp):
    cards = []
    for it in comp["items"]:
        L = it["latest"]
        tags = []
        if it["actions_today"]:
            m = {"title": "改标题", "image": "改主图", "ad": "新增广告", "ld": "上秒杀", "coupon": "上券", "price": "调价"}
            for t in dict.fromkeys(it["actions_today"]):
                tags.append(f'<span class="tag">{m.get(t, t)}</span>')
        else:
            tags.append('<span class="tag none">今日无动作</span>')
        if it["sub_delta"]:
            cls = "down" if it["sub_delta"] > 0 else "up"
            sgn = "↑" if it["sub_delta"] > 0 else "↓"
            tags.append(f'<span class="tag {cls}">小类 {sgn}{abs(it["sub_delta"])} 位</span>')

        mini = "".join(
            f'<tr><td>{s["d"]}</td><td>{"" if s["p"] is None else "$" + str(s["p"])}</td>'
            f'<td>{"" if s["c"] is None else "$" + str(s["c"])}</td>'
            f'<td>{s["s"] if s["s"] else "—"}</td><td>{s["ad"]}%</td></tr>'
            for s in it["series"])

        coupon = f'${L["coupon"]}' if L.get("coupon") else "—"
        cards.append(f"""
<div class="ccard">
  <div class="hd">
    <div><div class="brand">{it['brand'] or '未命名品牌'}</div>
    <div class="meta"><a class="mono" href="{it['url']}" target="_blank" rel="noopener">{it['asin']}</a>
    {' · ' + it['model'] if it['model'] else ''}</div></div>
    <div style="text-align:right">{''.join(tags)}</div>
  </div>
  <div class="kv">
    <div><b>{'$' + str(L['bb_price']) if L.get('bb_price') else '—'}</b><span>Buybox 价</span></div>
    <div><b>{coupon}</b><span>优惠券</span></div>
    <div><b>{int(L['sub_bsr']) if L.get('sub_bsr') else '—'}</b><span>小类排名</span></div>
    <div><b>{int(L['main_bsr']) if L.get('main_bsr') else '—'}</b><span>大类排名</span></div>
  </div>
  <table class="mini">
    <thead><tr><th>日期</th><th>BB价</th><th>券</th><th>小类</th><th>广告占比</th></tr></thead>
    <tbody>{mini}</tbody>
  </table>
  <div style="margin-top:9px;font-size:11.5px;color:#6B7A90;line-height:1.7">
    {' · '.join(e['text'] for e in it['events'][:6]) or '近 7 天无标记动作'}
  </div>
</div>""")

    body = f"""<div class="top"><h1>竞品动态</h1>
<span class="badge">数据日期 {comp['latest_date'] or '—'}</span></div>
<div class="sub">监控 {comp['tracked']} 个竞品 · 今日有动作 {comp['action_count']} 个 ·
<a href="index.html">← 返回导航</a></div>

<div class="cgrid">{''.join(cards) if cards else '<div class="empty">暂无竞品数据</div>'}</div>

<div class="note">
<b>这些动作是怎么来的</b>：页面上的「改标题 / 改主图 / 新增广告活动 / 上秒杀 / 上券 / 调价」都不是推测，
而是卖家精灵「流量时光机」源表里逐日自带标记位与数值直接读出来的，口径可复现。<br>
<b>能不能证明是它靠某个动作进的榜</b>：只能做<b>信号关联</b>，不能做因果结论。榜单位次由销量驱动，
而广告投放力度、报名秒杀等后台操作天然不可见；能观察到的是动作时间点与排名拐点是否重合。
因此这里展示的是「动作 + 排名拐点」的并置证据，供你判断，不替你下结论。<br>
<b>没被监控的新 ASIN</b>：卖家精灵只对监控名单里的 ASIN 导出，榜上其它新品目前只有前台快照（价格/评分/评论），
看不出它做过什么。要覆盖，需把新进榜 ASIN 加进监控名单每日导出。
</div>

<footer>数据源：卖家精灵「流量时光机_流量变化」· 口径：美西时间 · 排名数值变小＝上升</footer>
"""
    return page("竞品动态", CSS, body)


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bsr", default=str(DEFAULT_BSR))
    ap.add_argument("--sif", default=str(DEFAULT_SIF))
    ap.add_argument("--out", default=str(BASE.parent))
    args = ap.parse_args()

    out = Path(args.out)
    bsr = load_bsr(Path(args.bsr))
    comp = load_competitor(Path(args.sif))

    (out / "index.html").write_text(build_index(bsr, comp), encoding="utf-8")
    (out / "bsr.html").write_text(build_bsr(bsr), encoding="utf-8")
    (out / "competitor.html").write_text(build_competitor(comp), encoding="utf-8")

    data = out / "data"
    data.mkdir(exist_ok=True)
    (data / "bsr.json").write_text(json.dumps(bsr, ensure_ascii=False, indent=1), encoding="utf-8")
    (data / "competitor.json").write_text(json.dumps(comp, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"[OK] 新增 ASIN {bsr['count']} 个（数据日期 {bsr['source_date']}）")
    print(f"[OK] 监控竞品 {comp['tracked']} 个（数据日期 {comp['latest_date']}），今日有动作 {comp['action_count']} 个")
    print(f"[OK] 输出目录 {out}")


if __name__ == "__main__":
    main()
