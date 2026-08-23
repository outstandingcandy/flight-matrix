# Scraper 统一规划

把主仓 `src/scraper/` 的抓取框架和 5 个航空 scraper 全部下沉到
submodule `lib/resilient-scraper/`,让 flight-matrix 只留业务薄层(sinks +
调度 + 入口)。

## 决策锁定

| 决策 | 选项 |
|---|---|
| submodule 承接范围 | **全部 scraper 迁入 submodule**(含 `fr24_*` / `jetphotos` / `airport_data`) |
| 并发模型 | **全仓改异步**,queue / worker / browser_pool 统一用 submodule 的异步实现。scraper 的 `scrape()` 保持同步,由 worker 的 `asyncio.to_thread` 驱动 |
| 业务 DB 写入 | **sink 注入**:submodule 提供 hook 点,flight-matrix 实现 sink 写自家表 |
| 切换方式 | **一次切换**,不留 shim |
| 数据库迁移 | **直接迁移**,非破坏性(加 2 张表 + 1 个部分唯一索引) |
| scraper 返回类型 | **Pydantic 结构化结果**,不再让 extractor 从 HTML 二次解析 |
| submodule 定位 | 多领域通用库:同时支持小红书和航空网站 |

## 目标架构

```
lib/resilient-scraper/                     多领域通用抓取库
├── resilient_scraper/
│   ├── base.py                             唯一基类(当前 ResilientScraper)
│   ├── models.py                           ScraperTask + ScraperResult + WorkerStatus + WorkerInfo + ScraperConfig
│   ├── errors.py                           统一异常层级
│   ├── service/
│   │   ├── browser_pool.py                 异步,保留 Xvfb+headless=false 规约
│   │   ├── queue.py                        异步 TaskQueue
│   │   ├── worker.py                       异步 Worker + sink 派发
│   │   ├── config.py
│   │   └── registry.py
│   └── scrapers/
│       ├── aviation/                       新增分组
│       │   ├── airport_data/
│       │   ├── jetphotos/
│       │   ├── fr24_map/
│       │   ├── fr24_airport/               含 arrivals/departures
│       │   └── fr24_aircraft/
│       ├── social/
│       │   └── xiaohongshu/
│       ├── aviation_photos/
│       │   └── planespotters/
│       └── commerce/
│           └── ebay/

flight-matrix/src/scraper/                  应用薄层
├── sinks/
│   ├── fr24_map_sink.py                    → aircraft_realtime_positions
│   ├── jetphotos_sink.py                   → aircraft_static_info.images_downloaded + S3
│   ├── fr24_aircraft_sink.py               → aircraft_static_info
│   ├── fr24_airport_sink.py                → arrival / departure 表
│   └── airport_data_sink.py                → 机场元数据表
├── sources/                                 async,保留
├── task_scheduler.py                        async,保留
├── xiaohongshu_cycle_scheduler.py           async,保留
└── reextractor.py                           基于 Pydantic 结果重跑,保留

flight-matrix/src/scraper_main.py            组装入口:YAMLConfig → Worker + sinks 注入
```

### 删除清单(主仓)

- `src/scraper/base.py`
- `src/scraper/browser_pool.py`
- `src/scraper/task_queue.py`
- `src/scraper/worker.py`
- `src/scraper/models.py`
- `src/scraper/local_task_provider.py`
- `src/scraper/local_task_source.py`
- `src/scraper/scrapers/` 整个目录
- `src/scraper/extractors/` 整个目录(scraper 直接返回结构化结果,不再需要中间 HTML 解析层)

## 框架差异矩阵(步骤 1 产出)

### 基类对比

**结论:`ResilientScraper` 是 `BaseScraper` 的纯超集,没有能力缺失。**

