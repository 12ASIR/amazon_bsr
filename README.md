---
AIGC:
    Label: "1"
    ContentProducer: 001191440300708461136T1XGW3
    ProduceID: 5423bb38532327b9fcaf467f0fa5f6f3_164a8b85df0911f19232525400248c00
    ReservedCode1: 7sliYSa1Lh/x8KgRwn7/klPY3bLG8XGg3wNk74ZMJcLUIMvpIA7ogDy2+8oCk1XuFE9y7e3wryammoJhOrZ6cSeeu5beohDLUPQPsokjncyED8KQuxEJz8lAV5Za5uAb3RYB1GpDNGaG65SJvHXpIEoZ3vwrzSyCiSUm7cp74BLApd8WFZMJUwv8k60=
    ContentPropagator: 001191440300708461136T1XGW3
    PropagateID: 5423bb38532327b9fcaf467f0fa5f6f3_164a8b85df0911f19232525400248c00
    ReservedCode2: 7sliYSa1Lh/x8KgRwn7/klPY3bLG8XGg3wNk74ZMJcLUIMvpIA7ogDy2+8oCk1XuFE9y7e3wryammoJhOrZ6cSeeu5beohDLUPQPsokjncyED8KQuxEJz8lAV5Za5uAb3RYB1GpDNGaG65SJvHXpIEoZ3vwrzSyCiSUm7cp74BLApd8WFZMJUwv8k60=
---


# AM BSR 每日监控

亚马逊 Best Sellers 榜单（US / Automotive / Running Boards，节点 15736621）Top 100 每日监控。
每天采集一次榜单 → 落 SQLite 快照 → 与上一快照日对比 → 输出「新进入 / 掉出 / 排名变动≥10」。

本文档同时承担项目规则、口径说明与结构说明（原 `AM.BSR_主体规则文档.md`、`AM.BSR_项目结构说明.md` 已并入本文，不再单独保留）。

## 仓库总览

本仓库由两个独立但同源的亚马逊美国站竞品监控项目合并而成：**项目一定义「算成什么样」，项目二负责「每天怎么跑」**。两者共用同一数据来源形态（卖家精灵后台导出 / 亚马逊前台页面）与同一目录分层约定（`1-数据源` / `2-脚本` / `3-输出`），因此收在一个仓库内维护，避免口径与目录规范两处漂移。

### 两个项目一览

| 维度 | 项目一 · AM 竞品宽表监控 | 项目二 · AM BSR 每日监控 |
| --- | --- | --- |
| 定位 | 竞品动作的**横向宽表**：把多份后台导出合并为一张单 Sheet 宽表 + 单文件 HTML 看板 | 榜单名次的**纵向时序**：每日采集 Best Sellers Top 100，跨快照日对比新进入 / 掉出 / 名次异动 |
| 输入 | 卖家精灵「流量时光机_流量变化」导出文件（单文件约 72 行 × 40 列，含约 71 天逐日趋势） | 亚马逊美国站前台页面（Playwright 渲染，采集前锁定配送邮编 10001） |
| 输出 | `竞品宽表_YYYYMMDD.xlsx` + 同名 `.html` | 明细 CSV / 新增名单 CSV / 可视化 PNG / 单页看板 `index.html` |
| 回答的问题 | 谁改了标题、谁换了主图、谁上了券、谁调了价 | 谁新进榜、谁掉榜、谁的名次异动需要盯 |
| 核心口径 | 时区 America/Los_Angeles；Buybox 价；小类位差 > 20 位、大类相对幅度 ≥ 10% 才警示 | 榜单名次 `rank` 直接取前台徽标；新进入 / 掉出按上一快照日基线判定 |
| 落地位置 | `0-提示词/AM竞品宽表_项目提示词.md`（本仓库只沉淀规范文档，暂未含实现脚本） | 仓库根目录（`1-数据源/`、`2-脚本/`、`3-输出/`、`.github/workflows/`） |
| 运行方式 | 按提示词实现脚本后本地运行 | 本地 `python 启动.py`，或 GitHub Actions 每日定时 |

### 两者关系

