# Flight Matrix 数据库文档

本文档记录项目中所有数据库表的结构和用途。

## 概览

项目使用 **PostgreSQL** (Aurora Serverless) 作为生产数据库。表分为两类：
- **SQLAlchemy ORM 模型**: 定义在 `src/data/models.py`
- **Raw SQL 表**: 在各模块中通过 `CREATE TABLE` 动态创建

---

## 1. 飞行追踪核心表

### aircraft_snapshots
ADS-B 实时位置快照，记录每次追踪周期捕获的飞机位置。

| 字段 | 类型 | 说明 |
|------|------|------|
| id | BIGSERIAL | 主键 |
| hex_code | VARCHAR(10) | ICAO 24位地址 (Mode S) |
| registration | VARCHAR(20) | 注册号 |
| callsign | VARCHAR(20) | 呼号 |
| aircraft_type | VARCHAR(20) | 飞机型号 (ICAO) |
| latitude | NUMERIC(10,6) | 纬度 |
| longitude | NUMERIC(10,6) | 经度 |
| altitude | INTEGER | 高度 (ft) |
| speed | INTEGER | 地速 (knots) |
| heading | INTEGER | 航向 (度) |
| vertical_rate | INTEGER | 垂直速率 (ft/min) |
| squawk | VARCHAR(10) | 应答机代码 |
| is_military | BOOLEAN | 是否军机 |
| snapshot_time | TIMESTAMP | 快照时间 |

**索引**: `idx_snapshot_time`, `idx_hex_time`, `idx_recent_military`, `idx_location`

---

### aircraft_static_info
飞机静态信息缓存，减少重复 API 调用。

| 字段 | 类型 | 说明 |
|------|------|------|
| id | BIGSERIAL | 主键 |
| hex_code | VARCHAR(10) | ICAO 地址 (唯一) |
| registration | VARCHAR(20) | 注册号 |
| aircraft_type | VARCHAR(50) | 飞机型号 |
| manufacturer | VARCHAR(100) | 制造商 |
| owner | VARCHAR(200) | 所有者 |
| operator | VARCHAR(200) | 运营商 |
| country | VARCHAR(100) | 注册国家 |
| image_url | VARCHAR(500) | 图片 URL |
| summary | TEXT | AI 生成的摘要 |
| data_source | VARCHAR(50) | 数据来源 |
| freshness_score | NUMERIC(5,2) | 数据新鲜度 |
| created_at | TIMESTAMP | 创建时间 |
| updated_at | TIMESTAMP | 更新时间 |

---

### aircraft_realtime_positions
FR24 地图实时位置数据 (由 `fr24_map` 抓取器填充)。

| 字段 | 类型 | 说明 |
|------|------|------|
| id | BIGSERIAL | 主键 |
| hex_code | VARCHAR(10) | ICAO 地址 |
| registration | VARCHAR(20) | 注册号 |
| callsign | VARCHAR(20) | 呼号 |
| aircraft_type | VARCHAR(20) | 飞机型号 |
| latitude | NUMERIC(10,6) | 纬度 |
| longitude | NUMERIC(10,6) | 经度 |
| altitude | INTEGER | 高度 |
| speed | INTEGER | 地速 |
| heading | INTEGER | 航向 |
| origin | VARCHAR(10) | 出发机场 |
| destination | VARCHAR(10) | 目的机场 |
| flight_number | VARCHAR(20) | 航班号 |
| fr24_id | VARCHAR(20) | FR24 唯一 ID |
| scraped_at | TIMESTAMP | 抓取时间 |

**唯一约束**: `(hex_code, scraped_at)`

---

### aircraft_images
飞机图片元数据。

| 字段 | 类型 | 说明 |
|------|------|------|
| id | BIGSERIAL | 主键 |
| registration | VARCHAR(20) | 注册号 |
| hex_code | VARCHAR(10) | ICAO 地址 |
| source | VARCHAR(50) | 来源 (jetphotos/planespotters) |
| image_url | VARCHAR(500) | 原始 URL |
| s3_key | VARCHAR(255) | 对象存储路径 (S3/GCS/本地) |
| photographer | VARCHAR(200) | 摄影师 |
| location | VARCHAR(200) | 拍摄地点 |
| airport_icao | VARCHAR(4) | 从 `location` 解析出的机场 ICAO(用于"同机场同机型"回退) |
| photo_date | DATE | 拍摄日期 |
| created_at | TIMESTAMP | 创建时间 |

**索引**: `idx_image_airport`, `idx_image_airport_reg`

---

### aircraft_attention_aggregate
飞机关注度聚合统计 (由 `note_analysis_service` 从小红书笔记分析结果聚合)。

