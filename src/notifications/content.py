"""
Notification content builder.

This module prepares email content (HTML, text, attachments) for aircraft notifications.
It does NOT perform AI analysis - analysis results should be passed in.

Separation of concerns:
- This module: Builds HTML/text content, prepares attachments
- Analysis module: Runs AI analysis (called separately)
- Email module: Sends the prepared content
"""

import logging
import os
from datetime import datetime

from .base import EmailAttachment, EmailContent

logger = logging.getLogger("notifications.content")


class NotificationContentBuilder:
    """Builds email content for aircraft notifications.

    This class is responsible for:
    - Building HTML and text email bodies
    - Preparing image attachments (aircraft images, maps)
    - Formatting data for display

    This class is NOT responsible for:
    - Running AI analysis (pass analysis_html as parameter)
    - Generating maps (pass map paths as parameter)
    - Downloading images (pass image paths as parameter)
    """

    def __init__(self):
        """Initialize the content builder."""
        logger.info("NotificationContentBuilder initialized")

    def build_content(
        self,
        subject: str,
        aircraft_data: dict,
        aircraft_image_paths: list[str] | None = None,
        map_image_paths: list[str] | None = None,
        analysis_html: str | None = None,
        static_info: dict | None = None,
        flight_endpoints: dict | None = None,
    ) -> EmailContent:
        """Build complete email content.

        Args:
            subject: Email subject line
            aircraft_data: Aircraft data dictionary from API
            aircraft_image_paths: Optional list of aircraft image file paths
            map_image_paths: Optional list of map image file paths
            analysis_html: Optional pre-generated AI analysis HTML
            static_info: Optional static aircraft information (owner, operator, etc.)
            flight_endpoints: Optional flight departure/arrival information

        Returns:
            EmailContent object ready to send
        """
        # Extract aircraft information
        identifiers = self._extract_identifiers(aircraft_data)
        position = self._extract_position(aircraft_data)
        tracking_links = self._create_tracking_links(aircraft_data)

        # Prepare attachments and HTML references
        attachments = []
        aircraft_image_html = ""
        map_image_html = ""

        # Process aircraft images
        if aircraft_image_paths:
            aircraft_attachments, aircraft_image_html = self._prepare_aircraft_images(
                aircraft_image_paths, identifiers["registration"]
            )
            attachments.extend(aircraft_attachments)

        # Process map images
        if map_image_paths:
            map_attachments, map_image_html = self._prepare_map_images(map_image_paths)
            attachments.extend(map_attachments)

        # Generate static info HTML
        static_info_html = self._generate_static_info_html(static_info)

        # Generate flight endpoints HTML
        flight_endpoints_html = self._generate_flight_endpoints_html(flight_endpoints)

        # Build HTML content
        html_body = self._build_html(
            identifiers=identifiers,
            position=position,
            tracking_links=tracking_links,
            aircraft_image_html=aircraft_image_html,
            map_image_html=map_image_html,
            analysis_html=analysis_html or "",
            static_info_html=static_info_html,
            flight_endpoints_html=flight_endpoints_html,
        )

        # Build text content
        text_body = self._build_text(
            identifiers, position, tracking_links, static_info, flight_endpoints
        )

        return EmailContent(
            subject=subject, html_body=html_body, text_body=text_body, attachments=attachments
        )

    def _extract_identifiers(self, aircraft_data: dict) -> dict:
        """Extract aircraft identifiers from data."""
        return {
            "flight_number": (aircraft_data.get("flight") or "").strip() or None,
            "callsign": (aircraft_data.get("call") or "").strip() or None,
            "registration": (aircraft_data.get("r") or "").strip() or None,
            "aircraft_type": (aircraft_data.get("t") or "Unknown").strip(),
            "icao": (aircraft_data.get("hex") or "").strip().upper(),
            "squawk": aircraft_data.get("squawk") or "Unknown",
        }

    def _extract_position(self, aircraft_data: dict) -> dict:
        """Extract position data from aircraft data."""
        return {
            "latitude": aircraft_data.get("lat", "Unknown"),
            "longitude": aircraft_data.get("lon", "Unknown"),
            "altitude_ft": aircraft_data.get("alt_baro")
            or aircraft_data.get("alt_geom")
            or "Unknown",
            "speed_kts": aircraft_data.get("gs") or "Unknown",
            "heading": aircraft_data.get("track") or "Unknown",
            "vertical_rate": aircraft_data.get("baro_rate") or aircraft_data.get("geom_rate") or 0,
        }

    def _create_tracking_links(self, aircraft_data: dict) -> dict:
        """Create tracking links for the aircraft."""
        links = {}
        icao = (aircraft_data.get("hex") or "").strip().upper()

        if icao:
            links["adsb_exchange"] = f"https://globe.adsbexchange.com/?icao={icao}"
            links["flightradar24"] = f"https://www.flightradar24.com/{icao}"

        return links

    def _prepare_aircraft_images(
        self, image_paths: list[str], registration: str
    ) -> tuple[list[EmailAttachment], str]:
        """Prepare aircraft image attachments and HTML.

        Args:
            image_paths: List of image file paths
            registration: Aircraft registration for naming

        Returns:
            Tuple of (attachments list, HTML string)
        """
        attachments = []
        html_parts = []

        for i, path in enumerate(image_paths[:3]):  # Max 3 images
            if not os.path.exists(path):
                continue

            try:
                with open(path, "rb") as f:
                    data = f.read()

                content_id = f"aircraft_image_{i}"
                ext = path.lower().rsplit(".", 1)[-1] if "." in path else "jpg"
                subtype = "jpeg" if ext in ("jpg", "jpeg") else ext

                attachments.append(
                    EmailAttachment(
                        data=data,
                        content_id=content_id,
                        filename=f"aircraft_{registration}_{i + 1}.{ext}",
                        subtype=subtype,
                        inline=True,
                    )
                )

                html_parts.append(f"""
                <div style="flex: 1; text-align: center; padding: 5px;">
                    <img src="cid:{content_id}" alt="Aircraft {i + 1}"
                         style="max-width: 100%; height: auto; border-radius: 8px;
                                box-shadow: 0 2px 8px rgba(0,0,0,0.1);">
                </div>""")

            except Exception as e:
                logger.warning(f"Failed to prepare aircraft image {path}: {e}")

        if html_parts:
            html = f"""
            <div style="margin: 15px 0;">
                <div style="display: flex; flex-wrap: wrap; gap: 10px; justify-content: center;">
                    {"".join(html_parts)}
                </div>
                <p style="font-size: 0.9em; color: #6c757d; margin-top: 10px; text-align: center;">
                    Aircraft {registration}
                </p>
            </div>
            """
            return attachments, html

        return [], ""

    def _prepare_map_images(self, map_paths: list[str]) -> tuple[list[EmailAttachment], str]:
        """Prepare map image attachments and HTML.

        Args:
            map_paths: List of map image file paths

        Returns:
            Tuple of (attachments list, HTML string)
        """
        attachments = []
        html_parts = []

        map_labels = ["Detail View", "Globe View"]

        for i, path in enumerate(map_paths[:2]):  # Max 2 maps
            if not os.path.exists(path):
                continue

            try:
                with open(path, "rb") as f:
                    data = f.read()

                content_id = f"map_image_{i}"
                ext = path.lower().rsplit(".", 1)[-1] if "." in path else "png"
                subtype = "jpeg" if ext in ("jpg", "jpeg") else ext

                attachments.append(
                    EmailAttachment(
                        data=data,
                        content_id=content_id,
                        filename=f"map_{i + 1}.{ext}",
                        subtype=subtype,
                        inline=True,
                    )
                )

                label = map_labels[i] if i < len(map_labels) else f"Map {i + 1}"
                html_parts.append(f'''
                <div style="flex: 1; text-align: center; padding: 5px; min-width: 300px;">
                    <h4 style="color: #34495e; margin-bottom: 8px;">{label}</h4>
                    <img src="cid:{content_id}" alt="{label}"
                         style="max-width: 100%; height: auto; border-radius: 8px;
                                box-shadow: 0 2px 8px rgba(0,0,0,0.1);">
                </div>''')

            except Exception as e:
                logger.warning(f"Failed to prepare map image {path}: {e}")

        if html_parts:
            html = f"""
            <div style="margin-top: 15px;">
                <h3 style="color: #2c3e50; margin-bottom: 10px;">Location Maps</h3>
                <div style="display: flex; flex-wrap: wrap; gap: 15px; justify-content: center;">
                    {"".join(html_parts)}
                </div>
            </div>
            """
            return attachments, html

        return [], ""

    def _build_html(
        self,
        identifiers: dict,
        position: dict,
        tracking_links: dict,
        aircraft_image_html: str,
        map_image_html: str,
        analysis_html: str,
        static_info_html: str = "",
        flight_endpoints_html: str = "",
    ) -> str:
        """Build complete HTML email body."""

        # Aircraft info items (Chinese)
        info_items = []
        if identifiers["flight_number"]:
            info_items.append(f"<li><strong>航班号:</strong> {identifiers['flight_number']}</li>")
        if identifiers["callsign"] and identifiers["callsign"] != identifiers["flight_number"]:
            info_items.append(f"<li><strong>呼号:</strong> {identifiers['callsign']}</li>")
        if identifiers["registration"]:
            info_items.append(f"<li><strong>注册号:</strong> {identifiers['registration']}</li>")
        if identifiers["aircraft_type"] != "Unknown":
            info_items.append(f"<li><strong>机型:</strong> {identifiers['aircraft_type']}</li>")
        if identifiers["icao"]:
            info_items.append(f"<li><strong>ICAO代码:</strong> {identifiers['icao']}</li>")
        if identifiers["squawk"] != "Unknown":
            info_items.append(f"<li><strong>应答机代码:</strong> {identifiers['squawk']}</li>")

        # Tracking links (Chinese)
        links_html = "<p>追踪此飞机:</p><ul>"
        if "adsb_exchange" in tracking_links:
            links_html += f'<li><a href="{tracking_links["adsb_exchange"]}">ADS-B Exchange</a></li>'
        if "flightradar24" in tracking_links:
            links_html += f'<li><a href="{tracking_links["flightradar24"]}">FlightRadar24</a></li>'
        links_html += "</ul>"

        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S UTC")

        return f"""
        <html>
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
        </head>
        <body style="font-family: 'Segoe UI', Arial, sans-serif; max-width: 900px; margin: 0 auto; padding: 20px; background-color: #f8f9fa;">
            <h1 style="color: #2c3e50; text-align: center; border-bottom: 4px solid #3498db; padding-bottom: 15px;">
                飞机追踪通知
            </h1>

            <!-- Aircraft Info Section -->
            <div style="background: linear-gradient(135deg, #ffffff 0%, #f8f9fa 100%); border: 2px solid #3498db; border-radius: 12px; padding: 25px; margin-bottom: 25px;">
                <h2 style="color: #2c3e50; margin-top: 0; border-bottom: 2px solid #3498db; padding-bottom: 8px;">
                    飞机信息
                </h2>
                {aircraft_image_html}
                <ul style="list-style-type: none; padding-left: 0; margin: 15px 0;">
                    {"".join(info_items)}
                </ul>
                {static_info_html}
            </div>

            <!-- Flight Route Section -->
            {flight_endpoints_html}

            <!-- Position Section -->
            <div style="background: linear-gradient(135deg, #ffffff 0%, #f8f9fa 100%); border: 2px solid #27ae60; border-radius: 12px; padding: 25px; margin-bottom: 25px;">
                <h2 style="color: #2c3e50; margin-top: 0; border-bottom: 2px solid #27ae60; padding-bottom: 8px;">
                    当前位置
                </h2>
                <ul style="list-style-type: none; padding-left: 0; margin: 15px 0;">
                    <li><strong>纬度:</strong> {position["latitude"]}</li>
                    <li><strong>经度:</strong> {position["longitude"]}</li>
                    <li><strong>高度:</strong> {position["altitude_ft"]} 英尺</li>
                    <li><strong>速度:</strong> {position["speed_kts"]} 节</li>
                    <li><strong>航向:</strong> {position["heading"]}°</li>
                    <li><strong>垂直速度:</strong> {position["vertical_rate"]} 英尺/分钟</li>
                </ul>

                {map_image_html}

                <div style="margin-top: 20px;">
                    {links_html}
                </div>
            </div>

            <!-- Analysis Section -->
            {analysis_html}

            <div style="text-align: center; margin-top: 30px; padding: 15px; background-color: #ecf0f1; border-radius: 8px;">
                <p style="color: #7f8c8d; margin: 0; font-size: 0.9em;">
                    飞机追踪系统自动通知 | {current_time}
                </p>
            </div>
        </body>
        </html>
        """

    def _generate_static_info_html(self, static_info: dict | None) -> str:
        """Generate HTML for aircraft static information (Chinese).

        Args:
            static_info: Static info dictionary

        Returns:
            HTML string for static info section
        """
        if not static_info:
            return ""

        info_items = []

        if static_info.get("owner"):
            info_items.append(f"<li><strong>所有者:</strong> {static_info['owner']}</li>")

        if static_info.get("operator"):
            info_items.append(f"<li><strong>运营商:</strong> {static_info['operator']}</li>")

        if static_info.get("manufacturer"):
            manufacturer_info = static_info["manufacturer"]
            if static_info.get("model"):
                manufacturer_info += f" {static_info['model']}"
            info_items.append(f"<li><strong>制造商/型号:</strong> {manufacturer_info}</li>")

        if static_info.get("serial_number"):
            info_items.append(f"<li><strong>序列号:</strong> {static_info['serial_number']}</li>")

        if static_info.get("year_built"):
            from datetime import datetime

            age = datetime.now().year - static_info["year_built"]
            info_items.append(
                f"<li><strong>出厂年份:</strong> {static_info['year_built']} ({age}年机龄)</li>"
            )

        if static_info.get("country_of_registration"):
            info_items.append(
                f"<li><strong>注册国家:</strong> {static_info['country_of_registration']}</li>"
            )

        if not info_items:
            return ""

        return f"""
        <div style="margin-top: 15px; padding-top: 15px; border-top: 1px dashed #bdc3c7;">
            <h3 style="color: #2c3e50; margin-top: 0; font-size: 1.1em;">飞机详情</h3>
            <ul style="list-style-type: none; padding-left: 0; margin: 10px 0;">
                {"".join(info_items)}
            </ul>
        </div>
        """

    def _generate_flight_endpoints_html(self, flight_endpoints: dict | None) -> str:
        """Generate HTML for flight departure/arrival information (Chinese).

        Args:
            flight_endpoints: Flight endpoints dictionary

        Returns:
            HTML string for flight route section
        """
        if not flight_endpoints:
            return ""

        departure = flight_endpoints.get("departure", {})
        arrival = flight_endpoints.get("arrival", {})
        track_count = flight_endpoints.get("track_count", 0)

        # Format departure info
        dep_location = departure.get("location_name") or departure.get("country") or "未知"
        dep_lat = departure.get("lat")
        dep_lon = departure.get("lon")
        dep_time = departure.get("time", "")
        dep_coords = f"({dep_lat:.4f}, {dep_lon:.4f})" if dep_lat and dep_lon else ""

        # Format arrival info
        arr_location = arrival.get("location_name") or arrival.get("country") or "未知"
        arr_lat = arrival.get("lat")
        arr_lon = arrival.get("lon")
        arr_time = arrival.get("time", "")
        arr_coords = f"({arr_lat:.4f}, {arr_lon:.4f})" if arr_lat and arr_lon else ""

        return f"""
        <div style="background: linear-gradient(135deg, #ffffff 0%, #f8f9fa 100%); border: 2px solid #9b59b6; border-radius: 12px; padding: 25px; margin-bottom: 25px;">
            <h2 style="color: #2c3e50; margin-top: 0; border-bottom: 2px solid #9b59b6; padding-bottom: 8px;">
                航班路线
            </h2>
            <div style="display: flex; justify-content: space-between; flex-wrap: wrap;">
                <div style="flex: 1; min-width: 200px; margin: 10px;">
                    <h3 style="color: #27ae60; margin: 0 0 10px 0;">起飞</h3>
                    <ul style="list-style-type: none; padding-left: 0; margin: 0;">
                        <li><strong>地点:</strong> {dep_location}</li>
                        <li><strong>坐标:</strong> {dep_coords}</li>
                        <li><strong>时间:</strong> {dep_time}</li>
                    </ul>
                </div>
                <div style="flex: 0; display: flex; align-items: center; font-size: 2em; color: #9b59b6; margin: 0 20px;">
                    →
                </div>
                <div style="flex: 1; min-width: 200px; margin: 10px;">
                    <h3 style="color: #e74c3c; margin: 0 0 10px 0;">降落</h3>
                    <ul style="list-style-type: none; padding-left: 0; margin: 0;">
                        <li><strong>地点:</strong> {arr_location}</li>
                        <li><strong>坐标:</strong> {arr_coords}</li>
                        <li><strong>时间:</strong> {arr_time}</li>
                    </ul>
                </div>
            </div>
            <p style="color: #7f8c8d; font-size: 0.9em; margin: 15px 0 0 0; text-align: center;">
                共 {track_count} 个追踪点
            </p>
        </div>
        """

    def _build_text(
        self,
        identifiers: dict,
        position: dict,
        tracking_links: dict,
        static_info: dict | None = None,
        flight_endpoints: dict | None = None,
    ) -> str:
        """Build plain text email body (Chinese)."""
        lines = ["飞机追踪通知\n"]

        if identifiers["flight_number"]:
            lines.append(f"航班号: {identifiers['flight_number']}")
        if identifiers["registration"]:
            lines.append(f"注册号: {identifiers['registration']}")
        if identifiers["aircraft_type"] != "Unknown":
            lines.append(f"机型: {identifiers['aircraft_type']}")
        if identifiers["icao"]:
            lines.append(f"ICAO代码: {identifiers['icao']}")

        # Static info
        if static_info:
            lines.append("\n飞机详情:")
            if static_info.get("owner"):
                lines.append(f"  所有者: {static_info['owner']}")
            if static_info.get("operator"):
                lines.append(f"  运营商: {static_info['operator']}")
            if static_info.get("manufacturer"):
                manufacturer = static_info["manufacturer"]
                if static_info.get("model"):
                    manufacturer += f" {static_info['model']}"
                lines.append(f"  制造商/型号: {manufacturer}")
            if static_info.get("serial_number"):
                lines.append(f"  序列号: {static_info['serial_number']}")
            if static_info.get("year_built"):
                lines.append(f"  出厂年份: {static_info['year_built']}")

        # Flight endpoints
        if flight_endpoints:
            departure = flight_endpoints.get("departure", {})
            arrival = flight_endpoints.get("arrival", {})

            lines.append("\n航班路线:")
            dep_location = departure.get("location_name") or departure.get("country") or "未知"
            dep_lat = departure.get("lat")
            dep_lon = departure.get("lon")
            lines.append(f"  起飞: {dep_location}")
            if dep_lat and dep_lon:
                lines.append(f"    坐标: ({dep_lat:.4f}, {dep_lon:.4f})")
            if departure.get("time"):
                lines.append(f"    时间: {departure['time']}")

            arr_location = arrival.get("location_name") or arrival.get("country") or "未知"
            arr_lat = arrival.get("lat")
            arr_lon = arrival.get("lon")
            lines.append(f"  降落: {arr_location}")
            if arr_lat and arr_lon:
                lines.append(f"    坐标: ({arr_lat:.4f}, {arr_lon:.4f})")
            if arrival.get("time"):
                lines.append(f"    时间: {arrival['time']}")

        lines.append("\n当前位置:")
        lines.append(f"  纬度: {position['latitude']}")
        lines.append(f"  经度: {position['longitude']}")
        lines.append(f"  高度: {position['altitude_ft']} 英尺")
        lines.append(f"  速度: {position['speed_kts']} 节")

        if tracking_links:
            lines.append("\n追踪链接:")
            for name, url in tracking_links.items():
                lines.append(f"  {name}: {url}")

        return "\n".join(lines)
