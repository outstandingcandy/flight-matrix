# Flight-Matrix 飞机追踪系统

一个功能完整的实时飞机监控和追踪系统，支持智能过滤、AI分析、邮件通知和地图可视化。

## 项目概述

Flight-Matrix 是一个基于 Python + Flask 的飞机实时追踪系统，通过 ADS-B Exchange API 获取全球飞机数据，支持自定义 SQL 过滤规则、地理位置反向查询、AI 智能分析和多种通知方式。

### 核心特性

- **实时数据采集**: 通过 ADS-B Exchange API 获取全球飞机实时位置
- **智能过滤系统**: 基于 SQL 的灵活过滤规则，支持复杂逻辑组合
- **AI 分析**: 集成 Claude/GPT 进行飞行情报分析
- **地图可视化**: 交互式地图展示和航迹回放
- **邮件通知**: 支持 SMTP 和 AWS SES，包含地图和 AI 分析报告
- **Web 界面**: 实时查询、航迹展示、统计分析
- **数据持久化**: SQLAlchemy ORM，支持 SQLite/MySQL/PostgreSQL

## 技术栈

### 后端技术

| 技术 | 版本/说明 | 用途 |
|------|----------|------|
| Python | 3.7+ | 核心语言 |
| Flask | 最新 | Web 框架 |
| SQLAlchemy | 最新 | ORM 数据库 |
| PyYAML | 最新 | 配置管理 |
| Plotly + Kaleido | 最新 | 地图生成 |
| Anthropic Claude | API | AI 分析 |
| Tavily | API | AI 搜索 |
| boto3 | 最新 | AWS SES |

### 前端技术

- HTML5 + CSS3 + JavaScript (ES6+)
- Bootstrap 5 (UI 框架)
- Leaflet (地图库)
- Plotly (数据可视化)

## 项目结构

```
flight-matrix/
├── config.yaml                   # 配置文件
├── config_template.yaml          # 配置模板
├── web_app.py                    # Flask Web 应用入口
├── src/
│   ├── main.py                   # CLI 入口 (向后兼容)
│   ├── track_main.py             # Track 服务入口
│   ├── report_main.py            # Report 服务入口
│   ├── image_main.py             # Image 服务入口
│   ├── services/
│   │   ├── track_service.py      # 数据采集服务
│   │   ├── report_service.py     # 报告生成服务
│   │   └── image_service.py      # 图片下载服务
│   └── utils/
│       ├── database.py           # 数据库管理 (SQLAlchemy ORM)
│       ├── yaml_config.py        # YAML 配置管理
│       ├── sql_filter.py         # SQL 过滤引擎
│       ├── email_notifier.py     # SMTP 邮件通知
│       ├── aws_email_notifier.py # AWS SES 邮件通知
│       ├── geo_locator.py        # 地理位置反向查询
│       ├── map_generator.py      # 地图生成工具
│       └── flight_analysis_agent_production.py  # AI 分析代理
├── web_templates/
│   └── index.html                # 主页模板
└── web_static/
    ├── css/
    │   └── style.css             # 样式表
    └── js/
        └── app.js                # 前端应用
```

## 快速开始

### 1. 安装依赖

```bash
# 必需依赖
pip install flask flask-cors sqlalchemy pyyaml requests python-dotenv

# 数据处理
pip install reverse_geocoder

# 邮件和 AWS
pip install boto3

# 地图生成
pip install plotly kaleido numpy

# AI 分析 (可选)
pip install anthropic tavily-python
```

### 2. 配置系统

```bash
# 复制配置模板
cp config_template.yaml config.yaml

# 设置环境变量
export ADSB_API_KEY="your_adsb_api_key"
export ANTHROPIC_API_KEY="sk-ant-..."
export TAVILY_API_KEY="tvly-..."
export AWS_REGION="us-east-1"
export AWS_ACCESS_KEY_ID="..."
export AWS_SECRET_ACCESS_KEY="..."
export RECIPIENT_EMAIL_1="user@example.com"
```

### 3. 编辑配置文件

编辑 `config.yaml` 文件，配置以下关键部分：

#### 3.1 数据库配置

```yaml
database:
  url: "sqlite:///aircraft_data.db"    # 或使用 MySQL/PostgreSQL
  cleanup_interval_hours: 24           # 数据保留时间
```