- **同源**：都以亚马逊美国站为唯一目标站点，不依赖第三方付费 API。卖家精灵 API 与 Keepa API 链路均已下线，旧代码仅归档在 `2-脚本/_旧版/`，不再被导入，仓库内不含任何密钥。
- **互补**：项目二回答「这个类目整体在怎么变」，项目一回答「我盯的这几个竞品各自做了什么动作」。同一件事（排名异动）两边口径一致（小类位差 > 20 位、大类相对幅度 ≥ 10%）。
- **不重叠**：项目一只做宽表与看板，不做定时采集；项目二只做榜单时序，不做竞品动作归因。两者字段不混用，合并仓库不合并口径。

### 仓库结构（合并后）

```
amazon_bsr/                            仓库根 = 项目二根目录
├─ 0-提示词/                            项目一：提示词规范文档
│   └─ AM竞品宽表_项目提示词.md
├─ 1-数据源/                            项目二：SQLite 快照库（不入库，首次运行自建）
├─ 2-脚本/                              项目二：主脚本、配置、日志、_旧版归档
├─ 3-输出/                              项目二：全部产物（不入库，运行时生成）
├─ .github/workflows/bsr-daily.yml      项目二：GitHub Actions 云端每日采集
├─ 启动.py                             项目二：一键启动入口
├─ requirements.txt                     项目二：根级依赖
├─ README.md                            本文件：仓库总览 + 项目二完整规则/口径/结构
├─ 测试报告_20261111.md                 项目二：历史测试记录
└─ 部署计划_20261112.md                 项目二：内网看板部署草案（未执行）
```

### 不提交的内容

`1-数据源/bsr_history.db`、`3-输出/` 全部内容、`2-脚本/logs/` 日志、`__pycache__/` 等已在 `.gitignore` 中排除。克隆后首次运行会自动创建数据库与输出目录，当日即为第一份基线快照；跨日对比需自行决定把 `bsr_history.db` 回写仓库或接对象存储。

### 脱敏说明

仓库内所有内网 IP、NAS 主机、SSH 用户名、内网服务端口均已替换为占位符（`<NAS_IP>`、`<LOCAL_IP>`、`<SSH_USER>`、`<BSR_PORT>`、`<EXIST_PORT_A>`、`<EXIST_PORT_B>`、`<NAS_UI_PORT>`、`<仓库根路径>`），部署相关的真实地址请勿提交回公开仓库。

---

## 一、目录结构（三层极简）

顶层只保留三个业务目录 + 五个根级文件，编号层（00/01/02/03/04/06）已全部取消。

```
AM BSR每日监控\                     （项目根，名称不变）
├─ 启动.py                          一键启动入口（BASE=项目根、SCRIPT_DIR=2-脚本、cwd=项目根，参数原样透传）
├─ README.md                        本文档：项目规则 + 口径 + 结构说明
├─ requirements.txt                 根级 Python 依赖（本地与 GitHub Actions 共用）
├─ 测试报告_20261111.md             历史测试记录（内容保持原样，旧路径按第二节映射表对照）
├─ 部署计划_20261112.md             内网看板部署草案（未执行）
├─ .github\workflows\bsr-daily.yml  GitHub Actions 云端每日采集（相对路径，无需改路径）
├─ 1-数据源\
│   └─ bsr_history.db               SQLite 快照库（按 node + source 分区，唯一数据源）
├─ 2-脚本\
│   ├─ 01_采集与生成.py             主脚本（采集 → 落库 → 对比 → 出明细/新增/图/看板）
│   ├─ 监控配置.json                节点 / 站点 / TopN / 配送邮编
│   ├─ logs\                        运行与测试日志
│   │   ├─ run_browser_20261112.log
│   │   ├─ test_browser_20261111.log
│   │   └─ test_sellersprite_20261111.log   （历史日志，对应链路已下线，仅留档）
│   └─ _旧版\                       只归档、不执行
│       ├─ bsr_monitor_v1_20261111.py      v1 旧版脚本
│       ├─ 02_卖家精灵自检.py               卖家精灵链路自检（已下线）
│       ├─ keepa_链路_备份.py               Keepa API 采集函数（已下线）
│       └─ 监控配置_旧格式副本_20261113.json
└─ 3-输出\
    ├─ 明细\{日期}\                 每日完整榜单明细（一天一个日期子目录，既定需求）
    │   ├─ 2026-11-11\bsr_snapshot_15736621_20261111_browser.csv
    │   └─ 2026-11-12\bsr_snapshot_15736621_20261112_browser.csv
    ├─ 新增名单\                    新进入清单 + 品牌汇总 CSV（不与明细混放）
    │   ├─ bsr_new_entrants_15736621_20261112_browser.csv
    │   └─ bsr_brand_summary_15736621_20261112_browser.csv
    ├─ 可视化\                      图表 PNG（新旧链路共用此目录）
    │   ├─ bsr_new_entrants_15736621_20261112_browser.png   新进入名单三分区图
    │   ├─ bsr_brand_share_15736621_20261112_browser.png    品牌份额时间序列
    │   ├─ bsr_sankey_15736621_20261112_browser.png         品牌进入/掉出桑基图
    │   └─ bsr_quadrant_15736621_20261112_browser.png       价格-评论四象限
    ├─ 看板\index.html              单页看板（自包含，图片 base64 内嵌，断网可用）
    └─ _旧版归档\                   v1 前台基线 CSV + 改造前平铺 CSV（只归档）
```