| 能力 | `BaseScraper` | `ResilientScraper` | 合并策略 |
|---|---|---|---|
| `task_type` / `default_delay` / `requires_browser` / `cloudflare_protected` / `task_timeout` | ✓ | ✓ | 完全一致 |
| `setup` / `teardown` / `on_success` / `on_failure` / `should_retry` | ✓ 空实现 | ✓ `setup()` 还会基于 config 初始化 S3 + DB engine | 用 submodule 版本 |
| `_dismiss_cookie_consent` / `handle_cloudflare` / `_save_cloudflare_screenshot` | ✓ | ✓ 代码逐行相同 | 保留 submodule 版 |
| `_init_s3_client` / `_init_db_engine` helper | ✓ | ✗(已被 `setup()` 内联) | **不迁移**,语义已等效 |
| 登录检测 / Cookie 持久化 / Scroll-to-load / Modal 处理 / S3 上传 / 调试文件保存 / Worker 回调 | ✗ | ✓ | submodule 独有,全部保留 |

合并动作:把 `scrape(task: Any, browser)` 的类型注解改回 `scrape(task: ScraperTask, browser)`,其余零改动。

### errors 对比

| 类 | 主仓 | submodule |
|---|---|---|
| `ScraperError` / `CloudflareBlockedError` / `PageLoadError` / `NoDataFoundError` | ✓ 构造签名完全一致 | ✓ |
| `LoginRequiredError` / `BrowserDisconnectedError` | ✗ | ✓ |

**合并动作:零行代码调整,主仓 `from src.scraper.base import ScraperError, ...` 改为
`from resilient_scraper.errors import ...`。**

### ScraperTask / ScraperResult 对比

| 字段/能力 | 主仓 | submodule | 合并策略 |
|---|---|---|---|
| `ScraperTask` 14 字段 | ✓ | ✓ 完全相同 | 直接对齐 |
| `TaskStatus` | 6 个 | 8 个(多 `LOGIN_REQUIRED` / `CANCELLED`) | 采用 submodule |
| `ScraperResult` 7 字段 | ✓ | ✓ 完全相同 | 直接对齐 |
| `WorkerStatus` / `WorkerInfo` / `ScraperConfig` | ✓ | ✗ | **下沉到 submodule** |
| 领域结果类(`JetPhotosResult` / `FR24*Result` / `FR24MapAircraftData` / `FlightData` / `ImageMetadata` / `AirportData*`) | ✓ | ✗ | 随 scraper 一起搬到 `scrapers/aviation/*/models.py` |

### scraper_tasks 表结构对比

**结论:表的 16 列完全一致,合并唯一的行为变更是查重方式。**

| 项 | 主仓 | submodule | 合并策略 |
|---|---|---|---|
| 表列 | 16 列 | 16 列完全相同 | 一致 |
| UNIQUE 去重 | 应用层 SELECT 再 INSERT(有竞态) | DB 层部分唯一索引 `idx_scraper_tasks_type_key_active` + `ON CONFLICT DO NOTHING` | 采用 submodule 方案 |
| 辅助表 | `scraper_workers` / `scraper_results` | + `scraper_screenshots` / `scraper_user_inputs` | 加 2 张登录交互表 |

生产 migration 内容(非破坏性):

```sql
-- 1. 新增 2 张登录/SMS 交互表
CREATE TABLE IF NOT EXISTS scraper_screenshots (...);
CREATE TABLE IF NOT EXISTS scraper_user_inputs (...);

-- 2. 新增部分唯一索引以支持 ON CONFLICT
CREATE UNIQUE INDEX idx_scraper_tasks_type_key_active
  ON scraper_tasks (task_type, task_key)
  WHERE status IN ('pending', 'claimed', 'processing', 'login_required');
```

### 同步 vs 异步

| 组件 | 主仓 | submodule | 合并策略 |
|---|---|---|---|
| `TaskQueue` | sync `sessionmaker` | async `AsyncSession` | 用 submodule |
| `BrowserPool` | sync(449 行,Xvfb 管理更成熟) | async(155 行) | 用 submodule,必要时回灌主仓的 Xvfb 逻辑 |
| `Worker` | sync + signal + 多线程心跳 | async + `asyncio.gather` | 用 submodule |
| scraper 的 `scrape()` | sync | worker 用 `asyncio.to_thread(scraper.scrape, ...)` 驱动 | **保持同步**,无需逐个改写 |