| 字段 | 类型 | 说明 |
|------|------|------|
| id | BIGSERIAL | 主键 |
| registration | VARCHAR | 注册号 (唯一) |
| total_mentions | INTEGER | 总提及次数 |
| avg_attention_index | NUMERIC | 平均关注指数 |
| max_attention_index | INTEGER | 最高关注指数 |
| mentions_7d | INTEGER | 近7天提及次数 |
| mentions_30d | INTEGER | 近30天提及次数 |
| first_seen | TIMESTAMP | 首次出现时间 |
| last_seen | TIMESTAMP | 最近出现时间 |
| top_topics | JSON | 热门话题 |
| sentiment_distribution | JSON | 情感分布 |
| source_distribution | JSON | 来源分布 |
| content_type_distribution | JSON | 内容类型分布 |
| trending_score | NUMERIC | 热度分数 (用于排序) |
| created_at | TIMESTAMP | 创建时间 |
| updated_at | TIMESTAMP | 更新时间 |

**注意**: `registration` 支持德国军机格式 (如 `10+01`, `14+02`)。

---

## 2. 机场与航班表

### airports
机场基础数据。

| 字段 | 类型 | 说明 |
|------|------|------|
| id | BIGSERIAL | 主键 |
| iata_code | VARCHAR(3) | IATA 代码 |
| icao_code | VARCHAR(4) | ICAO 代码 |
| name | VARCHAR(200) | 机场名称 |
| city | VARCHAR(100) | 城市 |
| country | VARCHAR(100) | 国家 |
| latitude | NUMERIC(10,6) | 纬度 |
| longitude | NUMERIC(10,6) | 经度 |
| timezone | VARCHAR(50) | 时区 |
| priority | INTEGER | 抓取优先级 |

---

### flight_schedules
航班时刻表 (从 FR24 抓取)。数据来源：
- `fr24_airport` 抓取器: `flight_type = 'arrival'` 或 `'departure'`
- `fr24_aircraft` 抓取器: `flight_type = 'aircraft_schedule'`

| 字段 | 类型 | 说明 |
|------|------|------|
| id | BIGSERIAL | 主键 |
| flight_type | VARCHAR | 类型: arrival/departure/aircraft_schedule |
| airport_icao | VARCHAR | 机场 ICAO 代码 |
| airport_iata | VARCHAR | 机场 IATA 代码 |
| flight_number | VARCHAR | 航班号 |
| callsign | VARCHAR | 呼号 |
| fr24_flight_id | VARCHAR | FR24 航班 ID (去重键) |
| airline_name | VARCHAR | 航空公司名称 |
| airline_iata | VARCHAR | 航空公司 IATA 代码 |
| remote_airport_iata | VARCHAR | 对端机场 IATA 代码 |
| remote_airport_name | VARCHAR | 对端机场名称 |
| aircraft_type | VARCHAR | 飞机型号 |
| aircraft_registration | VARCHAR | 飞机注册号 |
| scheduled_time | TIMESTAMP | 计划时间 |
| estimated_time | TIMESTAMP | 预计时间 |
| actual_time | TIMESTAMP | 实际时间 |
| status | VARCHAR | 航班状态 |
| terminal | VARCHAR | 航站楼 |
| gate | VARCHAR | 登机口 |
| scraped_at | TIMESTAMP | 抓取时间 |

**唯一约束**: `(fr24_flight_id, DATE(scheduled_time), flight_type)`

---

### geographic_regions
地理区域定义，用于区域扫描。

| 字段 | 类型 | 说明 |
|------|------|------|
| id | BIGSERIAL | 主键 |
| name | VARCHAR(100) | 区域名称 |
| min_lat | NUMERIC(10,6) | 最小纬度 |
| max_lat | NUMERIC(10,6) | 最大纬度 |
| min_lon | NUMERIC(10,6) | 最小经度 |
| max_lon | NUMERIC(10,6) | 最大经度 |

---

## 3. 多用户系统表

### users
用户账户。

| 字段 | 类型 | 说明 |
|------|------|------|
| id | BIGSERIAL | 主键 |
| email | VARCHAR(255) | 邮箱 (唯一) |
| name | VARCHAR(100) | 显示名称 |
| status | VARCHAR(20) | 状态 (active/suspended/deleted) |
| api_key | VARCHAR(64) | API 密钥 |
| created_at | TIMESTAMP | 创建时间 |
| updated_at | TIMESTAMP | 更新时间 |

---

### subscriptions
用户订阅。

