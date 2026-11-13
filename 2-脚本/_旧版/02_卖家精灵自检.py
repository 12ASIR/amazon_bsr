# -*- coding: utf-8 -*-
"""卖家精灵 API 分支离线自检：本地起 mock 服务，验证 01_采集与生成.py 中
fetch_sellersprite 的解析 / 子类目排名筛选 / 排序 / Top N 截断 逻辑是否正确。
不消耗真实接口次数。"""
import importlib.util
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# 本脚本固定位于  项目根\2-脚本\ 下，被检脚本同目录（文件名以数字开头，无法直接 import）
SCRIPT_DIR = Path(__file__).resolve().parent
BASE = SCRIPT_DIR.parent
MAIN_SCRIPT = SCRIPT_DIR / "01_采集与生成.py"

_spec = importlib.util.spec_from_file_location("bsr_monitor", MAIN_SCRIPT)
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)

NODE = "15736621"
NODE_PATH = "3375251:15736611:15736621"
CALLS = {"node": 0, "research": 0}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/v1/product/node"):
            CALLS["node"] += 1
            assert self.headers.get("secret-key") == "dummy-key"
            self._send({"code": "OK", "message": "成功",
                        "data": [{"nodeIdPath": NODE_PATH, "nodeLabelPath": "Automotive:Running Boards"}]})
        else:
            self._send({"code": "ERROR_URL_NOT_FOUND", "message": "URL未找到"})

    def do_POST(self):
        if self.path.startswith("/v1/product/research"):
            CALLS["research"] += 1
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            page, size = body.get("page", 1), body.get("size", 50)
            items = []
            for i in range(size):
                seq = (page - 1) * size + i
                asin = "B%09d" % seq
                item = {"asin": asin, "title": f"Running Board {seq}",
                        "price": 100 + seq, "rating": 4.5, "ratings": 1000 + seq,
                        "bsr": 5000 - seq, "units": 300}
                # 每 10 条里有 1 条属于别的叶子类目（无 15736621 排名），应被剔除
                if seq % 10 == 9:
                    item["subcategories"] = [{"code": "99999999", "rank": 3, "label": "Other"}]
                else:
                    item["subcategories"] = [{"code": NODE, "rank": seq + 1, "label": "Running Boards"}]
                items.append(item)
            self._send({"code": "OK", "message": "成功", "data": {"items": items}})
        else:
            self._send({"code": "ERROR_URL_NOT_FOUND", "message": "URL未找到"})


def main():
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    m.SS_GATEWAY = f"http://127.0.0.1:{port}"

    rows = m.fetch_sellersprite(NODE, top=100, market="US", key="dummy-key")
    print(f"mock 服务收到请求：node={CALLS['node']} 次，research={CALLS['research']} 次")
    print(f"返回 {len(rows)} 条，首条 {rows[0]['asin']} #{rows[0]['rank']}，末条 {rows[-1]['asin']} #{rows[-1]['rank']}")

    ok = True
    if len(rows) != 100:
        print(f"[FAIL] 期望 100 条，实际 {len(rows)}"); ok = False
    ranks = [r["rank"] for r in rows]
    if ranks != sorted(ranks):
        print("[FAIL] 名次未升序"); ok = False
    if ranks[0] != 1:
        print(f"[FAIL] 首名次应为 1，实际 {ranks[0]}"); ok = False
    if any(r["asin"] in ("B000000009",) for r in rows):
        print("[FAIL] 其他叶子类目商品未剔除"); ok = False
    if CALLS["node"] != 1:
        print(f"[FAIL] 节点解析应调用 1 次，实际 {CALLS['node']}"); ok = False
    if not all(r["title"] and r["price"] for r in rows):
        print("[FAIL] 标题/价格字段缺失"); ok = False

    print("自检结果：", "PASS" if ok else "FAIL")
    srv.shutdown()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