三层职责：`1-数据源` 只放库、`2-脚本` 放全部可执行件与配置/日志、`3-输出` 放全部产物并按类型分流。任何新增文件按此原则落位，不再引入编号前缀。

## 二、路径映射（历史重构，供旧文档对照）

| 旧位置（编号体系） | 新位置（三层结构） |
| --- | --- |
| `01-ods数据源\bsr_history.db` | `1-数据源\bsr_history.db` |
| `02-配置表\监控配置.json` | `2-脚本\监控配置.json` |
| `03-脚本\01_采集与生成.py` | `2-脚本\01_采集与生成.py` |
| `03-脚本\02_卖家精灵自检.py` | `2-脚本\_旧版\02_卖家精灵自检.py`（链路已下线） |
| `temp\logs\*.log` | `2-脚本\logs\*.log` |
| `00-历史存档\bsr_monitor_v1_20261111.py` | `2-脚本\_旧版\bsr_monitor_v1_20261111.py` |
| `04-输出\01-明细\{日期}\` | `3-输出\明细\{日期}\` |
| `04-输出\02-新增名单\` | `3-输出\新增名单\` |
| `04-输出\03-可视化\` | `3-输出\可视化\` |
| `04-输出\04-看板\index.html` | `3-输出\看板\index.html` |
| `00-历史存档\*`、`00-历史存档\旧平铺归档\*` | `3-输出\_旧版归档\*` |
| `06-文档\AM.BSR_主体规则文档.md` | `README.md`（根级） |
| `06-文档\AM.BSR_项目结构说明.md` | 内容并入 `README.md`（文件取消） |
| `06-文档\AM.BSR_测试报告_20261111.md` | `测试报告_20261111.md`（根级） |
| `06-文档\AM.BSR_部署计划_内网看板_20261112.md` | `部署计划_20261112.md`（根级） |
| `temp\`（运行期中间产物） | 取消，日志入 `2-脚本\logs\`，验证产物移出项目根 |

> 根级两个历史文档（测试报告 / 部署计划）正文**未改写**，其中出现的旧路径（`03-脚本\`、`04-输出\`、`reports\` 等）按上表对照理解；文中提到的卖家精灵 API 实测结论属历史记录，该链路已下线。

## 三、脚本内路径常量（全部相对项目根）

`2-脚本\01_采集与生成.py` 顶部集中定义，其余代码只引用常量；**不含任何绝对路径**，Windows 与 Linux 均可直接运行（GitHub Actions 依赖此点）：

| 常量 | 取值 | 用途 |
| --- | --- | --- |
| `SCRIPT_DIR` | `Path(__file__).parent` = `项目根\2-脚本` | 脚本自身目录 |
| `BASE` | `SCRIPT_DIR.parent` = 项目根 | 一切相对路径的基准 |
| `CONFIG_PATH` | `2-脚本\监控配置.json` | 配置读取 |
| `DB_PATH` | `1-数据源\bsr_history.db` | `--db` 默认值 |
| `OUT_DIR` | `3-输出` | `--outdir` 默认值 |
| `LOG_DIR` | `2-脚本\logs` | 运行日志 |
| `AMZ_SITE` | `https://www.amazon.com` | 站点锁定（美国站） |
| `ASIN_URL_BASE` | `https://www.amazon.com/dp/` | 名单每条 ASIN 拼商品链接 |
| `DEFAULT_ZIP` | `10001` | 配送邮编锁定默认值 |
| `DETAIL_SUB` / `NEW_SUB` / `CHART_SUB` / `BOARD_SUB` | `明细` / `新增名单` / `可视化` / `看板` | 四类产物落位 |

