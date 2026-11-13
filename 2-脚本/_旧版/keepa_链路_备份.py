# ============================================================
#  【已停用 · 存档】Keepa API 采集链路
# ============================================================
#  停用原因：采集口径统一为「亚马逊美国站前台」单一数据源，
#            第三方 API（卖家精灵、Keepa）链路全部下线。
#  停用日期：2026-11-13
#  本文件仅作历史留档，不参与任何自动执行，不要被 01_采集与生成.py 导入。
#
#  如需恢复，请把下面的函数与常量贴回 01_采集与生成.py 的
#  「站点 / 配送位置常量」与「Keepa」区块，并在 --source 的 choices 里
#  重新加入 "keepa"，同时在监控配置中补 keepa_api_key。
# ============================================================

# 原常量（已从主脚本移除）
KEEPA_DOMAIN = {"US": 1, "UK": 2, "DE": 3, "FR": 4, "JP": 5, "CA": 6, "IT": 8, "ES": 9, "MX": 10}


# 原函数（依赖主脚本内的 _get / os / load_config，仅作代码留档，直接运行会报 NameError）
def fetch_keepa(node: str, top: int, domain: str = "US") -> list[dict]:
    key = os.environ.get("KEEPA_API_KEY", "").strip() or load_config().get("keepa_api_key", "")
    if not key:
        raise RuntimeError("未设置 KEEPA_API_KEY 环境变量（或 2-脚本\\监控配置.json 的 keepa_api_key）")
    r = _get("https://api.keepa.com/bestsellers",
             {"key": key, "domain": KEEPA_DOMAIN.get(domain, 1), "category": node})
    payload = r.json()
    if payload.get("error"):
        raise RuntimeError(f"Keepa 返回错误: {payload['error']}（tokensLeft={payload.get('tokensLeft')}）")
    asins = (payload.get("bestSellersList") or {}).get("asinList") or []
    if not asins:
        raise RuntimeError(f"Keepa 无该节点列表: node={node} domain={domain}")
    return [{"rank": i + 1, "asin": a, "title": "", "price": ""} for i, a in enumerate(asins[:top])]
