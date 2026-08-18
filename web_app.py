#!/usr/bin/env python3
"""Aircraft Tracking System — Flask Web Interface.

This file is the application entry point. The Flask `app` object is
constructed here and `init_app()` is called on cold start by
`lambda_handler.py`. Route handlers are being migrated into blueprints
under `src/web/routes/` — see docs/web-blueprints.md.
"""

import logging
import os
import secrets
import sys
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple  # noqa: UP035 - legacy

import pytz
from flask import (
    Flask,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix

# Ensure the project root is on sys.path before importing anything under `src.`.
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from src.data.dialect import beijing_date, day_of, latest_rows, minutes_ago, minutes_from_now
from src.services.aircraft_service import AircraftService
from src.services.airport_service import AirportService
from src.storage import (
    ObjectStorage,
    resolve_media_base_url,
    resolve_static_base_url,
)
from src.utils.database import DatabaseManager, mask_database_url
from src.utils.yaml_config import YAMLConfig
from src.web.auth_shim import (
    AUTH_ENABLED,
    admin_required,
    flight_schedules_required,
    get_current_user,
    group_required,
    login_required,
    optional_login,
)
from src.web.middleware import CustomDomainMiddleware, TTLCache
from src.web.routes.auth import bp as auth_bp

if AUTH_ENABLED:
    from src.auth.factory import get_auth_provider

# API cache for hot data (TTL ~1 hour).
api_cache = TTLCache()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("web_app")


app = Flask(
    __name__,
    template_folder="web_templates",
    static_folder="web_static",
    static_url_path="/static",
)

# Trust proxy headers from API Gateway / ALB for correct URL generation.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
app.wsgi_app = CustomDomainMiddleware(app.wsgi_app)

CORS(app)

# Session configuration for authentication.
app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY", secrets.token_hex(32))
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("STAGE", "local") != "local"
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=7)

# Register blueprints.
app.register_blueprint(auth_bp)

# 全局变量
db_manager = None
config = None


# Context processor - inject the static asset base URL and current user
@app.context_processor
def inject_static_url():
    """
    Inject the static asset base URL into all templates.

    aws: the CloudFront domain, which fronts the bucket the CDK stack syncs
    `web_static/` to. Otherwise `/static`, served by Flask out of `web_static/`.

    Deliberately `resolve_static_base_url()` and not the media resolver: these
    are this commit's own CSS and JS, so they have to come from somewhere that
    holds this commit's copy of them.
    """
    base_url = resolve_static_base_url()
    static_url = f"{base_url}/static" if base_url else "/static"

    return {
        "static_base_url": base_url,
        "static_url": static_url,
    }


@app.context_processor
def inject_user():
    """Inject current user into all templates for login status display."""
    # Always call get_current_user() - it handles skip-auth mode internally
    user = get_current_user()
    return {"current_user": user}


@app.context_processor
def inject_is_admin():
    """Inject is_admin helper function into all templates."""

    def is_admin(user: dict[str, Any] | None) -> bool:
        if not user:
            return False
        user_role = user.get("role", "").lower()
        user_groups = [grp.lower() for grp in user.get("groups", [])]
        admin_groups = ["admin", "admins", "administrator", "superuser"]
        return user_role in admin_groups or any(grp in admin_groups for grp in user_groups)

    return {"is_admin": is_admin}


@app.context_processor
def inject_auth_config():
    """Inject auth configuration for frontend token refresh.

    Cognito only. The browser-side refresh in `_token_refresh.html` needs the
    client secret, which is acceptable there because Lambda sits in a VPC with
    no egress to the Cognito token endpoint (see docs/deployment.md). Google
    refreshes server-side in `src/auth/decorators.get_current_user()`, so this
    returns None for it — emitting a Google client secret into page source
    would be a real leak, not an accepted trade-off.
    """
    if not AUTH_ENABLED:
        return {"auth_config": None}

    from src.auth.cognito_auth import CognitoAuth

    auth = get_auth_provider()
    if not isinstance(auth, CognitoAuth):
        return {"auth_config": None}

    # Get token expiration from session (if available)
    id_token = session.get("id_token")
    token_exp = None
    if id_token:
        try:
            # Decode without verification just to get exp claim
            from jose import jwt

            claims = jwt.get_unverified_claims(id_token)
            token_exp = claims.get("exp")
        except Exception:
            pass

    return {
        "auth_config": {
            "domain": auth.domain,
            "client_id": auth.client_id,
            "client_secret": auth.client_secret or "",
            "callback_url": auth.callback_url,
            "token_exp": token_exp,
            "refresh_token": session.get("refresh_token", ""),
        }
    }


# Auth routes live in src/web/routes/auth.py (registered as `auth_bp` above).


# Timezone settings
UTC = pytz.UTC
BEIJING_TZ = pytz.timezone("Asia/Shanghai")

# "This aircraft has a livery worth showing", as a SQL predicate on
# `aircraft_static_info asi`.
#
# Three queries used to write `asi.has_special_livery = TRUE`, but no such
# column exists in any environment: it is absent from `AircraftStaticInfo`,
# from `_ensure_analysis_columns()` in the analysis service, and from every
# migration script. Those queries raised "no such column" on both dialects.
# `livery_type` is the field the analysis service actually populates (free text
# such as "special livery" or "government VIP"), so its presence is the
# available expression of the same idea.
HAS_LIVERY_SQL = "(asi.livery_type IS NOT NULL AND asi.livery_type != '')"


def _to_iso(value) -> str | None:
    """Render a timestamp column from a raw-SQL row as an ISO-8601 string.

    A `text()` query carries no type information, so the driver decides what a
    timestamp column becomes: psycopg2 returns a `datetime`, but SQLite hands
    back the stored string. `value.isoformat()` therefore raises
    `AttributeError` on SQLite for SQL that works fine against Aurora.

    Args:
        value: A `datetime`, a timestamp string, or None.

    Returns:
        The ISO-8601 form, or None when `value` is None or empty.
    """
    if not value:
        return None
    if isinstance(value, str):
        return value
    return value.isoformat()


def _table_exists(session, table_name: str) -> bool:
    """Dialect-agnostic 'does this table exist?' check.

    Originally the code used `SELECT EXISTS (SELECT FROM information_schema.tables
    WHERE table_name = ...)`, which is Postgres-only. SQLAlchemy's Inspector
    works across SQLite, Postgres, MySQL, etc.
    """
    from sqlalchemy import inspect

    try:
        return inspect(session.get_bind()).has_table(table_name)
    except Exception:
        return False


def get_image_url(relative_path: str | None) -> str | None:
    """Convert relative image path to full URL based on environment.

    Args:
        relative_path: Relative path stored in database (e.g., "data/jetphotos_images/B-1234_001.jpg")

    Returns:
        Full URL for the image (always uses CloudFront since images are stored on S3)
    """
    if not relative_path:
        return None

    # Already a full URL, return as-is
    if relative_path.startswith("https://") or relative_path.startswith("http://"):
        return relative_path

    # Ensure path starts with 'data/' for consistency
    if not relative_path.startswith("data/"):
        relative_path = f"data/{relative_path}"

    # Scraped images live in object storage and are served from the media base
    # URL (CloudFront on aws, the public GCS bucket on gcp).
    base_url = resolve_media_base_url()
    if not base_url:
        # No CDN configured; fall back to a relative path so local dev works.
        return f"/{relative_path}"
    return f"{base_url}/{relative_path}"


def transform_image_paths(data: dict) -> dict:
    """Transform image paths in a data dictionary to full URLs.

    Args:
        data: Dictionary that may contain image_path_1, image_path_2, image_path_3

    Returns:
        Same dictionary with image paths transformed to full URLs
    """
    for key in ["image_path_1", "image_path_2", "image_path_3"]:
        if data.get(key):
            data[key] = get_image_url(data[key])
    return data


def get_images_from_static_info(registration: str) -> dict[str, str | None]:
    """Get image paths from aircraft_images table.

    Args:
        registration: Aircraft registration number

    Returns:
        Dictionary with image_path_1, image_path_2, image_path_3 keys
        (for backward compatibility with existing code)
    """
    if not registration or not db_manager:
        return {"image_path_1": None, "image_path_2": None, "image_path_3": None}

    try:
        from sqlalchemy import text

        session = db_manager.get_session()
        try:
            # Query from aircraft_images table ordered by display_order
            result = session.execute(
                text("""
                SELECT image_path
                FROM aircraft_images
                WHERE registration = :reg
                AND image_path IS NOT NULL
                AND image_path != ''
                ORDER BY display_order ASC
                LIMIT 3
            """),
                {"reg": registration},
            ).fetchall()

            if result:
                paths = [row[0] for row in result]
                return {
                    "image_path_1": paths[0] if len(paths) > 0 else None,
                    "image_path_2": paths[1] if len(paths) > 1 else None,
                    "image_path_3": paths[2] if len(paths) > 2 else None,
                }
        finally:
            session.close()
    except Exception as e:
        logger.warning(f"Error getting images from aircraft_images: {e}")

    return {"image_path_1": None, "image_path_2": None, "image_path_3": None}


def batch_get_images_from_static_info(registrations: list[str]) -> dict[str, dict[str, str | None]]:
    """Batch get image paths from aircraft_images table.

    Args:
        registrations: List of aircraft registration numbers

    Returns:
        Dictionary mapping registration to image paths dict
        (for backward compatibility, returns image_path_1, image_path_2, image_path_3 keys)
    """
    if not registrations or not db_manager:
        return {}

    try:
        from sqlalchemy import text

        session = db_manager.get_session()
        try:
            # Filter out None and empty
            valid_regs = [r for r in registrations if r]
            if not valid_regs:
                return {}

            # Build query
            placeholders = ", ".join([f":reg{i}" for i in range(len(valid_regs))])
            params = {f"reg{i}": reg for i, reg in enumerate(valid_regs)}

            # Query from aircraft_images table ordered by display_order
            # Use ROW_NUMBER to get top 3 images per registration
            result = session.execute(
                text(f"""
                WITH ranked_images AS (
                    SELECT
                        registration,
                        image_path,
                        ROW_NUMBER() OVER (PARTITION BY registration ORDER BY display_order ASC) as rn
                    FROM aircraft_images
                    WHERE registration IN ({placeholders})
                    AND image_path IS NOT NULL
                    AND image_path != ''
                )
                SELECT registration, image_path, rn
                FROM ranked_images
                WHERE rn <= 3
                ORDER BY registration, rn
            """),
                params,
            ).fetchall()

            # Build result dictionary
            images_dict: dict[str, dict[str, str | None]] = {}
            for row in result:
                reg = row[0]
                image_path = row[1]
                rn = row[2]

                if reg not in images_dict:
                    images_dict[reg] = {
                        "image_path_1": None,
                        "image_path_2": None,
                        "image_path_3": None,
                    }

                images_dict[reg][f"image_path_{rn}"] = image_path

            return images_dict
        finally:
            session.close()
    except Exception as e:
        logger.warning(f"Error batch getting images from aircraft_images: {e}")
        return {}


def convert_utc_to_beijing(utc_datetime_str):
    """将UTC时间字符串转换为北京时间字符串"""
    if not utc_datetime_str:
        return None

    try:
        # 解析UTC时间
        if isinstance(utc_datetime_str, str):
            # 处理不同的时间格式
            if utc_datetime_str.endswith("Z"):
                utc_dt = datetime.fromisoformat(utc_datetime_str.replace("Z", "+00:00"))
            elif "+" in utc_datetime_str or utc_datetime_str.endswith("UTC"):
                utc_dt = datetime.fromisoformat(utc_datetime_str.replace("UTC", "").strip())
            else:
                utc_dt = datetime.fromisoformat(utc_datetime_str)
        else:
            utc_dt = utc_datetime_str

        # 确保是UTC时区
        if utc_dt.tzinfo is None:
            utc_dt = UTC.localize(utc_dt)

        # 转换为北京时间
        beijing_dt = utc_dt.astimezone(BEIJING_TZ)
        return beijing_dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception as e:
        logger.warning(f"Time conversion error for '{utc_datetime_str}': {e}")
        return str(utc_datetime_str)


def convert_beijing_to_utc(beijing_datetime_str):
    """将北京时间字符串转换为UTC时间"""
    if not beijing_datetime_str:
        return None

    try:
        # 解析北京时间
        beijing_dt = datetime.fromisoformat(beijing_datetime_str)
        # 检查是否已经有时区信息
        if beijing_dt.tzinfo is None:
            beijing_dt = BEIJING_TZ.localize(beijing_dt)

        # 转换为UTC
        utc_dt = beijing_dt.astimezone(UTC)
        return utc_dt.replace(tzinfo=None)  # DB stores naive datetimes.
    except Exception as e:
        logger.warning(f"Time conversion error: {e}")
        try:
            # Last-ditch: parse the string as ISO-format.
            return datetime.fromisoformat(beijing_datetime_str)
        except (ValueError, TypeError):
            return None


def init_app():
    """Initialise the app — Lambda-compatible, called once at cold start."""
    global db_manager, config

    try:
        # 加载配置 - support Lambda environment
        config_path = os.environ.get("CONFIG_PATH", "config/config.yaml")
        config = YAMLConfig(config_path)
        db_config = config.get_database_config()

        # 使用环境变量覆盖数据库URL (Lambda部署时使用)
        db_url = os.environ.get("DATABASE_URL", db_config["url"])
        logger.info(f"Database URL: {mask_database_url(db_url)}")

        # 初始化数据库 (支持PostgreSQL和SQLite)
        db_manager = DatabaseManager(db_url)

        logger.info("Web application initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize web application: {e}")
        raise


@app.route("/")
@login_required
def home():
    """首页 - Google风格搜索页面"""
    return render_template("home.html")


@app.route("/dashboard")
@login_required
def dashboard():
    """仪表盘页面 - 原主页"""
    return render_template("index.html")


@app.route("/data/<path:filepath>")
def serve_data_file(filepath):
    """
    Serve files from data directory (aircraft images)
    aws/gcp: Redirect to the CDN or public bucket
    local: Serve from local filesystem
    """
    base_url = resolve_media_base_url()

    if base_url:
        # Cloud deployment - redirect to the public asset host
        from flask import redirect

        redirect_url = f"{base_url}/data/{filepath}"
        logger.debug(f"Redirecting to object storage: {redirect_url}")
        return redirect(redirect_url, code=302)
    else:
        # Local development - serve from local filesystem
        data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
        return send_from_directory(data_dir, filepath)


@app.route("/api/aircraft/search")
def search_aircraft():
    """搜索飞机数据"""
    try:
        # 获取查询参数
        registration = request.args.get("registration", "").strip()
        hex_code = request.args.get("hex", "").strip()
        aircraft_type = request.args.get("aircraft_type", "").strip()
        start_date = request.args.get("start_date")
        end_date = request.args.get("end_date")
        is_military = request.args.get("is_military")
        limit = int(request.args.get("limit", 100))

        # 调试信息
        logger.info(
            f"Search parameters - registration: '{registration}', hex: '{hex_code}', aircraft_type: '{aircraft_type}'"
        )
        logger.info(f"All request args: {dict(request.args)}")

        # 构建WHERE条件（直接嵌入值，避免参数绑定问题）
        conditions = []

        if registration:
            conditions.append(f"registration LIKE '%{registration}%'")

        if hex_code:
            conditions.append(f"hex = '{hex_code}'")

        if aircraft_type:
            conditions.append(f"aircraft_type LIKE '%{aircraft_type}%'")

        if is_military is not None:
            is_mil_value = 1 if is_military.lower() == "true" else 0
            conditions.append(f"is_military = {is_mil_value}")

        # 时间范围过滤（前端传入北京时间，需要转换为UTC）
        if start_date:
            try:
                start_dt_utc = convert_beijing_to_utc(start_date)
                if start_dt_utc:
                    start_time_str = start_dt_utc.strftime("%Y-%m-%d %H:%M:%S")
                    conditions.append(f"snapshot_time >= '{start_time_str}'")
            except ValueError:
                pass

        if end_date:
            try:
                end_dt_utc = convert_beijing_to_utc(end_date)
                if end_dt_utc:
                    end_time_str = end_dt_utc.strftime("%Y-%m-%d %H:%M:%S")
                    conditions.append(f"snapshot_time <= '{end_time_str}'")
            except ValueError:
                pass

        # 默认过滤条件：只显示有注册号的飞机
        conditions.append(
            "registration IS NOT NULL AND registration != '' AND registration != 'None'"
        )

        # 如果没有其他搜索条件，添加默认的时间限制（最近24小时）
        has_search_conditions = any(
            [registration, hex_code, aircraft_type, start_date, end_date, is_military]
        )
        if not has_search_conditions:
            default_start = datetime.now() - timedelta(hours=24)
            default_start_str = default_start.strftime("%Y-%m-%d %H:%M:%S")
            conditions.append(f"snapshot_time >= '{default_start_str}'")

        where_clause = " AND ".join(conditions)

        logger.info(f"Search conditions: {conditions}")
        logger.info(f"Where clause: {where_clause}")

        # 执行查询
        results = db_manager.execute_filter_query(where_clause, limit)
        logger.info(f"Query returned {len(results)} results")
        logger.info(f"First few results: {[r.get('r') for r in results[:3]]}")

        # 批量获取图片路径 (从 aircraft_static_info)
        registrations = [r.get("r") for r in results if r.get("r")]
        static_images = batch_get_images_from_static_info(registrations)

        # 转换时间为北京时间，并转换图片路径
        for result in results:
            if result.get("timestamp"):
                result["timestamp"] = convert_utc_to_beijing(result["timestamp"])
            # 添加北京时间标识
            result["timezone"] = "Asia/Shanghai"
            # 从 aircraft_static_info 获取图片路径
            reg = result.get("r")
            if reg and reg in static_images:
                result["image_path_1"] = static_images[reg].get("image_path_1")
                result["image_path_2"] = static_images[reg].get("image_path_2")
                result["image_path_3"] = static_images[reg].get("image_path_3")
            # 转换图片路径为完整URL
            transform_image_paths(result)

        return jsonify({"success": True, "data": results, "count": len(results)})

    except Exception as e:
        logger.error(f"Error searching aircraft: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/aircraft/tracks/<registration>")
def get_aircraft_tracks(registration):
    """获取飞机轨迹数据"""
    try:
        # 获取查询参数
        start_time = request.args.get("start_time")
        limit = int(request.args.get("limit", 1000))

        # 转换开始时间（前端传入北京时间，需要转换为UTC时间戳）
        start_timestamp = None
        if start_time:
            try:
                # 假设前端传入的是北京时间
                start_dt_utc = convert_beijing_to_utc(start_time)
                if start_dt_utc:
                    start_timestamp = int(start_dt_utc.timestamp())
            except (ValueError, TypeError):
                try:
                    # 如果是时间戳格式
                    start_timestamp = int(start_time)
                except (ValueError, TypeError):
                    pass

        if start_timestamp is None:
            # 默认最近7天
            start_timestamp = int((datetime.now() - timedelta(days=7)).timestamp())

        # 获取轨迹数据
        logger.info(
            f"Getting tracks for {registration}, limit={limit}, start_time={start_timestamp}"
        )
        tracks = db_manager.get_flight_tracks_by_registration(
            registration, limit=limit, start_time=start_timestamp
        )
        logger.info(f"Retrieved {len(tracks)} track points")

        # 转换轨迹时间为北京时间
        for track in tracks:
            if track.get("timestamp"):
                # 时间戳转换为北京时间字符串
                utc_dt = datetime.fromtimestamp(track["timestamp"], tz=UTC)
                beijing_dt = utc_dt.astimezone(BEIJING_TZ)
                track["timestamp_beijing"] = beijing_dt.strftime("%Y-%m-%d %H:%M:%S")
                track["timezone"] = "Asia/Shanghai"

        return jsonify(
            {"success": True, "registration": registration, "tracks": tracks, "count": len(tracks)}
        )

    except Exception as e:
        logger.error(f"Error getting tracks for {registration}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/flight/trail/<fr24_id>")
def get_fr24_flight_trail(fr24_id):
    """从 FR24 获取航班实时轨迹"""
    import requests

    try:
        # FR24 clickhandler API 返回完整航班信息包括轨迹
        url = f"https://data-live.flightradar24.com/clickhandler/?version=1.5&flight={fr24_id}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
            "Origin": "https://www.flightradar24.com",
            "Referer": "https://www.flightradar24.com/",
        }

        response = requests.get(url, headers=headers, timeout=10)

        if response.status_code != 200:
            logger.warning(f"FR24 API returned status {response.status_code} for flight {fr24_id}")
            return jsonify(
                {"success": False, "error": f"FR24 API error: {response.status_code}"}
            ), 502

        data = response.json()

        # 提取轨迹数据
        trail = data.get("trail", [])

        # 提取航班信息
        flight_info = {
            "flight_number": data.get("identification", {}).get("number", {}).get("default"),
            "callsign": data.get("identification", {}).get("callsign"),
            "origin": data.get("airport", {}).get("origin", {}).get("code", {}).get("iata"),
            "destination": data.get("airport", {})
            .get("destination", {})
            .get("code", {})
            .get("iata"),
            "aircraft_type": data.get("aircraft", {}).get("model", {}).get("code"),
            "registration": data.get("aircraft", {}).get("registration"),
            "status": data.get("status", {}).get("text"),
        }

        # 转换轨迹格式，保持与现有 API 兼容
        tracks = []
        for point in trail:
            tracks.append(
                {
                    "lat": point.get("lat"),
                    "lon": point.get("lng"),
                    "alt_baro": point.get("alt"),
                    "ground_speed": point.get("spd"),
                    "heading": point.get("hd"),
                    "timestamp": point.get("ts"),
                }
            )

        return jsonify(
            {
                "success": True,
                "fr24_id": fr24_id,
                "flight_info": flight_info,
                "tracks": tracks,
                "count": len(tracks),
            }
        )

    except requests.exceptions.Timeout:
        logger.error(f"FR24 API timeout for flight {fr24_id}")
        return jsonify({"success": False, "error": "FR24 API timeout"}), 504
    except Exception as e:
        logger.error(f"Error getting FR24 trail for {fr24_id}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/aircraft/recent")
