"""
Social Media Note Aircraft Analysis Service

Uses an LLM (Bedrock on the aws target, Gemini on gcp — see src/llm/) to analyze
social media notes (Xiaohongshu, Weibo, Douyin) and extract aircraft
registrations, calculate attention indices, and identify trends.

Results are stored in:
- note_aircraft_analysis: Individual note analysis results
- aircraft_attention_aggregate: Per-aircraft aggregated metrics

Usage:
    # Analyze single note by ID
    uv run python -m src.services.note_analysis_service --note-id abc123

    # Process batch of pending notes
    uv run python -m src.services.note_analysis_service --limit 100

    # Show statistics
    uv run python -m src.services.note_analysis_service --stats

    # Re-analyze notes (including already analyzed)
    uv run python -m src.services.note_analysis_service --reanalyze --limit 50
"""

import argparse
import json
import logging
import math
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from botocore.exceptions import ClientError
from sqlalchemy import text
from sqlalchemy.orm import Session

from src.data.models import AircraftAttentionAggregate, NoteAircraftAnalysis
from src.llm.factory import LLMClientFactory, resolve_llm_provider_name, resolve_model_id
from src.utils.database import DatabaseManager
from src.utils.yaml_config import YAMLConfig

logger = logging.getLogger("note_analysis")


# =============================================================================
# LLM Prompts
# =============================================================================

SYSTEM_PROMPT = """You are a professional aviation social media analyst. Your task is to analyze social media notes, extract aircraft registration numbers from them, and assess the content's attention level.

## Your Capabilities

1. **Registration Recognition**: Identify aircraft registration formats from various countries
   - China: B-XXXX or B-XXXXX (e.g. B-1234, B-30EW)
   - USA: N + digits/letters (e.g. N12345, N1KE)
   - Malta: 9H-XXX (e.g. 9H-VJN)
   - Cayman Islands: VP-C + XX (e.g. VP-CAA)
   - Germany: D-XXXX (e.g. D-ABCD)
   - France: F-XXXX (e.g. F-GKXA)
   - Japan: JA + 4 digits (e.g. JA8119)
   - Russia: RA-XXXXX (e.g. RA-96022)
   - Other national registration formats

2. **Content Classification**: Determine note type
   - spotting: aviation-enthusiast photography records
   - news: aviation news reports
   - accident: accident-related
   - rumor: unverified information
   - fan: celebrity fans (celebrity private jets)
   - other: other

3. **Attention Assessment**: Based on engagement data and content value

## Output Format

Output strictly in the following JSON format (do not include other content):

```json
{
  "registrations": ["B-1234", "N12345"],
  "registration_details": [
    {
      "registration": "B-1234",
      "confidence": 0.95,
      "context": "Explicitly mentioned in the note title",
      "aircraft_type": "Boeing 737-800",
      "notes": "Air China livery"
    }
  ],
  "content_type": "spotting",
  "sentiment": "positive",
  "topics": ["livery", "airport", "plane spotting"],
  "content_quality_score": 75,
  "attention_reason": "Professional content with HD photos and detailed descriptions"
}
```

## Notes

- If no registration is found, return an empty array [] for registrations
- confidence represents extraction confidence (0-1)
- content_quality_score ranges 0-100, indicating content quality
- Always return valid JSON
"""

ANALYSIS_PROMPT_TEMPLATE = """Please analyze the following social media note, extract aircraft registrations, and assess attention level.

## Note Information

**Title**: {title}

**Body Content**:
{content}

**Tags**: {tags}

**Location**: {location}

## Engagement Data

- Likes: {like_count}
- Collects: {collect_count}
- Comments: {comment_count}

## Comment Content (top 50)

{comments}

---

Please output the analysis result in the JSON format specified in the system prompt."""


# =============================================================================
# Attention Index Calculator
# =============================================================================


