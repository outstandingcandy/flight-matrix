/**
 * FlightMatrix Aircraft Type Detail Page
 */

class AircraftTypeDetailPage {
    constructor(typeCode) {
        this.typeCode = typeCode;
        this.offset = 0;
        this.limit = 20;
        this.hasMore = true;
        this.loading = false;

        // DOM elements
        this.loadingState = document.getElementById('loadingState');
        this.errorState = document.getElementById('errorState');
        this.errorMessage = document.getElementById('errorMessage');
        this.mainContent = document.getElementById('mainContent');
        this.heroTypeCode = document.getElementById('heroTypeCode');
        this.heroTypeName = document.getElementById('heroTypeName');
        this.totalAircraftCount = document.getElementById('totalAircraftCount');
        this.withImagesCount = document.getElementById('withImagesCount');
        this.aircraftGrid = document.getElementById('aircraftGrid');
        this.emptyState = document.getElementById('emptyState');
        this.loadMoreSection = document.getElementById('loadMoreSection');
        this.loadMoreBtn = document.getElementById('loadMoreBtn');
        this.loadingMore = document.getElementById('loadingMore');

        this.init();
    }

    async init() {
        try {
            // Load type info and first page of aircraft in parallel
            const [typeInfoResponse, aircraftResponse] = await Promise.all([
                fetch(`/api/v1/aircraft/types/${this.typeCode}`),
                fetch(`/api/v1/aircraft/types/${this.typeCode}/instances?offset=0&limit=${this.limit}`)
            ]);

            const typeInfo = await typeInfoResponse.json();
            const aircraftData = await aircraftResponse.json();

            if (!typeInfo.success) {
                throw new Error(typeInfo.error || 'Failed to load type info');
            }

            // Update header
            this.heroTypeCode.textContent = typeInfo.type_code;
            this.heroTypeName.textContent = typeInfo.name !== typeInfo.type_code ? typeInfo.name : '';
            this.totalAircraftCount.textContent = typeInfo.total_aircraft.toLocaleString();
            this.withImagesCount.textContent = typeInfo.aircraft_with_images.toLocaleString();

            // Update page title
            document.title = `${typeInfo.type_code} - ${typeInfo.name || 'Aircraft Type'} - FlightMatrix`;

            // Render aircraft
            if (aircraftData.success) {
                this.renderAircraft(aircraftData.aircraft);
                this.hasMore = aircraftData.has_more;
                this.offset = this.limit;

                if (aircraftData.aircraft.length === 0) {
                    this.emptyState.classList.remove('d-none');
                } else if (this.hasMore) {
                    this.loadMoreSection.classList.remove('d-none');
                }
            }

            // Show content
            this.loadingState.classList.add('d-none');
            this.mainContent.classList.remove('d-none');

            // Bind load more button
            this.loadMoreBtn.addEventListener('click', () => this.loadMore());

        } catch (error) {
            console.error('Error loading aircraft type:', error);
            this.showError(error.message || 'Failed to load aircraft type information');
        }
    }

    renderAircraft(aircraft) {
        const html = aircraft.map(ac => this.createAircraftCard(ac)).join('');
        this.aircraftGrid.insertAdjacentHTML('beforeend', html);
    }

    createAircraftCard(aircraft) {
        const imageHtml = aircraft.image_url
            ? `<img src="${this.escapeHtml(aircraft.image_url)}"
                    alt="${this.escapeHtml(aircraft.registration)}"
                    class="aircraft-card-image"
                    loading="lazy"
                    onerror="this.onerror=null; this.outerHTML='<div class=\\'aircraft-card-image placeholder\\'><i class=\\'fas fa-plane\\'></i></div>'">`
            : `<div class="aircraft-card-image placeholder"><i class="fas fa-plane"></i></div>`;

        const ownerText = aircraft.operator || aircraft.owner || '-';

        return `
            <a href="/aircraft/${this.escapeHtml(aircraft.registration)}" class="aircraft-card">
                ${imageHtml}
                <div class="aircraft-card-body">
                    <div class="aircraft-card-registration">${this.escapeHtml(aircraft.registration)}</div>
                    <div class="aircraft-card-owner" title="${this.escapeHtml(ownerText)}">${this.escapeHtml(ownerText)}</div>
                </div>
            </a>
        `;
    }

    async loadMore() {
        if (this.loading || !this.hasMore) return;

        this.loading = true;
        this.loadMoreSection.classList.add('d-none');
        this.loadingMore.classList.remove('d-none');

        try {
            const response = await fetch(
                `/api/v1/aircraft/types/${this.typeCode}/instances?offset=${this.offset}&limit=${this.limit}`
            );
            const data = await response.json();

            if (data.success) {
                this.renderAircraft(data.aircraft);
                this.hasMore = data.has_more;
                this.offset += data.aircraft.length;

                if (this.hasMore) {
                    this.loadMoreSection.classList.remove('d-none');
                }
            }
        } catch (error) {
            console.error('Error loading more aircraft:', error);
            // Show load more button again so user can retry
            this.loadMoreSection.classList.remove('d-none');
        } finally {
            this.loading = false;
            this.loadingMore.classList.add('d-none');
        }
    }

    showError(message) {
        this.loadingState.classList.add('d-none');
        this.mainContent.classList.add('d-none');
        this.errorMessage.textContent = message;
        this.errorState.classList.remove('d-none');
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
    if (typeof AIRCRAFT_TYPE_CODE !== 'undefined') {
        window.typeDetailPage = new AircraftTypeDetailPage(AIRCRAFT_TYPE_CODE);
    }
});
