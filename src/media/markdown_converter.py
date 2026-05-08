"""
Markdown to HTML converter for email notifications.
Converts Markdown text into email-friendly HTML.
"""

import re


def convert_markdown_table_to_html(markdown_text: str) -> str:
    """
    Convert markdown tables to HTML tables.

    Args:
        markdown_text: Text containing markdown tables

    Returns:
        Converted text with markdown tables replaced by HTML tables
    """
    lines = markdown_text.split("\n")
    result_lines = []
    table_lines = []
    in_table = False

    def process_table(table_lines: list) -> str:
        """Process collected table lines and generate an HTML table."""
        if len(table_lines) < 2:
            # Not a valid table, return the original lines
            return "\n".join(table_lines)

        html_parts = []
        html_parts.append(
            '<table style="border-collapse: collapse; width: 100%; margin: 15px 0; font-size: 0.95em;">'
        )

        header_processed = False
        separator_found = False

        for i, line in enumerate(table_lines):
            line = line.strip()
            if not line:
                continue

            # Check if this is a separator row (|---|---|)
            if re.match(r"^\|[\s\-:]+\|[\s\-:|]*$", line):
                separator_found = True
                continue

            # Parse cells
            cells = [cell.strip() for cell in line.split("|")]
            # Strip leading/trailing empty elements (from leading/trailing '|')
            if cells and cells[0] == "":
                cells = cells[1:]
            if cells and cells[-1] == "":
                cells = cells[:-1]

            if not cells:
                continue

            if not header_processed:
                # Treat the first row as the header
                html_parts.append("<thead>")
                html_parts.append("<tr>")
                for cell in cells:
                    html_parts.append(
                        f'<th style="border: 1px solid #ddd; padding: 10px 12px; background-color: #3498db; color: white; font-weight: bold; text-align: left;">{cell}</th>'
                    )
                html_parts.append("</tr>")
                html_parts.append("</thead>")
                html_parts.append("<tbody>")
                header_processed = True
            else:
                # Data row
                html_parts.append("<tr>")
                for cell in cells:
                    html_parts.append(
                        f'<td style="border: 1px solid #ddd; padding: 8px 12px; background-color: #f9f9f9;">{cell}</td>'
                    )
                html_parts.append("</tr>")

        if header_processed:
            html_parts.append("</tbody>")
        html_parts.append("</table>")

        return "\n".join(html_parts)

    for line in lines:
        stripped = line.strip()

        # Check if this is a table row (starts with | or is | with content on both sides)
        is_table_line = bool(re.match(r"^\|.*\|$", stripped)) or bool(
            re.match(r"^\|[\s\-:]+\|[\s\-:|]*$", stripped)
        )

        if is_table_line:
            if not in_table:
                in_table = True
            table_lines.append(stripped)
        else:
            if in_table:
                # End of table; process the collected rows
                result_lines.append(process_table(table_lines))
                table_lines = []
                in_table = False
            result_lines.append(line)

    # Handle a table at the end of the file
    if in_table and table_lines:
        result_lines.append(process_table(table_lines))

    return "\n".join(result_lines)


