"""
Comprehensive Aircraft Analysis Service

Uses a vision-capable LLM (Bedrock on the aws target, Gemini on gcp — see
src/llm/) to perform comprehensive aircraft intelligence analysis by combining:
- Static aircraft information (owner, operator, registration details)
- Flight track history (recent positions, flight patterns)
- Aircraft images (up to 3 photos)

Results are stored in the aircraft_static_info table (ai_analysis column).

Usage:
    # Analyze specific aircraft
    python -m src.services.aircraft_analysis_service --config config.yaml -r N12345

    # Process batch of pending aircraft
    python -m src.services.aircraft_analysis_service --config config.yaml --limit 10

    # Process ALL pending aircraft
    python -m src.services.aircraft_analysis_service --config config.yaml --all --limit 50

    # Re-analyze ALL aircraft (including those already analyzed)
    python -m src.services.aircraft_analysis_service --config config.yaml --reanalyze --limit 50

    # Process aircraft from a file (one registration per line)
    python -m src.services.aircraft_analysis_service --config config.yaml --file data/chinese_aircraft_registrations.txt --limit 10

    # Show count of pending aircraft
    python -m src.services.aircraft_analysis_service --config config.yaml --count

    # Show count of all aircraft with images (for re-analysis)
    python -m src.services.aircraft_analysis_service --config config.yaml --count --reanalyze
"""

import argparse
import json
import logging
import os
from datetime import datetime, timedelta

import boto3

from src.core.deploy_target import DeployTarget, current_target
from src.core.exceptions import StorageError
from src.llm.factory import LLMClientFactory, resolve_llm_provider_name, resolve_model_id
from src.storage import ObjectStorage, StorageFactory
from src.utils.database import DatabaseManager
from src.utils.yaml_config import YAMLConfig

logger = logging.getLogger("aircraft_analysis")


# Comprehensive analysis prompt with structured JSON output
ANALYSIS_PROMPT = """You are a professional aviation intelligence analyst. Based on the provided aircraft information, flight track data, and aircraft photos, perform a comprehensive intelligence analysis.

## Aircraft Basic Information
{static_info}

## Recent Flight Tracks
{track_info}

## Analysis Requirements

Please perform an in-depth analysis across the following dimensions:

### 1. Aircraft Identification and Livery Analysis
- Identify the livery type from photos (airline / government VIP / military / private, etc.)
- Describe livery features, markings, color schemes
- Point out any discrepancies between the livery and registration info

### 2. Owner / Operator Analysis
- Analyze the background of the owner / operator
- Nature of the organization (commercial / government / military / private)
- Possible affiliated entities or parent companies

### 3. Flight Pattern Analysis
- Analyze flight track characteristics (routes, frequency, timing patterns)
- Identify abnormal flight behaviors (e.g., detours, circling, unusual altitudes)
- Infer the purpose of flights

### 4. Destination Analysis
- Infer likely destinations from the track
- Analyze the significance of destinations (political / commercial / military)

### 5. Intelligence Assessment
- Comprehensive assessment of the aircraft's attention level (high / medium / low)
- Justification for the attention level
- Recommendations for follow-up monitoring

### 6. Executive Summary
- Summarize key findings in 2-3 sentences

## Output Format

Please output in the following format, with two parts:

### Part 1: Structured Data (JSON)

Output the following JSON structure inside a ```json code block:
```json
{{
    "aircraft_type": "ICAO type code, e.g. B738/A320/B77W, identified from photo",
    "manufacturer": "Manufacturer, e.g. Boeing/Airbus/Embraer",
    "model": "Specific model, e.g. 737-800/A320-200",
    "operator": "Operator name (Chinese or English)",
    "owner": "Owner name; may be the same as operator",
    "livery_type": "Livery type, e.g. standard airline livery / government VIP / military / private / special livery",
    "organization": "Affiliated organization identified from the photo",
    "livery_description": "Livery description: primary colors, stripes, logos, etc.",
    "special_markings": "Special markings: fuselage text, flags, emblems, etc.; null if none",
    "attention_level": "Attention level: high / medium / low",
    "attention_reason": "Brief reason for the assigned attention level",
    "intelligence_summary": "Intelligence summary, 2-3 sentences of key findings",
    "anomalies": "Anomalies such as livery/registration mismatch, data issues; null if none",
    "flight_pattern": "Flight pattern summary, e.g. routine route / VIP transport / training flight / anomalous",
    "recommended_actions": "Follow-up monitoring recommendations"
}}
```

### Part 2: Detailed Analysis Report (Markdown)

After the JSON block, output the full analysis report in Markdown format.

"""


