/**
 * Airport Live Board Module
 * Provides airport-centric aircraft tracking functionality
 */

class AirportBoard {
    constructor() {
        this.map = null;
        this.markers = [];
        this.airportMarker = null;
        this.radiusCircle = null;
        this.selectedAirport = null;
        this.aircraftData = [];
        this.searchTimeout = null;
        this.refreshInterval = null;

        this.initializeMap();
        this.bindEvents();
    }

    initializeMap() {
        this.map = L.map('airportMap').setView([39.9, 116.4], 6);

        L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
            attribution: '&copy; OpenStreetMap contributors'
        }).addTo(this.map);
    }

    bindEvents() {
        // Airport search
        const searchInput = document.getElementById('airportSearch');
        searchInput.addEventListener('input', (e) => this.handleAirportSearch(e.target.value));
        searchInput.addEventListener('blur', () => {
            setTimeout(() => {
                document.getElementById('airportSearchResults').classList.add('d-none');
            }, 200);
        });

        // Radius slider
        const radiusSlider = document.getElementById('radiusKm');
        radiusSlider.addEventListener('input', (e) => {
            document.getElementById('radiusValue').textContent = e.target.value;
            this.updateRadiusCircle();
        });

        // Load nearby button
        document.getElementById('loadNearbyBtn').addEventListener('click', () => this.loadNearbyAircraft());

        // Clear selected airport
        document.getElementById('clearSelectedAirport').addEventListener('click', () => this.clearSelectedAirport());

        // Quick airport buttons
        document.querySelectorAll('.quick-airport').forEach(btn => {
            btn.addEventListener('click', (e) => this.selectAirportByCode(e.target.dataset.code));
        });

        // Refresh button
        document.getElementById('refreshMapBtn').addEventListener('click', () => this.loadNearbyAircraft());

        // Filters
        document.getElementById('filterWidebody').addEventListener('change', () => this.applyFilters());
        document.getElementById('filterCargo').addEventListener('change', () => this.applyFilters());
        document.getElementById('filterMilitary').addEventListener('change', () => this.applyFilters());
        document.getElementById('filterStatus').addEventListener('change', () => this.applyFilters());
    }

    async handleAirportSearch(query) {
        clearTimeout(this.searchTimeout);

        if (query.length < 2) {
            document.getElementById('airportSearchResults').classList.add('d-none');
            return;
        }

        this.searchTimeout = setTimeout(async () => {
            try {
                const response = await fetch(`/api/airports/search?q=${encodeURIComponent(query)}&limit=10`);
                const data = await response.json();

                if (data.success && data.airports.length > 0) {
                    this.displayAirportSearchResults(data.airports);
                } else {
                    document.getElementById('airportSearchResults').classList.add('d-none');
                }
            } catch (error) {
                console.error('Airport search error:', error);
            }
        }, 300);
    }

    displayAirportSearchResults(airports) {
        const resultsDiv = document.getElementById('airportSearchResults');
        resultsDiv.innerHTML = '';

        airports.forEach(airport => {
            const div = document.createElement('div');
            div.className = 'airport-search-result';
            div.innerHTML = `
                <strong>${airport.iata_code || airport.icao_code}</strong> - ${airport.name}
                <br><small class="text-muted">${airport.city || ''}, ${airport.country || ''}</small>
            `;
            div.addEventListener('click', () => this.selectAirport(airport));
            resultsDiv.appendChild(div);
        });

        resultsDiv.classList.remove('d-none');
    }

    async selectAirportByCode(code) {
        try {
            const response = await fetch(`/api/airports/${code}`);
            const data = await response.json();

            if (data.success && data.airport) {
                this.selectAirport(data.airport);
            } else {
                this.showMessage('Airport not found', 'warning');
            }
        } catch (error) {
            console.error('Error selecting airport:', error);
            this.showMessage('Failed to load airport', 'danger');
        }
    }

    selectAirport(airport) {
        this.selectedAirport = airport;

        // Update UI
        document.getElementById('selectedAirportName').textContent = airport.name;
        document.getElementById('selectedAirportCode').textContent =
            `${airport.iata_code || '-'} / ${airport.icao_code}`;
        document.getElementById('selectedAirport').classList.remove('d-none');
        document.getElementById('loadNearbyBtn').disabled = false;
        document.getElementById('airportSearch').value = '';
        document.getElementById('airportSearchResults').classList.add('d-none');

        // Update map
        const lat = airport.latitude;
        const lon = airport.longitude;

        if (this.airportMarker) {
            this.map.removeLayer(this.airportMarker);
        }

        // Airport marker
        const airportIcon = L.divIcon({
            className: 'airport-marker',
            html: '<i class="fas fa-building" style="color: #dc3545; font-size: 24px;"></i>',
            iconSize: [24, 24],
            iconAnchor: [12, 12]
        });

        this.airportMarker = L.marker([lat, lon], { icon: airportIcon })
            .bindPopup(`<strong>${airport.name}</strong><br>${airport.icao_code}`)
            .addTo(this.map);

        this.updateRadiusCircle();
        this.map.setView([lat, lon], 9);

        // Auto-load nearby aircraft
        this.loadNearbyAircraft();
    }

    clearSelectedAirport() {
        this.selectedAirport = null;
        document.getElementById('selectedAirport').classList.add('d-none');
        document.getElementById('loadNearbyBtn').disabled = true;

        if (this.airportMarker) {
            this.map.removeLayer(this.airportMarker);
            this.airportMarker = null;
        }

        if (this.radiusCircle) {
            this.map.removeLayer(this.radiusCircle);
            this.radiusCircle = null;
        }

        this.clearMarkers();
        this.clearLists();
        this.updateStats(0, 0, 0, 0);
    }

    updateRadiusCircle() {
        if (!this.selectedAirport) return;

        const radius = parseInt(document.getElementById('radiusKm').value) * 1000; // Convert to meters

        if (this.radiusCircle) {
            this.map.removeLayer(this.radiusCircle);
        }

        this.radiusCircle = L.circle(
            [this.selectedAirport.latitude, this.selectedAirport.longitude],
            {
                radius: radius,
                color: '#3388ff',
                fillColor: '#3388ff',
                fillOpacity: 0.1,
                weight: 2
            }
        ).addTo(this.map);
    }

    async loadNearbyAircraft() {
        if (!this.selectedAirport) return;

        const radius = document.getElementById('radiusKm').value;

        try {
            this.showLoading(true);

            const response = await fetch(
                `/api/airports/${this.selectedAirport.icao_code}/realtime-aircraft?radius_km=${radius}`
            );
            const data = await response.json();

            if (data.success) {
                this.aircraftData = data.aircraft || [];
                this.updateStats(
                    data.approaching_count || 0,
                    data.departing_count || 0,
                    data.cruising_count || 0,
                    data.total_count || 0
                );

                this.displayAircraftOnMap(data.aircraft || []);

                // Filter aircraft by flight_status for lists
                const approaching = this.aircraftData.filter(ac => ac.flight_status === 'approaching');
                const departing = this.aircraftData.filter(ac => ac.flight_status === 'departing');
                const cruising = this.aircraftData.filter(ac => ac.flight_status === 'cruising');
                this.updateLists({ approaching, departing, cruising });
            } else {
                this.showMessage(data.error || 'Failed to load aircraft', 'danger');
            }
        } catch (error) {
            console.error('Error loading nearby aircraft:', error);
            this.showMessage('Failed to load nearby aircraft', 'danger');
        } finally {
            this.showLoading(false);
        }
    }

    displayAircraftOnMap(aircraft) {
        this.clearMarkers();

        aircraft.forEach(ac => {
            if (!ac.latitude || !ac.longitude) return;

            const color = this.getStatusColor(ac.flight_status);
            const iconHtml = `<i class="fas fa-plane" style="color: ${color}; font-size: 14px; transform: rotate(${ac.track || 0}deg);"></i>`;

            const icon = L.divIcon({
                className: 'aircraft-marker',
                html: iconHtml,
                iconSize: [14, 14],
                iconAnchor: [7, 7]
            });

            const marker = L.marker([ac.latitude, ac.longitude], { icon: icon })
                .bindPopup(this.createAircraftPopup(ac))
                .addTo(this.map);

            this.markers.push(marker);
        });
    }

    createAircraftPopup(aircraft) {
        return `
            <div style="min-width: 200px;">
                <strong>${aircraft.flight_number || aircraft.registration || 'Unknown'}</strong>
                ${aircraft.is_military ? '<span class="badge bg-danger ms-1">Military</span>' : ''}
                <hr class="my-1">
                <table class="table table-sm mb-0">
                    <tr><td>Registration</td><td>${aircraft.registration || '-'}</td></tr>
                    <tr><td>Type</td><td>${aircraft.aircraft_type || '-'}</td></tr>
                    <tr><td>Altitude</td><td>${aircraft.altitude_baro ? aircraft.altitude_baro.toLocaleString() + ' ft' : '-'}</td></tr>
                    <tr><td>Speed</td><td>${aircraft.ground_speed ? Math.round(aircraft.ground_speed) + ' kts' : '-'}</td></tr>
                    <tr><td>Distance</td><td>${aircraft.distance_km ? aircraft.distance_km + ' km' : '-'}</td></tr>
                    <tr><td>Status</td><td><span class="badge status-badge-${aircraft.flight_status}">${this.getStatusText(aircraft.flight_status)}</span></td></tr>
                </table>
                <div class="mt-2">
                    <a href="/search-track?registration=${aircraft.registration}" class="btn btn-sm btn-primary">
                        <i class="fas fa-route"></i> Track
                    </a>
                </div>
            </div>
        `;
    }

    getStatusColor(status) {
        const colors = {
            'approaching': '#28a745',
            'departing': '#ffc107',
            'cruising': '#17a2b8',
            'ground': '#6c757d',
            'unknown': '#adb5bd'
        };
        return colors[status] || colors['unknown'];
    }

    getStatusText(status) {
        const texts = {
            'approaching': 'Approaching',
            'departing': 'Departing',
            'cruising': 'Cruising',
            'ground': 'Ground',
            'unknown': 'Unknown'
        };
        return texts[status] || status;
    }

    updateStats(approaching, departing, cruising, total) {
        document.getElementById('statApproaching').textContent = approaching;
        document.getElementById('statDeparting').textContent = departing;
        document.getElementById('statCruising').textContent = cruising;
        document.getElementById('statTotal').textContent = total;
    }

    updateLists(data) {
        // Update approaching list
        this.updateList('approachingList', 'approachingCount', data.approaching || []);

        // Update departing list
        this.updateList('departingList', 'departingCount', data.departing || []);

        // Update cruising list
        this.updateCruisingList(data.cruising || []);
    }

    updateList(listId, countId, aircraft) {
        const tbody = document.getElementById(listId);
        const countBadge = document.getElementById(countId);

        countBadge.textContent = aircraft.length;

        if (aircraft.length === 0) {
            tbody.innerHTML = '<tr><td colspan="5" class="text-center text-muted">No data</td></tr>';
            return;
        }

        tbody.innerHTML = aircraft.map(ac => `
            <tr class="${ac.is_military ? 'aircraft-military' : ''} ${ac.is_widebody ? 'aircraft-type-widebody' : ''}">
                <td>${ac.flight_number || '-'}</td>
                <td>${ac.aircraft_type || '-'}</td>
                <td><a href="/search-track?registration=${ac.registration}">${ac.registration || '-'}</a></td>
                <td>${ac.altitude_baro ? (ac.altitude_baro / 1000).toFixed(1) + 'k' : '-'}</td>
                <td>${ac.distance_km ? ac.distance_km + 'km' : '-'}</td>
            </tr>
        `).join('');
    }

    updateCruisingList(aircraft) {
        const tbody = document.getElementById('cruisingList');
        const countBadge = document.getElementById('cruisingCount');

        countBadge.textContent = aircraft.length;

        if (aircraft.length === 0) {
            tbody.innerHTML = '<tr><td colspan="7" class="text-center text-muted">No data</td></tr>';
            return;
        }

        tbody.innerHTML = aircraft.map(ac => `
            <tr class="${ac.is_military ? 'aircraft-military' : ''} ${ac.is_widebody ? 'aircraft-type-widebody' : ''}">
                <td>${ac.flight_number || '-'}</td>
                <td>${ac.aircraft_type || '-'}</td>
                <td>${ac.registration || '-'}</td>
                <td>${ac.altitude_baro ? (ac.altitude_baro / 1000).toFixed(1) + 'k' : '-'}</td>
                <td>${ac.ground_speed ? Math.round(ac.ground_speed) + 'kts' : '-'}</td>
                <td>${ac.distance_km ? ac.distance_km + 'km' : '-'}</td>
                <td>
                    <a href="/search-track?registration=${ac.registration}" class="btn btn-sm btn-outline-primary">
                        <i class="fas fa-route"></i>
                    </a>
                </td>
            </tr>
        `).join('');
    }

    applyFilters() {
        const filterWidebody = document.getElementById('filterWidebody').checked;
        const filterCargo = document.getElementById('filterCargo').checked;
        const filterMilitary = document.getElementById('filterMilitary').checked;
        const filterStatus = document.getElementById('filterStatus').value;

        let filtered = [...this.aircraftData];

        if (filterWidebody) {
            filtered = filtered.filter(ac => ac.is_widebody);
        }
        if (filterCargo) {
            filtered = filtered.filter(ac => ac.is_cargo);
        }
        if (filterMilitary) {
            filtered = filtered.filter(ac => ac.is_military);
        }
        if (filterStatus) {
            filtered = filtered.filter(ac => ac.flight_status === filterStatus);
        }

        // Rebuild display with filtered data
        const approaching = filtered.filter(ac => ac.flight_status === 'approaching');
        const departing = filtered.filter(ac => ac.flight_status === 'departing');
        const cruising = filtered.filter(ac => ac.flight_status === 'cruising');

        this.updateStats(approaching.length, departing.length, cruising.length, filtered.length);
        this.displayAircraftOnMap(filtered);
        this.updateLists({
            approaching: approaching,
            departing: departing,
            cruising: cruising
        });
    }

    clearMarkers() {
        this.markers.forEach(marker => this.map.removeLayer(marker));
        this.markers = [];
    }

    clearLists() {
        document.getElementById('approachingList').innerHTML = '<tr><td colspan="5" class="text-center text-muted">No data</td></tr>';
        document.getElementById('departingList').innerHTML = '<tr><td colspan="5" class="text-center text-muted">No data</td></tr>';
        document.getElementById('cruisingList').innerHTML = '<tr><td colspan="7" class="text-center text-muted">No data</td></tr>';
        document.getElementById('approachingCount').textContent = '0';
        document.getElementById('departingCount').textContent = '0';
        document.getElementById('cruisingCount').textContent = '0';
    }

    showLoading(show) {
        const btn = document.getElementById('loadNearbyBtn');
        if (show) {
            btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> Loading...';
            btn.disabled = true;
        } else {
            btn.innerHTML = '<i class="fas fa-search"></i> Load Nearby';
            btn.disabled = !this.selectedAirport;
        }
    }

    showMessage(message, type = 'info') {
        // Simple alert for now - could be replaced with toast
        alert(message);
    }
}

// Initialize when DOM is ready
document.addEventListener('DOMContentLoaded', () => {
    window.airportBoard = new AirportBoard();
});