def convert_markdown_to_html(markdown_text: str) -> str:
    """
    Convert markdown text to HTML, optimized for the FlightAnalysisAgent report format.

    Args:
        markdown_text: Text in Markdown format

    Returns:
        Text in HTML format
    """
    if not markdown_text:
        return ""

    html = markdown_text

    # Convert tables first (before any other processing)
    html = convert_markdown_table_to_html(html)

    # Convert headings (# ## ### etc.)
    html = re.sub(
        r"^# (.*?)$",
        r'<h1 style="color: #2c3e50; margin-top: 20px; margin-bottom: 10px;">\1</h1>',
        html,
        flags=re.MULTILINE,
    )
    html = re.sub(
        r"^## (.*?)$",
        r'<h2 style="color: #34495e; margin-top: 15px; margin-bottom: 8px; font-size: 1.3em;">\1</h2>',
        html,
        flags=re.MULTILINE,
    )
    html = re.sub(
        r"^### (.*?)$",
        r'<h3 style="color: #7f8c8d; margin-top: 12px; margin-bottom: 6px; font-size: 1.1em;">\1</h3>',
        html,
        flags=re.MULTILINE,
    )
    html = re.sub(
        r"^#### (.*?)$",
        r'<h4 style="color: #95a5a6; margin-top: 10px; margin-bottom: 4px;">\1</h4>',
        html,
        flags=re.MULTILINE,
    )

    # Convert bold **text**
    html = re.sub(r"\*\*(.*?)\*\*", r'<strong style="color: #2c3e50;">\1</strong>', html)

    # Convert italic *text*
    html = re.sub(r"\*(.*?)\*", r"<em>\1</em>", html)

    # Convert inline code `code`
    html = re.sub(
        r"`(.*?)`",
        r'<code style="background-color: #f8f9fa; padding: 2px 4px; border-radius: 3px; font-family: monospace;">\1</code>',
        html,
    )

    # Convert unordered lists
    lines = html.split("\n")
    new_lines = []
    in_list = False

    for line in lines:
        stripped = line.strip()

        # Check if this is a list item (starts with - or *)
        if re.match(r"^[\-\*]\s+(.+)", stripped):
            if not in_list:
                new_lines.append('<ul style="margin: 10px 0; padding-left: 20px;">')
                in_list = True

            # Extract list item content
            list_content = re.sub(r"^[\-\*]\s+(.+)", r"\1", stripped)
            new_lines.append(f'<li style="margin: 4px 0; line-height: 1.4;">{list_content}</li>')
        else:
            if in_list:
                new_lines.append("</ul>")
                in_list = False

            # Handle regular paragraphs
            if stripped:
                # Check if it is already an HTML tag (skip already-converted tables, etc.)
                if stripped.startswith("<") and (
                    stripped.startswith("<table")
                    or stripped.startswith("</table")
                    or stripped.startswith("<thead")
                    or stripped.startswith("</thead")
                    or stripped.startswith("<tbody")
                    or stripped.startswith("</tbody")
                    or stripped.startswith("<tr")
                    or stripped.startswith("</tr")
                    or stripped.startswith("<th")
                    or stripped.startswith("<td")
                    or stripped.startswith("<h1")
                    or stripped.startswith("<h2")
                    or stripped.startswith("<h3")
                    or stripped.startswith("<h4")
                ):
                    # Already an HTML tag, keep as-is
                    new_lines.append(stripped)
                # Check for special formatting
                elif re.match(r"^[\=\-]{3,}$", stripped):
                    # Horizontal rule
                    new_lines.append(
                        '<hr style="border: none; border-top: 2px solid #ecf0f1; margin: 15px 0;">'
                    )
                elif "**" in stripped or "*" in stripped or "`" in stripped:
                    # Paragraph containing formatting
                    new_lines.append(f'<p style="margin: 8px 0; line-height: 1.5;">{stripped}</p>')
                else:
                    # Regular paragraph
                    new_lines.append(f'<p style="margin: 8px 0; line-height: 1.5;">{stripped}</p>')
            else:
                # Convert empty lines to paragraph spacing
                new_lines.append("<br>")

    # Close any unclosed list
    if in_list:
        new_lines.append("</ul>")

    html = "\n".join(new_lines)

    # Convert links [text](url)
    html = re.sub(
        r"\[([^\]]+)\]\(([^\)]+)\)",
        r'<a href="\2" style="color: #3498db; text-decoration: none;">\1</a>',
        html,
    )

    # Clean up extra newlines and whitespace
    html = re.sub(r"\n\s*\n", "\n", html)
    html = re.sub(r"<br>\s*<br>", "<br>", html)

    return html


