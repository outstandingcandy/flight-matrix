/**
 * FlightMatrix Home Page - Search Logic
 */

class HomeSearch {
    constructor() {
        // DOM elements
        this.searchInput = document.getElementById('unifiedSearch');
        this.clearBtn = document.getElementById('clearSearchBtn');
        this.dropdown = document.getElementById('searchDropdown');
        this.dropdownLoading = document.getElementById('dropdownLoading');
        this.dropdownResults = document.getElementById('dropdownResults');
        this.noResults = document.getElementById('noResults');

        // Result lists
        this.airportsResultList = document.getElementById('airportsResultList');
        this.aircraftResultList = document.getElementById('aircraftResultList');
        this.aircraftTypesResultList = document.getElementById('aircraftTypesResultList');

        // Sections
        this.airportsResultSection = document.getElementById('airportsResultSection');
        this.aircraftResultSection = document.getElementById('aircraftResultSection');
        this.aircraftTypesResultSection = document.getElementById('aircraftTypesResultSection');

        // State
        this.debounceTimer = null;
        this.debounceDelay = 300;
        this.activeIndex = -1;
        this.resultItems = [];

        this.init();
    }

    init() {
        this.bindEvents();
    }

    bindEvents() {
        // Focus event - only show dropdown if there's query
        this.searchInput.addEventListener('focus', () => this.onFocus());

        // Input event - debounced search
        this.searchInput.addEventListener('input', (e) => this.onInput(e));

        // Blur event - hide dropdown after delay
        this.searchInput.addEventListener('blur', () => this.onBlur());

        // Keyboard navigation
        this.searchInput.addEventListener('keydown', (e) => this.onKeydown(e));

        // Clear button
        this.clearBtn.addEventListener('click', () => this.clearSearch());

        // Click outside to close
        document.addEventListener('click', (e) => {
            if (!e.target.closest('.search-wrapper')) {
                this.hideDropdown();
            }
        });
    }

    onFocus() {
        const query = this.searchInput.value.trim();
        if (query.length >= 2) {
            this.showDropdown();
            this.performSearch(query);
        }
    }

    onInput(e) {
        const query = e.target.value.trim();

        // Show/hide clear button
        if (query.length > 0) {
            this.clearBtn.classList.remove('d-none');
        } else {
            this.clearBtn.classList.add('d-none');
        }

        // Debounced search
        clearTimeout(this.debounceTimer);

        if (query.length < 2) {
            this.hideDropdown();
            return;
        }

        this.showDropdown();
        this.debounceTimer = setTimeout(() => {
            this.performSearch(query);
        }, this.debounceDelay);
    }

    onBlur() {
        // Delay hiding to allow click events on dropdown items
        setTimeout(() => {
            if (!document.activeElement.closest('.search-wrapper')) {
                this.hideDropdown();
            }
        }, 200);
    }

    onKeydown(e) {
        if (!this.dropdown.classList.contains('d-none')) {
            switch (e.key) {
                case 'ArrowDown':
                    e.preventDefault();
                    this.navigateResults(1);
                    break;
                case 'ArrowUp':
                    e.preventDefault();
                    this.navigateResults(-1);
                    break;
                case 'Enter':
                    e.preventDefault();
                    this.selectActiveResult();
                    break;
                case 'Escape':
                    e.preventDefault();
                    this.hideDropdown();
                    this.searchInput.blur();
                    break;
            }
        }
    }

    showDropdown() {
        this.dropdown.classList.remove('d-none');
    }

    hideDropdown() {
        this.dropdown.classList.add('d-none');
        this.activeIndex = -1;
        this.updateActiveState();
    }

    async performSearch(query) {
        this.dropdownResults.classList.remove('d-none');
        this.dropdownLoading.classList.remove('d-none');
        this.noResults.classList.add('d-none');
        this.airportsResultSection.classList.add('d-none');
        this.aircraftResultSection.classList.add('d-none');
        this.aircraftTypesResultSection.classList.add('d-none');

        try {
            const response = await fetch(`/api/v1/search/unified?q=${encodeURIComponent(query)}&limit=10`);
            const data = await response.json();

            this.dropdownLoading.classList.add('d-none');

            if (data.success) {
                const airports = data.results?.airports || [];
                const aircraft = data.results?.aircraft || [];
                const aircraftTypes = data.results?.aircraft_types || [];

                if (airports.length === 0 && aircraft.length === 0 && aircraftTypes.length === 0) {
                    this.noResults.classList.remove('d-none');
                } else {
                    this.renderAirportResults(airports);
                    this.renderAircraftResults(aircraft);
                    this.renderAircraftTypeResults(aircraftTypes);
                }
            } else {
                this.noResults.classList.remove('d-none');
            }
        } catch (error) {
            console.error('Search error:', error);
            this.dropdownLoading.classList.add('d-none');
            this.noResults.classList.remove('d-none');
        }

        this.updateResultItems();
    }

