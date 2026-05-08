# web_static/

Frontend static assets (CSS, JavaScript).

## Structure

```
web_static/
├── css/
│   └── style.css    # Custom styles (Bootstrap 5 base)
└── js/
    └── app.js       # Main frontend application
```

## JavaScript (app.js)

Single-page application features:
- Aircraft search and filtering
- Interactive Leaflet maps with flight tracks
- Real-time position updates
- Flight schedule display
- Airport information pages

## CSS (style.css)

Custom styles extending Bootstrap 5:
- Map container styling
- Aircraft card layouts
- Flight track visualization
- Responsive breakpoints

## CDN Deployment

Static files are deployed to S3 and served via CloudFront:
```bash
aws s3 sync web_static/ s3://flight-matrix-static/static/ --delete
```

Production URL: `https://cdn.flightmatrix.app/static/`

## Local Development

Flask serves static files directly in development:
```python
app = Flask(__name__, static_folder='web_static', static_url_path='/static')
```

Sync to CDN before testing frontend changes locally.