class AttentionIndexCalculator:
    """Calculate attention index based on multiple weighted factors."""

    def __init__(self, weights: dict[str, float] | None = None):
        """Initialize calculator with configurable weights.

        Args:
            weights: Dict with factor weights. Keys: engagement, content_quality,
                    author_influence, recency, specificity
        """
        self.weights = weights or {
            "engagement": 0.30,
            "content_quality": 0.25,
            "author_influence": 0.20,
            "recency": 0.15,
            "specificity": 0.10,
        }

    def calculate(
        self,
        like_count: int = 0,
        collect_count: int = 0,
        comment_count: int = 0,
        content_quality_score: int = 50,
        follower_count: int = 0,
        is_verified: bool = False,
        note_created_at: datetime | None = None,
        registration_count: int = 0,
        avg_confidence: float = 0.5,
    ) -> tuple[int, str]:
        """Calculate attention index and determine level.

        Returns:
            Tuple of (attention_index, attention_level)
        """
        # 1. Engagement score (30%) - logarithmic scale
        engagement_raw = like_count + collect_count * 2 + comment_count * 3
        engagement_score = min(100, math.log10(max(1, engagement_raw)) * 25)

        # 2. Content quality score (25%) - from LLM
        quality_score = min(100, max(0, content_quality_score))

        # 3. Author influence score (20%)
        influence_base = math.log10(max(1, follower_count)) * 10 if follower_count > 0 else 0
        influence_bonus = 20 if is_verified else 0
        influence_score = min(100, influence_base + influence_bonus)

        # 4. Recency score (15%) - exponential decay
        if note_created_at:
            now = datetime.now(UTC)
            if note_created_at.tzinfo is None:
                note_created_at = note_created_at.replace(tzinfo=UTC)
            days_old = (now - note_created_at).days
            recency_score = max(0, 100 * math.exp(-days_old / 30))  # Decay over 30 days
        else:
            recency_score = 50  # Default if no date

        # 5. Specificity score (10%) - based on registration extraction quality
        specificity_base = min(100, registration_count * 30)
        specificity_confidence = avg_confidence * 100
        specificity_score = (specificity_base + specificity_confidence) / 2

        # Calculate weighted total
        attention_index = int(
            engagement_score * self.weights["engagement"]
            + quality_score * self.weights["content_quality"]
            + influence_score * self.weights["author_influence"]
            + recency_score * self.weights["recency"]
            + specificity_score * self.weights["specificity"]
        )

        # Determine level
        if attention_index >= 70:
            level = "high"
        elif attention_index >= 40:
            level = "medium"
        else:
            level = "low"

        return attention_index, level


# =============================================================================
# Note Analysis Service
# =============================================================================