def format_analysis_report_html(analysis_report: str, include_wrapper: bool = True) -> str:
    """
    Format an analysis report as styled HTML with a wrapper container.

    Args:
        analysis_report: Original analysis report (Markdown or HTML)
        include_wrapper: Whether to include the outer wrapper styling (default True)

    Returns:
        Styled HTML report
    """
    # Check if the content is already HTML
    if analysis_report.strip().startswith("<!DOCTYPE html>") or analysis_report.strip().startswith(
        "<html"
    ):
        # Content is already HTML, return as-is
        return analysis_report

    # Convert Markdown to HTML
    analysis_html = convert_markdown_to_html(analysis_report)

    if not include_wrapper:
        return analysis_html

    # Wrap in an email-style styled container
    formatted_html = f"""
    <div style="background: linear-gradient(135deg, #ffffff 0%, #f8f9fa 100%); border: 2px solid #007acc; border-radius: 12px; padding: 25px; margin-bottom: 25px; box-shadow: 0 4px 12px rgba(0,122,204,0.1);">
        <h2 style="color: #2c3e50; margin-top: 0; border-bottom: 2px solid #007acc; padding-bottom: 8px; font-size: 1.4em;">
            🔍 智能航空分析报告
        </h2>
        <div style="font-family: 'Segoe UI', Arial, sans-serif; color: #2c3e50; line-height: 1.6; margin-top: 15px;">
            {analysis_html}
        </div>
        <div style="margin-top: 20px; padding-top: 12px; border-top: 1px solid #dee2e6; font-size: 0.9em; color: #6c757d; text-align: center;">
            🤖 <strong>分析引擎:</strong> AWS Bedrock AI | 📊 <strong>数据源:</strong> 实时ADS-B + 历史轨迹分析
        </div>
    </div>
    """

    return formatted_html


def extract_summary_from_markdown(markdown_text: str, max_length: int = 500) -> str | None:
    """
    Extract a summary from Markdown text.

    Uses the first non-heading, non-empty paragraph as the summary.

    Args:
        markdown_text: Text in Markdown format
        max_length: Maximum summary length

    Returns:
        Summary text, or None if no summary can be extracted
    """
    if not markdown_text:
        return None

    lines = markdown_text.strip().split("\n")
    summary_parts = []

    for line in lines:
        stripped = line.strip()

        # Skip empty lines
        if not stripped:
            continue

        # Skip heading lines
        if stripped.startswith("#"):
            continue

        # Skip horizontal rules
        if re.match(r"^[\=\-]{3,}$", stripped):
            continue

        # Skip table rows
        if stripped.startswith("|") or re.match(r"^\|[\s\-:]+\|", stripped):
            continue

        # Strip list-item markers
        if re.match(r"^[\-\*]\s+", stripped):
            stripped = re.sub(r"^[\-\*]\s+", "", stripped)

        # Remove Markdown formatting
        clean_text = re.sub(r"\*\*(.*?)\*\*", r"\1", stripped)  # Remove bold
        clean_text = re.sub(r"\*(.*?)\*", r"\1", clean_text)  # Remove italic
        clean_text = re.sub(r"`(.*?)`", r"\1", clean_text)  # Remove code
        clean_text = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", clean_text)  # Remove links

        if clean_text:
            summary_parts.append(clean_text)

        # Stop once enough content has been collected
        current_length = sum(len(p) for p in summary_parts)
        if current_length >= max_length:
            break

    if not summary_parts:
        return None

    summary = " ".join(summary_parts)

    # Truncate to the maximum length
    if len(summary) > max_length:
        summary = summary[: max_length - 3] + "..."

    return summary


def create_error_analysis_html(error_message: str | None = None) -> str:
    """
    Create HTML for an error analysis report.

    Args:
        error_message: Optional error message

    Returns:
        HTML for the error report
    """
    error_detail = (
        error_message if error_message else "Flight analysis could not be performed at this time."
    )

    return f"""
    <h2>Flight Analysis Report</h2>
    <div style="border: 2px solid #ffa500; border-radius: 8px; padding: 15px; margin: 10px 0; background-color: #fff3cd;">
        <p style="color: #856404;"><strong>⚠️ Analysis Unavailable</strong></p>
        <p>{error_detail} Basic aircraft information is provided above.</p>
    </div>
    """
