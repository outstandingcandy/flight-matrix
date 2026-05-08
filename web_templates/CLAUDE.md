# web_templates/

Jinja2 HTML templates for the Flask web application.

## Templates

| File | Purpose |
|------|---------|
| `index.html` | Main SPA template with navigation and content containers |
| `airport.html` | Airport details page |
| `aircraft.html` | Aircraft details page |

## Template Structure

Templates use Bootstrap 5 for styling and Leaflet for maps.

```html
{% extends "base.html" %}
{% block content %}
<!-- Page content -->
{% endblock %}
```

## Static Assets

Static files (CSS, JS) are in `web_static/` and served via CloudFront CDN in production.

CDN URL pattern: `https://cdn.flightmatrix.app/static/`

## Development

Templates are rendered by Flask routes in `web_app.py`:
```python
@app.route("/")
def index():
    return render_template("index.html")
```