`启动.py`：`BASE = 脚本所在目录`（项目根）、`SCRIPT_DIR = BASE\2-脚本`、`cwd = BASE`，`subprocess` 调用 `01_采集与生成.py` 并透传参数，退出码逐级上传。

## 四、采集数据源（已收敛为单一前台链路）

| 数据源 | 参数 | 说明 | 耗时 | 状态 |
| --- | --- | --- | --- | --- |
| 前台页面（渲染） | `--source browser` | Playwright 渲染 + 滚动懒加载，取满 Top100，**开抓前锁定配送邮编** | 约 33s | 主用 |
| 前台页面（轻量） | `--source html` | requests 静态解析，反爬命中时仅约 58 条且缺号；无法携带邮编 | 约 10s | 备用 |

**已下线的第三方 API 链路**（2026-11-13 移除，代码存档在 `2-脚本\_旧版\`，不再被导入、不再出现在 `--source` 选项里）：

| 链路 | 原参数 | 停用原因 |
| --- | --- | --- |
| 卖家精灵 API | `--source sellersprite` | 采集口径统一为亚马逊前台单一数据源，且需外部密钥 |
| Keepa API | `--source keepa` | 同上（`keepa_api_key` 配置项已从 `监控配置.json` 删除） |

## 五、站点与配送邮编锁定（10001）

- 站点固定：`AMZ_SITE = https://www.amazon.com`，脚本内不再有站点切换参数
- 邮编固定：`监控配置.json` 的 `delivery_zip = 10001`（纽约曼哈顿），`--zipcode` 可临时覆盖，`DEFAULT_ZIP` 兜底同为 10001
- 生效方式（`--source browser`）：开抓前打开首页「配送至」弹窗 → 填入 10001 → 提交 → **回读页面显示的配送位置校验**，把结果写入 `ZIP_STATE` 并由主流程打印「邮编锁定：成功 / 失败原因」
- 局限（如实说明）：亚马逊前台弹窗的 DOM 会随版本变化，脚本已做多选择器兜底与回读校验，但**无法对外承诺永久稳定**；若回读不到邮编，日志会明确打印失败原因而不是静默继续。`--source html` 走静态请求，无法携带配送位置，价格按亚马逊默认位置口径，运行时也会打印该提示

## 六、用法

一键入口（项目根）：

```powershell
cd "<仓库根路径>"
python -m pip install -r requirements.txt      # 首次：装依赖
python -m playwright install chromium          # 首次：装浏览器（仅 browser 源需要）
python 启动.py --source browser                # 末尾参数原样透传给 2-脚本\01_采集与生成.py
```

分步调用（等价）：

```powershell
cd "<仓库根路径>"

# 1) 前台页面采集（主用，锁定邮编 10001）
python 2-脚本\01_采集与生成.py --source browser --zipcode 10001

# 2) 变更参数
python 2-脚本\01_采集与生成.py --source browser --node 15736621 --top 100 --date 2026-11-12

# 3) 看板相关开关
python 2-脚本\01_采集与生成.py --source browser --html-days 0    # 0=内嵌全部已有快照日（默认）
python 2-脚本\01_采集与生成.py --source browser --html-days 30   # 只内嵌最近 30 个快照日
python 2-脚本\01_采集与生成.py --source browser --no-html        # 只出 CSV/PNG，不生成 HTML

# 4) 离线验证：只读库重建图表与看板，不发起任何采集（不消耗请求）
python 2-脚本\01_采集与生成.py --rebuild-only
```

输出（四类分流：明细 / 新增名单 / 可视化 / 看板）：

- `3-输出\明细\{日期}\bsr_snapshot_{节点}_{日期}_{数据源}.csv` —— 当日完整榜单明细，**一天一个日期子目录**，含 `brand` / `brand_type` / `link` 列
- `3-输出\新增名单\bsr_new_entrants_{节点}_{日期}_{数据源}.csv` —— 新进入清单（有上一快照日基线时才生成），**按品牌分组 → 组内按今日名次 → 组间按各组最佳名次**排序，首列为 brand / brand_type，每条含 `link`
- `3-输出\新增名单\bsr_brand_summary_{节点}_{日期}_{数据源}.csv` —— 品牌维度汇总：new_count / best_rank / avg_price / median_price / avg_reviews / asin_list
- `3-输出\可视化\*.png` —— 新进入三分区图 + 品牌份额时间序列 + 桑基图 + 四象限图
- `3-输出\看板\index.html` —— 单页看板，见第九节
- 控制台/日志同时打印新进入 / 掉出 / 排名变动≥10

