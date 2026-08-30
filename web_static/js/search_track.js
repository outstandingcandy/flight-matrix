/**
 * Flight Track Module
 * Simplified aircraft track search and visualization
 */

class FlightTracker {
    constructor() {
        this.map = null;
        this.trackLine = null;
        this.startMarker = null;
        this.endMarker = null;
        this.trackData = [];
        this.currentRegistration = null;
        this.currentAircraftInfo = null;

        this.initializeMap();
        this.bindEvents();
        this.setDefaultTimeRange();
        this.checkURLParams();
    }

    initializeMap() {
        this.map = L.map('trackMap').setView([35, 105], 4);

        L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
            attribution: '&copy; OpenStreetMap contributors'
        }).addTo(this.map);
    }

    bindEvents() {
        // Search form
        document.getElementById('searchForm').addEventListener('submit', (e) => {
            e.preventDefault();
            this.searchTrack();
        });

        // Track controls
        document.getElementById('clearTrackBtn').addEventListener('click', () => this.clearTrack());
        document.getElementById('fitTrackBtn').addEventListener('click', () => this.fitTrackBounds());
    }

    setDefaultTimeRange() {
        // Default: last 24 hours
        const now = new Date();
        const yesterday = new Date(now.getTime() - 24 * 60 * 60 * 1000);

        const formatDateTime = (date) => {
            const pad = (n) => n.toString().padStart(2, '0');
            return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
        };

        document.getElementById('startTime').value = formatDateTime(yesterday);
        document.getElementById('endTime').value = formatDateTime(now);
    }

    checkURLParams() {
        const params = new URLSearchParams(window.location.search);
        const registration = params.get('registration') || params.get('reg');

        if (registration) {
            document.getElementById('searchRegistration').value = registration;
            this.searchTrack();
        }
    }

    async searchTrack() {
        const registration = document.getElementById('searchRegistration').value.trim();

        if (!registration) {
            this.showAlert('Please enter a registration number');
            return;
        }

        this.currentRegistration = registration;
        this.setLoading(true);

        try {
            // Fetch track data and aircraft details in parallel
            const startTime = document.getElementById('startTime').value;
            const endTime = document.getElementById('endTime').value;

            let trackUrl = `/api/v1/aircraft/tracks/${encodeURIComponent(registration)}?limit=2000`;
            if (startTime) {
                trackUrl += `&start_time=${encodeURIComponent(startTime)}`;
            }

            const [trackResponse, detailsResponse] = await Promise.all([
                fetch(trackUrl),
                fetch(`/api/v1/aircraft/${encodeURIComponent(registration)}/details`)
            ]);

            const trackData = await trackResponse.json();
            const detailsData = await detailsResponse.json();

            // Handle aircraft details
            if (detailsData.success && detailsData.aircraft) {
                this.currentAircraftInfo = detailsData.aircraft;
                this.displayAircraftInfo(detailsData.aircraft);
            } else {
                // Try to get basic info from track data
                this.displayAircraftInfo(null);
            }

            // Handle track data
            if (trackData.success && trackData.tracks && trackData.tracks.length > 0) {
                // Filter by end time if specified
                let tracks = trackData.tracks;
                if (endTime) {
                    const endTimestamp = new Date(endTime).getTime();
                    tracks = tracks.filter(t => {
                        const trackTime = new Date(t.timestamp_beijing || t.datetime).getTime();
                        return trackTime <= endTimestamp;
                    });
                }

                if (tracks.length > 0) {
                    this.trackData = tracks;
                    this.displayTrack(tracks);
                    this.displayTrackTable(tracks);
                    this.updateTelemetry(tracks[tracks.length - 1]);
                } else {
                    this.showAlert('No track data found for the specified time range');
                    this.clearTrack();
                }
            } else {
                this.showAlert('No track data found for this aircraft');
                this.clearTrack();
            }

        } catch (error) {
            console.error('Search error:', error);
            this.showAlert('Search failed: ' + error.message);
        } finally {
            this.setLoading(false);
        }
    }

    displayAircraftInfo(aircraft) {
        const infoCard = document.getElementById('aircraftInfoCard');
        const telemetryPanel = document.getElementById('telemetryPanel');

        infoCard.classList.add('show');
        telemetryPanel.classList.add('show');

        if (!aircraft) {
            document.getElementById('infoRegistration').textContent = this.currentRegistration || '-';
            document.getElementById('infoType').textContent = '-';
            document.getElementById('infoOperator').textContent = '-';
            document.getElementById('infoCountry').textContent = '-';
            document.getElementById('infoTags').innerHTML = '';
            document.getElementById('viewDetailLink').style.display = 'none';

            // Hide photo
            document.getElementById('aircraftPhoto').style.display = 'none';
            document.getElementById('aircraftPhotoPlaceholder').style.display = 'flex';
            return;
        }

        // Basic info
        document.getElementById('infoRegistration').textContent = aircraft.registration || '-';
        document.getElementById('infoType').textContent = aircraft.aircraft_type_code || aircraft.aircraft_type || aircraft.type_series || '-';
        document.getElementById('infoOperator').textContent = aircraft.operator || aircraft.owner || '-';
        document.getElementById('infoCountry').textContent = aircraft.country_of_registration || '-';

        // Tags
        const tags = [];
        if (aircraft.is_military) tags.push('<span class="badge bg-danger tag-badge">Military</span>');
        if (aircraft.is_widebody) tags.push('<span class="badge bg-success tag-badge">Widebody</span>');
        if (aircraft.is_cargo) tags.push('<span class="badge bg-warning tag-badge">Cargo</span>');
        if (aircraft.is_government) tags.push('<span class="badge bg-info tag-badge">Government</span>');
        document.getElementById('infoTags').innerHTML = tags.join('');

        // Photo - API returns image_path_1, image_path_2, image_path_3
        const photoEl = document.getElementById('aircraftPhoto');
        const placeholderEl = document.getElementById('aircraftPhotoPlaceholder');

        const firstImage = aircraft.image_path_1 || aircraft.photo_url ||
            (aircraft.images && aircraft.images.length > 0 ? aircraft.images[0] : null);
        if (firstImage && typeof firstImage === 'string') {
            const imgPath = firstImage.startsWith('data/') ? '/' + firstImage : firstImage;
            photoEl.src = imgPath;
            photoEl.style.display = 'block';
            placeholderEl.style.display = 'none';

            photoEl.onerror = () => {
                photoEl.style.display = 'none';
                placeholderEl.style.display = 'flex';
            };
        } else {
            photoEl.style.display = 'none';
            placeholderEl.style.display = 'flex';
        }

        // Detail link
        const detailLink = document.getElementById('viewDetailLink');
        if (aircraft.registration) {
            detailLink.href = `/aircraft/${aircraft.registration}`;
            detailLink.style.display = 'block';
        } else {
            detailLink.style.display = 'none';
        }
    }

    updateTelemetry(point) {
        if (!point) return;

        document.getElementById('telAltitude').textContent =
            point.alt_baro ? point.alt_baro.toLocaleString() : '-';
        document.getElementById('telSpeed').textContent =
            point.ground_speed ? Math.round(point.ground_speed) : '-';

        const verticalRate = point.vertical_rate;
        const verticalEl = document.getElementById('telVertical');
        const verticalContainer = document.getElementById('telVerticalContainer');

        verticalEl.textContent = verticalRate ? (verticalRate > 0 ? '+' : '') + verticalRate : '-';
        verticalContainer.classList.remove('positive', 'negative');
        if (verticalRate > 100) verticalContainer.classList.add('positive');
        else if (verticalRate < -100) verticalContainer.classList.add('negative');

        document.getElementById('telHeading').textContent =
            point.track ? Math.round(point.track) : '-';

        document.getElementById('telLastUpdate').textContent =
            point.timestamp_beijing || point.datetime || '-';
    }

    displayTrack(tracks) {
        this.clearTrack();

        if (!tracks || tracks.length === 0) return;

        // Create track line (blue)
        const coords = tracks.map(t => [t.lat, t.lon]);

        this.trackLine = L.polyline(coords, {
            color: '#0d6efd',
            weight: 3,
            opacity: 0.8
        }).addTo(this.map);

        // Start marker (green circle)
        const startPoint = tracks[0];
        this.startMarker = L.circleMarker([startPoint.lat, startPoint.lon], {
            radius: 8,
            color: '#28a745',
            fillColor: '#28a745',
            fillOpacity: 0.8
        }).bindPopup(this.createPointPopup(startPoint, 'Start')).addTo(this.map);

        // End marker (red plane icon with rotation)
        const endPoint = tracks[tracks.length - 1];
        const rotation = endPoint.track || 0;

        const planeIcon = L.divIcon({
            className: 'aircraft-marker-end',
            html: `<i class="fas fa-plane" style="color: #dc3545; font-size: 20px; transform: rotate(${rotation}deg);"></i>`,
            iconSize: [20, 20],
            iconAnchor: [10, 10]
        });

        this.endMarker = L.marker([endPoint.lat, endPoint.lon], { icon: planeIcon })
            .bindPopup(this.createPointPopup(endPoint, 'Latest'))
            .addTo(this.map);

        // Add click handler for track line points
        this.trackLine.on('click', (e) => {
            const nearestPoint = this.findNearestPoint(e.latlng, tracks);
            if (nearestPoint) {
                L.popup()
                    .setLatLng([nearestPoint.lat, nearestPoint.lon])
                    .setContent(this.createPointPopup(nearestPoint))
                    .openOn(this.map);
            }
        });

        // Fit map to track bounds
        this.fitTrackBounds();
    }

    createPointPopup(point, label = null) {
        const time = point.timestamp_beijing || point.datetime || '-';
        const alt = point.alt_baro ? point.alt_baro.toLocaleString() + ' ft' : '-';
        const speed = point.ground_speed ? Math.round(point.ground_speed) + ' kts' : '-';
        const heading = point.track ? Math.round(point.track) + '°' : '-';

        let html = '';
        if (label) {
            html += `<strong>${label}</strong><hr class="my-1">`;
        }
        html += `
            <small>
                <b>Time:</b> ${time}<br>
                <b>Alt:</b> ${alt}<br>
                <b>Speed:</b> ${speed}<br>
                <b>Heading:</b> ${heading}
            </small>
        `;
        return html;
    }

    findNearestPoint(latlng, tracks) {
        let nearest = null;
        let minDist = Infinity;

        for (const point of tracks) {
            const dist = Math.pow(latlng.lat - point.lat, 2) + Math.pow(latlng.lng - point.lon, 2);
            if (dist < minDist) {
                minDist = dist;
                nearest = point;
            }
        }

        return nearest;
    }

    displayTrackTable(tracks) {
        const tableCard = document.getElementById('trackTableCard');
        const tableBody = document.getElementById('trackTableBody');
        const countBadge = document.getElementById('trackPointCount');

        if (!tracks || tracks.length === 0) {
            tableCard.style.display = 'none';
            return;
        }

        tableCard.style.display = 'block';
        countBadge.textContent = `${tracks.length} points`;

        // Show last 100 points in reverse order (newest first)
        const displayTracks = tracks.slice(-100).reverse();

        tableBody.innerHTML = displayTracks.map(t => `
            <tr data-lat="${t.lat}" data-lng="${t.lon}">
                <td>${t.timestamp_beijing || t.datetime || '-'}</td>
                <td>${t.lat.toFixed(4)}, ${t.lon.toFixed(4)}</td>
                <td>${t.alt_baro ? t.alt_baro.toLocaleString() : '-'}</td>
                <td>${t.ground_speed ? Math.round(t.ground_speed) : '-'}</td>
                <td>${t.track ? Math.round(t.track) + '°' : '-'}</td>
            </tr>
        `).join('');

        // Add click handler for table rows
        tableBody.querySelectorAll('tr').forEach(row => {
            row.style.cursor = 'pointer';
            row.addEventListener('click', () => {
                const lat = parseFloat(row.dataset.lat);
                const lng = parseFloat(row.dataset.lng);
                this.map.setView([lat, lng], 10);

                // Find the point and show popup
                const point = tracks.find(t =>
                    Math.abs(t.lat - lat) < 0.0001 && Math.abs(t.lon - lng) < 0.0001
                );
                if (point) {
                    L.popup()
                        .setLatLng([lat, lng])
                        .setContent(this.createPointPopup(point))
                        .openOn(this.map);
                }
            });
        });
    }

    clearTrack() {
        if (this.trackLine) {
            this.map.removeLayer(this.trackLine);
            this.trackLine = null;
        }
        if (this.startMarker) {
            this.map.removeLayer(this.startMarker);
            this.startMarker = null;
        }
        if (this.endMarker) {
            this.map.removeLayer(this.endMarker);
            this.endMarker = null;
        }

        // Clear table
        document.getElementById('trackTableCard').style.display = 'none';
        document.getElementById('trackTableBody').innerHTML = '';
    }

    fitTrackBounds() {
        if (this.trackLine) {
            this.map.fitBounds(this.trackLine.getBounds(), { padding: [30, 30] });
        }
    }

    setLoading(loading) {
        const btn = document.getElementById('searchBtn');
        if (loading) {
            btn.classList.add('btn-loading');
            btn.disabled = true;
        } else {
            btn.classList.remove('btn-loading');
            btn.disabled = false;
        }
    }

    showAlert(message) {
        // Simple alert - could be replaced with a toast notification
        alert(message);
    }
}

// Initialize when DOM is ready
document.addEventListener('DOMContentLoaded', () => {
    window.flightTracker = new FlightTracker();
});