class NoteAnalysisService:
    """Service for analyzing social media notes using LLM."""

    def __init__(self, config_file: str = "config.yaml"):
        """Initialize the note analysis service.

        Args:
            config_file: Path to YAML configuration file
        """
        self.config_file = config_file
        self.yaml_config = YAMLConfig(config_file)
        self.db = self._init_database()

        # Initialize the LLM client
        self._init_llm_client()

        # Load note analysis configuration
        self._load_config()

        # Initialize attention calculator
        self.attention_calculator = AttentionIndexCalculator(weights=self.attention_weights)

        # Statistics
        self._processed_count = 0
        self._success_count = 0
        self._failed_count = 0
        self._total_input_tokens = 0
        self._total_output_tokens = 0

        logger.info(
            f"NoteAnalysisService initialized (model={self.model_id}, batch_size={self.batch_size})"
        )

    def _init_database(self) -> DatabaseManager:
        """Initialize database connection and ensure required tables exist."""
        db_config = self.yaml_config.get_database_config()
        db = DatabaseManager(db_config["url"])

        # Ensure tables exist
        self._ensure_tables(db)

        return db

    def _ensure_tables(self, db: DatabaseManager) -> None:
        """Ensure note analysis tables exist."""
        from src.data.models import Base

        session = db.get_session()
        try:
            # Create tables if they don't exist
            Base.metadata.create_all(
                db.engine,
                tables=[
                    NoteAircraftAnalysis.__table__,
                    AircraftAttentionAggregate.__table__,
                ],
            )
            session.commit()
            logger.info("Note analysis tables ensured")
        except Exception as e:
            session.rollback()
            logger.warning(f"Table creation check: {e}")
        finally:
            session.close()

    def _init_llm_client(self) -> None:
        """Initialize the LLM client for the active deployment target."""
        region = self.yaml_config.get("aws.region", "us-east-1")
        access_key = self.yaml_config.get("aws.access_key_id")
        secret_key = self.yaml_config.get("aws.secret_access_key")

        llm_config = self.yaml_config.get_llm_config()
        self.provider = resolve_llm_provider_name(llm_config.get("provider"))

        # Bedrock model ID comes from note_analysis config or falls back to llm config
        self.model_id = resolve_model_id(
            llm_config,
            self.provider,
            bedrock_model_id=self.yaml_config.get(
                "note_analysis.bedrock_model_id",
                self.yaml_config.get(
                    "llm.bedrock_model_id", "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
                ),
            ),
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

    def _load_config(self) -> None:
        """Load note analysis configuration."""
        config = self.yaml_config.config.get("note_analysis", {})

        self.enabled = config.get("enabled", True)
        self.batch_size = config.get("batch_size", 50)
        self.max_comments_to_analyze = config.get("max_comments_to_analyze", 50)

        # Attention weights
        self.attention_weights = config.get(
            "attention_weights",
            {
                "engagement": 0.30,
                "content_quality": 0.25,
                "author_influence": 0.20,
                "recency": 0.15,
                "specificity": 0.10,
            },
        )

    # -------------------------------------------------------------------------
    # Data Access Methods
    # -------------------------------------------------------------------------

    def get_pending_notes(
        self,
        source_type: str = "xiaohongshu",
        limit: int | None = None,
        reanalyze: bool = False,
    ) -> list[dict[str, Any]]:
        """Get notes pending analysis.

        Args:
            source_type: Platform source (xiaohongshu, weibo, douyin)
            limit: Maximum number of notes to return
            reanalyze: If True, include already analyzed notes

        Returns:
            List of note dictionaries
        """
        if limit is None:
            limit = self.batch_size

        session = self.db.get_session()
        try:
            if source_type == "xiaohongshu":
                return self._get_xiaohongshu_notes(session, limit, reanalyze)
            else:
                logger.warning(f"Unsupported source type: {source_type}")
                return []
        finally:
            session.close()

    def _get_xiaohongshu_notes(
        self,
        session: Session,
        limit: int,
        reanalyze: bool,
    ) -> list[dict[str, Any]]:
        """Get Xiaohongshu notes pending analysis."""
        if reanalyze:
            # Get all notes
            query = """
                SELECT note_id, source_url, title, content, tags, location,
                       author_id, author_name, like_count, collect_count,
                       comment_count, comments, note_created_at, scraped_at
                FROM xiaohongshu_notes
                WHERE content IS NOT NULL OR title IS NOT NULL
                ORDER BY like_count DESC NULLS LAST
                LIMIT :limit
            """
        else:
            # Get notes not yet analyzed
            query = """
                SELECT n.note_id, n.source_url, n.title, n.content, n.tags, n.location,
                       n.author_id, n.author_name, n.like_count, n.collect_count,
                       n.comment_count, n.comments, n.note_created_at, n.scraped_at
                FROM xiaohongshu_notes n
                LEFT JOIN note_aircraft_analysis a ON n.note_id = a.note_id
                WHERE a.id IS NULL
                  AND (n.content IS NOT NULL OR n.title IS NOT NULL)
                ORDER BY n.like_count DESC NULLS LAST
                LIMIT :limit
            """

        result = session.execute(text(query), {"limit": limit})
        notes = []
        for row in result:
            notes.append(
                {
                    "note_id": row[0],
                    "source_url": row[1],
                    "title": row[2],
                    "content": row[3],
                    "tags": row[4],
                    "location": row[5],
                    "author_id": row[6],
                    "author_name": row[7],
                    "like_count": row[8] or 0,
                    "collect_count": row[9] or 0,
                    "comment_count": row[10] or 0,
                    "comments": row[11],
                    "note_created_at": row[12],
                    "scraped_at": row[13],
                }
            )

        logger.info(f"Found {len(notes)} Xiaohongshu notes pending analysis")
        return notes

    def get_note_by_id(
        self, note_id: str, source_type: str = "xiaohongshu"
    ) -> dict[str, Any] | None:
        """Get a specific note by ID.

        Args:
            note_id: Note identifier
            source_type: Platform source

        Returns:
            Note dictionary or None
        """
        session = self.db.get_session()
        try:
            if source_type == "xiaohongshu":
                query = """
                    SELECT note_id, source_url, title, content, tags, location,
                           author_id, author_name, like_count, collect_count,
                           comment_count, comments, note_created_at, scraped_at
                    FROM xiaohongshu_notes
                    WHERE note_id = :note_id
                """
                result = session.execute(text(query), {"note_id": note_id})
                row = result.fetchone()
                if row:
                    return {
                        "note_id": row[0],
                        "source_url": row[1],
                        "title": row[2],
                        "content": row[3],
                        "tags": row[4],
                        "location": row[5],
                        "author_id": row[6],
                        "author_name": row[7],
                        "like_count": row[8] or 0,
                        "collect_count": row[9] or 0,
                        "comment_count": row[10] or 0,
                        "comments": row[11],
                        "note_created_at": row[12],
                        "scraped_at": row[13],
                    }
            return None
        finally:
            session.close()

    # -------------------------------------------------------------------------
    # LLM Analysis Methods
    # -------------------------------------------------------------------------

    def _format_comments_for_analysis(self, comments: Any) -> str:
        """Format comments for LLM analysis.

        Args:
            comments: Comments data (JSON or list)

        Returns:
            Formatted comments string
        """
        if not comments:
            return "(no comments)"

        # Parse comments if JSON string
        if isinstance(comments, str):
            try:
                comments = json.loads(comments)
            except json.JSONDecodeError:
                return "(failed to parse comments)"

        if not isinstance(comments, list):
            return "(no valid comments)"

        formatted = []
        count = 0
        for comment in comments[: self.max_comments_to_analyze]:
            author = comment.get("author_name", "anonymous")
            content = comment.get("content", "")
            likes = comment.get("like_count", 0)

            if content:
                formatted.append(f"- [{author}] {content} (likes: {likes})")
                count += 1

                # Include replies
                replies = comment.get("replies", [])
                for reply in replies[:5]:  # Limit replies per comment
                    reply_author = reply.get("author_name", "anonymous")
                    reply_content = reply.get("content", "")
                    if reply_content:
                        formatted.append(f"  └ [{reply_author}] {reply_content}")

        if not formatted:
            return "(no comment content)"

        return "\n".join(formatted)

    def _format_tags(self, tags: Any) -> str:
        """Format tags for display."""
        if not tags:
            return "(no tags)"

        if isinstance(tags, str):
            try:
                tags = json.loads(tags)
            except json.JSONDecodeError:
                return tags

        if isinstance(tags, list):
            return ", ".join(f"#{tag}" for tag in tags)

        return str(tags)

    def analyze_note(self, note: dict[str, Any]) -> dict[str, Any] | None:
        """Analyze a single note using LLM.

        Args:
            note: Note dictionary

        Returns:
            Analysis result dictionary or None on failure
        """
        note_id = note.get("note_id", "unknown")

        # Format prompt
        prompt = ANALYSIS_PROMPT_TEMPLATE.format(
            title=note.get("title") or "(no title)",
            content=note.get("content") or "(no body)",
            tags=self._format_tags(note.get("tags")),
            location=note.get("location") or "(unknown)",
            like_count=note.get("like_count", 0),
            collect_count=note.get("collect_count", 0),
            comment_count=note.get("comment_count", 0),
            comments=self._format_comments_for_analysis(note.get("comments")),
        )

        # Build request
        request_body = {
            "modelId": self.model_id,
            "messages": [
                {"role": "user", "content": [{"text": prompt}]},
            ],
            "system": [{"text": SYSTEM_PROMPT}],
            "inferenceConfig": {
                "maxTokens": 8192,
                "temperature": 0.3,
            },
        }

        try:
            response = self.llm_client.converse(**request_body)

            # Extract token usage
            usage = response.get("usage", {})
            input_tokens = usage.get("inputTokens", 0)
            output_tokens = usage.get("outputTokens", 0)

            self._total_input_tokens += input_tokens
            self._total_output_tokens += output_tokens

            # Extract text response
            output = response.get("output", {})
            message = output.get("message", {})
            content = message.get("content", [])

            result_text = ""
            for item in content:
                if "text" in item:
                    result_text += item["text"]

            # Parse JSON from response
            parsed_result = self._parse_llm_response(result_text)

            if parsed_result:
                parsed_result["input_tokens"] = input_tokens
                parsed_result["output_tokens"] = output_tokens
                parsed_result["raw_response"] = result_text
                logger.info(
                    f"Analyzed note {note_id}: "
                    f"{len(parsed_result.get('registrations', []))} registrations found "
                    f"(tokens: {input_tokens}/{output_tokens})"
                )
                return parsed_result
            else:
                logger.warning(f"Failed to parse LLM response for note {note_id}")
                return None

        except ClientError as e:
            # Bedrock-specific; Gemini failures surface as AnalysisError below.
            logger.error(f"Bedrock API error for note {note_id}: {e}")
            return None
        except Exception as e:
            logger.error(f"Error analyzing note {note_id}: {e}")
            return None

    def _parse_llm_response(self, response_text: str) -> dict[str, Any] | None:
        """Parse JSON from LLM response.

        Args:
            response_text: Raw LLM response text

        Returns:
            Parsed JSON dict or None
        """
        # Try to find JSON in response
        json_match = re.search(r"```json\s*([\s\S]*?)\s*```", response_text)
        if json_match:
            json_str = json_match.group(1)
        else:
            # Try to find raw JSON
            json_match = re.search(r"\{[\s\S]*\}", response_text)
            if json_match:
                json_str = json_match.group(0)
            else:
                return None

        # Try to parse JSON, with fallback to fix common issues
        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            # Try to fix common JSON issues from LLM output
            fixed_json = self._fix_json(json_str)
            try:
                return json.loads(fixed_json)
            except json.JSONDecodeError as e2:
                # Log context around error position for debugging
                pos = e2.pos if hasattr(e2, "pos") else 0
                context_start = max(0, pos - 50)
                context_end = min(len(fixed_json), pos + 50)
                context = fixed_json[context_start:context_end]
                logger.warning(f"JSON parse error at pos {pos}: {e2.msg}")
                logger.debug(f"JSON context around error: ...{context}...")
                return None

    def _fix_json(self, json_str: str) -> str:
        """Fix common JSON syntax errors from LLM output.

        Args:
            json_str: Potentially malformed JSON string

        Returns:
            Fixed JSON string
        """
        # Remove trailing commas before ] or }
        fixed = re.sub(r",\s*([}\]])", r"\1", json_str)
        # Fix missing commas between array elements or object properties
        fixed = re.sub(r'"\s*\n\s*"', '",\n"', fixed)
        fixed = re.sub(r"}\s*\n\s*{", "},\n{", fixed)
        fixed = re.sub(r']\s*\n\s*"', '],\n"', fixed)
        fixed = re.sub(r'(\d)\s*\n\s*"', r'\1,\n"', fixed)  # Fix: number followed by string
        fixed = re.sub(
            r'(true|false|null)\s*\n\s*"', r'\1,\n"', fixed
        )  # Fix: bool/null followed by string
        # Remove any control characters except newlines
        fixed = re.sub(r"[\x00-\x09\x0b-\x1f\x7f-\x9f]", "", fixed)
        return fixed

    # -------------------------------------------------------------------------
    # Result Storage Methods
    # -------------------------------------------------------------------------

    def save_analysis(
        self,
        note: dict[str, Any],
        analysis: dict[str, Any],
        source_type: str = "xiaohongshu",
    ) -> bool:
        """Save analysis result to database.

        Args:
            note: Original note data
            analysis: LLM analysis result
            source_type: Platform source

        Returns:
            True if saved successfully
        """
        session = self.db.get_session()
        try:
            # Calculate attention index
            registrations = analysis.get("registrations", [])
            registration_details = analysis.get("registration_details", [])

            # Calculate average confidence
            avg_confidence = 0.5
            if registration_details:
                confidences = [d.get("confidence", 0.5) for d in registration_details]
                avg_confidence = sum(confidences) / len(confidences)

            attention_index, attention_level = self.attention_calculator.calculate(
                like_count=note.get("like_count", 0),
                collect_count=note.get("collect_count", 0),
                comment_count=note.get("comment_count", 0),
                content_quality_score=analysis.get("content_quality_score", 50),
                follower_count=0,  # Not available in current schema
                is_verified=False,
                note_created_at=note.get("note_created_at"),
                registration_count=len(registrations),
                avg_confidence=avg_confidence,
            )

            # Create or update analysis record
            existing = (
                session.query(NoteAircraftAnalysis).filter_by(note_id=note["note_id"]).first()
            )

            if existing:
                # Update existing record
                existing.registrations = registrations
                existing.registration_details = registration_details
                existing.attention_index = attention_index
                existing.attention_level = attention_level
                existing.attention_reason = analysis.get("attention_reason")
                existing.content_type = analysis.get("content_type")
                existing.sentiment = analysis.get("sentiment")
                existing.topics = analysis.get("topics")
                existing.llm_model = self.model_id
                existing.input_tokens = analysis.get("input_tokens")
                existing.output_tokens = analysis.get("output_tokens")
                existing.raw_response = analysis.get("raw_response")
                existing.analyzed_at = datetime.now(UTC)
            else:
                # Create new record
                new_analysis = NoteAircraftAnalysis(
                    note_id=note["note_id"],
                    source_type=source_type,
                    registrations=registrations,
                    registration_details=registration_details,
                    attention_index=attention_index,
                    attention_level=attention_level,
                    attention_reason=analysis.get("attention_reason"),
                    content_type=analysis.get("content_type"),
                    sentiment=analysis.get("sentiment"),
                    topics=analysis.get("topics"),
                    llm_model=self.model_id,
                    input_tokens=analysis.get("input_tokens"),
                    output_tokens=analysis.get("output_tokens"),
                    raw_response=analysis.get("raw_response"),
                    analyzed_at=datetime.now(UTC),
                )
                session.add(new_analysis)

            session.commit()
            logger.debug(f"Saved analysis for note {note['note_id']}")
            return True

        except Exception as e:
            session.rollback()
            logger.error(f"Failed to save analysis for note {note.get('note_id')}: {e}")
            return False
        finally:
            session.close()

    def update_aggregates(self, registrations: list[str], source_type: str = "xiaohongshu") -> None:
        """Update aggregate statistics for registrations.

        Args:
            registrations: List of registration numbers to update
            source_type: Platform source for filtering
        """
        if not registrations:
            return

        session = self.db.get_session()
        try:
            now = datetime.now(UTC)
            seven_days_ago = now - timedelta(days=7)
            thirty_days_ago = now - timedelta(days=30)

            for registration in registrations:
                # Get all analyses mentioning this registration
                # Use CAST() instead of :: to avoid SQLAlchemy param binding conflicts
                query = """
                    SELECT
                        COUNT(*) as total_mentions,
                        AVG(attention_index) as avg_attention,
                        MAX(attention_index) as max_attention,
                        MIN(analyzed_at) as first_seen,
                        MAX(analyzed_at) as last_seen,
                        SUM(CASE WHEN analyzed_at >= :seven_days THEN 1 ELSE 0 END) as mentions_7d,
                        SUM(CASE WHEN analyzed_at >= :thirty_days THEN 1 ELSE 0 END) as mentions_30d,
                        SUM(COALESCE(attention_index, 0)) as total_attention
                    FROM note_aircraft_analysis
                    WHERE CAST(registrations AS jsonb) @> CAST(:reg_json AS jsonb)
                """

                result = session.execute(
                    text(query),
                    {
                        "reg_json": json.dumps([registration]),
                        "seven_days": seven_days_ago,
                        "thirty_days": thirty_days_ago,
                    },
                )
                row = result.fetchone()

                if row and row[0] > 0:
                    # Get topic distribution
                    topic_query = """
                        SELECT topics
                        FROM note_aircraft_analysis
                        WHERE CAST(registrations AS jsonb) @> CAST(:reg_json AS jsonb)
                    """
                    topic_result = session.execute(
                        text(topic_query),
                        {
                            "reg_json": json.dumps([registration]),
                        },
                    )

                    topic_counts: dict[str, int] = {}
                    sentiment_counts: dict[str, int] = {"positive": 0, "neutral": 0, "negative": 0}
                    source_counts: dict[str, int] = {}
                    content_type_counts: dict[str, int] = {}

                    for topic_row in topic_result:
                        topics = topic_row[0]
                        if topics:
                            for topic in topics:
                                topic_counts[topic] = topic_counts.get(topic, 0) + 1

                    # Get sentiment and source distribution
                    dist_query = """
                        SELECT sentiment, source_type, content_type
                        FROM note_aircraft_analysis
                        WHERE CAST(registrations AS jsonb) @> CAST(:reg_json AS jsonb)
                    """
                    dist_result = session.execute(
                        text(dist_query),
                        {
                            "reg_json": json.dumps([registration]),
                        },
                    )

                    for dist_row in dist_result:
                        sentiment = dist_row[0] or "neutral"
                        source = dist_row[1] or "unknown"
                        content_type = dist_row[2] or "other"

                        sentiment_counts[sentiment] = sentiment_counts.get(sentiment, 0) + 1
                        source_counts[source] = source_counts.get(source, 0) + 1
                        content_type_counts[content_type] = (
                            content_type_counts.get(content_type, 0) + 1
                        )

                    # Sort topics by count
                    top_topics = sorted(
                        [{"topic": k, "count": v} for k, v in topic_counts.items()],
                        key=lambda x: int(x["count"]),  # type: ignore[arg-type]
                        reverse=True,
                    )[:10]

                    # Calculate trending score (sum of attention_index across all mentions)
                    mentions_7d = row[5] or 0
                    mentions_30d = row[6] or 0
                    total_attention = row[7] or 0  # SUM(attention_index)
                    trending_score = total_attention  # Higher engagement = higher score

                    # Upsert aggregate record
                    existing = (
                        session.query(AircraftAttentionAggregate)
                        .filter_by(registration=registration)
                        .first()
                    )

                    if existing:
                        existing.total_mentions = row[0]
                        existing.avg_attention_index = row[1]
                        existing.max_attention_index = row[2]
                        existing.first_seen = row[3]
                        existing.last_seen = row[4]
                        existing.mentions_7d = mentions_7d
                        existing.mentions_30d = mentions_30d
                        existing.top_topics = top_topics
                        existing.sentiment_distribution = sentiment_counts
                        existing.source_distribution = source_counts
                        existing.content_type_distribution = content_type_counts
                        existing.trending_score = trending_score
                    else:
                        new_agg = AircraftAttentionAggregate(
                            registration=registration,
                            total_mentions=row[0],
                            avg_attention_index=row[1],
                            max_attention_index=row[2],
                            first_seen=row[3],
                            last_seen=row[4],
                            mentions_7d=mentions_7d,
                            mentions_30d=mentions_30d,
                            top_topics=top_topics,
                            sentiment_distribution=sentiment_counts,
                            source_distribution=source_counts,
                            content_type_distribution=content_type_counts,
                            trending_score=trending_score,
                        )
                        session.add(new_agg)

            session.commit()
            logger.debug(f"Updated aggregates for {len(registrations)} registrations")

        except Exception as e:
            session.rollback()
            logger.error(f"Failed to update aggregates: {e}")
        finally:
            session.close()

    # -------------------------------------------------------------------------
    # Batch Processing Methods
    # -------------------------------------------------------------------------

    def process_batch(
        self,
        source_type: str = "xiaohongshu",
        limit: int | None = None,
        reanalyze: bool = False,
    ) -> dict[str, Any]:
        """Process a batch of notes.

        Args:
            source_type: Platform source
            limit: Maximum notes to process
            reanalyze: If True, re-process already analyzed notes

        Returns:
            Processing statistics
        """
        notes = self.get_pending_notes(source_type, limit, reanalyze)

        if not notes:
            logger.info("No pending notes to process")
            return {
                "processed": 0,
                "success": 0,
                "failed": 0,
                "registrations_found": 0,
            }

        all_registrations: set[str] = set()

        # Track batch-specific counts (not cumulative)
        batch_processed = 0
        batch_success = 0
        batch_failed = 0

        for note in notes:
            self._processed_count += 1
            batch_processed += 1

            analysis = self.analyze_note(note)

            if analysis:
                if self.save_analysis(note, analysis, source_type):
                    self._success_count += 1
                    batch_success += 1
                    registrations = analysis.get("registrations", [])
                    all_registrations.update(registrations)
                else:
                    self._failed_count += 1
                    batch_failed += 1
            else:
                self._failed_count += 1
                batch_failed += 1

        # Update aggregates for all found registrations
        if all_registrations:
            self.update_aggregates(list(all_registrations), source_type)

        # Return batch-specific counts, not cumulative
        return {
            "processed": batch_processed,
            "success": batch_success,
            "failed": batch_failed,
            "registrations_found": len(all_registrations),
            "input_tokens": self._total_input_tokens,
            "output_tokens": self._total_output_tokens,
        }

    def process_single_note(
        self,
        note_id: str,
        source_type: str = "xiaohongshu",
    ) -> dict[str, Any] | None:
        """Process a single note by ID.

        Args:
            note_id: Note identifier
            source_type: Platform source

        Returns:
            Analysis result or None
        """
        note = self.get_note_by_id(note_id, source_type)

        if not note:
            logger.error(f"Note not found: {note_id}")
            return None

        analysis = self.analyze_note(note)

        if analysis:
            self.save_analysis(note, analysis, source_type)
            registrations = analysis.get("registrations", [])
            if registrations:
                self.update_aggregates(registrations, source_type)

        return analysis

    # -------------------------------------------------------------------------
    # Statistics Methods
    # -------------------------------------------------------------------------

    def get_stats(self) -> dict[str, Any]:
        """Get analysis statistics.

        Returns:
            Statistics dictionary
        """
        session = self.db.get_session()
        try:
            # Total analyzed notes
            total_analyzed = session.execute(
                text("SELECT COUNT(*) FROM note_aircraft_analysis")
            ).scalar()

            # Breakdown by attention level
            level_query = """
                SELECT attention_level, COUNT(*)
                FROM note_aircraft_analysis
                GROUP BY attention_level
            """
            level_result = session.execute(text(level_query))
            levels = {row[0]: row[1] for row in level_result}

            # Total unique registrations
            total_registrations = session.execute(
                text("SELECT COUNT(*) FROM aircraft_attention_aggregate")
            ).scalar()

            # Top trending aircraft
            trending_query = """
                SELECT registration, trending_score, total_mentions
                FROM aircraft_attention_aggregate
                ORDER BY trending_score DESC NULLS LAST
                LIMIT 10
            """
            trending_result = session.execute(text(trending_query))
            trending = [
                {
                    "registration": row[0],
                    "score": float(row[1]) if row[1] else 0,
                    "mentions": row[2],
                }
                for row in trending_result
            ]

            # Pending notes count
            pending_query = """
                SELECT COUNT(*)
                FROM xiaohongshu_notes n
                LEFT JOIN note_aircraft_analysis a ON n.note_id = a.note_id
                WHERE a.id IS NULL
                  AND (n.content IS NOT NULL OR n.title IS NOT NULL)
            """
            pending = session.execute(text(pending_query)).scalar()

            return {
                "total_analyzed": total_analyzed,
                "attention_levels": levels,
                "total_registrations": total_registrations,
                "trending_aircraft": trending,
                "pending_notes": pending,
            }

        finally:
            session.close()


# =============================================================================
# CLI Interface
# =============================================================================


def main() -> None:
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Analyze social media notes for aircraft registrations"
    )
    parser.add_argument(
        "--config",
        default="config/config.yaml",
        help="Configuration file path",
    )
    parser.add_argument(
        "--note-id",
        help="Analyze a specific note by ID",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Maximum notes to process (default: 50)",
    )
    parser.add_argument(
        "--source",
        default="xiaohongshu",
        choices=["xiaohongshu", "weibo", "douyin"],
        help="Source platform (default: xiaohongshu)",
    )
    parser.add_argument(
        "--reanalyze",
        action="store_true",
        help="Re-analyze notes that have already been processed",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Show analysis statistics",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )

    args = parser.parse_args()

    # Configure logging
    log_level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    # Initialize service
    service = NoteAnalysisService(args.config)

    if args.stats:
        # Show statistics
        stats = service.get_stats()
        print("\n=== Note Analysis Statistics ===\n")
        print(f"Total analyzed notes: {stats['total_analyzed']}")
        print(f"Pending notes: {stats['pending_notes']}")
        print(f"Unique registrations found: {stats['total_registrations']}")
        print("\nAttention levels:")
        for level, count in stats.get("attention_levels", {}).items():
            print(f"  {level}: {count}")
        print("\nTrending aircraft:")
        for item in stats.get("trending_aircraft", []):
            print(
                f"  {item['registration']}: score={item['score']:.2f}, mentions={item['mentions']}"
            )

    elif args.note_id:
        # Analyze single note
        result = service.process_single_note(args.note_id, args.source)
        if result:
            print("\n=== Analysis Result ===\n")
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            print(f"Failed to analyze note: {args.note_id}")

    else:
        # Batch processing
        print(f"\nProcessing up to {args.limit} notes from {args.source}...")
        stats = service.process_batch(args.source, args.limit, args.reanalyze)
        print("\n=== Processing Complete ===\n")
        print(f"Processed: {stats['processed']}")
        print(f"Success: {stats['success']}")
        print(f"Failed: {stats['failed']}")
        print(f"Registrations found: {stats['registrations_found']}")
        print(f"Tokens used: {stats['input_tokens']} input, {stats['output_tokens']} output")


if __name__ == "__main__":
    main()
