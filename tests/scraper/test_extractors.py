"""
Tests for HTML extractors.
"""

import pytest
from resilient_scraper.scrapers.aviation.airport_data.extractor import AirportDataExtractor
from resilient_scraper.scrapers.aviation.jetphotos.extractor import JetPhotosExtractor


class TestJetPhotosExtractor:
    """Tests for JetPhotosExtractor."""

    def setup_method(self) -> None:
        """Set up test fixtures."""
        self.extractor = JetPhotosExtractor()

    def test_version(self) -> None:
        """Test that version is set."""
        assert self.extractor.version == "1.0.0"

    def test_extract_empty_html(self) -> None:
        """Test extraction from empty HTML returns default fields."""
        result = self.extractor.extract("", {})
        assert result["jetphotos_id"] is None
        assert result["photographer"] is None
        assert "source_url" in result

    def test_extract_with_source_url_context(self) -> None:
        """Test that source_url from context is used."""
        result = self.extractor.extract(
            "<html></html>",
            {"source_url": "https://www.jetphotos.com/photo/12345678"},
        )
        assert result["source_url"] == "https://www.jetphotos.com/photo/12345678"
        assert result["jetphotos_id"] == "12345678"

    def test_extract_h3h4_fields(self) -> None:
        """Test extraction of h3/h4 structured fields."""
        html = """
        <html>
        <h3>Photo Date</h3><h4>Mar 15, 2024</h4>
        <h3>Uploaded</h3><h4>Mar 20, 2024</h4>
        <h3>Camera</h3><h4>Canon EOS R5</h4>
        <h3>Views</h3><h4>1,234</h4>
        <h3>Likes</h3><h4>56</h4>
        </html>
        """
        result = self.extractor.extract(html, {})
        assert result["photo_date"] == "2024-03-15"
        assert result["upload_date"] == "2024-03-20"
        assert result["camera"] == "Canon EOS R5"
        assert result["views"] == 1234
        assert result["likes"] == 56

    def test_extract_photographer(self) -> None:
        """Test extraction of photographer name."""
        html = """
        <html>
        <h2><span>Photographer</span></h2>
        <h6>John Doe</h6>
        </html>
        """
        result = self.extractor.extract(html, {})
        assert result["photographer"] == "John Doe"

    def test_extract_photographer_link_fallback(self) -> None:
        """Test extraction of photographer from link."""
        html = """
        <html>
        <a href="/photographer/johndoe">John Doe</a>
        </html>
        """
        result = self.extractor.extract(html, {})
        assert result["photographer"] == "John Doe"

    def test_extract_airport_location(self) -> None:
        """Test extraction of airport/location."""
        html = """
        <html>
        <h2><span>Photo Location</span></h2>
        <a href="/airport/KJFK">John F. Kennedy International Airport</a>
        </html>
        """
        result = self.extractor.extract(html, {})
        assert result["airport_icao"] == "KJFK"
        assert result["airport_name"] == "John F. Kennedy International Airport"
        assert result["location"] == "John F. Kennedy International Airport"

    def test_extract_json_ld(self) -> None:
        """Test extraction from JSON-LD structured data."""
        html = """
        <html>
        <script type="application/ld+json">
        {
            "dateCreated": "2024-03-15T10:00:00Z",
            "datePublished": "2024-03-20T12:00:00Z",
            "author": {"name": "Jane Smith"},
            "contentLocation": {"name": "LAX Airport"}
        }
        </script>
        </html>
        """
        result = self.extractor.extract(html, {})
        assert result["photo_date"] == "2024-03-15"
        assert result["upload_date"] == "2024-03-20"
        assert result["photographer"] == "Jane Smith"
        assert result["location"] == "LAX Airport"

    def test_extract_safe_with_error(self) -> None:
        """Test extract_safe catches errors and returns empty dict."""
        # This should not raise an exception
        result, errors = self.extractor.extract_safe("<invalid", {})
        # Should return some result even for malformed HTML
        assert isinstance(result, dict)

    def test_validate_location_rejects_json(self) -> None:
        """Test that JSON fragments in location are rejected."""
        html = """
        <html>
        <h2><span>Photo Location</span></h2>
        <a href="/airport/TEST">{"contentUrl": "test"}</a>
        </html>
        """
        result = self.extractor.extract(html, {})
        # Location should be None because it contains JSON
        assert result["location"] is None