#### 3.2 API 配置

```yaml
api:
  adsb_api_key: "${ADSB_API_KEY}"     # 必需
  update_interval: 300                 # 更新间隔(秒)
```

#### 3.3 召回配置 (数据来源)

```yaml
recall:
  sources:
    specific_registrations:            # 特定注册号
      - "YU-RSB"
      - "86-0022"
    military_global: true              # 全球军用飞机
    aircraft_types:                    # 特定机型
      - "B742"
      - "IL76"
    regional_scan:                     # 区域扫描
      - "United States"
      - "China"
```

#### 3.4 过滤配置 (关键业务逻辑)

```yaml
filters:
  mode: "custom_sql"
  custom_sql: |
    (is_military = 1 AND current_country = 'China') OR
    aircraft_type IN ('B742', 'AN124') OR
    registration IN ('YU-RSB', '86-0022', 'N757AF')
```

#### 3.5 邮件通知配置

```yaml
email:
  provider: "aws_ses"                  # smtp | aws_ses

  # AWS SES 配置
  aws_ses:
    region: "${AWS_REGION}"
    sender: "${AWS_SES_SENDER_EMAIL}"
    access_key_id: "${AWS_ACCESS_KEY_ID}"
    secret_access_key: "${AWS_SECRET_ACCESS_KEY}"

  recipients:
    - "${RECIPIENT_EMAIL_1}"

  features:
    enable_maps: true                  # 包含地图
    enable_aircraft_images: false      # 包含飞机图片
    enable_flight_analysis: true       # AI 分析
    enable_tavily_search: true         # 网络搜索
```

### 4. 运行应用

系统由三个独立服务组成，可以分别启动：

#### 服务架构

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│  Track Service  │     │ Report Service  │     │  Image Service  │
│   (数据采集)     │     │   (报告生成)     │     │   (图片下载)    │
│                 │     │                 │     │                 │
│  API → 数据库   │     │ 数据库 → 邮件   │     │ 数据库 → 图片   │
└─────────────────┘     └─────────────────┘     └─────────────────┘
```

#### 启动 Track Service (数据采集)

从 ADS-B Exchange API 获取飞机数据并存储到数据库。

```bash
# 持续运行
python -m src.track_main --config config.yaml

# 运行一次后退出
python -m src.track_main --config config.yaml --once

# 查看状态
python -m src.track_main --config config.yaml --status
```

#### 启动 Report Service (报告生成)

从数据库过滤飞机并生成邮件报告。

```bash
# 持续运行
python -m src.report_main --config config.yaml

# 运行一次后退出
python -m src.report_main --config config.yaml --once

# 查看状态
python -m src.report_main --config config.yaml --status

# 清理旧的冷却记录
python -m src.report_main --config config.yaml --cleanup-cooldowns
```

#### 启动 Image Service (图片下载)

从 JetPhotos 下载飞机图片。需要 Xvfb 支持无头浏览器。

```bash
# 启动 Xvfb (首次运行)
Xvfb :55 -screen 0 1920x1080x24 &

# 持续运行
DISPLAY=:55 python -m src.image_main --config config.yaml

# 运行一次后退出
DISPLAY=:55 python -m src.image_main --config config.yaml --once --limit 10

# 查看状态
python -m src.image_main --config config.yaml --status
```

#### 后台运行 (生产环境)

```bash
# 使用 nohup 后台运行各服务
nohup python -m src.track_main --config config.yaml > track.log 2>&1 &
nohup python -m src.report_main --config config.yaml > report.log 2>&1 &
DISPLAY=:55 nohup python -m src.image_main --config config.yaml > image.log 2>&1 &
```

#### 启动 Web 服务器

```bash
python web_app.py
# 访问: http://localhost:5000
```

## 核心功能详解

### 1. 服务数据流

系统采用三服务解耦架构，各服务独立运行：

```
┌─────────────────────────────────────────────────────────────────┐
│                        Track Service                            │
│  ADS-B API → 数据处理 → 地理编码 → 属性判断 → aircraft_snapshots │
└─────────────────────────────────────────────────────────────────┘
                                ↓
                        aircraft_snapshots 表
                          ↙            ↘