## 七、品牌解析口径（parse_brand / build_brand_map）

标题解析，不依赖外部品牌库，两遍判定：

| 类型 | 判定规则 | 归组名 |
| --- | --- | --- |
| 品牌 `brand` | 标题首个有效词不是通用词（Running / Side / Aluminum / Set / For…），且第二词若为 Auto、Star、Dynasty 等后缀则合并 | 品牌原名，如 `YZONA`、`Tyger Auto` |
| 白牌 `white` | 首词命中通用词表，且标题前 65 字符无配件关键词 | `白牌/无品牌` |
| 配件 `parts` | 首词为通用词，且前 65 字符命中 motor / end cap / step pad / replacement / bracket / hardware / mounting / clip | `配件/OEM` |
| 品牌配件 `bparts` | 品牌词开头但前段命中配件关键词（如 `ZUDKSUY … End Cap … Replacement`） | 品牌原名，图中按配件橙色展示 |
| 回捞 | 判为白牌的条目，若标题 `|` 之后出现已识别品牌名（如 `… Compatible for … | AUTOSAVER88 …`），改判为该品牌 | 品牌原名 |

已知边界：纯数字/字母混合的厂商编码（如 `68461581AA`）不作为品牌；标题内出现竞品名的极端情况可能误归，需人工抽查。

## 八、字段说明

| 字段 | 含义 | 来源 |
| --- | --- | --- |
| rank | 榜单名次（1=最高） | 前台徽标 |
| asin | 商品 ASIN | 前台链接 data-asin |
| link | 亚马逊商品链接 `https://www.amazon.com/dp/{ASIN}` | 由 ASIN 拼接（明细、新增名单均带此列） |
| brand | 品牌归组名 | `build_brand_map` 标题解析 |
| brand_type | 品牌类型（brand / white / parts / bparts） | 同上 |
| title | 标题 | 前台商品名 |
| price | 价格（USD） | 前台售价（受配送邮编影响） |
| rating | 评分 | 前台星标 |
| reviews | 评分数 | 前台评分数 |

## 九、网页看板（3-输出\看板\index.html）

每次运行采集流程后自动生成（除非加 `--no-html`），双击即可在浏览器打开，**不依赖网络、不起服务、纯本地文件**。

- **自包含**：唯一的外部资源是内嵌 PNG，图片以 `data:image/png;base64` 写入 HTML；不引用任何 CDN / 字体 / JS 库，断网可用
- **PNG 处理**：内嵌前按 PNG chunk 结构剥离 `tEXt / iTXt / zTXt` 文本块（保留 IHDR/PLTE/pHYs/IDAT/IEND 等），去掉 Matplotlib Software 水印字段，不影响图像内容
- **数据来源**：全部来自项目内真实 CSV（`3-输出\明细\{日期}\*_snapshot_*.csv`、`新增名单\*_new_entrants_*.csv`、`*_brand_summary_*.csv`），Python 侧解析后序列化 JSON 嵌入，前端不做任何外部请求
- **看板区块（8 个）**：① 新增名单（按品牌分组）② 品牌维度汇总 ③ 新增进入可视化图 ④ 品牌份额时间序列（跨全部快照日）⑤ 品牌进入/掉出桑基图 ⑥ 价格-评论四象限散点 ⑦ 当日明细（品牌分组 / 逐行表切换）⑧ 历史日期切换
- **历史回溯**：`--html-days` 默认 `0` = **内嵌全部已有快照日**；顶部下拉与 ⑧ 区日期表任一点击即切换，切换后 ①②③⑤⑥⑦ 与指标卡全部联动；日期取自 `3-输出\明细\` 下实际存在的日期子目录
- **ASIN 可点击**：明细、新增名单、品牌汇总里的每个 ASIN 都是 `https://www.amazon.com/dp/{ASIN}` 超链接（`target="_blank" rel="noopener noreferrer"`），点击直达商品页
- **过滤**：顶部「过滤」框按 ASIN / 品牌 / 标题匹配，作用于 ①②⑦ 列表；指标卡保持全量口径
- **响应式**：桌面 1440px 与手机 390px 实测 `scrollWidth == innerWidth`，无横向溢出；窄屏表格横向滚动、分组纵向堆叠

