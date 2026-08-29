"""Image-URL helpers used by every ``*_fastapi.py`` route that serves
photo data.

Two responsibilities that used to live at the top of ``web_app.py``:

- **URL construction**: relative DB paths → full URLs pointing at the
  media base URL (CloudFront on aws, GCS public bucket on gcp, or
  relative-path fallback for local dev). :func:`get_image_url` /
  :func:`transform_image_paths`.
- **Batch lookup**: fetching top-3 image paths for one or many
  aircraft registrations from the ``aircraft_images`` table.
  :func:`get_images_from_static_info` / :func:`batch_get_images_from_static_info`.

The batch helpers read the process-wide DB from
:mod:`src.web.runtime`. That lets image_helpers be a pure module —
callers don't have to thread ``db_manager`` through every layer.
"""

from __future__ import annotations

import logging

from sqlalchemy import text

from src.storage import resolve_media_base_url
from src.web import runtime

logger = logging.getLogger("web.image_helpers")

_EMPTY_PATHS: dict[str, str | None] = {
    "image_path_1": None,
    "image_path_2": None,
    "image_path_3": None,
}


def get_image_url(relative_path: str | None) -> str | None:
    """Convert a relative image path to a full URL.

    Args:
        relative_path: Stored path like ``data/jetphotos_images/B-1234_001.jpg``,
            or ``None`` / empty.

    Returns:
        Full URL. Rules:

        - ``None`` / empty in → ``None`` out.
        - Already an ``http(s)://`` URL → returned unchanged.
        - Missing ``data/`` prefix → prepended.
        - No ``MEDIA_BASE_URL`` configured → relative ``/data/...`` path
          so local dev keeps working via the FastAPI static mount.
        - Otherwise → ``{media_base_url}/data/...``.
    """
    if not relative_path:
        return None

    if relative_path.startswith(("https://", "http://")):
        return relative_path

    if not relative_path.startswith("data/"):
        relative_path = f"data/{relative_path}"

    base_url = resolve_media_base_url()
    if not base_url:
        return f"/{relative_path}"
    return f"{base_url}/{relative_path}"


def transform_image_paths(data: dict) -> dict:
    """In-place rewrite of ``image_path_[1-3]`` keys to full URLs.

    Same dict shape the ``aircraft_static_info``-serialised rows use.
    Returns the dict for call-chain ergonomics.
    """
    for key in ("image_path_1", "image_path_2", "image_path_3"):
        if data.get(key):
            data[key] = get_image_url(data[key])
    return data


def get_images_from_static_info(registration: str) -> dict[str, str | None]:
    """Return top-3 image paths for one aircraft registration.

    Reads from the ``aircraft_images`` table sorted by
    ``display_order``. Falsy inputs (or a missing ``runtime.db_manager``
    — e.g. called before startup) return three-``None`` dict rather
    than raise, so page handlers can render the row without a guard.
    """
    if not registration or runtime.db_manager is None:
        return dict(_EMPTY_PATHS)

    try:
        session = runtime.db_manager.get_session()
        try:
            result = session.execute(
                text(
                    """
                    SELECT image_path
                    FROM aircraft_images
                    WHERE registration = :reg
                      AND image_path IS NOT NULL
                      AND image_path != ''
                    ORDER BY display_order ASC
                    LIMIT 3
                    """
                ),
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
        logger.warning("Error getting images from aircraft_images: %s", e)

    return dict(_EMPTY_PATHS)


def batch_get_images_from_static_info(
    registrations: list[str],
) -> dict[str, dict[str, str | None]]:
    """Batch version of :func:`get_images_from_static_info`.

    Runs a single ``ROW_NUMBER() OVER (PARTITION BY registration ...)``
    query with the input registrations bound via named placeholders,
    then buckets the results into ``{registration: {image_path_1,
    image_path_2, image_path_3}}``.

    Empty input, no ``runtime.db_manager``, or a DB error → ``{}``.
    """
    if not registrations or runtime.db_manager is None:
        return {}

    try:
        session = runtime.db_manager.get_session()
        try:
            valid_regs = [r for r in registrations if r]
            if not valid_regs:
                return {}

            placeholders = ", ".join(f":reg{i}" for i in range(len(valid_regs)))
            params = {f"reg{i}": reg for i, reg in enumerate(valid_regs)}

            result = session.execute(
                text(
                    f"""
                    WITH ranked_images AS (
                        SELECT
                            registration,
                            image_path,
                            ROW_NUMBER() OVER (
                                PARTITION BY registration
                                ORDER BY display_order ASC
                            ) AS rn
                        FROM aircraft_images
                        WHERE registration IN ({placeholders})
                          AND image_path IS NOT NULL
                          AND image_path != ''
                    )
                    SELECT registration, image_path, rn
                    FROM ranked_images
                    WHERE rn <= 3
                    ORDER BY registration, rn
                    """
                ),
                params,
            ).fetchall()

            images_dict: dict[str, dict[str, str | None]] = {}
            for row in result:
                reg = row[0]
                image_path = row[1]
                rn = row[2]

                if reg not in images_dict:
                    images_dict[reg] = dict(_EMPTY_PATHS)
                images_dict[reg][f"image_path_{rn}"] = image_path

            return images_dict
        finally:
            session.close()
    except Exception as e:
        logger.warning("Error batch getting images from aircraft_images: %s", e)
        return {}