`scrape()` 不变这一发现,把步骤 4 的工作量从"逐文件改 async"降到了"换基类 + 换 import + 剥 DB 写入"。

## Sink 注入机制

submodule worker 已有一套回调机制,复用它、不另起炉灶:

```python
# submodule 里已有的注入点(resilient_scraper/scraper.py)
scraper.on_login_screenshot    # 登录二维码截图入库
scraper.on_poll_user_input     # 从 DB 读取用户提交的 SMS code
scraper.on_login_success       # 登录成功 → 恢复任务
scraper.on_page_screenshot     # 调试截图入库
scraper.on_send_alert          # Feishu / 邮件告警

# flight-matrix 注入业务写入:用同一套机制扩展
scraper.on_success = flight_sink.persist        # 写自家表
scraper.on_failure = flight_sink.mark_failed
```

**关键约束:submodule 不 import 任何 flight 模型**。sink 里的表建表语句 /
UPSERT SQL / `aircraft_static_info.images_downloaded` 标记更新,全部留在
flight-matrix 的 `sinks/`。

## 执行路线(7 步)

| # | 阶段 | 仓 | 产物 | 风险 / 注意 |
|---|---|---|---|---|
| 1 | **框架对齐** ✅ | submodule | 差异矩阵,决定 "保留 submodule 版,主仓仅向 submodule 加 WorkerStatus/WorkerInfo/ScraperConfig" | 已完成 |
| 2 | **errors 合并** ✅ | flight-matrix | 主仓 `from resilient_scraper.errors import ...` 替换 | 已完成:主仓 `src/scraper/` 内不再直接 import 这些错误类 |
| 3 | **models 合并** ✅ | submodule + flight-matrix | `TaskStatus` 采 submodule 全量版,`WorkerStatus/WorkerInfo/ScraperConfig` 下沉到 submodule;主仓改 import | 已完成:`src/scraper/models.py` 精简为纯重导出;9 个领域结果类删除,所有 sink/tests 已从 `resilient_scraper.scrapers.aviation.*.models` 拉。 |
| 4 | **迁 5 个航空 scraper** ✅ | submodule | `airport_data` / `jetphotos` / `fr24_{map,airport,aircraft}` 搬入 `scrapers/aviation/`;基类换 `ResilientScraper`;剥 DB 建表/写表逻辑;领域结果类挪到对应 `models.py`;jetphotos 的 `_download_image` 可继续用同步 `requests`(worker 里 to_thread) | 已完成:主仓 `scrapers/` 目录已删除;所有 sink 从 `resilient_scraper.scrapers.aviation.*` 拉领域 model。额外落地了 `adsbx_map`(计划外)。 |
| 5 | **flight-matrix 写 5 个 sink** ✅ | flight-matrix | 吸收现有 `on_success` / `_save_positions_to_db` / `_ensure_table_exists` / `images_downloaded` 更新逻辑 | 已完成,且超出计划:`src/scraper/sinks/` 有 8 个 sink(计划 5 个 + `fr24_airport_api_sink`、`adsbx_map_sink`、`adsbx_snapshots_sink`)。 |
| 6 | **框架删除 + 入口重写** ⚠️ 半完成 | flight-matrix | `scraper_main.py` 切到 submodule `Worker` + sinks 注入;删除 `base/queue/browser_pool/worker/models/scrapers/extractors`;`sources/` + `task_scheduler.py` + `xiaohongshu_cycle_scheduler.py` 改 async 调用 | 已删除:`base.py`、`browser_pool.py`、`worker.py`、`scrapers/`、`extractors/`、`local_task_provider.py`。**待办**:`local_task_source.py`(计划要求删除,仍在);`models.py` 依赖步骤 3 的清理。 |
| 7 | **tests + scripts + docs + 生产 migration** 🚧 进行中 | 两仓 | 测试文件重写 import;`scripts/seed-tasks.py` + `scripts/reextract_fields.py` + `scripts/demo.sh` 对齐;两仓 `CLAUDE.md` / `docs/scraping.md` / `docs/architecture.md` 更新;生产执行 DB migration;12 个 task_type 冒烟 | 主仓 docs 已更新(`docs/architecture.md`、`docs/scraping.md`、`src/scraper/CLAUDE.md`)。scripts 已核对:`seed-tasks.py` 走 `src.scraper.task_queue.TaskQueue`(未动)、`reextract_fields.py` 走 `src.scraper.reextractor` 且后者从 `resilient_scraper.scrapers.aviation.*.extractor` 拉 extractor(已对齐)、`demo.sh` 只调用 db_manager/sqlite/`start-all.sh`(已对齐)。tests 已改从 `resilient_scraper.scrapers.aviation.*` import 领域 model。**未核实**:生产 DB migration(2 张登录/SMS 交互表 + `idx_scraper_tasks_type_key_active` 唯一索引)是否已在生产库执行。 |