> 内网 NAS 部署（<NAS_IP>:<BSR_PORT>）仍只停留在 `部署计划_20261112.md` 草案阶段，**未执行任何部署动作**，本看板仅在本机产出文件。

## 十、GitHub Actions 云端运行

- 依赖清单在根级 `requirements.txt`（playwright / matplotlib / requests，其余为标准库）
- 工作流 `.github\workflows\bsr-daily.yml`：检出 → 装 Python 3.11 → `pip install -r requirements.txt` → `playwright install --with-deps chromium` → 装 `fonts-noto-cjk`（图表中文不乱码）→ `python 启动.py --source browser --zipcode 10001` → 归档 `1-数据源\bsr_history.db` 与 `3-输出\`
- 触发：每天 UTC 01:00（北京时间 09:00）定时 + 手动 `workflow_dispatch`（可选 source）
- **无需改任何路径**：脚本常量全部基于 `BASE = __file__.parent.parent`，仓库根即项目根，云端 checkout 后可直接运行
- 云端注意：Actions 每轮是全新容器，`bsr_history.db` 若不提交回仓库或做 artifact 回取，就无法形成跨日对比，首次运行只会建基线（工作流已归档 db 供下载，跨日回溯需自行决定 commit 回写或接对象存储）

## 十一、验证记录（离线，未消耗真实采集次数）

| 验证项 | 方法 | 结果 |
| --- | --- | --- |
| 结构与四类分流 | 遍历项目根 | `1-数据源` / `2-脚本` / `3-输出` + 根级文件；明细按日期子目录、新增名单独立、可视化独立、看板独立 |
| 路径常量 | 静态扫描 + 运行时打印 | 无 `C:\Users`、无盘符硬编码；常量全部基于 `BASE` |
| 数据源收敛 | `--help` + 源码检索 | `--source` 仅 `browser` / `html`；`sellersprite`、`keepa` 分支与配置项已不存在，代码移入 `2-脚本\_旧版` |
| 邮编锁定 | 离线 mock 弹窗 DOM | 弹窗→填 10001→提交→回读 全链路逻辑通过，未触网 |
| ASIN 链接（CSV） | 隔离目录内两次 DryRun（`fetch_html` 打桩） | 明细列 `rank,asin,link,brand,title,price,rating,reviews`、新增名单列含 `link`；缺链接行 0 |
| ASIN 链接（看板） | Playwright 打开本机 `index.html` | 可点击链接 201 个（明细 100 + 新增 29 + 汇总 72），href 均为 `amazon.com/dp/B0...` |
| 历史回溯 | `--rebuild-only` + Playwright 点击历史行 | 内嵌「全部已有快照日」；点击切到 2026-11-11 后页面数据联动，无 pageerror |
| 三张增强图 | `--rebuild-only` 重建 | 份额趋势 / 桑基 / 四象限 PNG 均生成并内嵌，看板三区块图片均可见 |
| 响应式 | Playwright 1440 / 390 视口 | `scrollWidth == innerWidth`（1440、390），无横向溢出，无 pageerror |
| 自包含 | 检索 HTML | 外链 `<script src=>` 0 个、外链样式表 0 个；`file://` 断网可开 |

未做的事：未执行任何真实 browser / html 采集（全部替身或读库重建）；未触碰内网服务器（<NAS_IP>:<BSR_PORT>）；未创建或修改任何计划任务。

## 十二、后续注意

1. 如已配置定时任务或快捷方式，参数改为 `启动.py` 或 `2-脚本\01_采集与生成.py`，起始目录仍为项目根。
2. 新文件按三层落位：库进 `1-数据源`，脚本 / 配置 / 日志进 `2-脚本`，产物进 `3-输出` 对应子目录（明细必须继续按日期分子目录）。
3. `2-脚本\_旧版` 与 `3-输出\_旧版归档` 只增不删，用于沉淀旧版本脚本与过期产物。

*（内容由AI生成，仅供参考）*