def get_recent_aircraft():
    """获取最近的飞机数据"""
    try:
        hours = int(request.args.get("hours", 1))
        limit = int(request.args.get("limit", 50))

        # 构建查询（使用Python datetime而不是SQLite函数）
        recent_time = datetime.now() - timedelta(hours=hours)
        # 使用标准时间格式，不包含微秒，以匹配数据库中的格式
        recent_time_str = recent_time.strftime("%Y-%m-%d %H:%M:%S")
        where_clause = f"snapshot_time >= '{recent_time_str}'"
        logger.info(f"Recent aircraft query: {where_clause}")
        results = db_manager.execute_filter_query(where_clause, limit)
        logger.info(f"Recent query returned {len(results)} results")

        # 批量获取图片路径 (从 aircraft_static_info)
        registrations = [r.get("r") for r in results if r.get("r")]
        static_images = batch_get_images_from_static_info(registrations)

        # 转换时间为北京时间，并转换图片路径
        for result in results:
            if result.get("timestamp"):
                result["timestamp"] = convert_utc_to_beijing(result["timestamp"])
            result["timezone"] = "Asia/Shanghai"
            # 从 aircraft_static_info 获取图片路径
            reg = result.get("r")
            if reg and reg in static_images:
                result["image_path_1"] = static_images[reg].get("image_path_1")
                result["image_path_2"] = static_images[reg].get("image_path_2")
                result["image_path_3"] = static_images[reg].get("image_path_3")
            # 转换图片路径为完整URL
            transform_image_paths(result)

        return jsonify({"success": True, "data": results, "count": len(results)})

    except Exception as e:
        logger.error(f"Error getting recent aircraft: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/aircraft/types")