_(状态审计时间:2026-08-23。步骤 2/3/4/5 已完成;6 尚有 `local_task_source.py` 未删,依赖将 `sources/` 一并搬到 submodule;7 进行中,scripts/tests 已对齐,生产 DB migration 待核实。)_

## 关键约束

### Xvfb + 非 headless(贯穿所有步骤)

主仓 `src/scraper/CLAUDE.md` 强调:所有 Cloudflare-protected scraper 必须跑在
`Xvfb :55` + `headless=false` 下。submodule 的 `service/browser_pool.py` 必须:

- 默认 `headless=False`;
- 启动前由 worker 调 `_start_xvfb()`;
- 不允许在测试或开发环境里悄悄把 `headless=True` 传进来。

所有 scraper 的迁移验证都要在 Xvfb 环境下跑。

### submodule 是独立 GitHub 仓

`.gitmodules` 指向 `outstandingcandy/resilient-scraper`。每个步骤都要:

1. 在 submodule 仓里单独提交 + PR + tag;
2. 在 flight-matrix 里更新 submodule 指针。

建议每个步骤一次 submodule release,flight-matrix 最后一次统一 bump。

### 生产停机窗口

步骤 6 的入口切换 + 步骤 7 的 DB migration 会短暂停 scraper worker。需要在
切换前约定停机窗口。

## 12 个 task_type 现状

| task_type | 当前位置 | 反爬 | 登录 | 业务 DB 写入 |
|---|---|---|---|---|
| `airport_data` | 主仓 | 否 | 否 | `aircraft_static_info` |
| `jetphotos` | 主仓 | Cloudflare | 否 | `aircraft_static_info.images_downloaded` + S3 |
| `fr24_aircraft` | 主仓 | Cloudflare | 否 | `aircraft_static_info` |
| `fr24_map` | 主仓 | Cloudflare | 否 | `aircraft_realtime_positions` |
| `fr24_airport` | 主仓 | Cloudflare | 否 | 机场元数据表 |
| `fr24_arrivals` | 主仓(与 `fr24_airport` 同文件) | Cloudflare | 否 | arrival 表 |
| `fr24_departures` | 主仓(同上) | Cloudflare | 否 | departure 表 |
| `planespotters` | submodule | Cloudflare | 否 | 自管 |
| `xiaohongshu` | submodule | 否 | **是(QR)** | 自管 |
| `xiaohongshu_following` | submodule | 否 | 是 | 自管 |
| `xiaohongshu_search_author` | submodule | 否 | 是 | 自管 |
| `ebay_store` | submodule | 否 | 否 | 自管 |

迁移后全部落在 submodule `scrapers/` 下,flight 业务写入走 flight-matrix 的 sink。