class AircraftAnalysisService:
    """Comprehensive Aircraft Analysis Service using a vision-capable LLM."""

    def __init__(self, config_file: str = "config.yaml"):
        """Initialize the aircraft analysis service.

        Args:
            config_file: Path to YAML configuration file
        """
        self.config_file = config_file
        self.yaml_config = YAMLConfig(config_file)
        self.db = self._init_database()

        # Initialize the LLM client
        self._init_llm_client()

        # Load remote-image configuration. The `image_download.s3.*` keys
        # predate the storage abstraction and are read as written; the provider
        # they resolve to now follows DEPLOY_TARGET.
        image_config = self.yaml_config.config.get("image_download", {})
        s3_config = image_config.get("s3", {})
        self.remote_images_enabled = s3_config.get("enabled", False)
        self.image_bucket = self.yaml_config.get("image_download.s3.bucket", "")
        self.local_images_dir = image_config.get("images_dir", "data/jetphotos_images")
        self.storage = self._init_storage()

        # Analysis configuration
        self.track_days = 30  # Days of track history to include
        self.max_track_points = 100  # Max track points to include

        # Statistics
        self._processed_count = 0
        self._success_count = 0
        self._failed_count = 0
        self._total_input_tokens = 0
        self._total_output_tokens = 0

        logger.info(
            f"AircraftAnalysisService initialized (model={self.model_id}, "
            f"storage={type(self.storage).__name__ if self.storage else 'local files only'})"
        )

    def _init_database(self) -> DatabaseManager:
        """Initialize database connection and ensure required columns exist."""
        db_config = self.yaml_config.get_database_config()
        db = DatabaseManager(db_config["url"])

        # Ensure analysis columns exist in aircraft_static_info table
        self._ensure_analysis_columns(db)

        return db

    def _ensure_analysis_columns(self, db: DatabaseManager) -> None:
        """Ensure ai_analysis and related columns exist in aircraft_static_info."""
        from sqlalchemy import text

        # Define all required columns for structured analysis
        timestamp_type = "TIMESTAMP" if db.is_postgres else "DATETIME"
        columns = [
            # Basic analysis columns
            ("ai_analysis", "TEXT"),
            ("livery", "TEXT"),
            ("livery_analyzed_at", timestamp_type),
            # Structured analysis columns
            ("livery_type", "VARCHAR(100)"),
            ("organization", "VARCHAR(255)"),
            ("livery_description", "TEXT"),
            ("special_markings", "TEXT"),
            ("attention_level", "VARCHAR(20)"),
            ("attention_reason", "TEXT"),
            ("intelligence_summary", "TEXT"),
            ("anomalies", "TEXT"),
            ("flight_pattern", "VARCHAR(255)"),
            ("recommended_actions", "TEXT"),
            ("analyzed_at", timestamp_type),
        ]

        # Add each column if it doesn't exist (separate transaction for each)
        for col_name, col_type in columns:
            session = db.get_session()
            try:
                session.execute(
                    text(f"ALTER TABLE aircraft_static_info ADD COLUMN {col_name} {col_type}")
                )
                session.commit()
                logger.info(f"Added column {col_name}")
            except Exception as e:
                session.rollback()
                # Column likely already exists - this is expected
                logger.debug(f"Column {col_name} check: {e}")
            finally:
                session.close()

        logger.debug("Analysis columns check completed")

    def _init_llm_client(self) -> None:
        """Initialize the vision-capable LLM client for the active target."""
        region = self.yaml_config.get("aws.region", "us-east-1")
        access_key = self.yaml_config.get("aws.access_key_id")
        secret_key = self.yaml_config.get("aws.secret_access_key")

        llm_config = self.yaml_config.get_llm_config()
        self.provider = resolve_llm_provider_name(llm_config.get("provider"))

        # Image analysis takes the vision model, which on Gemini is a separate
        # (quality-first) model; Bedrock uses one multimodal model for both.
        self.model_id = resolve_model_id(
            llm_config,
            self.provider,
            bedrock_model_id=self.yaml_config.get(
                "llm.bedrock_model_id", "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
            ),
            vision=True,
        )

        self.llm_client = LLMClientFactory.create_from_dict(
            {
                **llm_config,
                "provider": self.provider,
                "aws_region": region,
                "aws_access_key_id": access_key,
                "aws_secret_access_key": secret_key,
            }
        )

        logger.info(
            f"LLM client initialized (provider={self.provider}, region={region}, "
            f"model={self.model_id})"
        )

    def _init_storage(self) -> ObjectStorage | None:
        """Create the object storage used for remote aircraft images.

        Returns:
            The storage instance, or ``None`` when remote images are disabled
            or misconfigured — in which case only the local filesystem is
            consulted, matching the previous behaviour.
        """
        if not self.remote_images_enabled:
            return None

        # Explicit static credentials only apply to S3; every other provider
        # authenticates through its own ambient credential chain.
        client = None
        access_key = self.yaml_config.get("aws.access_key_id")
        secret_key = self.yaml_config.get("aws.secret_access_key")
        if current_target() is DeployTarget.AWS and access_key and secret_key:
            client = boto3.client(
                "s3",
                region_name=self.yaml_config.get("aws.region", "us-east-1"),
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
            )

        try:
            return StorageFactory.create(
                self.yaml_config, bucket=self.image_bucket or None, client=client
            )
        except StorageError as e:
            logger.warning(f"Remote image storage unavailable, using local files only: {e}")
            return None

    # -------------------------------------------------------------------------
    # Data Collection Methods
    # -------------------------------------------------------------------------

    def get_static_info(self, registration: str) -> dict | None:
        """Get static aircraft information from database.

        Args:
            registration: Aircraft registration number

        Returns:
            Dict with aircraft static info or None
        """
        from sqlalchemy import text

        session = self.db.get_session()
        try:
            # Get static info
            result = session.execute(
                text("""
                SELECT
                    registration, hex_code, aircraft_type,
                    owner, operator, manufacturer, model,
                    serial_number, year_built, country_of_registration,
                    livery, data_source
                FROM aircraft_static_info
                WHERE registration = :reg
            """),
                {"reg": registration.upper()},
            )

            row = result.fetchone()
            if not row:
                return None

            # Get images from aircraft_images table
            images_result = session.execute(
                text("""
                SELECT image_path
                FROM aircraft_images
                WHERE registration = :reg
                ORDER BY display_order ASC
                LIMIT 10
            """),
                {"reg": registration.upper()},
            )

            images = [r[0] for r in images_result.fetchall()]

            return {
                "registration": row[0],
                "hex_code": row[1],
                "aircraft_type": row[2],
                "owner": row[3],
                "operator": row[4],
                "manufacturer": row[5],
                "model": row[6],
                "serial_number": row[7],
                "year_built": row[8],
                "country": row[9],
                "images": images,
                "livery": row[10],
                "data_source": row[11],
            }
        finally:
            session.close()

    def get_flight_tracks(self, registration: str, days: int = None) -> list[dict]:
        """Get recent flight track data for an aircraft.

        Args:
            registration: Aircraft registration number
            days: Number of days of history to retrieve

        Returns:
            List of track points with position and flight info
        """
        from sqlalchemy import text

        if days is None:
            days = self.track_days

        session = self.db.get_session()
        try:
            result = session.execute(
                text("""
                SELECT
                    snapshot_time, latitude, longitude,
                    altitude_baro, ground_speed, track,
                    flight_number, squawk, vertical_rate,
                    is_military, current_country
                FROM aircraft_snapshots
                WHERE registration = :reg
                AND snapshot_time >= :start_time
                ORDER BY snapshot_time DESC
                LIMIT :limit
            """),
                {
                    "reg": registration.upper(),
                    "start_time": datetime.utcnow() - timedelta(days=days),
                    "limit": self.max_track_points,
                },
            )

            tracks = []
            for row in result:
                tracks.append(
                    {
                        "time": row[0].isoformat() if row[0] else None,
                        "lat": float(row[1]) if row[1] else None,
                        "lon": float(row[2]) if row[2] else None,
                        "altitude": row[3],
                        "speed": float(row[4]) if row[4] else None,
                        "heading": float(row[5]) if row[5] else None,
                        "flight": row[6],
                        "squawk": row[7],
                        "vertical_rate": row[8],
                        "is_military": row[9],
                        "country": row[10],
                    }
                )

            logger.info(f"Found {len(tracks)} track points for {registration}")
            return tracks
        finally:
            session.close()

    def get_flight_summary(self, tracks: list[dict]) -> dict:
        """Generate a summary of flight activity from track points.

        Args:
            tracks: List of track point dicts

        Returns:
            Summary dict with statistics and patterns
        """
        if not tracks:
            return {"total_points": 0, "message": "No recent flight data available"}

        # Extract unique flights
        flights = set()
        countries = set()
        altitudes = []
        speeds = []

        for t in tracks:
            if t.get("flight"):
                flights.add(t["flight"])
            if t.get("country"):
                countries.add(t["country"])
            if t.get("altitude"):
                altitudes.append(t["altitude"])
            if t.get("speed"):
                speeds.append(t["speed"])

        # Calculate statistics
        summary = {
            "total_points": len(tracks),
            "unique_flights": list(flights),
            "countries_visited": list(countries),
            "date_range": {
                "earliest": tracks[-1]["time"] if tracks else None,
                "latest": tracks[0]["time"] if tracks else None,
            },
        }

        if altitudes:
            summary["altitude"] = {
                "min": min(altitudes),
                "max": max(altitudes),
                "avg": int(sum(altitudes) / len(altitudes)),
            }

        if speeds:
            summary["speed"] = {
                "min": min(speeds),
                "max": max(speeds),
                "avg": int(sum(speeds) / len(speeds)),
            }

        return summary

    # -------------------------------------------------------------------------
    # Image Loading Methods
    # -------------------------------------------------------------------------

    def _load_image_from_storage(self, key: str) -> bytes | None:
        """Load image from object storage."""
        if self.storage is None:
            return None

        try:
            return self.storage.download_bytes(key)
        except StorageError as e:
            logger.error(f"Failed to load image from object storage ({key}): {e}")
            return None

    def _load_image_from_local(self, path: str) -> bytes | None:
        """Load image from local filesystem."""
        paths_to_try = [
            path,
            os.path.join(self.local_images_dir, os.path.basename(path)),
            os.path.join(os.getcwd(), path),
        ]

        for p in paths_to_try:
            if os.path.exists(p):
                try:
                    with open(p, "rb") as f:
                        return f.read()
                except OSError as e:
                    logger.error(f"Failed to read local image ({p}): {e}")
                    continue

        logger.warning(f"Image not found locally: {path}")
        return None

    def _compress_image_for_bedrock(self, image_data: bytes, max_raw_mb: float = 3.7) -> bytes:
        """Compress image to stay under Bedrock's 5MB base64 limit.

        Bedrock API's 5MB limit applies to base64-encoded size, which is ~1.33x
        the raw size. So raw images need to be under ~3.75MB to be safe.

        Args:
            image_data: Raw image bytes
            max_raw_mb: Maximum raw size in MB (default 3.7 for safety margin)

        Returns:
            Compressed image bytes if over limit, otherwise original bytes
        """
        import io

        from PIL import Image

        max_bytes = max_raw_mb * 1024 * 1024
        if len(image_data) <= max_bytes:
            return image_data

        try:
            img = Image.open(io.BytesIO(image_data))
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")

            # Progressively compress until under limit
            compression_settings = [
                (1800, 75),
                (1600, 70),
                (1400, 65),
                (1200, 60),
                (1000, 55),
            ]

            for max_dim, quality in compression_settings:
                if max(img.size) > max_dim:
                    ratio = max_dim / max(img.size)
                    resized = img.resize(
                        (int(img.size[0] * ratio), int(img.size[1] * ratio)), Image.LANCZOS
                    )
                else:
                    resized = img

                buffer = io.BytesIO()
                resized.save(buffer, format="JPEG", quality=quality, optimize=True)

                if buffer.tell() <= max_bytes:
                    logger.info(
                        f"Compressed image from {len(image_data) / 1024 / 1024:.2f}MB "
                        f"to {buffer.tell() / 1024 / 1024:.2f}MB (dim={max_dim}, q={quality})"
                    )
                    return buffer.getvalue()

            # Return last attempt even if still over limit
            logger.warning(f"Could not compress image below {max_raw_mb}MB")
            return buffer.getvalue()

        except Exception as e:
            logger.error(f"Failed to compress image: {e}")
            return image_data

    def load_image(self, image_path: str) -> bytes | None:
        """Load image from object storage or local files, compressing for Bedrock."""
        if not image_path:
            return None

        image_data = None

        if self.storage is not None:
            image_data = self._load_image_from_storage(image_path)

        if not image_data:
            image_data = self._load_image_from_local(image_path)

        if image_data:
            # Compress if needed for Bedrock's base64 size limit
            image_data = self._compress_image_for_bedrock(image_data)

        return image_data

    def _get_image_media_type(self, image_path: str) -> str:
        """Determine image media type from path."""
        path_lower = image_path.lower()
        if path_lower.endswith(".png"):
            return "image/png"
        elif path_lower.endswith(".gif"):
            return "image/gif"
        elif path_lower.endswith(".webp"):
            return "image/webp"
        else:
            return "image/jpeg"

    # -------------------------------------------------------------------------
    # Analysis Methods
    # -------------------------------------------------------------------------

    def format_static_info(self, info: dict) -> str:
        """Format static info for the prompt."""
        if not info:
            return "No static information available"

        lines = []
        fields = [
            ("registration", "Registration"),
            ("hex_code", "ICAO Hex"),
            ("aircraft_type", "Aircraft Type"),
            ("manufacturer", "Manufacturer"),
            ("model", "Model"),
            ("serial_number", "Serial Number"),
            ("year_built", "Year Built"),
            ("country", "Country of Registration"),
            ("owner", "Owner"),
            ("operator", "Operator"),
            ("livery", "Previous Livery Analysis"),
            ("data_source", "Data Source"),
        ]

        for key, label in fields:
            value = info.get(key)
            if value:
                lines.append(f"- **{label}**: {value}")

        return "\n".join(lines) if lines else "No static information available"

    def format_track_info(self, tracks: list[dict], summary: dict) -> str:
        """Format track info for the prompt."""
        if not tracks:
            return "No recent flight track data available"

        lines = []

        # Add summary
        lines.append(f"**Track Summary** (last {self.track_days} days):")
        lines.append(f"- Total data points: {summary.get('total_points', 0)}")

        if summary.get("unique_flights"):
            lines.append(f"- Flight numbers used: {', '.join(summary['unique_flights'])}")

        if summary.get("countries_visited"):
            lines.append(f"- Countries/regions: {', '.join(summary['countries_visited'])}")

        if summary.get("altitude"):
            alt = summary["altitude"]
            lines.append(f"- Altitude range: {alt['min']} - {alt['max']} ft (avg: {alt['avg']} ft)")

        if summary.get("speed"):
            spd = summary["speed"]
            lines.append(f"- Speed range: {spd['min']} - {spd['max']} kts (avg: {spd['avg']} kts)")

        if summary.get("date_range"):
            dr = summary["date_range"]
            lines.append(f"- Date range: {dr['earliest']} to {dr['latest']}")

        # Add sample track points (most recent 20)
        lines.append("\n**Recent Track Points**:")
        for i, t in enumerate(tracks[:20]):
            if t.get("lat") and t.get("lon"):
                point = f"  {i + 1}. {t['time']}: ({t['lat']:.4f}, {t['lon']:.4f})"
                if t.get("altitude"):
                    point += f" @ {t['altitude']}ft"
                if t.get("speed"):
                    point += f", {t['speed']}kts"
                if t.get("flight"):
                    point += f" [{t['flight']}]"
                lines.append(point)

        return "\n".join(lines)

    def analyze_aircraft(
        self, static_info: dict, tracks: list[dict], images: list[tuple[bytes, str]]
    ) -> tuple[str | None, dict]:
        """Perform comprehensive aircraft analysis using a vision-capable LLM.

        Args:
            static_info: Static aircraft information dict
            tracks: List of flight track points
            images: List of (image_data, image_path) tuples

        Returns:
            Tuple of (analysis result text, token usage dict)
        """
        # Format context information
        static_text = self.format_static_info(static_info)
        track_summary = self.get_flight_summary(tracks)
        track_text = self.format_track_info(tracks, track_summary)

        # Build the prompt
        prompt = ANALYSIS_PROMPT.format(static_info=static_text, track_info=track_text)

        # Build content array with images first
        content = []

        # Add all images
        for image_data, image_path in images:
            image_format = self._get_image_media_type(image_path).split("/")[-1]
            content.append({"image": {"format": image_format, "source": {"bytes": image_data}}})

        # Add text prompt after images
        content.append({"text": prompt})

        # Build request
        request_body = {
            "modelId": self.model_id,
            "messages": [{"role": "user", "content": content}],
            "inferenceConfig": {"maxTokens": 4096, "temperature": 0.3},
        }

        logger.info(f"Analyzing aircraft with {len(images)} image(s), {len(tracks)} track points")

        try:
            response = self.llm_client.converse(**request_body)

            # Extract token usage
            usage = response.get("usage", {})
            token_usage = {
                "input_tokens": usage.get("inputTokens", 0),
                "output_tokens": usage.get("outputTokens", 0),
            }

            # Extract text response
            output = response.get("output", {})
            message = output.get("message", {})
            content = message.get("content", [])

            result_text = ""
            for item in content:
                if "text" in item:
                    result_text += item["text"]

            logger.info(
                f"Analysis complete "
                f"(tokens: {token_usage['input_tokens']}/{token_usage['output_tokens']})"
            )
            return result_text, token_usage

        except Exception as e:
            logger.error(f"LLM call failed: {e}")
            return None, {"input_tokens": 0, "output_tokens": 0}

    def parse_structured_analysis(self, analysis_text: str) -> tuple[dict | None, str]:
        """Parse structured JSON data from analysis response.

        Args:
            analysis_text: Full analysis text containing JSON block and Markdown report

        Returns:
            Tuple of (structured_data dict or None, markdown_report)
        """
        import re

        structured_data = None
        markdown_report = analysis_text

        # Try to extract JSON from ```json ... ``` block
        json_pattern = r"```json\s*([\s\S]*?)\s*```"
        match = re.search(json_pattern, analysis_text)

        if match:
            json_str = match.group(1).strip()
            try:
                structured_data = json.loads(json_str)
                logger.debug(f"Parsed structured data: {list(structured_data.keys())}")

                # Extract markdown report (everything after the JSON block)
                json_end = match.end()
                markdown_report = analysis_text[json_end:].strip()

                # If markdown report is empty, use original text
                if not markdown_report:
                    markdown_report = analysis_text

            except json.JSONDecodeError as e:
                logger.warning(f"Failed to parse JSON from analysis: {e}")
                # Keep original text as markdown report
        else:
            logger.warning("No JSON block found in analysis response")

        logger.info(
            f"Log Data: {json.dumps(structured_data, indent=2, ensure_ascii=False) if structured_data else 'No structured data found'}"
        )
        return structured_data, markdown_report

    def update_analysis(
        self, registration: str, analysis: str, structured_data: dict | None = None
    ) -> bool:
        """Update aircraft analysis in database with structured data.

        Args:
            registration: Aircraft registration number
            analysis: Full analysis text (Markdown report)
            structured_data: Optional dict with structured analysis fields

        Returns:
            True if update successful, False otherwise
        """
        from sqlalchemy import text

        session = self.db.get_session()
        try:
            # Build update query with structured fields
            params = {
                "reg": registration,
                "analysis": analysis,
            }

            # Base SQL update
            update_fields = [
                "ai_analysis = :analysis",
                "analyzed_at = CURRENT_TIMESTAMP",
                "last_updated = CURRENT_TIMESTAMP",
            ]

            # Add structured fields if available
            if structured_data:
                field_mapping = {
                    # Basic aircraft info (new fields)
                    "aircraft_type": "aircraft_type",
                    "manufacturer": "manufacturer",
                    "model": "model",
                    "operator": "operator",
                    "owner": "owner",
                    # Analysis fields
                    "livery_type": "livery_type",
                    "organization": "organization",
                    "livery_description": "livery_description",
                    "special_markings": "special_markings",
                    "attention_level": "attention_level",
                    "attention_reason": "attention_reason",
                    "intelligence_summary": "intelligence_summary",
                    "anomalies": "anomalies",
                    "flight_pattern": "flight_pattern",
                    "recommended_actions": "recommended_actions",
                }

                for json_key, db_col in field_mapping.items():
                    value = structured_data.get(json_key)
                    if value is not None:
                        # Convert null string to None
                        if isinstance(value, str) and value.lower() == "null":
                            value = None
                        params[db_col] = value
                        update_fields.append(f"{db_col} = :{db_col}")

                # Also update livery field with description for backward compatibility
                if structured_data.get("livery_description"):
                    params["livery"] = structured_data["livery_description"]
                    update_fields.append("livery = :livery")
                    update_fields.append("livery_analyzed_at = CURRENT_TIMESTAMP")

            sql = f"""
                UPDATE aircraft_static_info
                SET {", ".join(update_fields)}
                WHERE registration = :reg
            """

            session.execute(text(sql), params)
            session.commit()

            if structured_data:
                logger.info(
                    f"Updated analysis for {registration} with {len(structured_data)} structured fields"
                )
            else:
                logger.info(f"Updated analysis for {registration}")

            return True
        except Exception as e:
            session.rollback()
            logger.error(f"Failed to update analysis for {registration}: {e}")
            return False
        finally:
            session.close()

    # -------------------------------------------------------------------------
    # Processing Methods
    # -------------------------------------------------------------------------

    def process_aircraft(self, registration: str) -> tuple[bool, str | None]:
        """Process a single aircraft for comprehensive analysis.

        Args:
            registration: Aircraft registration number

        Returns:
            Tuple of (success, analysis_result)
        """
        registration = registration.upper()
        logger.info(f"Processing comprehensive analysis for {registration}")

        # 1. Get static info
        static_info = self.get_static_info(registration)
        if not static_info:
            logger.warning(f"No static info found for {registration}")
            # Create minimal static info
            static_info = {"registration": registration}

        # 2. Get flight tracks
        tracks = self.get_flight_tracks(registration)

        # 3. Load images (up to 10)
        image_paths = [static_info.get(f"image_path_{i}") for i in range(1, 11)]

        images = []
        for image_path in image_paths:
            if not image_path:
                continue
            image_data = self.load_image(image_path)
            if image_data:
                images.append((image_data, image_path))
                logger.debug(f"Loaded image: {image_path}")

        # 4. Check if we have enough data
        if not images and not tracks:
            logger.warning(f"No images or track data for {registration}")
            return False, None

        # 5. Perform analysis
        result, token_usage = self.analyze_aircraft(static_info, tracks, images)

        # Accumulate token usage
        self._total_input_tokens += token_usage.get("input_tokens", 0)
        self._total_output_tokens += token_usage.get("output_tokens", 0)

        if result:
            # Parse structured data from response
            structured_data, markdown_report = self.parse_structured_analysis(result)

            # Update database with structured data
            if self.update_analysis(registration, markdown_report, structured_data):
                self._success_count += 1
                return True, markdown_report
            else:
                self._failed_count += 1
                return False, markdown_report
        else:
            self._failed_count += 1
            return False, None

    def get_pending_aircraft(self, limit: int = 100, include_analyzed: bool = False) -> list[str]:
        """Get aircraft that need analysis.

        Args:
            limit: Maximum number of registrations to return
            include_analyzed: If True, include aircraft that already have analysis (for re-analysis)

        Returns:
            List of registration numbers
        """
        from sqlalchemy import text

        session = self.db.get_session()
        try:
            if include_analyzed:
                # Get all aircraft with images (for re-analysis)
                result = session.execute(
                    text("""
                    SELECT DISTINCT asi.registration
                    FROM aircraft_static_info asi
                    INNER JOIN aircraft_images ai ON asi.registration = ai.registration
                    WHERE asi.images_downloaded = true
                    ORDER BY asi.last_updated DESC
                    LIMIT :limit
                """),
                    {"limit": limit},
                )
            else:
                # Get only aircraft without analysis
                result = session.execute(
                    text("""
                    SELECT DISTINCT asi.registration
                    FROM aircraft_static_info asi
                    INNER JOIN aircraft_images ai ON asi.registration = ai.registration
                    WHERE asi.images_downloaded = true
                    AND (asi.ai_analysis IS NULL OR asi.ai_analysis = '')
                    ORDER BY asi.last_updated DESC
                    LIMIT :limit
                """),
                    {"limit": limit},
                )

            registrations = [row[0] for row in result]
            mode = "all (including analyzed)" if include_analyzed else "pending"
            logger.info(f"Found {len(registrations)} {mode} aircraft")
            return registrations
        finally:
            session.close()

    def process_batch(self, limit: int = 10, include_analyzed: bool = False) -> tuple[int, int]:
        """Process a batch of aircraft for analysis.

        Args:
            limit: Maximum number of aircraft to process
            include_analyzed: If True, re-analyze aircraft that already have analysis

        Returns:
            Tuple of (success count, failed count)
        """
        registrations = self.get_pending_aircraft(limit, include_analyzed=include_analyzed)

        if not registrations:
            logger.info("No aircraft pending analysis")
            return 0, 0

        success = 0
        failed = 0

        for i, reg in enumerate(registrations, 1):
            logger.info(f"[{i}/{len(registrations)}] Processing: {reg}")
            self._processed_count += 1

            try:
                ok, _ = self.process_aircraft(reg)
                if ok:
                    success += 1
                else:
                    failed += 1
            except Exception as e:
                logger.error(f"Error processing {reg}: {e}")
                failed += 1

        token_stats = self.get_token_stats()
        logger.info(
            f"Batch completed: {success} success, {failed} failed "
            f"(tokens: {token_stats['input_tokens']:,}/{token_stats['output_tokens']:,})"
        )
        return success, failed

    def get_stats(self) -> dict:
        """Get service statistics."""
        return {
            "processed": self._processed_count,
            "success": self._success_count,
            "failed": self._failed_count,
            "model": self.model_id,
            "remote_images_enabled": self.storage is not None,
            "total_input_tokens": self._total_input_tokens,
            "total_output_tokens": self._total_output_tokens,
            "total_tokens": self._total_input_tokens + self._total_output_tokens,
        }

    def get_token_stats(self) -> dict:
        """Get token usage statistics."""
        return {
            "input_tokens": self._total_input_tokens,
            "output_tokens": self._total_output_tokens,
            "total_tokens": self._total_input_tokens + self._total_output_tokens,
        }

    def reset_token_stats(self) -> None:
        """Reset token statistics."""
        self._total_input_tokens = 0
        self._total_output_tokens = 0

    def get_total_pending_count(self, include_analyzed: bool = False) -> int:
        """Get total count of aircraft pending analysis.

        Args:
            include_analyzed: If True, count all aircraft with images (for re-analysis)

        Returns:
            Number of aircraft with images (optionally filtered by analysis status)
        """
        from sqlalchemy import text

        session = self.db.get_session()
        try:
            if include_analyzed:
                result = session.execute(
                    text("""
                    SELECT COUNT(DISTINCT asi.registration)
                    FROM aircraft_static_info asi
                    INNER JOIN aircraft_images ai ON asi.registration = ai.registration
                    WHERE asi.images_downloaded = true
                """)
                )
            else:
                result = session.execute(
                    text("""
                    SELECT COUNT(DISTINCT asi.registration)
                    FROM aircraft_static_info asi
                    INNER JOIN aircraft_images ai ON asi.registration = ai.registration
                    WHERE asi.images_downloaded = true
                    AND (asi.ai_analysis IS NULL OR asi.ai_analysis = '')
                """)
                )
            count = result.scalar()
            return count or 0
        finally:
            session.close()

    def process_all(self, batch_size: int = 50, include_analyzed: bool = False) -> tuple[int, int]:
        """Process all aircraft pending analysis.

        Processes in batches to avoid memory issues and provide progress updates.

        Args:
            batch_size: Number of aircraft to process per batch
            include_analyzed: If True, re-analyze aircraft that already have analysis

        Returns:
            Tuple of (total success count, total failed count)
        """
        total_success = 0
        total_failed = 0
        batch_num = 0

        # Get initial count
        total_pending = self.get_total_pending_count(include_analyzed=include_analyzed)
        mode = "ALL (re-analysis)" if include_analyzed else "pending"
        logger.info(f"Starting to process {total_pending} {mode} aircraft")

        if total_pending == 0:
            logger.info("No aircraft pending analysis")
            return 0, 0

        while True:
            batch_num += 1
            logger.info(f"Processing batch {batch_num} (batch_size={batch_size})")

            # Process one batch
            success, failed = self.process_batch(
                limit=batch_size, include_analyzed=include_analyzed
            )

            total_success += success
            total_failed += failed

            # Log progress
            processed = total_success + total_failed
            remaining = self.get_total_pending_count(include_analyzed=include_analyzed) - processed
            token_stats = self.get_token_stats()
            logger.info(
                f"Batch {batch_num} complete: {success} success, {failed} failed. "
                f"Total progress: {processed}/{total_pending} ({remaining} remaining). "
                f"Tokens: {token_stats['total_tokens']:,} (in:{token_stats['input_tokens']:,}/out:{token_stats['output_tokens']:,})"
            )

            # Stop if no more aircraft to process
            if success == 0 and failed == 0:
                break

            # Also stop if remaining is 0 or less
            if remaining <= 0:
                break

        token_stats = self.get_token_stats()
        logger.info(
            f"All batches complete: {total_success} success, {total_failed} failed "
            f"(total {total_success + total_failed} processed). "
            f"Total tokens: {token_stats['total_tokens']:,} (in:{token_stats['input_tokens']:,}/out:{token_stats['output_tokens']:,})"
        )
        return total_success, total_failed