class TestAirportDataExtractor:
    """Tests for AirportDataExtractor."""

    def setup_method(self) -> None:
        """Set up test fixtures."""
        self.extractor = AirportDataExtractor()

    def test_version(self) -> None:
        """Test that version is set."""
        assert self.extractor.version == "1.1.0"

    def test_extract_empty_html(self) -> None:
        """Test extraction from empty HTML returns default fields."""
        result = self.extractor.extract("", {"registration": "N12345"})
        assert result["registration"] == "N12345"
        assert result["manufacturer"] is None

    def test_extract_from_table_rows(self) -> None:
        """Test extraction from table row format."""
        html = """
        <html>
        <table>
            <tr><td>Manufacturer</td><td>Boeing</td></tr>
            <tr><td>Model</td><td>737-800</td></tr>
            <tr><td>Year Built</td><td>2015</td></tr>
            <tr><td>Serial Number</td><td>12345</td></tr>
            <tr><td>Engines</td><td>2</td></tr>
            <tr><td>Seats</td><td>180</td></tr>
            <tr><td>Owner</td><td>United Airlines</td></tr>
            <tr><td>Status</td><td>Active</td></tr>
        </table>
        </html>
        """
        result = self.extractor.extract(html, {"registration": "N12345"})
        assert result["manufacturer"] == "Boeing"
        assert result["model"] == "737-800"
        assert result["year_built"] == 2015
        assert result["serial_number"] == "12345"
        assert result["engines"] == 2
        assert result["seats"] == 180
        assert result["owner"] == "United Airlines"
        assert result["status"] == "Active"

    def test_extract_source_url_construction(self) -> None:
        """Test that source_url is constructed from registration."""
        result = self.extractor.extract("<html></html>", {"registration": "N12345"})
        assert result["source_url"] == "https://www.airport-data.com/aircraft/N12345.html"

    def test_extract_not_found_page(self) -> None:
        """Test extraction from 'not found' page."""
        html = "<html>Aircraft not found</html>"
        result = self.extractor.extract(html, {"registration": "N99999"})
        # Should return minimal data
        assert result["registration"] == "N99999"
        assert result["manufacturer"] is None

    def test_extract_from_table_page(self) -> None:
        """Test extraction of aircraft list from manufacturer page."""
        html = """
        <html>
        <table>
            <tr>
                <td><a href="/aircraft/N12345.html">N12345</a></td>
                <td>2015 Boeing 737</td>
                <td>12345</td>
                <td>2</td>
                <td>180</td>
                <td>Chicago</td>
            </tr>
            <tr>
                <td><a href="/aircraft/N67890.html">N67890</a></td>
                <td>2018 Airbus A320</td>
                <td>67890</td>
                <td>2</td>
                <td>150</td>
                <td>New York</td>
            </tr>
        </table>
        </html>
        """
        result = self.extractor.extract_from_table_page(html, "Boeing")
        assert len(result) == 2
        assert result[0]["registration"] == "N12345"
        assert result[0]["year_built"] == 2015
        assert result[1]["registration"] == "N67890"
        assert result[1]["year_built"] == 2018

    def test_extract_mode_s_code(self) -> None:
        """Test extraction of Mode S transponder code."""
        html = """
        <html>
        <table>
            <tr><td>Mode S Code</td><td>A1B2C3</td></tr>
        </table>
        </html>
        """
        result = self.extractor.extract(html, {"registration": "N12345"})
        assert result["mode_s_code"] == "A1B2C3"

    def test_get_version_info(self) -> None:
        """Test get_version_info method."""
        info = self.extractor.get_version_info()
        assert info["extractor"] == "AirportDataExtractor"
        assert info["version"] == "1.1.0"
