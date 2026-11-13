# ============================================================
#  AM BSR 每日监控 — 一键启动
# ============================================================
#  执行链路：
#    2-脚本\01_采集与生成.py  →  采集 Top N 榜单
#                             →  落 1-数据源\bsr_history.db 快照
#                             →  与上一个快照日对比
#                             →  写 3-输出\明细 / 新增名单 / 可视化
#                             →  生成 3-输出\看板\index.html
#  配置：2-脚本\监控配置.json（站点 US / 配送邮编 10001 / 运行日志：2-脚本\logs\）
#  用法：python 启动.py [--source browser|html] [--zipcode 10001] [其他原脚本参数]
#        附带的全部参数原样透传给 01_采集与生成.py
# ============================================================

import os
import subprocess
import sys
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
SCRIPT_DIR = os.path.join(BASE, '2-脚本')

SCRIPTS = [
    '01_采集与生成.py',
]


def run_script(filename, extra_args=None):
    path = os.path.join(SCRIPT_DIR, filename)
    print(f'\n{"=" * 60}')
    print(f'  ▶ 执行: {filename}')
    print(f'{"=" * 60}')
    result = subprocess.run(
        [sys.executable, path] + list(extra_args or []),
        cwd=BASE,
        capture_output=False,
    )
    if result.returncode != 0:
        print(f'\n  ✘ {filename} 执行失败 (exit code: {result.returncode})')
        return False
    print(f'  ✔ {filename} 完成')
    return True


def main():
    extra = sys.argv[1:]
    print(f'启动时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
    print(f'项目根  : {BASE}')
    print(f'参数透传: {" ".join(extra) if extra else "（无，使用默认 browser 数据源 + 邮编 10001）"}')
    for name in SCRIPTS:
        if not run_script(name, extra):
            return 1
    print(f'\n{"=" * 60}')
    print('  全部完成')
    print(f'{"=" * 60}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