def get_aircraft_types():
    """获取飞机机型列表"""
    try:
        session = db_manager.get_session()
        try:
            from sqlalchemy import func, text

            from src.data.models import AircraftSnapshot

            # 获取最近7天的机型统计
            result = session.execute(
                text("""
                SELECT aircraft_type, COUNT(*) as count
                FROM aircraft_snapshots
                WHERE aircraft_type IS NOT NULL
                  AND aircraft_type != ''
                  AND snapshot_time >= datetime('now', '-7 days')
                GROUP BY aircraft_type
                ORDER BY count DESC
                LIMIT 50
            """)
            )

            aircraft_types = []
            for row in result:
                aircraft_types.append(
                    {
                        "code": row.aircraft_type,
                        "count": row.count,
                        "name": get_aircraft_type_name(row.aircraft_type),
                    }
                )

            return jsonify(
                {"success": True, "aircraft_types": aircraft_types, "count": len(aircraft_types)}
            )

        finally:
            session.close()

    except Exception as e:
        logger.error(f"Error getting aircraft types: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


def get_aircraft_type_name(code: str) -> str:
    """获取机型的中文名称"""
    type_names = {
        "H60": "黑鹰直升机",
        "C17": "环球霸王运输机",
        "TWR": "塔台",
        "C30J": "大力神运输机",
        "TEX2": "教练机",
        "C130": "大力神运输机",
        "EC45": "欧直直升机",
        "H47": "支奴干直升机",
        "C295": "运输机",
        "A139": "阿古斯塔直升机",
        "B350": "商务机",
        "A400": "运输机",
        "K35R": "加油机",
        "A332": "空客A330",
        "BE20": "商务机",
        "B737": "波音737",
        "EC35": "欧直直升机",
        "CN35": "其他",
        "C172": "塞斯纳172",
        "B762": "波音767",
        "B738": "波音737-800",
        "B739": "波音737-900",
        "B77W": "波音777-300ER",
        "B788": "波音787-8",
        "B789": "波音787-9",
        "B78X": "波音787-10",
        "A320": "空客A320",
        "A321": "空客A321",
        "A319": "空客A319",
        "A20N": "空客A320neo",
        "A21N": "空客A321neo",
        "A350": "空客A350",
        "A359": "空客A350-900",
        "A35K": "空客A350-1000",
        "A333": "空客A330-300",
        "A339": "空客A330-900neo",
        "A388": "空客A380-800",
        "E190": "Embraer E190",
        "E195": "Embraer E195",
        "CRJ9": "CRJ-900",
        "CRJ7": "CRJ-700",
        "C919": "C919",
        "ARJ2": "ARJ21",
        "MA60": "MA60",
    }
    return type_names.get(code, code)


@app.route("/api/aircraft/types/<type_code>")
@login_required
def get_aircraft_type_info(type_code: str):
    """获取指定机型的统计信息"""
    try:
        type_code_upper = type_code.upper()
        db_session = db_manager.get_session()
        try:
            from sqlalchemy import text

            # 获取机型统计信息
            stats_result = db_session.execute(
                text("""
                SELECT
                    COUNT(*) as total_aircraft,
                    COUNT(DISTINCT CASE WHEN ai.registration IS NOT NULL THEN asi.registration END) as aircraft_with_images
                FROM aircraft_static_info asi
                LEFT JOIN aircraft_images ai ON asi.registration = ai.registration
                WHERE asi.aircraft_type = :type_code
            """),
                {"type_code": type_code_upper},
            )

            stats_row = stats_result.fetchone()

            return jsonify(
                {
                    "success": True,
                    "type_code": type_code_upper,
                    "name": get_aircraft_type_name(type_code_upper),
                    "total_aircraft": stats_row.total_aircraft if stats_row else 0,
                    "aircraft_with_images": stats_row.aircraft_with_images if stats_row else 0,
                }
            )
        finally:
            db_session.close()

    except Exception as e:
        logger.error(f"Error getting aircraft type info: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/aircraft/types/<type_code>/instances")
@login_required
def get_aircraft_type_instances(type_code: str):
    """获取指定机型的飞机列表（分页）"""
    try:
        type_code_upper = type_code.upper()
        offset = int(request.args.get("offset", 0))
        limit = int(request.args.get("limit", 20))

        db_session = db_manager.get_session()
        try:
            from sqlalchemy import text

            # Aircraft of this type, photo-bearing rows first. LATERAL
            # replaced by a correlated subquery for dialect portability.
            aircraft_result = db_session.execute(
                text("""
                SELECT
                    asi.registration,
                    asi.aircraft_type,
                    asi.owner,
                    asi.operator,
                    asi.hex_code,
                    (SELECT image_path FROM aircraft_images
                     WHERE registration = asi.registration
                     ORDER BY display_order ASC, created_at DESC
                     LIMIT 1) AS image_path
                FROM aircraft_static_info asi
                WHERE asi.aircraft_type = :type_code
                ORDER BY
                    CASE WHEN (SELECT image_path FROM aircraft_images
                               WHERE registration = asi.registration
                               LIMIT 1) IS NOT NULL THEN 0 ELSE 1 END,
                    asi.registration
                LIMIT :limit OFFSET :offset
            """),
                {"type_code": type_code_upper, "limit": limit, "offset": offset},
            )

            aircraft_list = []
            for row in aircraft_result:
                aircraft_list.append(
                    {
                        "registration": row.registration,
                        "aircraft_type": row.aircraft_type,
                        "owner": row.owner,
                        "operator": row.operator,
                        "hex_code": row.hex_code,
                        "image_url": get_image_url(row.image_path) if row.image_path else None,
                    }
                )

            return jsonify(
                {
                    "success": True,
                    "aircraft": aircraft_list,
                    "offset": offset,
                    "limit": limit,
                    "has_more": len(aircraft_list) == limit,
                }
            )
        finally:
            db_session.close()

    except Exception as e:
        logger.error(f"Error getting aircraft type instances: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/statistics")
def get_statistics():
    """获取统计数据"""
    try:
        stats = db_manager.get_statistics()
        return jsonify({"success": True, "statistics": stats})

    except Exception as e:
        logger.error(f"Error getting statistics: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/aircraft/unique")
def get_unique_aircraft():
    """获取唯一飞机列表"""
    try:
        days = int(request.args.get("days", 7))

        session = db_manager.get_session()
        try:
            from src.data.models import AircraftSnapshot

            # 查询指定天数内的唯一飞机
            cutoff_time = datetime.now() - timedelta(days=days)

            unique_aircraft = (
                session.query(
                    AircraftSnapshot.registration,
                    AircraftSnapshot.aircraft_type,
                    AircraftSnapshot.hex,
                    AircraftSnapshot.is_military,
                    AircraftSnapshot.country_of_registration,
                )
                .filter(
                    AircraftSnapshot.snapshot_time >= cutoff_time,
                    AircraftSnapshot.registration.isnot(None),
                )
                .distinct()
                .limit(1000)
                .all()
            )

            result = []
            for aircraft in unique_aircraft:
                result.append(
                    {
                        "registration": aircraft.registration,
                        "aircraft_type": aircraft.aircraft_type,
                        "hex": aircraft.hex,
                        "is_military": aircraft.is_military,
                        "country_of_registration": aircraft.country_of_registration,
                    }
                )

            return jsonify({"success": True, "aircraft": result, "count": len(result)})

        finally:
            session.close()

    except Exception as e:
        logger.error(f"Error getting unique aircraft: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/aircraft/static")
def get_all_static_info():
    """获取所有飞机的静态信息列表"""
    try:
        session = db_manager.get_session()
        try:
            from sqlalchemy import text

            # 查询所有静态信息 (使用正确的列名)
            result = session.execute(
                text("""
                SELECT
                    id, registration, hex_code, owner, operator,
                    model, manufacturer, serial_number, year_built,
                    country_of_registration, aircraft_type, ai_analysis,
                    last_updated, data_source, images_downloaded
                FROM aircraft_static_info
                ORDER BY last_updated DESC NULLS LAST
                LIMIT 1000
            """)
            )

            aircraft_list = []
            for row in result:
                # 从 ai_analysis 中提取军用/政府/VIP 标记
                is_military = False
                is_government = False
                is_vip = False
                summary = None

                if row.ai_analysis:
                    ai_data = row.ai_analysis if isinstance(row.ai_analysis, dict) else {}
                    is_military = ai_data.get("is_military", False)
                    is_government = ai_data.get("is_government", False)
                    is_vip = ai_data.get("is_vip", False)
                    summary = ai_data.get("summary", None)

                aircraft_list.append(
                    {
                        "id": row.id,
                        "registration": row.registration,
                        "hex": row.hex_code,
                        "owner": row.owner,
                        "operator": row.operator,
                        "aircraft_model": row.model,
                        "aircraft_type": row.aircraft_type,
                        "manufacturer": row.manufacturer,
                        "serial_number": row.serial_number,
                        "year_built": row.year_built,
                        "country": row.country_of_registration,
                        "is_military": is_military,
                        "is_government": is_government,
                        "is_vip": is_vip,
                        "summary": summary,
                        "updated_at": convert_utc_to_beijing(str(row.last_updated))
                        if row.last_updated
                        else None,
                        "has_images": bool(row.images_downloaded),
                    }
                )

            return jsonify({"success": True, "data": aircraft_list, "count": len(aircraft_list)})

        finally:
            session.close()

    except Exception as e:
        logger.error(f"Error getting static info list: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/aircraft/static/batch", methods=["POST"])
def get_batch_static_info():
    """批量获取指定注册号的飞机静态信息"""
    try:
        # 获取请求中的注册号列表
        data = request.get_json()
        if not data or "registrations" not in data:
            return jsonify(
                {"success": False, "error": "Missing registrations list in request body"}
            ), 400

        registrations = data["registrations"]
        if not isinstance(registrations, list) or len(registrations) == 0:
            return jsonify({"success": True, "data": [], "count": 0})

        # 限制单次查询数量，防止滥用
        if len(registrations) > 500:
            registrations = registrations[:500]

        session = db_manager.get_session()
        try:
            from sqlalchemy import text

            # 构建参数化查询
            placeholders = ", ".join([f":reg{i}" for i in range(len(registrations))])
            params = {f"reg{i}": reg for i, reg in enumerate(registrations)}

            result = session.execute(
                text(f"""
                SELECT
                    id, registration, hex_code, owner, operator,
                    model, manufacturer, serial_number, year_built,
                    country_of_registration, aircraft_type, ai_analysis,
                    last_updated, data_source, images_downloaded
                FROM aircraft_static_info
                WHERE registration IN ({placeholders})
                ORDER BY registration
            """),
                params,
            )

            aircraft_list = []
            for row in result:
                # 从 ai_analysis 中提取军用/政府/VIP 标记
                is_military = False
                is_government = False
                is_vip = False
                summary = None

                if row.ai_analysis:
                    ai_data = row.ai_analysis if isinstance(row.ai_analysis, dict) else {}
                    is_military = ai_data.get("is_military", False)
                    is_government = ai_data.get("is_government", False)
                    is_vip = ai_data.get("is_vip", False)
                    summary = ai_data.get("summary", None)

                aircraft_list.append(
                    {
                        "id": row.id,
                        "registration": row.registration,
                        "hex": row.hex_code,
                        "owner": row.owner,
                        "operator": row.operator,
                        "aircraft_model": row.model,
                        "aircraft_type": row.aircraft_type,
                        "manufacturer": row.manufacturer,
                        "serial_number": row.serial_number,
                        "year_built": row.year_built,
                        "country": row.country_of_registration,
                        "is_military": is_military,
                        "is_government": is_government,
                        "is_vip": is_vip,
                        "summary": summary,
                        "updated_at": convert_utc_to_beijing(str(row.last_updated))
                        if row.last_updated
                        else None,
                        "has_images": bool(row.images_downloaded),
                    }
                )

            return jsonify({"success": True, "data": aircraft_list, "count": len(aircraft_list)})

        finally:
            session.close()

    except Exception as e:
        logger.error(f"Error getting batch static info: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/aircraft/static/<registration>")
def get_static_info(registration):
    """获取单架飞机的详细静态信息"""
    try:
        session = db_manager.get_session()
        try:
            import json

            from sqlalchemy import text

            # 查询指定飞机的静态信息
            result = session.execute(
                text("""
                SELECT * FROM aircraft_static_info
                WHERE registration = :reg
            """),
                {"reg": registration},
            ).fetchone()

            if not result:
                return jsonify(
                    {
                        "success": False,
                        "error": f"No static info found for registration: {registration}",
                    }
                ), 404

            # 获取列名 (兼容 SQLAlchemy 2.0)
            raw_data = result._mapping if hasattr(result, "_mapping") else dict(result)

            # 构建标准化的响应数据 - 返回所有字段
            data = {
                "id": raw_data.get("id"),
                "registration": raw_data.get("registration"),
                "hex": raw_data.get("hex_code"),
                "aircraft_type": raw_data.get("aircraft_type"),
                "aircraft_model": raw_data.get("model"),
                "owner": raw_data.get("owner"),
                "operator": raw_data.get("operator"),
                "manufacturer": raw_data.get("manufacturer"),
                "serial_number": raw_data.get("serial_number"),
                "year_built": raw_data.get("year_built"),
                "country": raw_data.get("country_of_registration"),
                "data_source": raw_data.get("data_source"),
                "updated_at": convert_utc_to_beijing(str(raw_data["last_updated"]))
                if raw_data.get("last_updated")
                else None,
                "organization": raw_data.get("organization"),
                "livery_type": raw_data.get("livery_type"),
                # 额外字段
                "livery_name": raw_data.get("livery_name"),
                "livery_description": raw_data.get("livery_description"),
                "special_markings": raw_data.get("special_markings"),
                "attention_level": raw_data.get("attention_level"),
                "attention_reason": raw_data.get("attention_reason"),
                "intelligence_summary": raw_data.get("intelligence_summary"),
                "anomalies": raw_data.get("anomalies"),
                "flight_pattern": raw_data.get("flight_pattern"),
                "recommended_actions": raw_data.get("recommended_actions"),
                "hit_count": raw_data.get("hit_count"),
                "images_downloaded": raw_data.get("images_downloaded"),
                "images_updated_at": convert_utc_to_beijing(str(raw_data["images_updated_at"]))
                if raw_data.get("images_updated_at")
                else None,
            }

            # 从 ai_analysis 中提取扩展信息
            ai_analysis = raw_data.get("ai_analysis")
            if ai_analysis:
                # 尝试解析JSON字符串
                if isinstance(ai_analysis, str):
                    try:
                        ai_data = json.loads(ai_analysis)
                    except json.JSONDecodeError:
                        ai_data = {}
                else:
                    ai_data = ai_analysis
                data["is_military"] = ai_data.get("is_military", False)
                data["is_government"] = ai_data.get("is_government", False)
                data["is_vip"] = ai_data.get("is_vip", False)
                data["summary"] = ai_data.get("summary")
                data["tags"] = ai_data.get("tags", [])
                data["previous_owners"] = ai_data.get("previous_owners")
            else:
                data["is_military"] = False
                data["is_government"] = False
                data["is_vip"] = False

            # 从 aircraft_images 表获取图片路径并转换为完整URL
            images_result = session.execute(
                text("""
                SELECT image_path FROM aircraft_images
                WHERE registration = :reg
                ORDER BY display_order LIMIT 3
            """),
                {"reg": registration},
            ).fetchall()
            image_paths = [row[0] for row in images_result if row[0]]
            data["image_path_1"] = get_image_url(image_paths[0]) if len(image_paths) > 0 else None
            data["image_path_2"] = get_image_url(image_paths[1]) if len(image_paths) > 1 else None
            data["image_path_3"] = get_image_url(image_paths[2]) if len(image_paths) > 2 else None

            return jsonify({"success": True, "data": data})

        finally:
            session.close()

    except Exception as e:
        logger.error(f"Error getting static info for {registration}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/aircraft/static/stats")
def get_static_info_stats():
    """获取静态信息统计数据"""
    try:
        session = db_manager.get_session()
        try:
            from sqlalchemy import text

            total = session.execute(text("SELECT COUNT(*) FROM aircraft_static_info")).scalar() or 0
            military = (
                session.execute(
                    text("SELECT COUNT(*) FROM aircraft_static_info WHERE is_military = 1")
                ).scalar()
                or 0
            )
            government = (
                session.execute(
                    text("SELECT COUNT(*) FROM aircraft_static_info WHERE is_government = 1")
                ).scalar()
                or 0
            )
            vip = (
                session.execute(
                    text("SELECT COUNT(*) FROM aircraft_static_info WHERE is_vip = 1")
                ).scalar()
                or 0
            )

            # 按国家统计
            country_stats = session.execute(
                text("""
                SELECT country, COUNT(*) as count
                FROM aircraft_static_info
                WHERE country IS NOT NULL AND country != ''
                GROUP BY country
                ORDER BY count DESC
            """)
            ).fetchall()

            # 按制造商统计
            manufacturer_stats = session.execute(
                text("""
                SELECT manufacturer, COUNT(*) as count
                FROM aircraft_static_info
                WHERE manufacturer IS NOT NULL AND manufacturer != ''
                GROUP BY manufacturer
                ORDER BY count DESC
            """)
            ).fetchall()

            return jsonify(
                {
                    "success": True,
                    "stats": {
                        "total": total,
                        "military": military,
                        "government": government,
                        "vip": vip,
                        "by_country": [{"country": r[0], "count": r[1]} for r in country_stats],
                        "by_manufacturer": [
                            {"manufacturer": r[0], "count": r[1]} for r in manufacturer_stats
                        ],
                    },
                }
            )

        finally:
            session.close()

    except Exception as e:
        logger.error(f"Error getting static info stats: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


# ==================== 新增: 机场看板和搜索追踪功能 ====================


@app.route("/airport-board")
@login_required
def airport_board():
    """机场情报看板页面 - 需要登录"""
    return render_template("airport_board.html")


@app.route("/search-track")
@login_required
def search_track():
    """搜索与追踪页面 - 需要登录"""
    return render_template("search_track.html")


@app.route("/aircraft/<registration>")
@login_required
def aircraft_detail(registration: str):
    """飞机详情页 - 展示飞机基础信息、图片和飞行轨迹"""
    return render_template("aircraft_detail.html", registration=registration)


@app.route("/aircraft-type/<type_code>")
@login_required
def aircraft_type_detail(type_code: str):
    """机型详情页 - 展示该机型的所有飞机列表"""
    return render_template("aircraft_type_detail.html", type_code=type_code.upper())


@app.route("/airport/<airport_code>")
@login_required
@flight_schedules_required
def airport_detail(airport_code):
    """机场详情页 - 显示该机场的航班计划"""
    return render_template("flight_schedules.html", airport_code=airport_code.upper())


@app.route("/api/search/unified")
def unified_search():
    """统一搜索API - 同时搜索机场、飞机和机型"""
    try:
        query = request.args.get("q", "").strip()
        limit = int(request.args.get("limit", 10))

        if not query or len(query) < 2:
            return jsonify(
                {"success": False, "error": "Search query must be at least 2 characters"}
            ), 400

        results: dict[str, list[Any]] = {"airports": [], "aircraft": [], "aircraft_types": []}

        db_session = db_manager.get_session()
        try:
            # Search airports using AirportService
            airport_service = AirportService(db_session, config.config if config else {})
            airports = airport_service.search_airports(query, limit)
            results["airports"] = airports

            # Search aircraft by registration (prefix match)
            from sqlalchemy import text

            query_upper = query.upper()

            # Query aircraft_static_info table for registration matches
            aircraft_result = db_session.execute(
                text("""
                SELECT registration, aircraft_type, owner, operator, hex_code
                FROM aircraft_static_info
                WHERE LOWER(registration) LIKE LOWER(:pattern)
                ORDER BY registration
                LIMIT :limit
            """),
                {"pattern": f"{query_upper}%", "limit": limit},
            )

            for row in aircraft_result:
                results["aircraft"].append(
                    {
                        "registration": row.registration,
                        "aircraft_type": row.aircraft_type,
                        "owner": row.owner,
                        "operator": row.operator,
                        "hex_code": row.hex_code,
                    }
                )

            # Search aircraft types (ICAO type codes like B738, A350)
            type_result = db_session.execute(
                text("""
                SELECT aircraft_type, COUNT(*) as aircraft_count
                FROM aircraft_static_info
                WHERE aircraft_type IS NOT NULL AND aircraft_type != ''
                  AND LOWER(aircraft_type) LIKE LOWER(:pattern)
                GROUP BY aircraft_type
                ORDER BY aircraft_count DESC
                LIMIT :limit
            """),
                {"pattern": f"{query_upper}%", "limit": limit},
            )

            for row in type_result:
                results["aircraft_types"].append(
                    {
                        "type_code": row.aircraft_type,
                        "aircraft_count": row.aircraft_count,
                        "name": get_aircraft_type_name(row.aircraft_type),
                    }
                )

            return jsonify({"success": True, "results": results})
        finally:
            db_session.close()

    except Exception as e:
        logger.error(f"Error in unified search: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/search/suggestions")
def search_suggestions():
    """获取搜索建议 - 航班最多的机场和图片最多的飞机，合并配置文件中的自定义项

    结果会缓存 1 小时，避免频繁查询数据库。
    """
    # 检查缓存
    cache_key = "search_suggestions"
    cached_data, hit = api_cache.get(cache_key)
    if hit:
        return jsonify(cached_data)

    try:
        db_session = db_manager.get_session()
        try:
            from sqlalchemy import text

            # Read custom popular items from config
            home_config = config.get("home_popular", {}) if config else {}
            config_airports = home_config.get("airports", [])  # List of IATA/ICAO codes
            config_aircraft = home_config.get("aircraft", [])  # List of registrations

            # Get config airports details first (priority)
            # Note: Skip flight_count for config items to avoid slow subqueries
            config_airport_details = []
            if config_airports:
                placeholders = ", ".join([f":code{i}" for i in range(len(config_airports))])
                params = {f"code{i}": code.upper() for i, code in enumerate(config_airports)}
                config_airports_result = db_session.execute(
                    text(f"""
                    SELECT
                        a.iata_code,
                        a.icao_code,
                        a.name,
                        a.city,
                        a.country,
                        a.country_code
                    FROM airports a
                    WHERE a.iata_code IN ({placeholders}) OR a.icao_code IN ({placeholders})
                """),
                    params,
                )

                for row in config_airports_result:
                    config_airport_details.append(
                        {
                            "iata_code": row.iata_code,
                            "icao_code": row.icao_code,
                            "name": row.name or row.iata_code,
                            "city": row.city,
                            "country": row.country,
                            "country_code": row.country_code,
                            "from_config": True,
                        }
                    )

            # Get config aircraft details first (priority)
            # Note: Skip image_count for config items to avoid slow subqueries
            config_aircraft_details = []
            if config_aircraft:
                placeholders = ", ".join([f":reg{i}" for i in range(len(config_aircraft))])
                params = {f"reg{i}": reg.upper() for i, reg in enumerate(config_aircraft)}
                config_aircraft_result = db_session.execute(
                    text(f"""
                    SELECT
                        asi.registration,
                        asi.aircraft_type,
                        asi.owner,
                        asi.operator
                    FROM aircraft_static_info asi
                    WHERE asi.registration IN ({placeholders})
                """),
                    params,
                )

                for row in config_aircraft_result:
                    config_aircraft_details.append(
                        {
                            "registration": row.registration,
                            "aircraft_type": row.aircraft_type,
                            "owner": row.owner,
                            "operator": row.operator,
                            "from_config": True,
                        }
                    )

            # Skip slow database fallback queries - only use config items
            # If config items not found in DB, just show fewer items

            result_data = {
                "success": True,
                "popular_airports": config_airport_details[:5],
                "recent_aircraft": config_aircraft_details[:5],
            }
            # 缓存 1 小时
            api_cache.set(cache_key, result_data, ttl_seconds=3600)
            return jsonify(result_data)
        finally:
            db_session.close()

    except Exception as e:
        logger.error(f"Error getting search suggestions: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/airports/search")
def search_airports():
    """搜索机场"""
    try:
        query = request.args.get("q", "").strip()
        limit = int(request.args.get("limit", 20))
        airport_type = request.args.get("type")  # large_airport, medium_airport, etc.

        if not query:
            return jsonify({"success": False, "error": "Search query is required"}), 400

        session = db_manager.get_session()
        try:
            airport_service = AirportService(session, config.config if config else {})
            airport_types = [airport_type] if airport_type else None
            airports = airport_service.search_airports(query, limit, airport_types)

            return jsonify({"success": True, "airports": airports, "count": len(airports)})
        finally:
            session.close()

    except Exception as e:
        logger.error(f"Error searching airports: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/airports/<airport_code>")
def get_airport(airport_code):
    """获取机场详情"""
    try:
        session = db_manager.get_session()
        try:
            airport_service = AirportService(session, config.config if config else {})
            airport = airport_service.get_airport_by_code(airport_code)

            if not airport:
                return jsonify(
                    {"success": False, "error": f"Airport not found: {airport_code}"}
                ), 404

            return jsonify({"success": True, "airport": airport})
        finally:
            session.close()

    except Exception as e:
        logger.error(f"Error getting airport {airport_code}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/airports/<airport_code>/nearby")
def get_aircraft_near_airport(airport_code):
    """获取机场周边飞机"""
    try:
        radius_km = float(request.args.get("radius_km", 1000))
        hours_back = float(request.args.get("hours_back", 0.5))
        limit = int(request.args.get("limit", 500))

        session = db_manager.get_session()
        try:
            airport_service = AirportService(session, config.config if config else {})
            result = airport_service.get_aircraft_near_airport(
                airport_code, radius_km, hours_back, limit
            )

            if "error" in result and not result.get("aircraft"):
                return jsonify({"success": False, "error": result["error"]}), 404

            return jsonify({"success": True, **result})
        finally:
            session.close()

    except Exception as e:
        logger.error(f"Error getting aircraft near airport {airport_code}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/airports/<airport_code>/realtime-aircraft")
def get_realtime_aircraft_near_airport(airport_code: str):
    """获取机场周边FR24实时飞机位置

    从aircraft_realtime_positions表查询最新飞机位置数据。
    支持按机型、涂装等筛选。
    """
    try:
        import math

        from sqlalchemy import bindparam, text

        radius_km_param = request.args.get("radius_km")
        radius_km = float(radius_km_param) if radius_km_param else None
        minutes_back = float(request.args.get("minutes_back", 10))
        limit = int(request.args.get("limit", 500))
        aircraft_type = request.args.get("aircraft_type", "")
        has_livery = request.args.get("has_livery", "")
        flight_numbers_param = request.args.get("flight_numbers", "")
        flight_numbers_filter = (
            [fn.strip().upper() for fn in flight_numbers_param.split(",") if fn.strip()]
            if flight_numbers_param
            else []
        )

        db_session = db_manager.get_session()
        try:
            # Get airport coordinates
            airport_service = AirportService(db_session, config.config if config else {})
            airport = airport_service.get_airport_by_code(airport_code)

            if not airport:
                return jsonify(
                    {"success": False, "error": f"Airport not found: {airport_code}"}
                ), 404

            airport_lat = airport["latitude"]
            airport_lon = airport["longitude"]

            # Build query for latest positions from aircraft_realtime_positions
            minutes_back_int = int(minutes_back)
            query_params = {}

            # Geographic filter clause (optional)
            geo_filter_clause = ""
            if radius_km:
                lat_delta = radius_km / 111.0
                lon_delta = radius_km / (111.0 * max(0.1, math.cos(math.radians(airport_lat))))
                query_params.update(
                    {
                        "min_lat": airport_lat - lat_delta,
                        "max_lat": airport_lat + lat_delta,
                        "min_lon": airport_lon - lon_delta,
                        "max_lon": airport_lon + lon_delta,
                    }
                )
                geo_filter_clause = "AND latitude BETWEEN :min_lat AND :max_lat AND longitude BETWEEN :min_lon AND :max_lon"

            # Flight numbers filter clause (optional). `IN` with an expanding
            # bind parameter rather than Postgres's `= ANY(:param)`, which SQLite
            # cannot parse.
            flight_numbers_clause = ""
            if flight_numbers_filter:
                flight_numbers_clause = "WHERE UPPER(flight_number) IN :flight_numbers"
                query_params["flight_numbers"] = flight_numbers_filter

            latest_positions = latest_rows(
                columns="""fr24_id, flight_number, callsign, registration, aircraft_type,
                        latitude, longitude, altitude, ground_speed, heading,
                        vertical_speed, squawk, origin_iata, destination_iata,
                        on_ground, fr24_timestamp, scraped_at""",
                source="aircraft_realtime_positions",
                partition_by="fr24_id",
                order_by="scraped_at DESC",
                where=f"""scraped_at >= {
                    minutes_ago(minutes_back_int, is_postgres=db_manager.is_postgres)
                }
                      {geo_filter_clause}""",
                is_postgres=db_manager.is_postgres,
            )

            query = f"""
                WITH latest_positions AS (
                    {latest_positions}
                )
                SELECT * FROM latest_positions
                {flight_numbers_clause}
            """

            # Execute query
            statement = text(query)
            if flight_numbers_filter:
                statement = statement.bindparams(bindparam("flight_numbers", expanding=True))
            result = db_session.execute(statement, query_params)

            # Process results and calculate distance
            aircraft_list = []
            for row in result:
                if row.latitude is None or row.longitude is None:
                    continue

                lat = float(row.latitude)
                lon = float(row.longitude)

                # Haversine distance calculation
                R = 6371  # Earth radius in km
                lat1, lon1 = math.radians(airport_lat), math.radians(airport_lon)
                lat2, lon2 = math.radians(lat), math.radians(lon)
                dlat, dlon = lat2 - lat1, lon2 - lon1
                a = (
                    math.sin(dlat / 2) ** 2
                    + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
                )
                distance_km = 2 * R * math.asin(math.sqrt(a))

                if radius_km and distance_km > radius_km:
                    continue

                # Filter by aircraft type
                if aircraft_type and row.aircraft_type:
                    if aircraft_type.upper() not in row.aircraft_type.upper():
                        continue

                # Determine flight status based on realtime position data
                altitude = row.altitude or 0
                vertical_rate = row.vertical_speed or 0

                if altitude < 500 or row.on_ground:
                    status = "ground"
                elif distance_km < 50 and altitude < 10000:
                    if vertical_rate < -300:
                        status = "approaching"
                    elif vertical_rate > 300:
                        status = "departing"
                    else:
                        status = "approaching" if altitude < 5000 else "cruising"
                else:
                    status = "cruising"

                aircraft_data = {
                    "fr24_id": row.fr24_id,
                    "registration": row.registration,
                    "flight_number": row.flight_number,
                    "callsign": row.callsign,
                    "aircraft_type": row.aircraft_type,
                    "latitude": lat,
                    "longitude": lon,
                    "altitude": altitude,
                    "altitude_baro": altitude,  # Alias for frontend compatibility
                    "ground_speed": row.ground_speed,
                    "heading": row.heading,
                    "track": row.heading,  # Alias for frontend compatibility
                    "vertical_speed": row.vertical_speed,
                    "squawk": row.squawk,
                    "origin_iata": row.origin_iata,
                    "destination_iata": row.destination_iata,
                    "on_ground": row.on_ground,
                    "distance_km": round(distance_km, 2),
                    "flight_status": status,
                    "scraped_at": _to_iso(row.scraped_at),
                    # Frontend compatibility fields (not available in realtime data)
                    "is_military": False,
                    "is_widebody": False,
                    "is_cargo": False,
                }
                aircraft_list.append(aircraft_data)

            # Sort by distance and limit
            aircraft_list.sort(key=lambda x: x["distance_km"])
            aircraft_list = aircraft_list[:limit]

            # Match aircraft with flight schedules
            airport_iata = airport.get("iata_code", "").upper()
            if airport_iata:
                is_postgres = db_manager.is_postgres
                schedule_query = f"""
                    SELECT
                        fs.id as schedule_id,
                        fs.flight_number,
                        fs.aircraft_registration,
                        fs.scheduled_time,
                        fs.status,
                        fs.flight_type
                    FROM flight_schedules fs
                    WHERE fs.airport_iata = :airport_iata
                      AND fs.scheduled_time BETWEEN
                          {minutes_from_now(-2 * 60, is_postgres=is_postgres)}
                          AND {minutes_from_now(4 * 60, is_postgres=is_postgres)}
                """
                schedule_result = db_session.execute(
                    text(schedule_query), {"airport_iata": airport_iata}
                )
                schedules = [dict(row._mapping) for row in schedule_result]

                # Build lookup dicts for matching
                schedule_by_flight_number: dict[str, dict] = {}
                schedule_by_registration: dict[str, dict] = {}
                for sched in schedules:
                    fn = (sched.get("flight_number") or "").upper()
                    reg = (sched.get("aircraft_registration") or "").upper()
                    if fn and fn not in schedule_by_flight_number:
                        schedule_by_flight_number[fn] = sched
                    if reg and reg not in schedule_by_registration:
                        schedule_by_registration[reg] = sched

                # Match each aircraft
                for ac in aircraft_list:
                    ac_fn = (ac.get("flight_number") or "").upper()
                    ac_reg = (ac.get("registration") or "").upper()

                    # Priority: flight_number > registration
                    matched_schedule = None
                    if ac_fn and ac_fn in schedule_by_flight_number:
                        matched_schedule = schedule_by_flight_number[ac_fn]
                    elif ac_reg and ac_reg in schedule_by_registration:
                        matched_schedule = schedule_by_registration[ac_reg]

                    if matched_schedule:
                        ac["schedule_id"] = matched_schedule.get("schedule_id")
                        ac["has_schedule"] = True
                        sched_time = matched_schedule.get("scheduled_time")
                        ac["scheduled_time"] = (
                            convert_utc_to_beijing(_to_iso(sched_time)) if sched_time else None
                        )
                        ac["schedule_status"] = matched_schedule.get("status")
                        ac["schedule_flight_type"] = matched_schedule.get("flight_type")
                    else:
                        ac["schedule_id"] = None
                        ac["has_schedule"] = False
                        ac["scheduled_time"] = None
                        ac["schedule_status"] = None
                        ac["schedule_flight_type"] = None

            # Categorize
            approaching = [a for a in aircraft_list if a["flight_status"] == "approaching"]
            departing = [a for a in aircraft_list if a["flight_status"] == "departing"]
            cruising = [a for a in aircraft_list if a["flight_status"] == "cruising"]
            ground = [a for a in aircraft_list if a["flight_status"] == "ground"]

            return jsonify(
                {
                    "success": True,
                    "airport": airport,
                    "radius_km": radius_km,
                    "query_time": datetime.utcnow().isoformat(),
                    "total_count": len(aircraft_list),
                    "approaching_count": len(approaching),
                    "departing_count": len(departing),
                    "cruising_count": len(cruising),
                    "ground_count": len(ground),
                    "aircraft": aircraft_list,
                }
            )
        finally:
            db_session.close()

    except Exception as e:
        logger.error(f"Error getting realtime aircraft near airport {airport_code}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/airports/popular")
def get_popular_airports():
    """获取热门机场列表"""
    try:
        country_code = request.args.get("country")
        limit = int(request.args.get("limit", 50))

        session = db_manager.get_session()
        try:
            airport_service = AirportService(session, config.config if config else {})
            airports = airport_service.get_popular_airports(country_code, limit)

            return jsonify({"success": True, "airports": airports, "count": len(airports)})
        finally:
            session.close()

    except Exception as e:
        logger.error(f"Error getting popular airports: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/search/aircraft")
def super_search_aircraft():
    """超级搜索飞机"""
    try:
        registration = request.args.get("registration", "").strip() or None
        flight_number = request.args.get("flight_number", "").strip() or None
        type_series = request.args.get("type_series", "").strip() or None
        operator = request.args.get("operator", "").strip() or None

        is_military_str = request.args.get("is_military")
        is_military = None
        if is_military_str is not None:
            is_military = is_military_str.lower() == "true"

        is_widebody_str = request.args.get("is_widebody")
        is_widebody = None
        if is_widebody_str is not None:
            is_widebody = is_widebody_str.lower() == "true"

        is_cargo_str = request.args.get("is_cargo")
        is_cargo = None
        if is_cargo_str is not None:
            is_cargo = is_cargo_str.lower() == "true"

        hours_back_str = request.args.get("hours_back")
        hours_back = float(hours_back_str) if hours_back_str else None
        limit = int(request.args.get("limit", 100))

        session = db_manager.get_session()
        try:
            aircraft_service = AircraftService(session, config.config if config else {})
            results = aircraft_service.search_aircraft(
                registration=registration,
                flight_number=flight_number,
                type_series=type_series,
                operator=operator,
                is_military=is_military,
                is_widebody=is_widebody,
                is_cargo=is_cargo,
                hours_back=hours_back,
                limit=limit,
            )

            # 转换时间为北京时间，并转换图片路径
            for result in results:
                if result.get("snapshot_time"):
                    result["snapshot_time"] = convert_utc_to_beijing(result["snapshot_time"])
                result["timezone"] = "Asia/Shanghai"
                # 转换图片路径为完整URL
                transform_image_paths(result)

            return jsonify({"success": True, "aircraft": results, "count": len(results)})
        finally:
            session.close()

    except Exception as e:
        logger.error(f"Error in super search: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/aircraft/<identifier>/live")
def get_aircraft_live(identifier):
    """获取飞机实时位置"""
    try:
        session = db_manager.get_session()
        try:
            aircraft_service = AircraftService(session, config.config if config else {})
            result = aircraft_service.get_aircraft_live_position(identifier)

            if not result:
                return jsonify(
                    {"success": False, "error": f"Aircraft not found: {identifier}"}
                ), 404

            # 转换时间为北京时间，并转换图片路径
            if result.get("snapshot_time"):
                result["snapshot_time"] = convert_utc_to_beijing(result["snapshot_time"])
            result["timezone"] = "Asia/Shanghai"
            # 转换图片路径为完整URL
            transform_image_paths(result)

            return jsonify({"success": True, "aircraft": result})
        finally:
            session.close()

    except Exception as e:
        logger.error(f"Error getting live position for {identifier}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/aircraft/<identifier>/details")
def get_aircraft_details_api(identifier):
    """获取飞机详细信息"""
    try:
        session = db_manager.get_session()
        try:
            aircraft_service = AircraftService(session, config.config if config else {})
            result = aircraft_service.get_aircraft_details(identifier)

            if not result:
                return jsonify(
                    {"success": False, "error": f"Aircraft not found: {identifier}"}
                ), 404

            # 转换时间为北京时间，并转换图片路径
            if result.get("last_updated"):
                result["last_updated"] = convert_utc_to_beijing(result["last_updated"])
            # 转换图片路径为完整URL
            transform_image_paths(result)

            return jsonify({"success": True, "aircraft": result})
        finally:
            session.close()

    except Exception as e:
        logger.error(f"Error getting aircraft details for {identifier}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/aircraft/<identifier>/history")
def get_aircraft_history_api(identifier):
    """获取飞机历史轨迹"""
    try:
        # 解析时间参数 (前端传入北京时间)
        date_str = request.args.get("date")
        start_time_str = request.args.get("start_time")
        end_time_str = request.args.get("end_time")
        limit = int(request.args.get("limit", 1000))

        date = None
        start_time = None
        end_time = None

        if date_str:
            try:
                date = datetime.strptime(date_str, "%Y-%m-%d")
            except ValueError:
                pass

        if start_time_str:
            start_time = convert_beijing_to_utc(start_time_str)

        if end_time_str:
            end_time = convert_beijing_to_utc(end_time_str)

        session = db_manager.get_session()
        try:
            aircraft_service = AircraftService(session, config.config if config else {})
            track_points = aircraft_service.get_aircraft_history(
                identifier, date, start_time, end_time, limit
            )

            # 转换时间为北京时间
            for point in track_points:
                if point.get("datetime"):
                    point["datetime_beijing"] = convert_utc_to_beijing(point["datetime"])
                point["timezone"] = "Asia/Shanghai"

            return jsonify(
                {
                    "success": True,
                    "identifier": identifier,
                    "tracks": track_points,
                    "count": len(track_points),
                }
            )
        finally:
            session.close()

    except Exception as e:
        logger.error(f"Error getting aircraft history for {identifier}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/aircraft/<identifier>/flight-dates")
def get_aircraft_flight_dates(identifier):
    """获取飞机有飞行记录的日期列表"""
    try:
        days_back = int(request.args.get("days_back", 30))

        session = db_manager.get_session()
        try:
            aircraft_service = AircraftService(session, config.config if config else {})
            dates = aircraft_service.get_aircraft_flight_dates(identifier, days_back)

            return jsonify(
                {"success": True, "identifier": identifier, "dates": dates, "count": len(dates)}
            )
        finally:
            session.close()

    except Exception as e:
        logger.error(f"Error getting flight dates for {identifier}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/admin/import-data", methods=["POST"])
def import_data_api():
    """
    Bulk import data (admin endpoint)
    Supports multiple tables: aircraft_snapshots, airports, aircraft_static_info, report_cooldowns
    """
    try:
        # Check for admin secret in header
        admin_secret = request.headers.get("X-Admin-Secret")
        expected_secret = os.environ.get("ADMIN_SECRET", "flight-matrix-admin-2026")

        if admin_secret != expected_secret:
            return jsonify({"success": False, "error": "Unauthorized"}), 401

        data = request.get_json()
        if not data:
            return jsonify({"success": False, "error": "Missing request body"}), 400

        from src.data.models import AircraftSnapshot, AircraftStaticInfo, Airport, GeographicRegion

        # Map table names to model classes
        table_models = {
            "snapshots": AircraftSnapshot,  # Legacy support
            "aircraft_snapshots": AircraftSnapshot,
            "airports": Airport,
            "aircraft_static_info": AircraftStaticInfo,
            "geographic_regions": GeographicRegion,
        }

        session = db_manager.get_session()
        try:
            imported = 0
            errors = 0
            total = 0

            for table_name, records in data.items():
                if table_name not in table_models:
                    logger.warning(f"Unknown table: {table_name}, skipping")
                    continue

                model_class = table_models[table_name]
                total += len(records)

                for record_data in records:
                    try:
                        # Remove 'id' field to let database auto-generate
                        record_data.pop("id", None)

                        # Clean data types for aircraft_snapshots
                        if table_name in ("snapshots", "aircraft_snapshots"):
                            if record_data.get("altitude_baro") == "ground":
                                record_data["altitude_baro"] = None
                            if record_data.get("altitude_geom") == "ground":
                                record_data["altitude_geom"] = None

                        # Create model object
                        obj = model_class(**record_data)
                        session.add(obj)
                        imported += 1

                        # Commit every 500 records for performance
                        if imported % 500 == 0:
                            session.commit()

                    except Exception as e:
                        errors += 1
                        logger.error(f"Error importing {table_name} record: {e}")
                        session.rollback()

            # Final commit
            session.commit()

            return jsonify(
                {"success": True, "imported": imported, "errors": errors, "total": total}
            )
        finally:
            session.close()

    except Exception as e:
        logger.error(f"Error in import_data_api: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


# ==================== Aircraft Info API ====================


@app.route("/api/aircraft/<registration>/recent-flights")
def get_aircraft_recent_flights(registration: str):
    """获取飞机最近10次起降航班信息"""
    try:
        from sqlalchemy import text

        session = db_manager.get_session()
        try:
            result = session.execute(
                text("""
                SELECT
                    flight_type,
                    airport_iata,
                    remote_airport_iata,
                    remote_airport_name,
                    scheduled_time,
                    status
                FROM flight_schedules
                WHERE aircraft_registration = :reg
                ORDER BY scheduled_time DESC
                LIMIT 10
            """),
                {"reg": registration.upper()},
            ).fetchall()

            flights = []
            for row in result:
                flights.append(
                    {
                        "flight_type": row[0],
                        "airport_iata": row[1],
                        "remote_airport_iata": row[2],
                        "remote_airport_name": row[3],
                        "scheduled_time": row[4].isoformat() if row[4] else None,
                        "status": row[5],
                    }
                )

            return jsonify(
                {
                    "success": True,
                    "registration": registration.upper(),
                    "flights": flights,
                    "count": len(flights),
                }
            )
        finally:
            session.close()

    except Exception as e:
        logger.error(f"Error getting recent flights for {registration}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/aircraft/<identifier>/images")
def get_aircraft_images_api(identifier):
    """获取飞机图片URL列表（包含元数据）"""
    try:
        # 获取飞机信息（可能是注册号或 hex）
        registration = identifier.upper()
        from sqlalchemy import text

        session = db_manager.get_session()
        try:
            # 尝试用 hex 查找对应的注册号
            result = session.execute(
                text("""
                SELECT registration
                FROM aircraft_snapshots
                WHERE hex = :hex AND registration IS NOT NULL AND registration != ''
                ORDER BY snapshot_time DESC
                LIMIT 1
            """),
                {"hex": identifier.lower()},
            ).fetchone()

            if result and result[0]:
                registration = result[0]

            # 从 aircraft_images 表获取所有图片（按拍摄时间排序）
            images_result = session.execute(
                text("""
                SELECT
                    image_path, photographer, photo_date, location,
                    airport_name, notes, display_order, is_primary,
                    jetphotos_id, source_url, upload_date
                FROM aircraft_images
                WHERE registration = :reg
                AND image_path IS NOT NULL
                AND image_path != ''
                ORDER BY photo_date DESC NULLS LAST
            """),
                {"reg": registration},
            ).fetchall()

            # 构建带元数据的图片列表
            images_with_metadata = []
            image_urls = []
            for row in images_result:
                url = get_image_url(row[0])
                if url:
                    image_urls.append(url)
                    images_with_metadata.append(
                        {
                            "url": url,
                            "photographer": row[1],
                            "photo_date": row[2].isoformat() if row[2] else None,
                            "location": row[3],
                            "airport_name": row[4],
                            "notes": row[5],
                            "display_order": row[6],
                            "is_primary": row[7],
                            "jetphotos_id": row[8],
                            "source_url": row[9],
                            "upload_date": row[10].isoformat() if row[10] else None,
                        }
                    )

            return jsonify(
                {
                    "success": True,
                    "identifier": identifier,
                    "registration": registration,
                    "images": image_urls,  # Simple URL list for backward compatibility
                    "images_with_metadata": images_with_metadata,  # Rich metadata
                    "count": len(image_urls),
                }
            )
        finally:
            session.close()

    except Exception as e:
        logger.error(f"Error getting aircraft images for {identifier}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/aircraft/<identifier>/static-info")
def get_aircraft_static_info_api(identifier):
    """获取飞机静态信息（所有者、运营商等）"""
    try:
        registration = identifier.upper()

        from sqlalchemy import text

        session = db_manager.get_session()
        try:
            # 先尝试直接查找
            result = session.execute(
                text("""
                SELECT
                    registration, hex_code, owner, operator,
                    manufacturer, model, aircraft_type,
                    serial_number, year_built, country_of_registration,
                    organization, livery_type
                FROM aircraft_static_info
                WHERE registration = :reg OR hex_code = :hex
            """),
                {"reg": registration, "hex": identifier.lower()},
            ).fetchone()

            if result:
                # Get images from aircraft_images table
                images_result = session.execute(
                    text("""
                    SELECT image_path FROM aircraft_images
                    WHERE registration = :reg
                    ORDER BY display_order LIMIT 3
                """),
                    {"reg": result[0]},
                ).fetchall()
                image_paths = [get_image_url(row[0]) for row in images_result if row[0]]

                static_info = {
                    "registration": result[0],
                    "hex_code": result[1],
                    "owner": result[2],
                    "operator": result[3] or result[10],  # fallback to organization
                    "manufacturer": result[4],
                    "model": result[5],
                    "aircraft_type": result[6],
                    "serial_number": result[7],
                    "year_built": result[8],
                    "country_of_registration": result[9],
                    "organization": result[10],
                    "livery_type": result[11],
                    "images": image_paths,
                }

                return jsonify({"success": True, "static_info": static_info})
            else:
                return jsonify({"success": False, "error": "Static info not found"}), 404

        finally:
            session.close()

    except Exception as e:
        logger.error(f"Error getting static info for {identifier}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


# ==================== Multi-User Management API ====================

# Global services for multi-user mode
user_service = None
subscription_service = None
filter_service = None


def get_multi_user_services():
    """Initialize multi-user services if not already done."""
    global user_service, subscription_service, filter_service

    if user_service is None:
        from src.services.filter_service import FilterService
        from src.services.subscription_service import SubscriptionService
        from src.services.user_service import UserService

        # Ensure tables exist
        db_manager.ensure_multi_user_tables_exist()

        user_service = UserService(db_manager)
        subscription_service = SubscriptionService(db_manager, config)
        filter_service = FilterService(db_manager)

    return user_service, subscription_service, filter_service


# Admin Pages
@app.route("/admin")
@admin_required
def admin_main_page():
    """Admin main dashboard - shows links to all admin features."""
    return render_template("admin_dashboard.html")


@app.route("/admin/users")
@login_required
def admin_users_page():
    """User management admin page - 需要登录."""
    return render_template("admin_users.html")


@app.route("/admin/reports")
@login_required
def admin_reports_page():
    """Report history management admin page - 需要登录."""
    return render_template("admin_reports.html")


@app.route("/admin/track")
@admin_required
def admin_track_page():
    """Admin flight track page - 飞机轨迹查询 (管理员)."""
    return render_template("search_track.html")


@app.route("/admin/dashboard")
@admin_required
def admin_dashboard_page():
    """Admin dashboard page - 用户仪表盘 (管理员)."""
    return render_template("user_dashboard.html")


@app.route("/admin/filters")
@admin_required
def admin_filters_page():
    """Admin filters page - 过滤器管理 (管理员)."""
    return render_template("user_filters.html")


@app.route("/admin/aircraft-query")
@admin_required
def admin_aircraft_query_page():
    """Admin aircraft query page - comprehensive aircraft data lookup."""
    return render_template("admin_aircraft_query.html")


@app.route("/api/admin/aircraft-query/<registration>", methods=["GET"])
@admin_required
def api_admin_aircraft_query(registration: str):
    """Query comprehensive data for a specific aircraft registration.

    Searches across multiple tables:
    - aircraft_static_info: Static aircraft information
    - aircraft_snapshots: ADS-B position history
    - aircraft_realtime_positions: FR24 realtime positions
    - aircraft_images: Aircraft photos
    - flight_schedules: Flight schedule data
    - note_aircraft_analysis: Social media mentions (JSONB search)
    - aircraft_attention_aggregate: Attention metrics
    """
    from sqlalchemy import text

    try:
        import time as _time

        query_times: dict[str, float] = {}
        total_start = _time.time()

        # Normalize registration to uppercase for case-insensitive search
        reg_upper = registration.upper().strip()

        if not reg_upper:
            return jsonify({"success": False, "error": "Registration is required"}), 400

        session = db_manager.get_session()
        try:
            result_data: dict[str, Any] = {
                "success": True,
                "registration": reg_upper,
                "static_info": None,
                "snapshots": {"count": 0, "recent": []},
                "realtime_positions": {"count": 0, "recent": []},
                "images": {"count": 0, "items": []},
                "flight_schedules": {"total": 0, "items": []},
                "social_mentions": {"count": 0, "items": []},
                "attention_metrics": None,
            }

            # 1. Query aircraft_static_info
            t0 = _time.time()
            static_query = text("""
                SELECT id, registration, hex_code, aircraft_type, owner, operator,
                       manufacturer, model, serial_number, year_built,
                       country_of_registration, ai_analysis, images_downloaded,
                       images_updated_at, last_updated, data_source,
                       ad_status, ad_owner, ad_engines, ad_seats, ad_location, ad_delivery_date,
                       ps_status, ps_airline, ps_first_flight, ps_delivery_date,
                       jp_airline, jp_cn
                FROM aircraft_static_info
                WHERE UPPER(registration) = :reg
            """)
            static_result = session.execute(static_query, {"reg": reg_upper}).fetchone()
            if static_result:
                result_data["static_info"] = {
                    "id": static_result.id,
                    "registration": static_result.registration,
                    "hex_code": static_result.hex_code,
                    "aircraft_type": static_result.aircraft_type,
                    "owner": static_result.owner,
                    "operator": static_result.operator,
                    "manufacturer": static_result.manufacturer,
                    "model": static_result.model,
                    "serial_number": static_result.serial_number,
                    "year_built": static_result.year_built,
                    "country_of_registration": static_result.country_of_registration,
                    "ai_analysis": static_result.ai_analysis,
                    "images_downloaded": static_result.images_downloaded,
                    "images_updated_at": static_result.images_updated_at.isoformat()
                    if static_result.images_updated_at
                    else None,
                    "last_updated": static_result.last_updated.isoformat()
                    if static_result.last_updated
                    else None,
                    "data_source": static_result.data_source,
                    "ad_status": static_result.ad_status,
                    "ad_owner": static_result.ad_owner,
                    "ad_engines": static_result.ad_engines,
                    "ad_seats": static_result.ad_seats,
                    "ad_location": static_result.ad_location,
                    "ad_delivery_date": static_result.ad_delivery_date,
                    "ps_status": static_result.ps_status,
                    "ps_airline": static_result.ps_airline,
                    "ps_first_flight": static_result.ps_first_flight.isoformat()
                    if static_result.ps_first_flight
                    else None,
                    "ps_delivery_date": static_result.ps_delivery_date.isoformat()
                    if static_result.ps_delivery_date
                    else None,
                    "jp_airline": static_result.jp_airline,
                    "jp_cn": static_result.jp_cn,
                }
            query_times["static_info"] = _time.time() - t0

            # 2. Query aircraft_snapshots (ADS-B positions)
            t0 = _time.time()
            # Use direct registration match (indexed) for performance on large table
            # Try both upper and original case
            snapshots_query = text("""
                SELECT id, snapshot_time, hex, flight_number, registration, aircraft_type,
                       latitude, longitude, altitude_baro, altitude_geom, ground_speed,
                       track, vertical_rate, squawk, emergency, is_military, is_interesting
                FROM aircraft_snapshots
                WHERE registration = :reg OR registration = :reg_orig
                ORDER BY snapshot_time DESC
                LIMIT 20
            """)
            snapshots_result = session.execute(
                snapshots_query, {"reg": reg_upper, "reg_orig": registration.strip()}
            )
            snapshots_list = []
            for row in snapshots_result:
                snapshots_list.append(
                    {
                        "id": row.id,
                        "snapshot_time": row.snapshot_time.isoformat()
                        if row.snapshot_time
                        else None,
                        "hex": row.hex,
                        "flight_number": row.flight_number,
                        "aircraft_type": row.aircraft_type,
                        "latitude": float(row.latitude) if row.latitude else None,
                        "longitude": float(row.longitude) if row.longitude else None,
                        "altitude_baro": row.altitude_baro,
                        "altitude_geom": row.altitude_geom,
                        "ground_speed": float(row.ground_speed) if row.ground_speed else None,
                        "track": float(row.track) if row.track else None,
                        "vertical_rate": row.vertical_rate,
                        "squawk": row.squawk,
                        "emergency": row.emergency,
                        "is_military": row.is_military,
                        "is_interesting": row.is_interesting,
                    }
                )
            # Skip expensive COUNT on large table, just report items found
            result_data["snapshots"] = {"count": len(snapshots_list), "recent": snapshots_list}
            query_times["snapshots"] = _time.time() - t0

            # 3. Query aircraft_realtime_positions (FR24 positions)
            # Check if table exists first
            t0 = _time.time()
            realtime_exists = _table_exists(session, "aircraft_realtime_positions")

            if realtime_exists:
                # Use subquery to force registration index usage, then sort
                # Without this, PostgreSQL uses scraped_at index which is much slower
                realtime_query = text("""
                    SELECT * FROM (
                        SELECT id, fr24_id, flight_number, callsign, registration, aircraft_type,
                               latitude, longitude, altitude, ground_speed, heading,
                               vertical_speed, squawk, origin_iata, destination_iata,
                               on_ground, fr24_timestamp, scraped_at
                        FROM aircraft_realtime_positions
                        WHERE registration = :reg
                        LIMIT 1000
                    ) sub
                    ORDER BY scraped_at DESC
                    LIMIT 20
                """)
                realtime_result = session.execute(realtime_query, {"reg": reg_upper})
                realtime_list = []
                for row in realtime_result:
                    realtime_list.append(
                        {
                            "id": row.id,
                            "callsign": row.callsign,
                            "aircraft_type": row.aircraft_type,
                            "latitude": float(row.latitude) if row.latitude else None,
                            "longitude": float(row.longitude) if row.longitude else None,
                            "altitude": row.altitude,
                            "ground_speed": row.ground_speed,
                            "heading": row.heading,
                            "vertical_speed": row.vertical_speed,
                            "origin_iata": row.origin_iata,
                            "destination_iata": row.destination_iata,
                            "flight_number": row.flight_number,
                            "fr24_id": row.fr24_id,
                            "on_ground": row.on_ground,
                            "fr24_timestamp": row.fr24_timestamp.isoformat()
                            if row.fr24_timestamp
                            else None,
                            "scraped_at": row.scraped_at.isoformat() if row.scraped_at else None,
                        }
                    )
                result_data["realtime_positions"] = {
                    "count": len(realtime_list),
                    "recent": realtime_list,
                }
            query_times["realtime_positions"] = _time.time() - t0

            # 4. Query aircraft_images
            t0 = _time.time()
            images_query = text("""
                SELECT id, registration, image_path, source_url, source,
                       photographer, photo_date, upload_date, location,
                       airport_icao, airport_name, notes, display_order,
                       is_primary, width, height, jetphotos_id, created_at
                FROM aircraft_images
                WHERE UPPER(registration) = :reg
                  AND image_path IS NOT NULL
                  AND image_path != ''
                ORDER BY display_order ASC, created_at DESC
                LIMIT 20
            """)
            images_result = session.execute(images_query, {"reg": reg_upper})
            images_list = []
            for row in images_result:
                image_url = get_image_url(row.image_path)
                if not image_url:
                    continue  # Skip images without valid URL
                images_list.append(
                    {
                        "id": row.id,
                        "registration": row.registration,
                        "image_url": image_url,
                        "source_url": row.source_url,
                        "source": row.source,
                        "photographer": row.photographer,
                        "photo_date": row.photo_date.isoformat() if row.photo_date else None,
                        "upload_date": row.upload_date.isoformat() if row.upload_date else None,
                        "location": row.location,
                        "airport_icao": row.airport_icao,
                        "airport_name": row.airport_name,
                        "notes": row.notes,
                        "display_order": row.display_order,
                        "is_primary": row.is_primary,
                        "width": row.width,
                        "height": row.height,
                        "jetphotos_id": row.jetphotos_id,
                    }
                )
            result_data["images"] = {"count": len(images_list), "items": images_list}
            query_times["images"] = _time.time() - t0

            # 5. Query flight_schedules - use indexed lookup
            t0 = _time.time()
            schedules_query = text("""
                SELECT id, flight_type, airport_iata, airport_icao, flight_number,
                       callsign, fr24_flight_id, airline_name, airline_iata,
                       remote_airport_iata, remote_airport_name, aircraft_type,
                       aircraft_registration, scheduled_time, estimated_time,
                       actual_time, status, terminal, gate, scraped_at
                FROM flight_schedules
                WHERE aircraft_registration = :reg OR aircraft_registration = :reg_orig
                ORDER BY scheduled_time DESC
                LIMIT 30
            """)
            schedules_result = session.execute(
                schedules_query, {"reg": reg_upper, "reg_orig": registration.strip()}
            )
            schedules_list = []
            for row in schedules_result:
                schedules_list.append(
                    {
                        "id": row.id,
                        "flight_type": row.flight_type,
                        "airport_iata": row.airport_iata,
                        "airport_icao": row.airport_icao,
                        "flight_number": row.flight_number,
                        "callsign": row.callsign,
                        "fr24_flight_id": row.fr24_flight_id,
                        "airline_name": row.airline_name,
                        "airline_iata": row.airline_iata,
                        "remote_airport_iata": row.remote_airport_iata,
                        "remote_airport_name": row.remote_airport_name,
                        "aircraft_type": row.aircraft_type,
                        "scheduled_time": row.scheduled_time.isoformat()
                        if row.scheduled_time
                        else None,
                        "estimated_time": row.estimated_time.isoformat()
                        if row.estimated_time
                        else None,
                        "actual_time": row.actual_time.isoformat() if row.actual_time else None,
                        "status": row.status,
                        "terminal": row.terminal,
                        "gate": row.gate,
                        "scraped_at": row.scraped_at.isoformat() if row.scraped_at else None,
                    }
                )
            result_data["flight_schedules"] = {
                "total": len(schedules_list),
                "items": schedules_list,
            }
            query_times["flight_schedules"] = _time.time() - t0

            # 6. Query note_aircraft_analysis (social media mentions using JSONB)
            # Check if table exists
            t0 = _time.time()
            analysis_exists = _table_exists(session, "note_aircraft_analysis")

            if analysis_exists:
                # Use JSONB operator to search for registration in the array
                # Include original note data for display
                mentions_query = text("""
                    SELECT naa.id, naa.note_id, naa.source_type, naa.registrations,
                           naa.attention_index, naa.attention_level, naa.attention_reason,
                           naa.content_type, naa.sentiment, naa.topics, naa.analyzed_at,
                           xn.title, xn.author_name, xn.author_id, xn.content,
                           xn.like_count, xn.collect_count, xn.comment_count, xn.share_count,
                           xn.location, xn.tags,
                           xn.image_urls as original_image_urls,
                           xn.image_paths,
                           xn.scraped_at as note_scraped_at
                    FROM note_aircraft_analysis naa
                    LEFT JOIN xiaohongshu_notes xn ON naa.note_id = xn.note_id
                    -- Postgres stores registrations as JSONB; SQLite as TEXT.
                    -- `LIKE` over the serialized JSON works for both as long
                    -- as the reg is quoted (which it is in the JSON array).
                    WHERE CAST(naa.registrations AS TEXT) LIKE :reg_pattern
                    ORDER BY naa.analyzed_at DESC
                    LIMIT 20
                """)
                mentions_result = session.execute(
                    mentions_query, {"reg_pattern": f'%"{reg_upper}"%'}
                )
                mentions_list = []
                for row in mentions_result:
                    # Use S3 image_paths if available, fallback to original URLs
                    image_urls = []
                    if row.image_paths:
                        paths = row.image_paths if isinstance(row.image_paths, list) else []
                        # Reduce stored public URLs back to object keys, then
                        # re-render them against the active target's base URL
                        for p in paths[:5]:
                            if not p:
                                continue
                            image_urls.append(get_image_url(ObjectStorage.strip_public_prefix(p)))

                    # Fallback to original URLs if no S3 images available
                    if not image_urls and row.original_image_urls:
                        urls = (
                            row.original_image_urls
                            if isinstance(row.original_image_urls, list)
                            else []
                        )
                        image_urls = [url for url in urls if url][:5]
                    mentions_list.append(
                        {
                            "id": row.id,
                            "note_id": row.note_id,
                            "source_type": row.source_type,
                            "registrations": row.registrations,
                            "attention_index": row.attention_index,
                            "attention_level": row.attention_level,
                            "attention_reason": row.attention_reason,
                            "content_type": row.content_type,
                            "sentiment": row.sentiment,
                            "topics": row.topics,
                            "analyzed_at": row.analyzed_at.isoformat() if row.analyzed_at else None,
                            "title": row.title,
                            "author_name": row.author_name,
                            "author_id": row.author_id,
                            "content": row.content,
                            "like_count": row.like_count,
                            "collect_count": row.collect_count,
                            "comment_count": row.comment_count,
                            "share_count": row.share_count,
                            "location": row.location,
                            "tags": row.tags,
                            "image_urls": image_urls,  # S3 images preferred, fallback to original URLs
                            "note_scraped_at": row.note_scraped_at.isoformat()
                            if row.note_scraped_at
                            else None,
                        }
                    )
                result_data["social_mentions"] = {
                    "count": len(mentions_list),
                    "items": mentions_list,
                }
            query_times["social_mentions"] = _time.time() - t0

            # 7. Query aircraft_attention_aggregate
            t0 = _time.time()
            attention_exists = _table_exists(session, "aircraft_attention_aggregate")

            if attention_exists:
                attention_query = text("""
                    SELECT registration, total_mentions, avg_attention_index,
                           max_attention_index, mentions_7d, mentions_30d,
                           first_seen, last_seen, top_topics, sentiment_distribution,
                           source_distribution, content_type_distribution,
                           trending_score, updated_at
                    FROM aircraft_attention_aggregate
                    WHERE UPPER(registration) = :reg
                """)
                attention_result = session.execute(attention_query, {"reg": reg_upper}).fetchone()
                if attention_result:
                    result_data["attention_metrics"] = {
                        "registration": attention_result.registration,
                        "total_mentions": attention_result.total_mentions,
                        "avg_attention_index": float(attention_result.avg_attention_index)
                        if attention_result.avg_attention_index
                        else None,
                        "max_attention_index": attention_result.max_attention_index,
                        "mentions_7d": attention_result.mentions_7d,
                        "mentions_30d": attention_result.mentions_30d,
                        "first_seen": attention_result.first_seen.isoformat()
                        if attention_result.first_seen
                        else None,
                        "last_seen": attention_result.last_seen.isoformat()
                        if attention_result.last_seen
                        else None,
                        "top_topics": attention_result.top_topics,
                        "sentiment_distribution": attention_result.sentiment_distribution,
                        "source_distribution": attention_result.source_distribution,
                        "content_type_distribution": attention_result.content_type_distribution,
                        "trending_score": float(attention_result.trending_score)
                        if attention_result.trending_score
                        else None,
                        "updated_at": attention_result.updated_at.isoformat()
                        if attention_result.updated_at
                        else None,
                    }
            query_times["attention_metrics"] = _time.time() - t0

            # Add timing info to response
            query_times["total"] = _time.time() - total_start
            result_data["query_times_ms"] = {k: round(v * 1000, 2) for k, v in query_times.items()}

            return jsonify(result_data)
        finally:
            session.close()
    except Exception as e:
        logger.error(f"Error querying aircraft data for {registration}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/user/<email>/dashboard")
@login_required
def user_dashboard_page(email: str):
    """User dashboard page - 需要登录."""
    return render_template("user_dashboard.html", email=email)


@app.route("/user/<email>/filters")
@login_required
def user_filters_page(email: str):
    """User filter management page - 需要登录."""
    return render_template("user_filters.html", email=email)


# Admin API - User Management
@app.route("/api/admin/users", methods=["GET"])
def api_admin_list_users():
    """List users with pagination and filtering."""
    try:
        us, ss, fs = get_multi_user_services()

        page = int(request.args.get("page", 1))
        limit = int(request.args.get("limit", 20))
        status = request.args.get("status")
        search = request.args.get("search")
        tier = request.args.get("tier")

        offset = (page - 1) * limit
        users = us.list_users(status=status, limit=limit, offset=offset)

        # Apply search filter
        if search:
            search_lower = search.lower()
            users = [
                u
                for u in users
                if search_lower in (u.get("email") or "").lower()
                or search_lower in (u.get("name") or "").lower()
            ]

        # Apply tier filter
        if tier:
            users = [u for u in users if u.get("subscription", {}).get("tier") == tier]

        total = us.get_user_count(status=status)
        pages = (total + limit - 1) // limit

        return jsonify(
            {"success": True, "users": users, "total": total, "page": page, "pages": pages}
        )

    except Exception as e:
        logger.error(f"Error listing users: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/admin/users/stats", methods=["GET"])
def api_admin_user_stats():
    """Get user statistics."""
    try:
        us, ss, fs = get_multi_user_services()

        total = us.get_user_count()
        active = us.get_user_count(status="active")

        # Count by tier
        all_users = us.list_users(limit=10000)
        premium = len([u for u in all_users if u.get("subscription", {}).get("tier") == "premium"])
        enterprise = len(
            [u for u in all_users if u.get("subscription", {}).get("tier") == "enterprise"]
        )

        return jsonify(
            {
                "success": True,
                "stats": {
                    "total": total,
                    "active": active,
                    "premium": premium,
                    "enterprise": enterprise,
                },
            }
        )

    except Exception as e:
        logger.error(f"Error getting user stats: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/admin/users", methods=["POST"])
def api_admin_create_user():
    """Create a new user."""
    try:
        us, ss, fs = get_multi_user_services()
        data = request.get_json()

        email = data.get("email")
        name = data.get("name")
        tier = data.get("tier", "basic")
        generate_api_key = data.get("generate_api_key", False)

        if not email:
            return jsonify({"success": False, "error": "Email is required"}), 400

        user = us.create_user(email, name, tier, generate_api_key)
        if user:
            return jsonify({"success": True, "user": user.to_dict()})
        else:
            return jsonify(
                {"success": False, "error": "Failed to create user (email may already exist)"}
            ), 400

    except Exception as e:
        logger.error(f"Error creating user: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/admin/users/<int:user_id>", methods=["GET"])
def api_admin_get_user(user_id: int):
    """Get user details."""
    try:
        us, ss, fs = get_multi_user_services()
        user = us.get_user_with_subscription(user_id)

        if user:
            return jsonify({"success": True, "user": user})
        else:
            return jsonify({"success": False, "error": "User not found"}), 404

    except Exception as e:
        logger.error(f"Error getting user {user_id}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/admin/users/<int:user_id>", methods=["PUT"])
def api_admin_update_user(user_id: int):
    """Update user and optionally their subscription settings."""
    try:
        us, ss, fs = get_multi_user_services()
        data = request.get_json()

        # Update basic user info
        success = us.update_user(user_id, name=data.get("name"), status=data.get("status"))

        if not success:
            return jsonify({"success": False, "error": "Failed to update user"}), 400

        # Update subscription settings if provided
        subscription_data = data.get("subscription")
        if subscription_data:
            # Get user's active subscription
            subscription = ss.get_user_active_subscription(user_id)
            if subscription:
                # Prepare feature overrides
                feature_overrides = {}

                # Feature toggles
                if "enable_maps" in subscription_data:
                    feature_overrides["enable_maps"] = subscription_data["enable_maps"]
                if "enable_aircraft_images" in subscription_data:
                    feature_overrides["enable_aircraft_images"] = subscription_data[
                        "enable_aircraft_images"
                    ]

                # Report configuration
                if "cooldown_hours" in subscription_data:
                    value = subscription_data["cooldown_hours"]
                    feature_overrides["cooldown_hours"] = (
                        float(value) if value is not None and value != "" else 12.0
                    )
                if "daily_report_limit" in subscription_data:
                    value = subscription_data["daily_report_limit"]
                    feature_overrides["daily_report_limit"] = (
                        int(value) if value is not None and value != "" else -1
                    )
                if "monthly_report_limit" in subscription_data:
                    value = subscription_data["monthly_report_limit"]
                    feature_overrides["monthly_report_limit"] = (
                        int(value) if value is not None and value != "" else -1
                    )
                if "max_filters" in subscription_data:
                    value = subscription_data["max_filters"]
                    feature_overrides["max_filters"] = (
                        int(value) if value is not None and value != "" else -1
                    )

                # Update subscription tier if changed
                tier = subscription_data.get("tier")

                ss.update_subscription(subscription.id, tier=tier, **feature_overrides)

        return jsonify({"success": True})

    except Exception as e:
        logger.error(f"Error updating user {user_id}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/admin/users/<int:user_id>", methods=["DELETE"])
def api_admin_delete_user(user_id: int):
    """Delete user."""
    try:
        us, ss, fs = get_multi_user_services()
        success = us.delete_user(user_id, hard_delete=False)

        if success:
            return jsonify({"success": True})
        else:
            return jsonify({"success": False, "error": "Failed to delete user"}), 400

    except Exception as e:
        logger.error(f"Error deleting user {user_id}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/admin/users/<int:user_id>/api-key", methods=["POST"])
def api_admin_regenerate_api_key(user_id: int):
    """Regenerate API key for user."""
    try:
        us, ss, fs = get_multi_user_services()
        new_key = us.regenerate_api_key(user_id)

        if new_key:
            return jsonify({"success": True, "api_key": new_key})
        else:
            return jsonify({"success": False, "error": "Failed to regenerate API key"}), 400

    except Exception as e:
        logger.error(f"Error regenerating API key for user {user_id}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


# Admin API - Aircraft Management
@app.route("/api/admin/aircraft", methods=["GET"])
@admin_required
def api_admin_list_aircraft():
    """List aircraft static info with pagination and filters."""
    from sqlalchemy import text

    try:
        page = int(request.args.get("page", 1))
        limit = int(request.args.get("limit", 20))
        search = request.args.get("search", "").strip()
        aircraft_type = request.args.get("aircraft_type", "").strip()
        livery = request.args.get("livery", "").strip()
        category = request.args.get("category", "").strip()
        offset = (page - 1) * limit

        session = db_manager.get_session()
        try:
            params: dict = {"limit": limit, "offset": offset}
            where_clauses: list[str] = []

            # Search filter (registration or ICAO hex)
            if search:
                where_clauses.append(
                    "(LOWER(registration) LIKE LOWER(:search) OR LOWER(hex_code) LIKE LOWER(:search))"
                )
                params["search"] = f"%{search}%"

            # Aircraft type filter
            if aircraft_type:
                where_clauses.append("aircraft_type = :aircraft_type")
                params["aircraft_type"] = aircraft_type

            # Livery filter
            if livery:
                where_clauses.append("livery_name = :livery")
                params["livery"] = livery

            # Category filter (only 'special' works with current data)
            if category:
                category_map = {
                    "special": "attention_level IN ('高', '极高', 'high', 'very high')",
                }
                if category in category_map:
                    where_clauses.append(category_map[category])

            where_sql = ""
            if where_clauses:
                where_sql = "WHERE " + " AND ".join(where_clauses)

            query = f"""
                SELECT
                    asi.id,
                    asi.registration,
                    asi.hex_code,
                    asi.aircraft_type,
                    asi.manufacturer,
                    asi.model,
                    asi.operator,
                    asi.owner,
                    asi.country_of_registration,
                    asi.year_built,
                    (SELECT ai.image_path FROM aircraft_images ai WHERE ai.registration = asi.registration ORDER BY ai.display_order LIMIT 1) as image_path,
                    asi.images_downloaded,
                    asi.last_updated,
                    asi.livery_name,
                    asi.attention_level
                FROM aircraft_static_info asi
                {where_sql}
                ORDER BY asi.last_updated DESC NULLS LAST
                LIMIT :limit OFFSET :offset
            """

            result = session.execute(text(query), params).fetchall()

            aircraft_list = []
            for row in result:
                is_special = row[14] in ("高", "极高", "high", "very high") if row[14] else False
                aircraft = {
                    "id": row[0],
                    "registration": row[1],
                    "hex_code": row[2],
                    "aircraft_type": row[3],
                    "manufacturer": row[4],
                    "model": row[5],
                    "operator": row[6],
                    "owner": row[7],
                    "country_of_registration": row[8],
                    "year_built": row[9],
                    "image_url": get_image_url(row[10]) if row[10] else None,
                    "images_downloaded": row[11],
                    "last_updated": row[12].isoformat() if row[12] else None,
                    "livery_name": row[13],
                    "aircraft_type_code": row[3],  # Use aircraft_type as type_code
                    "aircraft_type_full": None,
                    "is_widebody": False,
                    "is_cargo": False,
                    "is_passenger": False,
                    "is_military": False,
                    "is_special": is_special,
                }
                aircraft_list.append(aircraft)

            # Get total count
            count_query = f"""
                SELECT COUNT(*)
                FROM aircraft_static_info
                {where_sql}
            """
            count_params = {k: v for k, v in params.items() if k not in ("limit", "offset")}
            total = session.execute(text(count_query), count_params).scalar()
            pages = (total + limit - 1) // limit if total else 0

            return jsonify(
                {
                    "success": True,
                    "aircraft": aircraft_list,
                    "total": total,
                    "page": page,
                    "pages": pages,
                }
            )

        finally:
            session.close()

    except Exception as e:
        logger.error(f"Error listing aircraft: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/admin/aircraft/stats", methods=["GET"])
@admin_required
def api_admin_aircraft_stats():
    """Get aircraft statistics."""
    from sqlalchemy import text

    try:
        session = db_manager.get_session()
        try:
            stats = {}

            # Total aircraft
            stats["total"] = (
                session.execute(text("SELECT COUNT(*) FROM aircraft_static_info")).scalar() or 0
            )

            # With images
            stats["with_images"] = (
                session.execute(
                    text("SELECT COUNT(*) FROM aircraft_static_info WHERE images_downloaded = true")
                ).scalar()
                or 0
            )

            # Category counts from aircraft_static_info only
            # (widebody/cargo/military not available in current data)
            stats["widebody"] = 0
            stats["cargo"] = 0
            stats["military"] = 0

            # Special: based on attention_level
            stats["special"] = (
                session.execute(
                    text("""
                    SELECT COUNT(*) FROM aircraft_static_info
                    WHERE attention_level IN ('高', '极高', 'high', 'very high')
                """)
                ).scalar()
                or 0
            )

            return jsonify({"success": True, "stats": stats})

        finally:
            session.close()

    except Exception as e:
        logger.error(f"Error getting aircraft stats: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/admin/aircraft/types", methods=["GET"])
@admin_required
def api_admin_aircraft_types():
    """Get distinct aircraft types for filter dropdown with optional search."""
    from sqlalchemy import text

    try:
        search = request.args.get("search", "").strip()
        session = db_manager.get_session()
        try:
            # Query aircraft types from aircraft_static_info
            if search and len(search) >= 2:
                result = session.execute(
                    text("""
                        SELECT aircraft_type, COUNT(*) as count
                        FROM aircraft_static_info
                        WHERE aircraft_type IS NOT NULL AND aircraft_type != ''
                          AND LOWER(aircraft_type) LIKE LOWER(:search)
                        GROUP BY aircraft_type
                        ORDER BY count DESC
                        LIMIT 20
                    """),
                    {"search": f"%{search}%"},
                ).fetchall()
            else:
                result = session.execute(
                    text("""
                    SELECT aircraft_type, COUNT(*) as count
                    FROM aircraft_static_info
                    WHERE aircraft_type IS NOT NULL AND aircraft_type != ''
                    GROUP BY aircraft_type
                    ORDER BY count DESC
                    LIMIT 200
                """)
                ).fetchall()

            types = [{"code": row[0], "full_name": row[0], "count": row[1]} for row in result]

            return jsonify({"success": True, "types": types})

        finally:
            session.close()

    except Exception as e:
        logger.error(f"Error getting aircraft types: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/admin/aircraft/liveries", methods=["GET"])
@admin_required
def api_admin_aircraft_liveries():
    """Get distinct liveries for filter dropdown with optional search."""
    from sqlalchemy import text

    try:
        search = request.args.get("search", "").strip()
        session = db_manager.get_session()
        try:
            # Query liveries from aircraft_static_info
            if search and len(search) >= 2:
                result = session.execute(
                    text("""
                        SELECT livery_name, COUNT(*) as count
                        FROM aircraft_static_info
                        WHERE livery_name IS NOT NULL AND livery_name != ''
                          AND LOWER(livery_name) LIKE LOWER(:search)
                        GROUP BY livery_name
                        ORDER BY count DESC
                        LIMIT 20
                    """),
                    {"search": f"%{search}%"},
                ).fetchall()
            else:
                result = session.execute(
                    text("""
                    SELECT livery_name, COUNT(*) as count
                    FROM aircraft_static_info
                    WHERE livery_name IS NOT NULL AND livery_name != ''
                    GROUP BY livery_name
                    ORDER BY count DESC
                    LIMIT 200
                """)
                ).fetchall()

            liveries = [{"name": row[0], "count": row[1]} for row in result]

            return jsonify({"success": True, "liveries": liveries})

        finally:
            session.close()

    except Exception as e:
        logger.error(f"Error getting aircraft liveries: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/admin/aircraft/registrations", methods=["GET"])
@admin_required
def api_admin_aircraft_registrations():
    """Search aircraft registrations for autocomplete."""
    from sqlalchemy import text

    try:
        search = request.args.get("search", "").strip()
        if len(search) < 2:
            return jsonify({"success": True, "registrations": []})

        session = db_manager.get_session()
        try:
            # Query registrations with hex_code and aircraft_type
            # Prioritize prefix matches first
            result = session.execute(
                text("""
                    SELECT registration, hex_code, aircraft_type
                    FROM aircraft_static_info
                    WHERE registration IS NOT NULL AND registration != ''
                      AND (LOWER(registration) LIKE LOWER(:prefix) OR LOWER(registration) LIKE LOWER(:contains) OR LOWER(hex_code) LIKE LOWER(:contains))
                    ORDER BY
                        CASE WHEN LOWER(registration) LIKE LOWER(:prefix) THEN 0 ELSE 1 END,
                        registration
                    LIMIT 15
                """),
                {"prefix": f"{search}%", "contains": f"%{search}%"},
            ).fetchall()

            registrations = [
                {"registration": row[0], "hex_code": row[1], "aircraft_type": row[2]}
                for row in result
            ]

            return jsonify({"success": True, "registrations": registrations})

        finally:
            session.close()

    except Exception as e:
        logger.error(f"Error searching aircraft registrations: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


# Admin API - Report Management
@app.route("/api/admin/reports", methods=["GET"])
def api_admin_list_reports():
    """List report history with pagination.

    Returns reports from user_cooldowns (multi-user mode) or report_cooldowns (single-user mode)
    joined with latest aircraft snapshot data.
    """
    from sqlalchemy import text

    try:
        page = int(request.args.get("page", 1))
        limit = int(request.args.get("limit", 20))
        search = request.args.get("search", "").strip()
        user_id = request.args.get("user_id")  # Optional: filter by user
        offset = (page - 1) * limit

        # Check if multi-user mode is enabled
        multi_user_enabled = config.is_multi_user_enabled() if config else False

        session = db_manager.get_session()
        try:
            params = {"limit": limit, "offset": offset}

            latest_snapshot = latest_rows(
                columns=(
                    "hex, registration, aircraft_type, flight_number, is_military, current_country"
                ),
                source="aircraft_snapshots",
                partition_by="hex",
                order_by="snapshot_time DESC",
                is_postgres=db_manager.is_postgres,
            )

            if multi_user_enabled:
                # Multi-user mode: query from user_cooldowns
                base_query = f"""
                    SELECT
                        uc.id,
                        uc.aircraft_hex,
                        uc.last_report_time,
                        uc.last_latitude,
                        uc.last_longitude,
                        uc.report_count,
                        uc.last_report_time as updated_at,
                        s.registration,
                        s.aircraft_type,
                        s.flight_number,
                        s.is_military,
                        s.current_country,
                        (SELECT ai.image_path FROM aircraft_images ai WHERE ai.registration = s.registration ORDER BY ai.display_order LIMIT 1) as image_path,
                        uc.user_id,
                        u.email as user_email
                    FROM user_cooldowns uc
                    LEFT JOIN users u ON uc.user_id = u.id
                    LEFT JOIN (
                        {latest_snapshot}
                    ) s ON uc.aircraft_hex = s.hex
                """

                # Add filters
                where_clauses = []
                if user_id:
                    where_clauses.append("uc.user_id = :user_id")
                    params["user_id"] = int(user_id)
                if search:
                    where_clauses.append("""
                        (LOWER(uc.aircraft_hex) LIKE LOWER(:search)
                        OR LOWER(s.registration) LIKE LOWER(:search)
                        OR LOWER(s.flight_number) LIKE LOWER(:search)
                        OR LOWER(u.email) LIKE LOWER(:search))
                    """)
                    params["search"] = f"%{search}%"

                if where_clauses:
                    base_query += " WHERE " + " AND ".join(where_clauses)

                base_query += """
                    ORDER BY uc.last_report_time DESC
                    LIMIT :limit OFFSET :offset
                """

                result = session.execute(text(base_query), params).fetchall()

                reports = []
                for row in result:
                    report = {
                        "id": row[0],
                        "aircraft_hex": row[1],
                        "last_report_time": _to_iso(row[2]),
                        "last_report_time_beijing": convert_utc_to_beijing(_to_iso(row[2]))
                        if row[2]
                        else None,
                        "last_latitude": float(row[3]) if row[3] else None,
                        "last_longitude": float(row[4]) if row[4] else None,
                        "report_count": row[5],
                        "updated_at": _to_iso(row[6]),
                        "registration": row[7],
                        "aircraft_type": row[8],
                        "flight_number": row[9],
                        "is_military": row[10],
                        "current_country": row[11],
                        "image_url": get_image_url(row[12]) if row[12] else None,
                        "user_id": row[13],
                        "user_email": row[14],
                    }
                    reports.append(report)

                # Get total count for multi-user
                count_params = {}
                count_where = []

                if user_id:
                    count_where.append("uc.user_id = :user_id")
                    count_params["user_id"] = int(user_id)

                if search:
                    # Need to join for search
                    count_query = """
                        SELECT COUNT(DISTINCT uc.id) FROM user_cooldowns uc
                        LEFT JOIN users u ON uc.user_id = u.id
                        LEFT JOIN aircraft_snapshots s ON uc.aircraft_hex = s.hex
                    """
                    count_where.append(
                        "(LOWER(uc.aircraft_hex) LIKE LOWER(:search) OR LOWER(s.registration) LIKE LOWER(:search) OR LOWER(u.email) LIKE LOWER(:search))"
                    )
                    count_params["search"] = f"%{search}%"
                else:
                    # Simple count without join
                    count_query = "SELECT COUNT(*) FROM user_cooldowns uc"

                if count_where:
                    count_query += " WHERE " + " AND ".join(count_where)

                total = session.execute(text(count_query), count_params).scalar() or 0

            else:
                # Single-user mode: query from report_cooldowns (original behavior)
                base_query = f"""
                    SELECT
                        rc.id,
                        rc.aircraft_hex,
                        rc.last_report_time,
                        rc.last_latitude,
                        rc.last_longitude,
                        rc.report_count,
                        rc.updated_at,
                        s.registration,
                        s.aircraft_type,
                        s.flight_number,
                        s.is_military,
                        s.current_country,
                        (SELECT ai.image_path FROM aircraft_images ai WHERE ai.registration = s.registration ORDER BY ai.display_order LIMIT 1) as image_path
                    FROM report_cooldowns rc
                    LEFT JOIN (
                        {latest_snapshot}
                    ) s ON rc.aircraft_hex = s.hex
                """

                # Add search filter
                if search:
                    base_query += """
                        WHERE LOWER(rc.aircraft_hex) LIKE LOWER(:search)
                        OR LOWER(s.registration) LIKE LOWER(:search)
                        OR LOWER(s.flight_number) LIKE LOWER(:search)
                    """
                    params["search"] = f"%{search}%"

                # Add ordering and pagination
                base_query += """
                    ORDER BY rc.last_report_time DESC
                    LIMIT :limit OFFSET :offset
                """

                result = session.execute(text(base_query), params).fetchall()

                reports = []
                for row in result:
                    report = {
                        "id": row[0],
                        "aircraft_hex": row[1],
                        "last_report_time": _to_iso(row[2]),
                        "last_report_time_beijing": convert_utc_to_beijing(_to_iso(row[2]))
                        if row[2]
                        else None,
                        "last_latitude": float(row[3]) if row[3] else None,
                        "last_longitude": float(row[4]) if row[4] else None,
                        "report_count": row[5],
                        "updated_at": _to_iso(row[6]),
                        "registration": row[7],
                        "aircraft_type": row[8],
                        "flight_number": row[9],
                        "is_military": row[10],
                        "current_country": row[11],
                        "image_url": get_image_url(row[12]) if row[12] else None,
                    }
                    reports.append(report)

                # Get total count
                count_query = "SELECT COUNT(*) FROM report_cooldowns"
                if search:
                    count_query = """
                        SELECT COUNT(*) FROM report_cooldowns rc
                        LEFT JOIN aircraft_snapshots s ON rc.aircraft_hex = s.hex
                        WHERE LOWER(rc.aircraft_hex) LIKE LOWER(:search)
                        OR LOWER(s.registration) LIKE LOWER(:search)
                    """

                total = (
                    session.execute(
                        text(count_query), {"search": f"%{search}%"} if search else {}
                    ).scalar()
                    or 0
                )

            pages = (total + limit - 1) // limit

            return jsonify(
                {
                    "success": True,
                    "reports": reports,
                    "total": total,
                    "page": page,
                    "pages": pages,
                    "multi_user_mode": multi_user_enabled,
                }
            )

        finally:
            session.close()

    except Exception as e:
        logger.error(f"Error listing reports: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/admin/reports/stats", methods=["GET"])
def api_admin_report_stats():
    """Get report statistics.

    Supports both multi-user mode (user_cooldowns) and single-user mode (report_cooldowns).
    """
    from sqlalchemy import text

    try:
        # Check if multi-user mode is enabled
        multi_user_enabled = config.is_multi_user_enabled() if config else False
        table_name = "user_cooldowns" if multi_user_enabled else "report_cooldowns"

        session = db_manager.get_session()
        try:
            # Total reports
            total = session.execute(text(f"SELECT COUNT(*) FROM {table_name}")).scalar() or 0

            # Reports today. Comparing both sides as dates also excludes rows
            # timestamped in the future, which `>= CURRENT_DATE` counted.
            is_postgres = db_manager.is_postgres
            today_query = f"""
                SELECT COUNT(*) FROM {table_name}
                WHERE {day_of("last_report_time", is_postgres=is_postgres)}
                      = {day_of("CURRENT_TIMESTAMP", is_postgres=is_postgres)}
            """
            today = session.execute(text(today_query)).scalar() or 0

            # Total report count (sum of all report_count)
            total_sent = (
                session.execute(
                    text(f"SELECT COALESCE(SUM(report_count), 0) FROM {table_name}")
                ).scalar()
                or 0
            )

            # Unique aircraft
            unique_aircraft = (
                session.execute(
                    text(f"SELECT COUNT(DISTINCT aircraft_hex) FROM {table_name}")
                ).scalar()
                or 0
            )

            return jsonify(
                {
                    "success": True,
                    "stats": {
                        "total_tracked": total,
                        "reports_today": today,
                        "total_sent": total_sent,
                        "unique_aircraft": unique_aircraft,
                    },
                    "multi_user_mode": multi_user_enabled,
                }
            )

        finally:
            session.close()

    except Exception as e:
        logger.error(f"Error getting report stats: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/admin/reports/<aircraft_hex>/detail", methods=["GET"])
def api_admin_report_detail(aircraft_hex: str):
    """Get detailed report info for an aircraft including recent snapshots.

    Supports both multi-user mode (user_cooldowns) and single-user mode (report_cooldowns).
    """
    from sqlalchemy import text

    try:
        # Check if multi-user mode is enabled
        multi_user_enabled = config.is_multi_user_enabled() if config else False
        user_id = request.args.get("user_id")  # Optional: filter by user in multi-user mode

        session = db_manager.get_session()
        try:
            # Get cooldown info based on mode
            if multi_user_enabled:
                if user_id:
                    cooldown = session.execute(
                        text("""
                        SELECT uc.id, uc.aircraft_hex, uc.last_report_time, uc.last_latitude, uc.last_longitude,
                               uc.report_count, uc.last_report_time as updated_at, uc.user_id, u.email
                        FROM user_cooldowns uc
                        LEFT JOIN users u ON uc.user_id = u.id
                        WHERE uc.aircraft_hex = :hex AND uc.user_id = :user_id
                    """),
                        {"hex": aircraft_hex, "user_id": int(user_id)},
                    ).fetchone()
                else:
                    # Get most recent cooldown for this aircraft across all users
                    cooldown = session.execute(
                        text("""
                        SELECT uc.id, uc.aircraft_hex, uc.last_report_time, uc.last_latitude, uc.last_longitude,
                               uc.report_count, uc.last_report_time as updated_at, uc.user_id, u.email
                        FROM user_cooldowns uc
                        LEFT JOIN users u ON uc.user_id = u.id
                        WHERE uc.aircraft_hex = :hex
                        ORDER BY uc.last_report_time DESC
                        LIMIT 1
                    """),
                        {"hex": aircraft_hex},
                    ).fetchone()
            else:
                cooldown = session.execute(
                    text("""
                    SELECT id, aircraft_hex, last_report_time, last_latitude, last_longitude,
                           report_count, updated_at
                    FROM report_cooldowns
                    WHERE aircraft_hex = :hex
                """),
                    {"hex": aircraft_hex},
                ).fetchone()

            if not cooldown:
                return jsonify({"success": False, "error": "Report not found"}), 404

            # Get recent snapshots
            snapshots = session.execute(
                text("""
                SELECT id, snapshot_time, hex, registration, aircraft_type, flight_number,
                       latitude, longitude, altitude_baro, ground_speed, track,
                       is_military, current_country
                FROM aircraft_snapshots
                WHERE hex = :hex
                ORDER BY snapshot_time DESC
                LIMIT 10
            """),
                {"hex": aircraft_hex},
            ).fetchall()

            # Get images for the registration (if available)
            registration = snapshots[0][3] if snapshots and snapshots[0][3] else None
            images = []
            if registration:
                images_result = session.execute(
                    text("""
                    SELECT image_path FROM aircraft_images
                    WHERE registration = :reg
                    ORDER BY display_order LIMIT 3
                """),
                    {"reg": registration},
                ).fetchall()
                images = [row[0] for row in images_result if row[0]]

            # Build cooldown response based on mode
            cooldown_data = {
                "id": cooldown[0],
                "aircraft_hex": cooldown[1],
                "last_report_time": cooldown[2].isoformat() if cooldown[2] else None,
                "last_report_time_beijing": convert_utc_to_beijing(cooldown[2].isoformat())
                if cooldown[2]
                else None,
                "last_latitude": float(cooldown[3]) if cooldown[3] else None,
                "last_longitude": float(cooldown[4]) if cooldown[4] else None,
                "report_count": cooldown[5],
                "updated_at": cooldown[6].isoformat() if cooldown[6] else None,
            }

            # Add user info in multi-user mode
            if multi_user_enabled and len(cooldown) > 7:
                cooldown_data["user_id"] = cooldown[7]
                cooldown_data["user_email"] = cooldown[8]

            report_detail = {
                "cooldown": cooldown_data,
                "recent_snapshots": [
                    {
                        "id": s[0],
                        "snapshot_time": s[1].isoformat() if s[1] else None,
                        "snapshot_time_beijing": convert_utc_to_beijing(s[1].isoformat())
                        if s[1]
                        else None,
                        "hex": s[2],
                        "registration": s[3],
                        "aircraft_type": s[4],
                        "flight_number": s[5],
                        "latitude": float(s[6]) if s[6] else None,
                        "longitude": float(s[7]) if s[7] else None,
                        "altitude": s[8],
                        "ground_speed": s[9],
                        "track": s[10],
                        "is_military": s[11],
                        "current_country": s[12],
                        "image_url_1": get_image_url(images[0]) if len(images) > 0 else None,
                        "image_url_2": get_image_url(images[1]) if len(images) > 1 else None,
                        "image_url_3": get_image_url(images[2]) if len(images) > 2 else None,
                    }
                    for s in snapshots
                ],
                "multi_user_mode": multi_user_enabled,
            }

            return jsonify({"success": True, "detail": report_detail})

        finally:
            session.close()

    except Exception as e:
        logger.error(f"Error getting report detail for {aircraft_hex}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


# ============== Scraped Data Admin APIs ==============


@app.route("/admin/scraped-data")
@admin_required
def admin_scraped_data_page():
    """Admin page for viewing scraped data from various sources."""
    return render_template("admin_scraped_data.html")


@app.route("/api/admin/scraped-data/xiaohongshu/stats")
@admin_required
def api_admin_xhs_stats():
    """Get Xiaohongshu scraped data statistics."""
    from sqlalchemy import text

    try:
        session = db_manager.get_session()
        try:
            # Check if tables exist
            table_exists = _table_exists(session, "xiaohongshu_notes")
            if not table_exists:
                return jsonify(
                    {
                        "success": True,
                        "notes_count": 0,
                        "authors_count": 0,
                        "images_count": 0,
                        "latest_scrape": None,
                    }
                )

            notes_count = (
                session.execute(text("SELECT COUNT(*) FROM xiaohongshu_notes")).scalar() or 0
            )

            authors_count = 0
            if _table_exists(session, "xiaohongshu_authors"):
                authors_count = (
                    session.execute(text("SELECT COUNT(*) FROM xiaohongshu_authors")).scalar() or 0
                )

            # Count images (sum of array lengths in image_paths). Compute
            # in Python rather than using the Postgres-only
            # `jsonb_array_length()`; the result set is small (one row per
            # note) and this is a rarely-hit admin endpoint.
            import json as _json_mod

            image_paths_rows = session.execute(
                text("SELECT image_paths FROM xiaohongshu_notes")
            ).fetchall()
            images_count = 0
            for (raw,) in image_paths_rows:
                if raw is None:
                    continue
                parsed = raw if isinstance(raw, list) else None
                if parsed is None and isinstance(raw, str):
                    try:
                        parsed = _json_mod.loads(raw)
                    except (ValueError, TypeError):
                        parsed = None
                if isinstance(parsed, list):
                    images_count += len(parsed)

            latest = session.execute(
                text("""
                SELECT MAX(scraped_at) FROM xiaohongshu_notes
            """)
            ).scalar()

            return jsonify(
                {
                    "success": True,
                    "notes_count": notes_count,
                    "authors_count": authors_count,
                    "images_count": images_count,
                    "latest_scrape": latest.strftime("%m-%d %H:%M") if latest else None,
                }
            )
        finally:
            session.close()
    except Exception as e:
        logger.error(f"Error getting XHS stats: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/admin/scraped-data/xiaohongshu/notes")
@admin_required
def api_admin_xhs_notes():
    """Get Xiaohongshu notes list."""
    from sqlalchemy import text

    try:
        author = request.args.get("author", "")
        title = request.args.get("title", "")
        limit = min(int(request.args.get("limit", 50)), 200)
        offset = int(request.args.get("offset", 0))

        session = db_manager.get_session()
        try:
            # Check if table exists
            table_exists = _table_exists(session, "xiaohongshu_notes")
            if not table_exists:
                return jsonify({"success": True, "notes": []})

            where_clause = " WHERE 1=1"
            filter_params: dict[str, Any] = {}

            if author:
                where_clause += " AND LOWER(author_name) LIKE LOWER(:author)"
                filter_params["author"] = f"%{author}%"
            if title:
                where_clause += " AND LOWER(title) LIKE LOWER(:title)"
                filter_params["title"] = f"%{title}%"

            total = (
                session.execute(
                    text(f"SELECT COUNT(*) FROM xiaohongshu_notes{where_clause}"),
                    filter_params,
                ).scalar()
                or 0
            )

            query = f"""
                SELECT note_id, source_url, title, author_name, author_id,
                       image_paths,
                       like_count, collect_count, comment_count, share_count,
                       location, scraped_at, updated_at, content, tags
                FROM xiaohongshu_notes{where_clause}
                ORDER BY COALESCE(updated_at, scraped_at) DESC
                LIMIT :limit OFFSET :offset
            """
            params = {**filter_params, "limit": limit, "offset": offset}

            import json as _json_mod

            def _count_images(raw):
                if raw is None:
                    return 0
                if isinstance(raw, list):
                    return len(raw)
                if isinstance(raw, str):
                    try:
                        parsed = _json_mod.loads(raw)
                    except (ValueError, TypeError):
                        return 0
                    return len(parsed) if isinstance(parsed, list) else 0
                return 0

            result = session.execute(text(query), params)
            notes = []
            for row in result:
                notes.append(
                    {
                        "note_id": row.note_id,
                        "source_url": row.source_url,
                        "title": row.title,
                        "author_name": row.author_name,
                        "author_id": row.author_id,
                        "image_count": _count_images(row.image_paths),
                        "like_count": row.like_count,
                        "collect_count": row.collect_count,
                        "comment_count": row.comment_count,
                        "share_count": row.share_count,
                        "location": row.location,
                        "scraped_at": row.scraped_at.isoformat() if row.scraped_at else None,
                        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
                        "content": row.content[:200] + "..."
                        if row.content and len(row.content) > 200
                        else row.content,
                        "tags": row.tags,
                    }
                )

            return jsonify(
                {"success": True, "notes": notes, "total": total, "limit": limit, "offset": offset}
            )
        finally:
            session.close()
    except Exception as e:
        logger.error(f"Error getting XHS notes: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/admin/scraped-data/xiaohongshu/notes/<note_id>")
@admin_required
def api_admin_xhs_note_detail(note_id: str):
    """Get full detail for a single Xiaohongshu note."""
    from sqlalchemy import text

    try:
        session = db_manager.get_session()
        try:
            table_exists = _table_exists(session, "xiaohongshu_notes")
            if not table_exists:
                return jsonify({"success": False, "error": "Note not found"}), 404

            result = session.execute(
                text("""
                SELECT note_id, source_url, title, content, tags, location,
                       author_id, author_name, image_urls, image_paths,
                       video_url, like_count, collect_count, comment_count, share_count,
                       comments, note_created_at, scraped_at
                FROM xiaohongshu_notes
                WHERE note_id = :note_id
            """),
                {"note_id": note_id},
            )
            row = result.fetchone()

            if not row:
                return jsonify({"success": False, "error": "Note not found"}), 404

            # Build image URLs from image_paths (CloudFront)
            display_images: list[str] = []
            if row.image_paths:
                paths = row.image_paths if isinstance(row.image_paths, list) else []
                for p in paths:
                    if isinstance(p, str):
                        p = os.path.normpath(ObjectStorage.strip_public_prefix(p))
                        url = get_image_url(p)
                        if url:
                            display_images.append(url)

            note = {
                "note_id": row.note_id,
                "source_url": row.source_url,
                "title": row.title,
                "content": row.content,
                "tags": row.tags,
                "location": row.location,
                "author_id": row.author_id,
                "author_name": row.author_name,
                "image_urls": display_images,
                "video_url": row.video_url,
                "like_count": row.like_count,
                "collect_count": row.collect_count,
                "comment_count": row.comment_count,
                "share_count": row.share_count,
                "comments": row.comments,
                "note_created_at": row.note_created_at.isoformat() if row.note_created_at else None,
                "scraped_at": row.scraped_at.isoformat() if row.scraped_at else None,
            }

            return jsonify({"success": True, "note": note})
        finally:
            session.close()
    except Exception as e:
        logger.error(f"Error getting XHS note detail: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/admin/scraped-data/fr24/stats")
@admin_required
def api_admin_fr24_stats():
    """Get FR24 flight schedules statistics."""
    from sqlalchemy import text

    try:
        session = db_manager.get_session()
        try:
            flights_count = (
                session.execute(text("SELECT COUNT(*) FROM flight_schedules")).scalar() or 0
            )

            airports_count = (
                session.execute(
                    text("""
                SELECT COUNT(DISTINCT airport_iata) FROM flight_schedules WHERE airport_iata IS NOT NULL
            """)
                ).scalar()
                or 0
            )

            today_count = (
                session.execute(
                    text("""
                SELECT COUNT(*) FROM flight_schedules
                WHERE DATE(scheduled_time) = CURRENT_DATE
            """)
                ).scalar()
                or 0
            )

            latest = session.execute(text("SELECT MAX(scraped_at) FROM flight_schedules")).scalar()

            return jsonify(
                {
                    "success": True,
                    "flights_count": flights_count,
                    "airports_count": airports_count,
                    "today_count": today_count,
                    "latest_scrape": latest.strftime("%m-%d %H:%M") if latest else None,
                }
            )
        finally:
            session.close()
    except Exception as e:
        logger.error(f"Error getting FR24 stats: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/admin/scraped-data/fr24/flights")
@admin_required
def api_admin_fr24_flights():
    """Get FR24 flights list."""
    from sqlalchemy import text

    try:
        airport = request.args.get("airport", "")
        registration = request.args.get("registration", "")
        flight_type = request.args.get("flight_type", "")
        limit = min(int(request.args.get("limit", 50)), 200)
        offset = int(request.args.get("offset", 0))

        session = db_manager.get_session()
        try:
            query = """
                SELECT fr24_flight_id, flight_type, airport_iata, airport_icao,
                       flight_number, callsign, airline_name,
                       remote_airport_iata, remote_airport_name,
                       aircraft_type, aircraft_registration,
                       scheduled_time, estimated_time, actual_time, status,
                       terminal, gate, scraped_at
                FROM flight_schedules
                WHERE 1=1
            """
            params: dict[str, Any] = {"limit": limit, "offset": offset}

            if airport:
                query += " AND (LOWER(airport_iata) LIKE LOWER(:airport) OR LOWER(airport_icao) LIKE LOWER(:airport))"
                params["airport"] = f"{airport}%"
            if registration:
                query += " AND LOWER(aircraft_registration) LIKE LOWER(:registration)"
                params["registration"] = f"%{registration}%"
            if flight_type:
                query += " AND flight_type = :flight_type"
                params["flight_type"] = flight_type

            query += " ORDER BY scraped_at DESC LIMIT :limit OFFSET :offset"

            result = session.execute(text(query), params)
            flights = []
            for row in result:
                flights.append(
                    {
                        "fr24_flight_id": row.fr24_flight_id,
                        "flight_type": row.flight_type,
                        "airport_iata": row.airport_iata,
                        "airport_icao": row.airport_icao,
                        "flight_number": row.flight_number,
                        "callsign": row.callsign,
                        "airline_name": row.airline_name,
                        "remote_airport_iata": row.remote_airport_iata,
                        "remote_airport_name": row.remote_airport_name,
                        "aircraft_type": row.aircraft_type,
                        "aircraft_registration": row.aircraft_registration,
                        "scheduled_time": row.scheduled_time.isoformat()
                        if row.scheduled_time
                        else None,
                        "status": row.status,
                        "scraped_at": row.scraped_at.isoformat() if row.scraped_at else None,
                    }
                )

            return jsonify({"success": True, "flights": flights})
        finally:
            session.close()
    except Exception as e:
        logger.error(f"Error getting FR24 flights: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/admin/scraped-data/jetphotos/stats")
@admin_required
def api_admin_jetphotos_stats():
    """Get JetPhotos scraped data statistics."""
    from sqlalchemy import text

    try:
        session = db_manager.get_session()
        try:
            images_count = (
                session.execute(
                    text("""
                SELECT COUNT(*) FROM aircraft_images WHERE source = 'jetphotos'
            """)
                ).scalar()
                or 0
            )

            aircraft_count = (
                session.execute(
                    text("""
                SELECT COUNT(DISTINCT registration) FROM aircraft_images WHERE source = 'jetphotos'
            """)
                ).scalar()
                or 0
            )

            photographers_count = (
                session.execute(
                    text("""
                SELECT COUNT(DISTINCT photographer) FROM aircraft_images
                WHERE source = 'jetphotos' AND photographer IS NOT NULL
            """)
                ).scalar()
                or 0
            )

            latest = session.execute(
                text("""
                SELECT MAX(created_at) FROM aircraft_images WHERE source = 'jetphotos'
            """)
            ).scalar()

            return jsonify(
                {
                    "success": True,
                    "images_count": images_count,
                    "aircraft_count": aircraft_count,
                    "photographers_count": photographers_count,
                    "latest_scrape": latest.strftime("%m-%d %H:%M") if latest else None,
                }
            )
        finally:
            session.close()
    except Exception as e:
        logger.error(f"Error getting JetPhotos stats: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/admin/scraped-data/jetphotos/images")
@admin_required
def api_admin_jetphotos_images():
    """Get JetPhotos images list."""
    from sqlalchemy import text

    try:
        registration = request.args.get("registration", "")
        photographer = request.args.get("photographer", "")
        airport = request.args.get("airport", "")
        limit = min(int(request.args.get("limit", 50)), 200)
        offset = int(request.args.get("offset", 0))

        session = db_manager.get_session()
        try:
            query = """
                SELECT id, registration, image_path, source_url, jetphotos_id,
                       photographer, photo_date, location, airport_icao,
                       width, height, file_size_bytes, notes, created_at
                FROM aircraft_images
                WHERE source = 'jetphotos'
            """
            params: dict[str, Any] = {"limit": limit, "offset": offset}

            if registration:
                query += " AND LOWER(registration) LIKE LOWER(:registration)"
                params["registration"] = f"%{registration}%"
            if photographer:
                query += " AND LOWER(photographer) LIKE LOWER(:photographer)"
                params["photographer"] = f"%{photographer}%"
            if airport:
                query += " AND LOWER(airport_icao) LIKE LOWER(:airport)"
                params["airport"] = f"{airport}%"

            query += " ORDER BY created_at DESC LIMIT :limit OFFSET :offset"

            result = session.execute(text(query), params)
            images = []
            for row in result:
                images.append(
                    {
                        "id": row.id,
                        "registration": row.registration,
                        "image_url": get_image_url(row.image_path),
                        "source_url": row.source_url,
                        "jetphotos_id": row.jetphotos_id,
                        "photographer": row.photographer,
                        "photo_date": row.photo_date.isoformat() if row.photo_date else None,
                        "location": row.location,
                        "airport_icao": row.airport_icao,
                        "width": row.width,
                        "height": row.height,
                        "file_size_bytes": row.file_size_bytes,
                        "notes": row.notes,
                        "created_at": row.created_at.isoformat() if row.created_at else None,
                    }
                )

            return jsonify({"success": True, "images": images})
        finally:
            session.close()
    except Exception as e:
        logger.error(f"Error getting JetPhotos images: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


# User API - Profile and Usage
@app.route("/api/user/<email>/profile", methods=["GET"])
def api_user_profile(email: str):
    """Get user profile with subscription info."""
    try:
        us, ss, fs = get_multi_user_services()

        user = us.get_user_by_email(email)
        if not user:
            return jsonify({"success": False, "error": "User not found"}), 404

        user_data = us.get_user_with_subscription(user.id)
        features = ss.get_user_features(user.id)

        # Get filter count
        filters = fs.get_user_filters(user.id, active_only=True)

        return jsonify(
            {
                "success": True,
                "user": user_data,
                "features": features,
                "active_filters_count": len(filters),
            }
        )

    except Exception as e:
        logger.error(f"Error getting profile for {email}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/user/<email>/usage", methods=["GET"])
def api_user_usage(email: str):
    """Get user usage statistics."""
    try:
        us, ss, fs = get_multi_user_services()

        user = us.get_user_by_email(email)
        if not user:
            return jsonify({"success": False, "error": "User not found"}), 404

        usage = ss.get_usage_stats(user.id)

        return jsonify({"success": True, "usage": usage})

    except Exception as e:
        logger.error(f"Error getting usage for {email}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/user/<email>/settings", methods=["PUT"])
def api_user_update_settings(email: str):
    """Update user report settings."""
    try:
        us, ss, fs = get_multi_user_services()
        data = request.get_json()

        user = us.get_user_by_email(email)
        if not user:
            return jsonify({"success": False, "error": "User not found"}), 404

        # Get user's active subscription
        subscription = ss.get_user_active_subscription(user.id)
        if not subscription:
            return jsonify({"success": False, "error": "No active subscription"}), 400

        # Prepare feature overrides
        feature_overrides = {}

        # Feature toggles
        if "enable_maps" in data:
            feature_overrides["enable_maps"] = data["enable_maps"]
        if "enable_aircraft_images" in data:
            feature_overrides["enable_aircraft_images"] = data["enable_aircraft_images"]

        # Report configuration
        if "cooldown_hours" in data:
            value = data["cooldown_hours"]
            feature_overrides["cooldown_hours"] = float(value) if value is not None else 12.0
        if "daily_report_limit" in data:
            value = data["daily_report_limit"]
            feature_overrides["daily_report_limit"] = int(value) if value is not None else -1
        if "monthly_report_limit" in data:
            value = data["monthly_report_limit"]
            feature_overrides["monthly_report_limit"] = int(value) if value is not None else -1
        if "max_filters" in data:
            value = data["max_filters"]
            feature_overrides["max_filters"] = int(value) if value is not None else -1

        # Update subscription
        success = ss.update_subscription(subscription.id, **feature_overrides)

        if success:
            return jsonify({"success": True})
        else:
            return jsonify({"success": False, "error": "Failed to update settings"}), 400

    except Exception as e:
        logger.error(f"Error updating settings for {email}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/user/<email>/cooldowns", methods=["GET"])
def api_user_cooldowns(email: str):
    """Get user cooldown status."""
    try:
        us, ss, fs = get_multi_user_services()

        user = us.get_user_by_email(email)
        if not user:
            return jsonify({"success": False, "error": "User not found"}), 404

        # Get recent cooldowns from database
        from sqlalchemy import text

        session = db_manager.get_session()
        try:
            result = session.execute(
                text("""
                SELECT aircraft_hex, last_report_time, last_latitude, last_longitude, report_count
                FROM user_cooldowns
                WHERE user_id = :user_id
                ORDER BY last_report_time DESC
                LIMIT 20
            """),
                {"user_id": user.id},
            ).fetchall()

            cooldowns = []
            for row in result:
                hours_since = (datetime.now() - row[1]).total_seconds() / 3600
                cooldowns.append(
                    {
                        "aircraft_hex": row[0],
                        "last_report_time": row[1].isoformat() if row[1] else None,
                        "hours_since_last_report": hours_since,
                        "last_latitude": float(row[2]) if row[2] else None,
                        "last_longitude": float(row[3]) if row[3] else None,
                        "report_count": row[4],
                    }
                )

            return jsonify({"success": True, "cooldowns": cooldowns})
        finally:
            session.close()

    except Exception as e:
        logger.error(f"Error getting cooldowns for {email}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


# User API - Filter Management
@app.route("/api/user/<email>/filters", methods=["GET"])
def api_user_list_filters(email: str):
    """List user's filters."""
    try:
        us, ss, fs = get_multi_user_services()

        user = us.get_user_by_email(email)
        if not user:
            return jsonify({"success": False, "error": "User not found"}), 404

        active_only = request.args.get("active_only", "false").lower() == "true"
        filters = fs.get_user_filters(user.id, active_only=active_only)

        return jsonify({"success": True, "filters": [f.to_dict() for f in filters]})

    except Exception as e:
        logger.error(f"Error listing filters for {email}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/user/<email>/filters", methods=["POST"])
def api_user_create_filter(email: str):
    """Create a new filter for user."""
    try:
        us, ss, fs = get_multi_user_services()

        user = us.get_user_by_email(email)
        if not user:
            return jsonify({"success": False, "error": "User not found"}), 404

        # Check filter limit
        features = ss.get_user_features(user.id)
        max_filters = features.get("max_filters", 3)
        current_filters = fs.get_user_filters(user.id, active_only=False)

        if max_filters != -1 and len(current_filters) >= max_filters:
            return jsonify(
                {
                    "success": False,
                    "error": f"Filter limit reached ({max_filters}). Upgrade your subscription for more filters.",
                }
            ), 400

        data = request.get_json()
        name = data.get("name")
        filter_sql = data.get("filter_sql")
        description = data.get("description")
        priority = data.get("priority", 0)

        if not name or not filter_sql:
            return jsonify({"success": False, "error": "Name and filter_sql are required"}), 400

        user_filter, error_msg = fs.create_filter(user.id, name, filter_sql, description, priority)

        if user_filter:
            return jsonify({"success": True, "filter": user_filter.to_dict()})
        else:
            return jsonify({"success": False, "error": error_msg or "Invalid filter SQL"}), 400

    except Exception as e:
        logger.error(f"Error creating filter for {email}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/user/<email>/filters/<int:filter_id>", methods=["GET"])
def api_user_get_filter(email: str, filter_id: int):
    """Get a specific filter."""
    try:
        us, ss, fs = get_multi_user_services()

        user = us.get_user_by_email(email)
        if not user:
            return jsonify({"success": False, "error": "User not found"}), 404

        user_filter = fs.get_filter(filter_id)
        if not user_filter or user_filter.user_id != user.id:
            return jsonify({"success": False, "error": "Filter not found"}), 404

        return jsonify({"success": True, "filter": user_filter.to_dict()})

    except Exception as e:
        logger.error(f"Error getting filter {filter_id}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/user/<email>/filters/<int:filter_id>", methods=["PUT"])
def api_user_update_filter(email: str, filter_id: int):
    """Update a filter."""
    try:
        us, ss, fs = get_multi_user_services()

        user = us.get_user_by_email(email)
        if not user:
            return jsonify({"success": False, "error": "User not found"}), 404

        user_filter = fs.get_filter(filter_id)
        if not user_filter or user_filter.user_id != user.id:
            return jsonify({"success": False, "error": "Filter not found"}), 404

        data = request.get_json()
        success = fs.update_filter(
            filter_id,
            name=data.get("name"),
            filter_sql=data.get("filter_sql"),
            description=data.get("description"),
            is_active=data.get("is_active"),
            priority=data.get("priority"),
        )

        if success:
            return jsonify({"success": True})
        else:
            return jsonify(
                {"success": False, "error": "Failed to update filter (invalid SQL?)"}
            ), 400

    except Exception as e:
        logger.error(f"Error updating filter {filter_id}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/user/<email>/filters/<int:filter_id>", methods=["DELETE"])
def api_user_delete_filter(email: str, filter_id: int):
    """Delete a filter."""
    try:
        us, ss, fs = get_multi_user_services()

        user = us.get_user_by_email(email)
        if not user:
            return jsonify({"success": False, "error": "User not found"}), 404

        user_filter = fs.get_filter(filter_id)
        if not user_filter or user_filter.user_id != user.id:
            return jsonify({"success": False, "error": "Filter not found"}), 404

        success = fs.delete_filter(filter_id)

        if success:
            return jsonify({"success": True})
        else:
            return jsonify({"success": False, "error": "Failed to delete filter"}), 400

    except Exception as e:
        logger.error(f"Error deleting filter {filter_id}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/user/<email>/filters/test", methods=["POST"])
def api_user_test_filter(email: str):
    """Test a filter SQL without saving."""
    try:
        us, ss, fs = get_multi_user_services()

        user = us.get_user_by_email(email)
        if not user:
            return jsonify({"success": False, "error": "User not found"}), 404

        data = request.get_json()
        filter_sql = data.get("filter_sql")
        limit = data.get("limit", 10)

        if not filter_sql:
            return jsonify({"success": False, "error": "filter_sql is required"}), 400

        success, results, error = fs.test_filter(filter_sql, limit)

        if success:
            return jsonify({"success": True, "results": results})
        else:
            return jsonify({"success": False, "error": error}), 400

    except Exception as e:
        logger.error(f"Error testing filter: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


# ==================== Flight Schedules API ====================

import re


def extract_livery_indicator(airline_name: str) -> str | None:
    """Extract livery indicator from airline name.

    Args:
        airline_name: Airline name that may contain livery info like
                      "China Eastern (SkyTeam Livery)"

    Returns:
        Livery indicator string or None if not found
    """
    if not airline_name:
        return None
    # Match patterns like "(SkyTeam Livery)" or "(Star Alliance)"
    match = re.search(
        r"\(([^)]*(?:Livery|Alliance|Special|Retro)[^)]*)\)", airline_name, re.IGNORECASE
    )
    if match:
        livery = match.group(1)
        # Clean up the livery string
        livery = re.sub(r"\s*Livery\s*", "", livery, flags=re.IGNORECASE).strip()
        return livery if livery else None
    return None


@app.route("/flight-schedules")
def flight_schedules_page():
    """Redirect to home page (legacy URL)."""
    return redirect(url_for("home"))


@app.route("/api/flight-schedules")
def get_flight_schedules():
    """Get flight schedules with filtering.

    Query parameters:
        - airport: Airport IATA or ICAO code (required)
        - flight_type: "arrival" or "departure" or empty for both
        - aircraft_type: Aircraft type code filter (e.g., "A320", "B787")
        - livery: Livery keyword filter (e.g., "SkyTeam", "Star Alliance")
        - date: Date in YYYY-MM-DD format (default: today)
        - limit: Maximum results (default: 200)
        - offset: Pagination offset (default: 0)

    Performance optimizations:
        - Normalizes ICAO to IATA to avoid OR conditions in WHERE clause
        - Uses DISTINCT ON instead of ROW_NUMBER for deduplication
        - Conditional JOIN on aircraft_static_info only when needed
        - Single query with window functions for counts
    """
    try:
        from sqlalchemy import text

        # Parse query parameters
        airport = request.args.get("airport", "").strip().upper()
        flight_type = request.args.get("flight_type", "").strip().lower()
        aircraft_type = request.args.get("aircraft_type", "").strip().upper()
        livery = request.args.get("livery", "").strip()
        has_livery = request.args.get("has_livery", "").strip().lower() == "true"
        date_str = request.args.get("date", "")
        limit = min(int(request.args.get("limit", 200)), 500)
        offset = int(request.args.get("offset", 0))

        if not airport:
            return jsonify({"success": False, "error": "Airport code is required"}), 400

        # Determine date range (in UTC)
        if date_str and date_str != "recent":
            # Specific date requested: show entire day
            try:
                target_date = datetime.strptime(date_str, "%Y-%m-%d")
            except ValueError:
                target_date = datetime.now()
            # Convert to Beijing timezone start/end of day, then to UTC
            beijing_start = BEIJING_TZ.localize(
                datetime(target_date.year, target_date.month, target_date.day, 0, 0, 0)
            )
            beijing_end = BEIJING_TZ.localize(
                datetime(target_date.year, target_date.month, target_date.day, 23, 59, 59)
            )
            utc_start = beijing_start.astimezone(UTC)
            utc_end = beijing_end.astimezone(UTC)
        else:
            # "recent" or no date: from 1 hour ago to far future (show upcoming flights)
            now_utc = datetime.now(UTC)
            utc_start = now_utc - timedelta(hours=1)
            utc_end = now_utc + timedelta(days=7)  # Up to 7 days in future

        session = db_manager.get_session()
        try:
            # Normalize airport code: convert ICAO to IATA for efficient index usage
            airport_iata = airport
            if len(airport) == 4:  # ICAO code
                icao_result = session.execute(
                    text("SELECT iata_code FROM airports WHERE icao_code = :icao"),
                    {"icao": airport},
                ).fetchone()
                if icao_result and icao_result[0]:
                    airport_iata = icao_result[0]

            # Build WHERE conditions - use only airport_iata for efficient index usage
            # NOTE: livery filtering is done in outer query for better performance
            params = {
                "airport_iata": airport_iata,
                "start_time": utc_start,
                "end_time": utc_end,
                "limit": limit,
                "offset": offset,
            }

            where_conditions = [
                "fs.airport_iata = :airport_iata",
                "fs.scheduled_time >= :start_time",
                "fs.scheduled_time <= :end_time",
            ]

            if flight_type in ("arrival", "departure"):
                where_conditions.append("fs.flight_type = :flight_type")
                params["flight_type"] = flight_type

            if aircraft_type:
                where_conditions.append(
                    "LOWER(fs.aircraft_type) LIKE LOWER(:aircraft_type_pattern)"
                )
                params["aircraft_type_pattern"] = f"%{aircraft_type}%"

            # NOTE: livery filtering is done in outer query for better performance
            # This avoids full scan of aircraft_static_info table
            where_clause = " AND ".join(where_conditions)

            # Build outer WHERE clause for livery filtering (applied after DISTINCT ON)
            outer_where_conditions = []
            if livery:
                outer_where_conditions.append("asi.livery_type = :livery_type")
                params["livery_type"] = livery
            if has_livery:
                outer_where_conditions.append(HAS_LIVERY_SQL)
            outer_where_clause = (
                " AND ".join(outer_where_conditions) if outer_where_conditions else "1=1"
            )

            # Main query: first deduplicate flights, then join with asi and filter by livery
            # This is much faster because we join only ~hundreds of flights instead of scanning 100k+ asi rows
            is_postgres = db_manager.is_postgres
            base_data = latest_rows(
                columns="""fs.id,
                        fs.flight_type,
                        fs.flight_number,
                        fs.callsign,
                        fs.airline_name,
                        fs.airline_iata,
                        fs.remote_airport_iata,
                        fs.remote_airport_name,
                        fs.aircraft_type,
                        fs.aircraft_registration,
                        fs.scheduled_time,
                        fs.estimated_time,
                        fs.actual_time,
                        fs.status,
                        fs.terminal,
                        fs.gate""",
                source="flight_schedules fs",
                partition_by=(
                    "COALESCE(fs.flight_number, fs.callsign), "
                    f"{day_of('fs.scheduled_time', is_postgres=is_postgres)}"
                ),
                order_by=(
                    "CASE WHEN fs.aircraft_registration IS NOT NULL THEN 0 ELSE 1 END, "
                    "fs.scheduled_time DESC"
                ),
                where=where_clause,
                is_postgres=is_postgres,
            )
            query = f"""
                WITH base_data AS (
                    {base_data}
                ),
                -- Join with aircraft_static_info and apply livery filter
                filtered_data AS (
                    SELECT bd.*
                    FROM base_data bd
                    LEFT JOIN aircraft_static_info asi ON bd.aircraft_registration = asi.registration
                    WHERE {outer_where_clause}
                ),
                -- Calculate counts after livery filtering
                counted_data AS (
                    SELECT
                        fd.*,
                        COUNT(*) OVER() as total_count,
                        COUNT(*) FILTER (WHERE fd.flight_type = 'arrival') OVER() as arrival_count,
                        COUNT(*) FILTER (WHERE fd.flight_type = 'departure') OVER() as departure_count
                    FROM filtered_data fd
                )
                SELECT
                    cd.id,
                    cd.flight_type,
                    cd.flight_number,
                    cd.callsign,
                    cd.airline_name,
                    cd.airline_iata,
                    cd.remote_airport_iata,
                    cd.remote_airport_name,
                    cd.aircraft_type,
                    cd.aircraft_registration,
                    cd.scheduled_time,
                    cd.estimated_time,
                    cd.actual_time,
                    cd.status,
                    cd.terminal,
                    cd.gate,
                    CASE WHEN asi.registration IS NOT NULL THEN true ELSE false END as has_static_info,
                    CASE WHEN asi.images_downloaded = true THEN true ELSE false END as has_images,
                    asi.livery_type,
                    (SELECT ai.image_path FROM aircraft_images ai WHERE ai.registration = cd.aircraft_registration ORDER BY ai.display_order LIMIT 1) as image_path,
                    cd.total_count,
                    cd.arrival_count,
                    cd.departure_count
                FROM counted_data cd
                LEFT JOIN aircraft_static_info asi ON cd.aircraft_registration = asi.registration
                ORDER BY cd.scheduled_time ASC
                LIMIT :limit OFFSET :offset
            """

            result = session.execute(text(query), params).fetchall()

            # Extract counts from first row (all rows have same counts due to window function)
            if result:
                total_count = result[0][20]
                arrival_count = result[0][21]
                departure_count = result[0][22]
            else:
                total_count = 0
                arrival_count = 0
                departure_count = 0

            # When flight_type filter is applied, arrival_count/departure_count from window
            # functions only reflect filtered data. We need a separate query to get
            # unfiltered type counts for the UI tabs.
            if flight_type in ("arrival", "departure"):
                # Build base WHERE without flight_type filter
                base_where_conditions = [
                    "fs.airport_iata = :airport_iata",
                    "fs.scheduled_time >= :start_time",
                    "fs.scheduled_time <= :end_time",
                ]
                type_count_params = {
                    "airport_iata": airport_iata,
                    "start_time": utc_start,
                    "end_time": utc_end,
                }
                if aircraft_type:
                    base_where_conditions.append(
                        "LOWER(fs.aircraft_type) LIKE LOWER(:aircraft_type_pattern)"
                    )
                    type_count_params["aircraft_type_pattern"] = f"%{aircraft_type}%"

                base_where_clause = " AND ".join(base_where_conditions)

                # Build outer WHERE for livery filtering (optimized)
                outer_livery_conditions = []
                if livery:
                    outer_livery_conditions.append("asi.livery_type = :livery_type")
                    type_count_params["livery_type"] = livery
                if has_livery:
                    outer_livery_conditions.append(HAS_LIVERY_SQL)
                outer_livery_clause = (
                    " AND ".join(outer_livery_conditions) if outer_livery_conditions else "1=1"
                )

                deduped_types = latest_rows(
                    columns="fs.flight_type, fs.aircraft_registration",
                    source="flight_schedules fs",
                    partition_by=(
                        "COALESCE(fs.flight_number, fs.callsign), "
                        f"{day_of('fs.scheduled_time', is_postgres=is_postgres)}, "
                        "fs.flight_type"
                    ),
                    order_by=(
                        "CASE WHEN fs.aircraft_registration IS NOT NULL THEN 0 ELSE 1 END, "
                        "fs.scheduled_time DESC"
                    ),
                    where=base_where_clause,
                    is_postgres=is_postgres,
                )
                type_count_query = f"""
                    SELECT
                        COUNT(*) FILTER (WHERE flight_type = 'arrival') as arrivals,
                        COUNT(*) FILTER (WHERE flight_type = 'departure') as departures
                    FROM (
                        {deduped_types}
                    ) deduped
                    LEFT JOIN aircraft_static_info asi ON deduped.aircraft_registration = asi.registration
                    WHERE {outer_livery_clause}
                """
                type_result = session.execute(text(type_count_query), type_count_params).fetchone()
                if type_result:
                    arrival_count = type_result[0] or 0
                    departure_count = type_result[1] or 0

            # Build response
            schedules = []
            for row in result:
                scheduled_time = row[10]
                estimated_time = row[11]
                actual_time = row[12]

                schedule = {
                    "id": row[0],
                    "flight_type": row[1],
                    "flight_number": row[2],
                    "callsign": row[3],
                    "airline_name": row[4],
                    "airline_iata": row[5],
                    "remote_airport_iata": row[6],
                    "remote_airport_name": row[7],
                    "aircraft_type": row[8],
                    "aircraft_registration": row[9],
                    "scheduled_time": convert_utc_to_beijing(_to_iso(scheduled_time))
                    if scheduled_time
                    else None,
                    "estimated_time": convert_utc_to_beijing(_to_iso(estimated_time))
                    if estimated_time
                    else None,
                    "actual_time": convert_utc_to_beijing(_to_iso(actual_time))
                    if actual_time
                    else None,
                    "status": row[13],
                    "terminal": row[14],
                    "gate": row[15],
                    "has_static_info": row[16],
                    "has_images": row[17],
                    "livery_indicator": extract_livery_indicator(row[4]),
                    "livery_type": row[18],
                    "image_url": get_image_url(row[19]) if row[19] else None,
                }
                schedules.append(schedule)

            return jsonify(
                {
                    "success": True,
                    "schedules": schedules,
                    "total_count": total_count,
                    "arrival_count": arrival_count,
                    "departure_count": departure_count,
                    "limit": limit,
                    "offset": offset,
                }
            )

        finally:
            session.close()

    except Exception as e:
        logger.error(f"Error getting flight schedules: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/flight-schedules/filter-options")
def get_flight_schedule_filter_options():
    """Get available filter options for flight schedules.

    Query parameters:
        - airport: Airport IATA or ICAO code (optional, for context-specific options)
        - date: Date in YYYY-MM-DD format (optional)
    """
    try:
        from sqlalchemy import text

        airport = request.args.get("airport", "").strip().upper()
        date_str = request.args.get("date", "")
        search = request.args.get("search", "").strip()

        session = db_manager.get_session()
        try:
            # Build search filter and ordering if search provided
            search_filter = ""
            search_order = "fs.airport_iata"
            search_params = {}

            if search:
                search_upper = search.upper()
                search_params["search_pattern"] = f"%{search_upper}%"
                search_params["search_exact"] = search_upper
                search_params["search_starts"] = f"{search_upper}%"

            # Get available airports - search from airports table directly (much faster)
            if search:
                airports_query = """
                    SELECT iata_code as airport_iata, icao_code as airport_icao, name
                    FROM airports
                    WHERE (
                        UPPER(iata_code) LIKE :search_pattern
                        OR UPPER(icao_code) LIKE :search_pattern
                        OR UPPER(COALESCE(name, '')) LIKE :search_pattern
                    )
                    ORDER BY
                        CASE
                            WHEN UPPER(iata_code) = :search_exact THEN 0
                            WHEN UPPER(icao_code) = :search_exact THEN 1
                            WHEN UPPER(iata_code) LIKE :search_starts THEN 2
                            WHEN UPPER(icao_code) LIKE :search_starts THEN 3
                            ELSE 4
                        END,
                        iata_code
                    LIMIT 50
                """
            else:
                # No search provided - return empty list to avoid expensive full table scan
                # Frontend has search box for airport selection
                airports_query = None

            if airports_query:
                airports_result = session.execute(text(airports_query), search_params).fetchall()
            else:
                airports_result = []
            airports = [
                {"iata": row[0], "icao": row[1], "name": row[2] or f"{row[0]}/{row[1]}"}
                for row in airports_result
            ]

            # If airport is specified, get aircraft types and liveries for that airport
            aircraft_types = []
            liveries = []

            if airport:
                # Build date filter - always limit to recent data for performance
                params = {"airport_iata": airport, "airport_icao": airport}

                if date_str:
                    try:
                        target_date = datetime.strptime(date_str, "%Y-%m-%d")
                        beijing_start = BEIJING_TZ.localize(
                            datetime(target_date.year, target_date.month, target_date.day, 0, 0, 0)
                        )
                        beijing_end = BEIJING_TZ.localize(
                            datetime(
                                target_date.year, target_date.month, target_date.day, 23, 59, 59
                            )
                        )
                        params["start_time"] = beijing_start.astimezone(UTC)
                        params["end_time"] = beijing_end.astimezone(UTC)
                    except ValueError:
                        # Default to recent 30 days
                        now_utc = datetime.now(UTC)
                        params["start_time"] = now_utc - timedelta(days=30)
                        params["end_time"] = now_utc + timedelta(days=7)
                else:
                    # Default to 1 day ago + 3 days future for fast filter options
                    now_utc = datetime.now(UTC)
                    params["start_time"] = now_utc - timedelta(days=1)
                    params["end_time"] = now_utc + timedelta(days=3)

                date_filter = (
                    "AND fs.scheduled_time >= :start_time AND fs.scheduled_time <= :end_time"
                )

                # Execute all 3 queries in a single round-trip using UNION ALL
                # This is faster than 3 separate queries
                local_day = beijing_date("fs.scheduled_time", is_postgres=db_manager.is_postgres)
                combined_query = f"""
                    -- Query 1: Aircraft types
                    SELECT 'type' as query_type, fs.aircraft_type as value, CAST(COUNT(DISTINCT fs.aircraft_registration) AS TEXT) as count
                    FROM flight_schedules fs
                    WHERE fs.airport_iata = :airport_iata
                      AND fs.aircraft_type IS NOT NULL
                      AND fs.aircraft_type != ''
                      {date_filter}
                    GROUP BY fs.aircraft_type

                    UNION ALL

                    -- Query 2: Liveries
                    SELECT 'livery' as query_type, asi.livery_type as value, CAST(COUNT(DISTINCT fs.aircraft_registration) AS TEXT) as count
                    FROM flight_schedules fs
                    JOIN aircraft_static_info asi ON fs.aircraft_registration = asi.registration
                    WHERE fs.airport_iata = :airport_iata
                      AND {HAS_LIVERY_SQL}
                      {date_filter}
                    GROUP BY asi.livery_type

                    UNION ALL

                    -- Query 3: Dates
                    SELECT 'date' as query_type,
                           {local_day} as value,
                           '0' as count
                    FROM flight_schedules fs
                    WHERE fs.airport_iata = :airport_iata
                      {date_filter}
                    GROUP BY {local_day}
                """
                combined_result = session.execute(
                    text(combined_query), {"airport_iata": airport, **params}
                ).fetchall()

                # Parse combined results
                aircraft_types = []
                liveries = []
                available_dates = []

                for row in combined_result:
                    query_type, value, count = row[0], row[1], row[2]
                    if query_type == "type":
                        aircraft_types.append({"code": value, "count": int(count)})
                    elif query_type == "livery":
                        liveries.append({"name": value, "count": int(count)})
                    elif query_type == "date":
                        available_dates.append(value)

                # Sort results
                aircraft_types.sort(key=lambda x: x["count"], reverse=True)
                liveries.sort(key=lambda x: x["count"], reverse=True)
                available_dates.sort(reverse=True)

                # Limit results
                aircraft_types = aircraft_types[:50]
                liveries = liveries[:50]
                available_dates = available_dates[:30]

            return jsonify(
                {
                    "success": True,
                    "airports": airports,
                    "aircraft_types": aircraft_types,
                    "liveries": liveries,
                    "available_dates": available_dates if airport else [],
                }
            )

        finally:
            session.close()

    except Exception as e:
        logger.error(f"Error getting flight schedule filter options: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


# ============================================================
# Scraper Status Admin Page and APIs
# ============================================================


def get_scraper_db_session():
    """Get a database session for scraper queries.

    Reuses the shared `db_manager` engine — it's critical for SQLite where
    in-memory databases are per-connection, but also the right thing for
    Postgres (reuses the connection pool instead of spinning up a second one).
    """
    if db_manager is None:
        # Lazy init for code paths that call this before init_app()
        # (e.g. a request arriving before cold-start finished).
        init_app()
    return db_manager.get_session()


@app.route("/admin/scraper-status")
@admin_required
def admin_scraper_status_page():
    """Scraper status monitoring page."""
    return render_template("admin_scraper_status.html")


@app.route("/api/admin/scraper/stats")
@admin_required
def api_admin_scraper_stats():
    """Get scraper queue statistics."""
    try:
        from sqlalchemy import text

        session = get_scraper_db_session()
        try:
            # Task counts by status
            result = session.execute(
                text(
                    """
                    SELECT status, COUNT(*) as count
                    FROM scraper_tasks
                    GROUP BY status
                """
                )
            )
            status_counts = {row.status: row.count for row in result}

            # Active workers — compute the cutoff in Python so the SQL is
            # dialect-agnostic (Postgres's NOW() - INTERVAL and SQLite's
            # datetime() have different syntax).
            cutoff = datetime.utcnow() - timedelta(minutes=5)
            result = session.execute(
                text(
                    """
                    SELECT COUNT(*) as count
                    FROM scraper_workers
                    WHERE status = 'active'
                    AND last_heartbeat > :cutoff
                """
                ),
                {"cutoff": cutoff},
            )
            active_workers = result.fetchone().count

            # Tasks by type
            result = session.execute(
                text(
                    """
                    SELECT task_type, COUNT(*) as count
                    FROM scraper_tasks
                    WHERE status = 'pending'
                    GROUP BY task_type
                """
                )
            )
            pending_by_type = {row.task_type: row.count for row in result}

            stats = {
                "status_counts": status_counts,
                "active_workers": active_workers,
                "pending_by_type": pending_by_type,
                "total_pending": status_counts.get("pending", 0),
                "total_processing": status_counts.get("claimed", 0)
                + status_counts.get("processing", 0),
                "total_completed": status_counts.get("completed", 0),
                "total_no_data": status_counts.get("no_data", 0),
                "total_failed": status_counts.get("failed", 0),
            }
            return jsonify({"success": True, "stats": stats})
        finally:
            session.close()
    except Exception as e:
        logger.error(f"Error getting scraper stats: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/admin/scraper/workers")
@admin_required
def api_admin_scraper_workers():
    """Get active workers list."""
    try:
        from sqlalchemy import text

        session = get_scraper_db_session()
        try:
            # Compute `seconds_since_heartbeat` in Python to keep the SQL
            # dialect-agnostic (no EXTRACT(EPOCH FROM ...)).
            result = session.execute(
                text(
                    """
                    SELECT worker_id, status, last_heartbeat, tasks_completed,
                           current_task_id, metadata
                    FROM scraper_workers
                    ORDER BY last_heartbeat DESC
                    LIMIT 50
                """
                )
            )
            now = datetime.utcnow()
            workers = []
            for row in result:
                secs = None
                if row.last_heartbeat:
                    hb = row.last_heartbeat
                    # SQLite returns strings; Postgres returns datetime.
                    if isinstance(hb, str):
                        try:
                            hb = datetime.fromisoformat(hb)
                        except ValueError:
                            hb = None
                    if hb is not None:
                        secs = (now - hb).total_seconds()
                workers.append(
                    {
                        "worker_id": row.worker_id,
                        "status": row.status,
                        "last_heartbeat": row.last_heartbeat.isoformat()
                        if row.last_heartbeat and not isinstance(row.last_heartbeat, str)
                        else row.last_heartbeat,
                        "tasks_completed": row.tasks_completed,
                        "current_task_id": row.current_task_id,
                        "metadata": row.metadata or {},
                        "seconds_since_heartbeat": secs,
                    }
                )
            return jsonify({"success": True, "workers": workers})
        finally:
            session.close()
    except Exception as e:
        logger.error(f"Error getting scraper workers: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/admin/scraper/recent-tasks")
@admin_required
def api_admin_scraper_recent_tasks():
    """Get recent task results with pagination."""
    try:
        from sqlalchemy import text

        page = int(request.args.get("page", 1))
        limit = int(request.args.get("limit", 20))
        status_filter = request.args.get("status")
        offset = (page - 1) * limit

        session = get_scraper_db_session()
        try:
            from sqlalchemy import text

            # Build status filter
            status_clause = ""
            params: dict = {"limit": limit, "offset": offset}
            if status_filter:
                status_clause = "WHERE t.status = :status"
                params["status"] = status_filter

            # Get tasks. Use correlated subqueries instead of LATERAL JOIN
            # (SQLite doesn't have LATERAL; Postgres accepts both).
            result = session.execute(
                text(
                    f"""
                    SELECT t.id, t.task_type, t.task_key, t.status, t.attempts,
                           t.max_attempts, t.last_error, t.created_at, t.completed_at,
                           t.claimed_by,
                           (SELECT duration_seconds FROM scraper_results
                            WHERE task_id = t.id
                            ORDER BY created_at DESC LIMIT 1) AS duration_seconds,
                           (SELECT success FROM scraper_results
                            WHERE task_id = t.id
                            ORDER BY created_at DESC LIMIT 1) AS result_success
                    FROM scraper_tasks t
                    {status_clause}
                    ORDER BY COALESCE(t.completed_at, t.created_at) DESC
                    LIMIT :limit OFFSET :offset
                """
                ),
                params,
            )
            tasks = []
            for row in result:
                tasks.append(
                    {
                        "id": row.id,
                        "task_type": row.task_type,
                        "task_key": row.task_key,
                        "status": row.status,
                        "attempts": row.attempts,
                        "max_attempts": row.max_attempts,
                        "last_error": row.last_error,
                        "created_at": row.created_at.isoformat() if row.created_at else None,
                        "completed_at": row.completed_at.isoformat() if row.completed_at else None,
                        "claimed_by": row.claimed_by,
                        "duration_seconds": float(row.duration_seconds)
                        if row.duration_seconds
                        else None,
                    }
                )

            # Get total count
            count_result = session.execute(
                text(
                    f"""
                    SELECT COUNT(*) as total FROM scraper_tasks t {status_clause}
                """
                ),
                params,
            )
            total = count_result.fetchone().total
            pages = (total + limit - 1) // limit

            return jsonify(
                {
                    "success": True,
                    "tasks": tasks,
                    "total": total,
                    "page": page,
                    "pages": pages,
                }
            )
        finally:
            session.close()
    except Exception as e:
        logger.error(f"Error getting recent tasks: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


# Main block for local development
# For Lambda deployment, use lambda_handler.py instead
if __name__ == "__main__":
    from src.web.auth_shim import SKIP_AUTH

    init_app()
    if SKIP_AUTH:
        logger.info("Starting server with authentication DISABLED")
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