┌──────────────────────────┐    ┌──────────────────────────┐
│     Report Service       │    │     Image Service        │
│                          │    │                          │
│ SQL 过滤 → 冷却检查      │    │ 查询待下载 → JetPhotos   │
│     ↓                    │    │     ↓                    │
│ AI 分析 → 地图生成       │    │ 下载图片 → 更新数据库    │
│     ↓                    │    │                          │
│ 邮件发送 → 更新冷却      │    │                          │
└──────────────────────────┘    └──────────────────────────┘
```

**Track Service 流程:**
1. API 轮询 (每 5 分钟)
2. 数据处理与验证
3. 地理编码 (批量坐标反向查询)
4. 属性判断 (军用/值得关注)
5. 批量存储到数据库

**Report Service 流程:**
1. 从 aircraft_snapshots 表读取数据
2. 应用 config.yaml 中的 custom_sql 过滤
3. 检查报告冷却 (避免重复报告)
4. 生成 AI 分析和地图
5. 发送邮件通知
6. 更新冷却记录

**Image Service 流程:**
1. 查询未下载图片的注册号
2. 访问 JetPhotos 搜索图片
3. 下载并去重图片
4. 更新数据库图片路径

### 2. 数据库结构

#### AircraftSnapshot 表

| 字段 | 类型 | 说明 |
|------|------|------|
| `hex` | String(6) | ICAO 24-bit hex 代码 |
| `flight_number` | String(10) | 航班号 |
| `registration` | String(20) | 注册号 (N号、G号等) |
| `aircraft_type` | String(10) | 机型代码 (B737、C17等) |
| `latitude` | Numeric(10,7) | 纬度 |
| `longitude` | Numeric(11,7) | 经度 |
| `altitude_baro` | Integer | 气压高度 (feet) |
| `ground_speed` | Numeric(6,2) | 地速 (knots) |
| `track` | Numeric(5,2) | 航向 (degrees) |
| `current_country` | String(50) | 当前所在国家 |
| `country_of_registration` | String(50) | 注册国 |
| `is_military` | Boolean | 军用飞机标志 |
| `is_interesting` | Boolean | 值得关注标志 |
| `snapshot_time` | DateTime | 快照时间 |
| `raw_data` | JSON | ADS-B API 原始数据 |

**关键索引**:
- `idx_snapshot_time`: 时间查询优化
- `idx_location`: 地理位置查询
- `idx_recent_military`: 最近军用飞机查询
- `idx_hex_time`: 特定飞机时间序列查询

### 3. SQL 过滤系统

#### 支持的 SQL 操作符

| 操作符 | 示例 |
|--------|------|
| `=`, `!=`, `<>` | `is_military = 1` |
| `>`, `<`, `>=`, `<=` | `altitude_baro > 30000` |
| `IN` | `aircraft_type IN ('B742', 'C17')` |
| `LIKE` | `registration LIKE 'N%'` |
| `AND`, `OR` | `is_military = 1 AND altitude > 20000` |
| `BETWEEN` | `ground_speed BETWEEN 300 AND 500` |

#### 过滤配置示例

**示例 1: 军用飞机过滤**
```yaml
filters:
  custom_sql: "is_military = 1"
```

**示例 2: 特定注册号过滤**
```yaml
filters:
  custom_sql: "registration IN ('N123AB', '86-0022', 'YU-RSB')"
```

**示例 3: 复杂综合过滤**
```yaml
filters:
  custom_sql: |
    (is_military = 1 AND current_country IN ('China', 'Russia')) OR
    (aircraft_type IN ('B742', 'IL76', 'AN124') AND ground_speed > 300) OR
    (registration LIKE 'N1%' AND altitude_baro > 30000)