    renderAirportResults(airports) {
        if (!airports || airports.length === 0) {
            this.airportsResultSection.classList.add('d-none');
            return;
        }

        this.airportsResultSection.classList.remove('d-none');
        this.airportsResultList.innerHTML = airports.map(airport => `
            <div class="result-item" data-type="airport" data-code="${airport.iata_code || airport.icao_code}">
                <div class="result-item-icon airport">
                    <i class="fas fa-building"></i>
                </div>
                <div class="result-item-content">
                    <div class="result-item-title">
                        ${airport.iata_code ? `<span class="code-badge iata">${airport.iata_code}</span>` : ''}
                        ${airport.icao_code ? `<span class="code-badge icao">${airport.icao_code}</span>` : ''}
                        <span>${this.escapeHtml(airport.name || '')}</span>
                    </div>
                    <div class="result-item-subtitle">
                        ${this.escapeHtml(airport.city || '')}${airport.country_code ? `, ${airport.country_code}` : ''}
                    </div>
                </div>
            </div>
        `).join('');

        // Bind click events
        this.airportsResultList.querySelectorAll('.result-item').forEach(item => {
            item.addEventListener('click', () => this.onResultClick(item));
            item.addEventListener('mouseenter', () => this.onResultHover(item));
        });
    }

    renderAircraftResults(aircraft) {
        if (!aircraft || aircraft.length === 0) {
            this.aircraftResultSection.classList.add('d-none');
            return;
        }

        this.aircraftResultSection.classList.remove('d-none');
        this.aircraftResultList.innerHTML = aircraft.map(ac => `
            <div class="result-item" data-type="aircraft" data-registration="${ac.registration}">
                <div class="result-item-icon aircraft">
                    <i class="fas fa-plane"></i>
                </div>
                <div class="result-item-content">
                    <div class="result-item-title">
                        <span class="code-badge registration">${this.escapeHtml(ac.registration)}</span>
                        <span>${this.escapeHtml(ac.aircraft_type || '')}</span>
                    </div>
                    <div class="result-item-subtitle">
                        ${this.escapeHtml(ac.owner || ac.operator || '')}
                    </div>
                </div>
            </div>
        `).join('');

        // Bind click events
        this.aircraftResultList.querySelectorAll('.result-item').forEach(item => {
            item.addEventListener('click', () => this.onResultClick(item));
            item.addEventListener('mouseenter', () => this.onResultHover(item));
        });
    }

    renderAircraftTypeResults(aircraftTypes) {
        if (!aircraftTypes || aircraftTypes.length === 0) {
            this.aircraftTypesResultSection.classList.add('d-none');
            return;
        }

        this.aircraftTypesResultSection.classList.remove('d-none');
        this.aircraftTypesResultList.innerHTML = aircraftTypes.map(type => `
            <div class="result-item" data-type="aircraft-type" data-type-code="${type.type_code}">
                <div class="result-item-icon aircraft-type">
                    <i class="fas fa-layer-group"></i>
                </div>
                <div class="result-item-content">
                    <div class="result-item-title">
                        <span class="code-badge type-code">${this.escapeHtml(type.type_code)}</span>
                        <span>${this.escapeHtml(type.name || type.type_code)}</span>
                    </div>
                    <div class="result-item-subtitle">
                        ${type.aircraft_count.toLocaleString()} aircraft
                    </div>
                </div>
            </div>
        `).join('');

        // Bind click events
        this.aircraftTypesResultList.querySelectorAll('.result-item').forEach(item => {
            item.addEventListener('click', () => this.onResultClick(item));
            item.addEventListener('mouseenter', () => this.onResultHover(item));
        });
    }

    updateResultItems() {
        // Collect all visible result items
        this.resultItems = Array.from(this.dropdown.querySelectorAll('.result-item:not(.d-none)'));
        this.activeIndex = -1;
        this.updateActiveState();
    }

    navigateResults(direction) {
        if (this.resultItems.length === 0) return;

        this.activeIndex += direction;

        // Wrap around
        if (this.activeIndex < 0) {
            this.activeIndex = this.resultItems.length - 1;
        } else if (this.activeIndex >= this.resultItems.length) {
            this.activeIndex = 0;
        }

        this.updateActiveState();

        // Scroll into view
        const activeItem = this.resultItems[this.activeIndex];
        if (activeItem) {
            activeItem.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
        }
    }

    updateActiveState() {
        this.resultItems.forEach((item, index) => {
            if (index === this.activeIndex) {
                item.classList.add('active');
            } else {
                item.classList.remove('active');
            }
        });
    }