| 字段 | 类型 | 说明 |
|------|------|------|
| id | BIGSERIAL | 主键 |
| user_id | BIGINT | 用户 ID (外键) |
| tier | VARCHAR(20) | 订阅等级 (basic/premium/enterprise) |
| status | VARCHAR(20) | 状态 (active/expired/cancelled) |
| enable_maps | BOOLEAN | 是否启用地图 |
| enable_aircraft_images | BOOLEAN | 是否启用飞机图片 |
| enable_deep_analysis | BOOLEAN | 是否启用深度分析 |
| cooldown_hours | NUMERIC(6,2) | 报告冷却时间 |
| daily_report_limit | INTEGER | 每日报告上限 |
| monthly_report_limit | INTEGER | 每月报告上限 |
| max_filters | INTEGER | 最大过滤器数量 |
| starts_at | TIMESTAMP | 开始时间 |
| expires_at | TIMESTAMP | 过期时间 |
| created_at | TIMESTAMP | 创建时间 |

---

### user_filters
用户自定义 SQL 过滤规则。

| 字段 | 类型 | 说明 |
|------|------|------|
| id | BIGSERIAL | 主键 |
| user_id | BIGINT | 用户 ID (外键) |
| name | VARCHAR(100) | 过滤器名称 |
| description | TEXT | 描述 |
| filter_sql | TEXT | SQL WHERE 子句 |
| is_active | BOOLEAN | 是否启用 |
| priority | INTEGER | 优先级 |
| created_at | TIMESTAMP | 创建时间 |
| updated_at | TIMESTAMP | 更新时间 |

---

### user_cooldowns
用户报告冷却时间追踪。

| 字段 | 类型 | 说明 |
|------|------|------|
| id | BIGSERIAL | 主键 |
| user_id | BIGINT | 用户 ID (外键) |
| hex_code | VARCHAR(10) | ICAO 地址 |
| last_reported_at | TIMESTAMP | 最后报告时间 |
| report_count | INTEGER | 报告次数 |

---

### user_usage
用户使用量统计。

| 字段 | 类型 | 说明 |
|------|------|------|
| id | BIGSERIAL | 主键 |
| user_id | BIGINT | 用户 ID (外键) |
| date | DATE | 日期 |
| reports_sent | INTEGER | 发送报告数 |
| api_calls | INTEGER | API 调用数 |
| aircraft_tracked | INTEGER | 追踪飞机数 |

---

### report_cooldowns
全局报告冷却记录 (遗留表)。

| 字段 | 类型 | 说明 |
|------|------|------|
| id | BIGSERIAL | 主键 |
| hex_code | VARCHAR(10) | ICAO 地址 |
| last_reported | TIMESTAMP | 最后报告时间 |

---

## 4. 抓取器框架表

### scraper_tasks
分布式任务队列。

| 字段 | 类型 | 说明 |
|------|------|------|
| id | BIGSERIAL | 主键 |
| task_type | VARCHAR(50) | 任务类型 (fr24_airport/fr24_aircraft/jetphotos/xiaohongshu 等) |
| task_key | VARCHAR(255) | 任务键 (航班号/注册号/用户ID) |
| status | VARCHAR(20) | 状态 (pending/processing/completed/failed) |
| priority | INTEGER | 优先级 (高优先) |
| payload | JSONB | 任务参数 |
| claimed_by | VARCHAR(100) | 处理 Worker ID |
| claimed_at | TIMESTAMP | 领取时间 |
| attempts | INTEGER | 尝试次数 |
| max_attempts | INTEGER | 最大尝试次数 |
| last_error | TEXT | 最后错误信息 |
| result | JSONB | 任务结果 |
| scheduled_for | TIMESTAMP | 计划执行时间 |
| created_at | TIMESTAMP | 创建时间 |
| completed_at | TIMESTAMP | 完成时间 |

**索引**: `idx_scraper_tasks_status`, `idx_scraper_tasks_type_key`, `idx_scraper_tasks_scheduled`, `idx_scraper_tasks_priority`

---

### scraper_workers
Worker 注册与心跳。

| 字段 | 类型 | 说明 |
|------|------|------|
| id | BIGSERIAL | 主键 |
| worker_id | VARCHAR(100) | Worker 唯一 ID |
| status | VARCHAR(20) | 状态 (active/inactive) |
| last_heartbeat | TIMESTAMP | 最后心跳时间 |
| tasks_completed | INTEGER | 已完成任务数 |
| current_task_id | BIGINT | 当前任务 ID |
| metadata | JSONB | 元数据 |

---

### scraper_results
抓取执行日志。

| 字段 | 类型 | 说明 |
|------|------|------|
| id | BIGSERIAL | 主键 |
| task_id | BIGINT | 任务 ID (外键) |
| worker_id | VARCHAR(100) | Worker ID |
| success | BOOLEAN | 是否成功 |
| duration_seconds | NUMERIC(10,3) | 执行时长 |
| result | JSONB | 结果数据 |
| error | TEXT | 错误信息 |
| created_at | TIMESTAMP | 创建时间 |

---