```

### 4. Web API 接口

#### 搜索飞机

**端点**: `GET /api/aircraft/search`

**参数**:
- `registration`: 飞机注册号
- `hex`: ICAO hex 代码
- `aircraft_type`: 机型代码
- `is_military`: 军用飞机标志 (true/false)
- `start_date`: 开始时间 (北京时间)
- `end_date`: 结束时间 (北京时间)
- `limit`: 返回数量限制

**请求示例**:
```bash
curl "http://localhost:5000/api/aircraft/search?registration=N123AB&is_military=false&limit=50"
```

**响应格式**:
```json
{
  "success": true,
  "data": [
    {
      "hex": "a12345",
      "flight": "UAL123",
      "r": "N123AB",
      "t": "B737",
      "lat": 39.9042,
      "lon": 116.4074,
      "alt_baro": 35000,
      "gs": 450.5,
      "current_country": "China",
      "is_military": false,
      "timestamp": "2024-01-05 14:30:00"
    }
  ],
  "count": 1
}
```

#### 获取航迹

**端点**: `GET /api/aircraft/tracks/<registration>`

**参数**:
- `start_time`: 开始时间 (北京时间)
- `limit`: 轨迹点数量限制

**请求示例**:
```bash
curl "http://localhost:5000/api/aircraft/tracks/N123AB?limit=1000"
```

#### 其他接口

- `GET /api/aircraft/recent` - 获取最近飞机
- `GET /api/aircraft/types` - 获取机型统计
- `GET /api/aircraft/unique` - 获取唯一飞机
- `GET /api/statistics` - 获取统计信息

### 5. 邮件通知系统

#### 支持的邮件服务商

1. **SMTP** (如 163、Gmail、QQ邮箱)
2. **AWS SES** (生产环境推荐)

#### 邮件内容特性

| 特性 | 配置项 | 说明 |
|------|--------|------|
| 地图 | `enable_maps` | Plotly 生成的航迹地图 |
| 飞机图片 | `enable_aircraft_images` | 飞机实际图片 |
| AI 分析 | `enable_flight_analysis` | Claude 分析报告 |
| 网络搜索 | `enable_tavily_search` | Tavily 搜索结果 |

#### 报告冷却机制

为避免重复通知同一飞机，系统实现了报告冷却机制：

```yaml
reporting:
  enable_report_cooldown: true
  cooldown_hours: 1.0              # 同一飞机 1 小时内最多 1 份报告
```

### 6. AI 分析系统

#### FlightAnalysisAgent

基于 Strands Agents 框架的 AI 分析代理，提供以下功能：

- **网络搜索**: 使用 Tavily API 搜索飞机相关信息
- **数据分析**: 解析飞机数据并推断飞行目的
- **报告生成**: 生成专业的飞行情报分析报告

**分析内容**:
- 飞机所有权和隶属关系
- 可能的飞行目的
- 飞行任务推测
- 风险评估

### 7. 地图可视化

#### 技术实现

- **后端**: Plotly + Kaleido (生成 PNG/PDF)
- **前端**: Leaflet (交互式地图)

#### 地图特性

- 航迹轨迹绘制
- 实时位置标记
- 颜色区分 (军用/民用)
- 信息弹窗显示
- 轨迹回放

## 高级配置

### 召回策略配置

```yaml
recall:
  sources:
    specific_flights: []             # 特定航班号
    specific_registrations:          # 特定注册号
      - "YU-RSB"
      - "86-0022"
    military_global: true            # 全球军用飞机
    aircraft_types:                  # 特定机型
      - "B742"
      - "IL76"
    regional_scan:                   # 区域扫描
      - "United States"
      - "China"
    global_scan: false               # 全球扫描 (慎用)

  strategy:
    update_interval: 300             # 召回周期 (秒)
    max_aircraft_per_call: 1000
    parallel_requests: true
    cache_duration: 60
```

### LLM 配置

```yaml
llm:
  provider: "anthropic"              # anthropic | openai | aws_bedrock
  anthropic_api_key: "${ANTHROPIC_API_KEY}"
  openai_api_key: "${OPENAI_API_KEY}"
  aws_bedrock_region: "us-east-1"
  tavily_api_key: "${TAVILY_API_KEY}"