    onResultHover(item) {
        const index = this.resultItems.indexOf(item);
        if (index !== -1) {
            this.activeIndex = index;
            this.updateActiveState();
        }
    }

    selectActiveResult() {
        if (this.activeIndex >= 0 && this.activeIndex < this.resultItems.length) {
            this.onResultClick(this.resultItems[this.activeIndex]);
        } else if (this.resultItems.length > 0) {
            // Select first item if none active
            this.onResultClick(this.resultItems[0]);
        }
    }

    onResultClick(item) {
        const type = item.dataset.type;

        if (type === 'airport') {
            const code = item.dataset.code;
            if (code) {
                window.location.href = `/airport/${code}`;
            }
        } else if (type === 'aircraft') {
            const registration = item.dataset.registration;
            if (registration) {
                window.location.href = `/aircraft/${registration}`;
            }
        } else if (type === 'aircraft-type') {
            const typeCode = item.dataset.typeCode;
            if (typeCode) {
                window.location.href = `/aircraft-type/${typeCode}`;
            }
        }
    }

    clearSearch() {
        this.searchInput.value = '';
        this.clearBtn.classList.add('d-none');
        this.searchInput.focus();
        this.hideDropdown();
    }

    escapeHtml(text) {
        if (!text) return '';
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }
}

/**
 * Popular Cards - Display popular airports and aircraft
 */
class PopularCards {
    constructor() {
        this.airportsContent = document.getElementById('popularAirportsContent');
        this.aircraftContent = document.getElementById('popularAircraftContent');
        this.loadPopularItems();
    }

    async loadPopularItems() {
        try {
            const response = await fetch('/api/v1/search/suggestions');
            const data = await response.json();

            if (data.success) {
                this.renderAirports(data.popular_airports || []);
                this.renderAircraft(data.recent_aircraft || []);
            }
        } catch (error) {
            console.error('Error loading popular items:', error);
            this.airportsContent.innerHTML = '<div class="loading-placeholder">加载失败</div>';
            this.aircraftContent.innerHTML = '<div class="loading-placeholder">加载失败</div>';
        }
    }

    renderAirports(airports) {
        if (!airports || airports.length === 0) {
            this.airportsContent.innerHTML = '<div class="loading-placeholder">暂无数据</div>';
            return;
        }

        this.airportsContent.innerHTML = airports.map(airport => `
            <a href="/airport/${airport.iata_code || airport.icao_code}" class="popular-item">
                <div class="popular-item-icon airport">
                    <i class="fas fa-plane-arrival"></i>
                </div>
                <div class="popular-item-content">
                    <div class="popular-item-title">
                        ${airport.iata_code ? `<span class="code-badge iata" style="background:#e3f2fd;color:#1565c0;padding:2px 6px;border-radius:4px;font-size:0.75rem;margin-right:4px;">${airport.iata_code}</span>` : ''}
                        ${this.escapeHtml(airport.name || '')}
                    </div>
                    <div class="popular-item-subtitle">
                        ${this.escapeHtml(airport.city || '')}${airport.country_code ? `, ${airport.country_code}` : ''}
                    </div>
                </div>
                ${airport.flight_count ? `<div class="popular-item-badge"><i class="fas fa-plane"></i>${airport.flight_count.toLocaleString()}</div>` : ''}
            </a>
        `).join('');
    }

    renderAircraft(aircraft) {
        if (!aircraft || aircraft.length === 0) {
            this.aircraftContent.innerHTML = '<div class="loading-placeholder">暂无数据</div>';
            return;
        }

        this.aircraftContent.innerHTML = aircraft.map(ac => `
            <a href="/aircraft/${ac.registration}" class="popular-item">
                <div class="popular-item-icon aircraft">
                    <i class="fas fa-plane"></i>
                </div>
                <div class="popular-item-content">
                    <div class="popular-item-title">
                        <span class="code-badge" style="background:#fff3e0;color:#e65100;padding:2px 6px;border-radius:4px;font-size:0.75rem;margin-right:4px;">${this.escapeHtml(ac.registration)}</span>
                        ${this.escapeHtml(ac.aircraft_type || '')}
                    </div>
                    <div class="popular-item-subtitle">
                        ${this.escapeHtml(ac.owner || ac.operator || '-')}
                    </div>
                </div>
                ${ac.image_count ? `<div class="popular-item-badge"><i class="fas fa-image"></i>${ac.image_count}</div>` : ''}
            </a>
        `).join('');
    }

    escapeHtml(text) {
        if (!text) return '';
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }
}

// Initialize on DOM ready
document.addEventListener('DOMContentLoaded', () => {
    window.homeSearch = new HomeSearch();
    window.popularCards = new PopularCards();
});