## 5. 小红书表

### xiaohongshu_authors
小红书作者信息。

| 字段 | 类型 | 说明 |
|------|------|------|
| id | BIGSERIAL | 主键 |
| user_id | VARCHAR(50) | 用户 ID (唯一) |
| nickname | VARCHAR(100) | 昵称 |
| avatar_url | VARCHAR(500) | 头像 URL |
| description | TEXT | 简介 |
| follower_count | INTEGER | 粉丝数 |
| following_count | INTEGER | 关注数 |
| note_count | INTEGER | 笔记数 |
| verified | BOOLEAN | 是否认证 |
| scraped_at | TIMESTAMP | 抓取时间 |
| updated_at | TIMESTAMP | 更新时间 |

---

### xiaohongshu_notes
小红书笔记内容。

| 字段 | 类型 | 说明 |
|------|------|------|
| id | BIGSERIAL | 主键 |
| note_id | VARCHAR(50) | 笔记 ID (唯一) |
| source_url | VARCHAR(500) | 原始 URL |
| title | VARCHAR(500) | 标题 |
| content | TEXT | 内容 |
| tags | JSONB | 标签列表 |
| location | VARCHAR(200) | 位置 |
| author_id | VARCHAR(50) | 作者 ID |
| author_name | VARCHAR(100) | 作者名称 |
| image_urls | JSONB | 图片 URL 列表 |
| image_paths | JSONB | 图片存储路径 |
| video_url | VARCHAR(500) | 视频 URL |
| like_count | INTEGER | 点赞数 |
| collect_count | INTEGER | 收藏数 |
| comment_count | INTEGER | 评论数 |
| share_count | INTEGER | 分享数 |
| comments | JSONB | 评论内容 |
| note_created_at | TIMESTAMP | 笔记创建时间 |
| scraped_at | TIMESTAMP | 抓取时间 |
| updated_at | TIMESTAMP | 更新时间 |

**索引**: `idx_xhs_notes_author`, `idx_xhs_notes_created`

---

### xiaohongshu_following
小红书关注关系。

| 字段 | 类型 | 说明 |
|------|------|------|
| id | BIGSERIAL | 主键 |
| follower_user_id | VARCHAR(50) | 关注者 ID |
| following_user_id | VARCHAR(50) | 被关注者 ID |
| following_nickname | VARCHAR(100) | 被关注者昵称 |
| following_avatar_url | VARCHAR(500) | 被关注者头像 |
| following_red_id | VARCHAR(50) | 小红书号 |
| following_description | TEXT | 被关注者简介 |
| following_verified | BOOLEAN | 是否认证 |
| following_follower_count | INTEGER | 被关注者粉丝数 |
| following_note_count | INTEGER | 被关注者笔记数 |
| scraped_at | TIMESTAMP | 抓取时间 |
| updated_at | TIMESTAMP | 更新时间 |

**唯一约束**: `(follower_user_id, following_user_id)`
**索引**: `idx_xhs_following_follower`, `idx_xhs_following_following`

---

## 6. AI 分析表

### note_aircraft_analysis
笔记飞机分析结果。

| 字段 | 类型 | 说明 |
|------|------|------|
| id | BIGSERIAL | 主键 |
| note_id | VARCHAR(50) | 笔记 ID |
| analysis_result | JSONB | 分析结果 |
| aircraft_mentioned | JSONB | 提及的飞机列表 |
| confidence_score | NUMERIC(5,2) | 置信度 |
| analyzed_at | TIMESTAMP | 分析时间 |

---

## ER 关系图

```
users ─────┬───< subscriptions
           ├───< user_filters
           ├───< user_cooldowns
           └───< user_usage

scraper_tasks ───< scraper_results

xiaohongshu_authors ───< xiaohongshu_notes
                    ───< xiaohongshu_following (as follower)
                    ───< xiaohongshu_following (as following)

aircraft_static_info ───< aircraft_images
                     ───< aircraft_snapshots (via hex_code)
```

---

## 维护说明

### 表创建位置

| 表 | 创建位置 |
|---|---------|
| users, subscriptions, user_* | `src/data/models.py` (SQLAlchemy) |
| aircraft_* | `src/data/models.py` + `src/utils/database.py` |
| scraper_* | `src/scraper/task_queue.py` |
| xiaohongshu_* | `lib/resilient-scraper/` submodule (原 `src/scraper/scrapers/xiaohongshu.py` 已下沉) |

### 索引优化建议

1. **高频查询表** (`aircraft_snapshots`, `scraper_tasks`): 已有完善索引
2. **小红书表**: 考虑为 `like_count`, `collect_count` 添加索引以支持排序
3. **用户表**: `user_id` 外键已自动索引

---

*文档更新时间: 2026-03-07*