```

## 性能优化

### 数据库优化

1. **索引优化**: 为常用查询字段创建索引
2. **批量插入**: 使用批量插入提高写入性能
3. **定期清理**: 自动清理 24 小时旧数据

### 地理编码优化

1. **内存缓存**: LRU 缓存机制
2. **坐标网格化**: 精确到 0.1 度
3. **批量查询**: 支持批量坐标查询

### API 优化

1. **并行请求**: 多个召回源并行查询
2. **请求缓存**: 60 秒缓存
3. **超时控制**: 30 秒请求超时

## 故障排除

### 常见问题

| 问题 | 原因 | 解决方案 |
|------|------|---------|
| API 连接失败 | API 密钥无效或网络问题 | 检查 `ADSB_API_KEY` 和网络连接 |
| 地理编码超时 | reverse_geocoder 初始化慢 | 第一次查询会较慢，后续会缓存 |
| 邮件发送失败 | AWS SES 或 SMTP 配置错误 | 检查邮件提供商配置和凭证 |
| 数据库锁定 | SQLite 并发写入 | 升级到 MySQL/PostgreSQL |

### 日志查看

```bash
# 查看追踪器日志
tail -f plane_tracker.log

# Web 应用日志在控制台直接输出
```

## 数据安全

### 安全措施

- **SQL 注入防护**: WHERE 子句关键字检查
- **敏感信息保护**: 使用环境变量存储 API 密钥
- **数据过期**: 自动清理 24 小时旧数据
- **CORS 配置**: Flask CORS 安全配置

### 配置最佳实践

```bash
# 不要提交敏感文件到版本控制
echo ".env" >> .gitignore
echo "*.db" >> .gitignore
echo "config.yaml" >> .gitignore

# 使用环境变量管理敏感信息
export ADSB_API_KEY="..."
export AWS_ACCESS_KEY_ID="..."
export ANTHROPIC_API_KEY="..."
```

## 应用场景

### 适用场景

1. **军事飞行监控**: 监控特定军用飞机的飞行活动
2. **航空安全**: 监控特殊机型或区域的飞行情况
3. **飞行数据分析**: 收集和分析飞行数据
4. **航空爱好**: 追踪感兴趣的飞机
5. **航空情报**: 结合 AI 分析生成情报报告

### 典型配置示例

**军用飞机监控**:
```yaml
recall:
  sources:
    military_global: true

filters:
  custom_sql: "is_military = 1 AND current_country IN ('China', 'Russia', 'North Korea')"
```

**特殊机型监控**:
```yaml
recall:
  sources:
    aircraft_types:
      - "B742"      # 波音 747-200
      - "AN124"     # 安东诺夫 124
      - "IL76"      # 伊尔 76

filters:
  custom_sql: "aircraft_type IN ('B742', 'AN124', 'IL76')"
```

**特定注册号追踪**:
```yaml
recall:
  sources:
    specific_registrations:
      - "N757AF"    # 特朗普私人飞机
      - "YU-RSB"    # 塞尔维亚政府专机

filters:
  custom_sql: "registration IN ('N757AF', 'YU-RSB')"
```

## 项目优势

### 核心优势

1. **SQL 驱动的过滤**: 灵活的自定义过滤规则，支持复杂逻辑组合
2. **实时地理编码**: 批量坐标查询，国家定位准确
3. **多邮件服务商支持**: SMTP 和 AWS SES，易于切换
4. **AI 分析集成**: 使用 Claude/GPT 生成专业情报报告
5. **完整的 Web 界面**: 实时数据查询、交互式地图、轨迹回放

### 创新特性

- **智能报告冷却**: 避免重复通知同一飞机
- **批量地理编码**: 性能优化的坐标查询
- **Markdown HTML 转换**: 优化邮件格式
- **原始数据保存**: 完整的数据审计追踪
- **多索引优化**: 快速查询性能

## 开发路线图

### 计划功能

- [ ] 支持更多 LLM 提供商 (AWS Bedrock 等)
- [ ] 实现 WebSocket 实时推送
- [ ] 增加更多地图可视化选项
- [ ] 支持多用户和权限管理
- [ ] 移动端适配
- [ ] 数据导出功能 (CSV、Excel)
- [ ] 自定义报表生成
- [ ] 飞行轨迹预测

## 贡献指南

欢迎提交 Issue 和 Pull Request！

## 许可证

本项目采用开源许可证，具体请查看 LICENSE 文件。

## 联系方式

如有问题或建议，请通过以下方式联系：

- 提交 GitHub Issue
- 发送邮件至项目维护者

## 致谢

感谢以下开源项目和服务：

- ADS-B Exchange API
- Flask Web Framework
- SQLAlchemy ORM
- Anthropic Claude
- Tavily Search API
- Plotly
- Leaflet

---

**最后更新**: 2026-01-14