def main():
    """CLI entry point for aircraft analysis service."""
    parser = argparse.ArgumentParser(description="Comprehensive Aircraft Analysis Service")
    parser.add_argument("--config", "-c", default="config/config.yaml", help="Config file path")
    parser.add_argument("--registration", "-r", help="Analyze specific registration")
    parser.add_argument("--file", "-f", help="File containing registration numbers (one per line)")
    parser.add_argument(
        "--limit", "-l", type=int, default=10, help="Max aircraft to process per batch"
    )
    parser.add_argument(
        "--all",
        "-a",
        action="store_true",
        help="Process ALL pending aircraft (uses --limit as batch size)",
    )
    parser.add_argument(
        "--reanalyze",
        action="store_true",
        help="Re-analyze ALL aircraft including those already analyzed",
    )
    parser.add_argument("--count", action="store_true", help="Only show count of pending aircraft")
    parser.add_argument("--model", "-m", help="Override model ID")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")
    parser.add_argument("--track-days", "-d", type=int, default=30, help="Days of track history")

    args = parser.parse_args()

    # Setup logging
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )

    # Initialize service
    service = AircraftAnalysisService(config_file=args.config)

    # Override settings if specified
    if args.model:
        service.model_id = args.model
        logging.info(f"Using model override: {args.model}")

    if args.track_days:
        service.track_days = args.track_days

    # Count only mode
    if args.count:
        pending = service.get_total_pending_count(include_analyzed=args.reanalyze)
        if args.reanalyze:
            print(f"Total aircraft with images (for re-analysis): {pending}")
        else:
            print(f"Aircraft pending analysis: {pending}")
        return

    if args.registration:
        # Process specific registration
        success, analysis = service.process_aircraft(args.registration)
        if success:
            print(f"\n{'=' * 60}")
            print(f"Analysis for {args.registration.upper()}")
            print("=" * 60)
            print(analysis)
            print("=" * 60)
            # Display token usage
            token_stats = service.get_token_stats()
            print("\nToken Usage:")
            print(f"  Input tokens: {token_stats['input_tokens']:,}")
            print(f"  Output tokens: {token_stats['output_tokens']:,}")
            print(f"  Total tokens: {token_stats['total_tokens']:,}")
        else:
            print(f"Failed to analyze {args.registration}")
    elif args.file:
        # Process aircraft from file (processes ALL registrations in file, ignores --limit)
        if not os.path.exists(args.file):
            print(f"Error: File not found: {args.file}")
            return

        with open(args.file) as f:
            registrations = [
                line.strip() for line in f if line.strip() and not line.startswith("#")
            ]

        print(f"Processing {len(registrations)} aircraft from {args.file}...")
        print("-" * 50)

        success_count = 0
        failed_count = 0
        for i, reg in enumerate(registrations, 1):
            logger.info(f"[{i}/{len(registrations)}] Processing: {reg}")
            try:
                success, _ = service.process_aircraft(reg)
                if success:
                    success_count += 1
                else:
                    failed_count += 1
            except Exception as e:
                logger.error(f"Error processing {reg}: {e}")
                failed_count += 1

        print("\n" + "=" * 50)
        print("File Processing Complete:")
        print(f"  Source: {args.file}")
        print(f"  Success: {success_count}")
        print(f"  Failed: {failed_count}")
        print(f"  Total processed: {success_count + failed_count}")
        # Display token usage
        token_stats = service.get_token_stats()
        print("\nToken Usage:")
        print(f"  Input tokens: {token_stats['input_tokens']:,}")
        print(f"  Output tokens: {token_stats['output_tokens']:,}")
        print(f"  Total tokens: {token_stats['total_tokens']:,}")
        print("=" * 50)
    elif args.all or args.reanalyze:
        # Process ALL aircraft (pending or re-analysis)
        pending = service.get_total_pending_count(include_analyzed=args.reanalyze)
        if args.reanalyze:
            print(f"Re-analyzing ALL {pending} aircraft with images...")
        else:
            print(f"Processing ALL {pending} aircraft pending analysis...")
        print(f"Batch size: {args.limit}")
        print("-" * 50)

        success, failed = service.process_all(
            batch_size=args.limit, include_analyzed=args.reanalyze
        )

        print("\n" + "=" * 50)
        mode = "Re-analysis" if args.reanalyze else "Analysis"
        print(f"Aircraft {mode} Complete (ALL):")
        print(f"  Success: {success}")
        print(f"  Failed: {failed}")
        print(f"  Total processed: {success + failed}")
        # Display token usage
        token_stats = service.get_token_stats()
        print("\nToken Usage:")
        print(f"  Input tokens: {token_stats['input_tokens']:,}")
        print(f"  Output tokens: {token_stats['output_tokens']:,}")
        print(f"  Total tokens: {token_stats['total_tokens']:,}")
        print("=" * 50)
    else:
        # Process single batch
        success, failed = service.process_batch(limit=args.limit)
        print("\nAircraft Analysis Complete:")
        print(f"  Success: {success}")
        print(f"  Failed: {failed}")
        print(f"  Total processed: {success + failed}")
        # Display token usage
        token_stats = service.get_token_stats()
        print("\nToken Usage:")
        print(f"  Input tokens: {token_stats['input_tokens']:,}")
        print(f"  Output tokens: {token_stats['output_tokens']:,}")
        print(f"  Total tokens: {token_stats['total_tokens']:,}")


if __name__ == "__main__":
    main()
