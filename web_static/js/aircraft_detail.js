/**
 * 飞机详情页 JavaScript
 * Aircraft Detail Page Controller
 */

class AircraftDetailPage {
    constructor(registration) {
        this.registration = registration;
        this.staticInfo = null;
        this.images = [];           // Simple URL list for backward compatibility
        this.imagesWithMeta = [];   // Full metadata
        this.currentImageIndex = 0;
        this.displayedImageCount = 0;  // 当前已显示的图片数量
        this.imagesPerPage = 32;       // 每次加载的图片数量

        this.initialize();
    }

    async initialize() {
        await this.loadAircraftData();
    }

    async loadAircraftData() {
        this.showLoading(true);

        try {
            // 并行加载静态信息和图片
            const [staticResponse, imagesResponse] = await Promise.all([
                fetch(`/api/v1/aircraft/static/${encodeURIComponent(this.registration)}`),
                fetch(`/api/v1/aircraft/${encodeURIComponent(this.registration)}/images`)
            ]);

            const staticData = await staticResponse.json();
            const imagesData = await imagesResponse.json();

            if (staticData.success) {
                this.staticInfo = staticData.data;
                this.displayStaticInfo(this.staticInfo);
            } else {
                // 即使没有静态信息，也显示基本内容
                this.displayStaticInfo({
                    registration: this.registration
                });
            }

            if (imagesData.success) {
                this.images = imagesData.images || [];
                this.imagesWithMeta = imagesData.images_with_metadata || [];
                this.displayImages();
                this.displayAllImagesGrid();
            }

            this.showLoading(false);
            this.showContent(true);

            // 加载最近航班（不阻塞主内容）
            this.loadRecentFlights();

        } catch (error) {
            console.error('加载飞机数据失败:', error);
            this.showLoading(false);
            this.showError(error.message);
        }
    }

    async loadRecentFlights() {
        const card = document.getElementById('recentFlightsCard');
        const loading = document.getElementById('recentFlightsLoading');
        const list = document.getElementById('recentFlightsList');
        const empty = document.getElementById('recentFlightsEmpty');

        try {
            const response = await fetch(`/api/v1/aircraft/${encodeURIComponent(this.registration)}/recent-flights`);
            const data = await response.json();

            if (loading) loading.classList.add('d-none');

            if (data.success && data.flights && data.flights.length > 0) {
                if (card) card.classList.remove('d-none');
                this.renderRecentFlights(data.flights);
                if (list) list.classList.remove('d-none');
            } else {
                if (card) card.classList.remove('d-none');
                if (empty) empty.classList.remove('d-none');
            }
        } catch (error) {
            console.error('Error loading recent flights:', error);
            if (loading) loading.classList.add('d-none');
        }
    }

    renderRecentFlights(flights) {
        const list = document.getElementById('recentFlightsList');
        if (!list) return;

        const html = flights.map(flight => {
            const isArrival = flight.flight_type === 'arrival';
            const badge = isArrival
                ? '<span class="badge bg-success">进港</span>'
                : '<span class="badge bg-warning text-dark">离港</span>';

            // 进港: 始发机场 → 当前机场
            // 离港: 当前机场 → 目的机场
            const route = isArrival
                ? `${flight.remote_airport_iata || '?'} → ${flight.airport_iata || '?'}`
                : `${flight.airport_iata || '?'} → ${flight.remote_airport_iata || '?'}`;

            const remoteName = flight.remote_airport_name || '';
            const timeStr = flight.scheduled_time
                ? this.formatFlightTime(flight.scheduled_time)
                : '-';

            return `
                <div class="recent-flight-item">
                    <div class="flight-type-badge">${badge}</div>
                    <div class="flight-route" title="${remoteName}">${route}</div>
                    <div class="flight-time">${timeStr}</div>
                </div>
            `;
        }).join('');

        list.innerHTML = html;
    }

    formatFlightTime(isoStr) {
        try {
            const date = new Date(isoStr);
            return date.toLocaleString('zh-CN', {
                month: '2-digit',
                day: '2-digit',
                hour: '2-digit',
                minute: '2-digit'
            });
        } catch {
            return isoStr;
        }
    }

    displayStaticInfo(info) {
        // 更新Hero区域
        const heroRegistration = document.getElementById('heroRegistration');
        if (heroRegistration) {
            heroRegistration.textContent = info.registration || this.registration;
        }

        // 更新机型信息 (manufacturer + model)
        const heroType = document.getElementById('heroType');
        if (heroType) {
            const typeParts = [];
            if (info.manufacturer) typeParts.push(info.manufacturer);
            if (info.aircraft_model) typeParts.push(info.aircraft_model);
            else if (info.aircraft_type) typeParts.push(info.aircraft_type);
            heroType.textContent = typeParts.join(' ') || '';
        }

        // 更新所有者/运营方信息
        const heroOwner = document.getElementById('heroOwner');
        if (heroOwner) {
            const ownerParts = [];
            if (info.owner) ownerParts.push(info.owner);
            if (info.operator && info.operator !== info.owner) ownerParts.push(`运营: ${info.operator}`);
            heroOwner.textContent = ownerParts.join(' | ') || '';
        }

        // 更新徽章 (国家, 军用, 政府, VIP等)
        const badgesContainer = document.getElementById('heroBadges');
        if (badgesContainer) {
            let badgesHtml = '';
            if (info.country) badgesHtml += `<span class="badge bg-light text-dark">${info.country}</span>`;
            if (info.is_military) badgesHtml += '<span class="badge bg-danger">军用</span>';
            if (info.is_government) badgesHtml += '<span class="badge bg-info">政府</span>';
            if (info.is_vip) badgesHtml += '<span class="badge bg-warning text-dark">VIP</span>';
            badgesContainer.innerHTML = badgesHtml;
        }

        // 更新基本信息卡片
        document.getElementById('infoRegistration').textContent = info.registration || '-';
        document.getElementById('infoHex').textContent = info.hex || '-';
        document.getElementById('infoType').textContent = info.aircraft_type || '-';
        document.getElementById('infoModel').textContent = info.aircraft_model || '-';
        document.getElementById('infoManufacturer').textContent = info.manufacturer || '-';
        document.getElementById('infoSerialNumber').textContent = info.serial_number || '-';
        document.getElementById('infoYearBuilt').textContent = info.year_built || '-';
        document.getElementById('infoCountry').textContent = info.country || '-';

        // 更新归属信息
        document.getElementById('infoOwner').textContent = info.owner || '-';
        document.getElementById('infoOperator').textContent = info.operator || '-';
        document.getElementById('infoOrganization').textContent = info.organization || '-';

        // 更新标签显示
        const tagsElement = document.getElementById('infoTags');
        if (info.tags && Array.isArray(info.tags) && info.tags.length > 0) {
            tagsElement.innerHTML = info.tags.map(tag =>
                `<span class="badge bg-secondary me-1">${tag}</span>`
            ).join('');
        } else {
            tagsElement.textContent = '-';
        }

        // 更新数据信息
        this.setElementText('infoDataSource', info.data_source);
        this.setElementText('infoUpdatedAt', info.updated_at);
        this.setElementText('infoId', info.id);
        this.setElementText('infoHitCount', info.hit_count);
        this.setElementText('infoImagesDownloaded', info.images_downloaded ? '是' : '否');
        this.setElementText('infoImagesUpdatedAt', info.images_updated_at);

        // 显示涂装信息卡片
        const hasLiveryInfo = info.livery_name || info.livery_type || info.livery_description || info.special_markings;
        if (hasLiveryInfo) {
            const liveryCard = document.getElementById('liveryCard');
            if (liveryCard) liveryCard.classList.remove('d-none');
            this.setElementText('infoLiveryName', info.livery_name);
            this.setElementText('infoLiveryType', info.livery_type);
            this.setElementText('infoLiveryDescription', info.livery_description);
            this.setElementText('infoSpecialMarkings', info.special_markings);
        }

        // 显示情报分析卡片
        const hasIntelligence = info.attention_level || info.attention_reason || info.intelligence_summary ||
                               info.flight_pattern || info.anomalies || info.recommended_actions;
        if (hasIntelligence) {
            const intelligenceCard = document.getElementById('intelligenceCard');
            if (intelligenceCard) intelligenceCard.classList.remove('d-none');
            this.setElementText('infoAttentionLevel', info.attention_level);
            this.setElementText('infoAttentionReason', info.attention_reason);
            this.setElementText('infoIntelligenceSummary', info.intelligence_summary);
            this.setElementText('infoFlightPattern', info.flight_pattern);
            this.setElementText('infoAnomalies', info.anomalies);
            this.setElementText('infoRecommendedActions', info.recommended_actions);
        }

        // 显示摘要
        if (info.summary) {
            const summaryCard = document.getElementById('summaryCard');
            const infoSummary = document.getElementById('infoSummary');
            if (summaryCard) summaryCard.classList.remove('d-none');
            if (infoSummary) infoSummary.textContent = info.summary;
        }

        // 显示历史所有者
        if (info.previous_owners) {
            const previousOwnersCard = document.getElementById('previousOwnersCard');
            const previousOwnersElement = document.getElementById('infoPreviousOwners');
            if (previousOwnersCard) previousOwnersCard.classList.remove('d-none');
            if (previousOwnersElement) {
                if (Array.isArray(info.previous_owners)) {
                    previousOwnersElement.innerHTML = info.previous_owners.map(owner =>
                        `<div class="mb-1">${owner}</div>`
                    ).join('');
                } else {
                    previousOwnersElement.textContent = info.previous_owners;
                }
            }
        }

        // 更新页面标题
        const titleParts = ['飞机详情', info.registration];
        if (info.aircraft_model) titleParts.push(info.aircraft_model);
        document.title = titleParts.join(' - ');
    }

    setElementText(elementId, value) {
        const el = document.getElementById(elementId);
        if (el) el.textContent = value || '-';
    }

    displayImages() {
        const mainImage = document.getElementById('galleryMainImage');
        const placeholder = document.getElementById('galleryPlaceholder');
        const thumbnailsContainer = document.getElementById('galleryThumbnails');
        const statsElement = document.getElementById('galleryStats');
        const metaContainer = document.getElementById('currentImageMeta');

        if (!this.imagesWithMeta || this.imagesWithMeta.length === 0) {
            if (mainImage) mainImage.classList.add('d-none');
            if (placeholder) placeholder.classList.remove('d-none');
            if (metaContainer) metaContainer.classList.add('d-none');
            if (thumbnailsContainer) thumbnailsContainer.innerHTML = '';
            if (statsElement) statsElement.textContent = '';
            return;
        }

        // 显示统计
        if (statsElement) statsElement.textContent = `共 ${this.imagesWithMeta.length} 张`;

        // 显示主图和元数据
        const firstImage = this.imagesWithMeta[0];
        if (mainImage) {
            mainImage.src = firstImage.url;
            mainImage.classList.remove('d-none');
            // 主图加载错误处理
            mainImage.onerror = () => {
                mainImage.classList.add('d-none');
                if (placeholder) placeholder.classList.remove('d-none');
            };
        }
        if (placeholder) placeholder.classList.add('d-none');
        if (metaContainer) metaContainer.classList.remove('d-none');

        // 更新元数据显示
        this.updateCurrentImageMeta(0);

        // 生成缩略图
        if (!thumbnailsContainer) return;
        if (this.imagesWithMeta.length > 1) {
            thumbnailsContainer.innerHTML = this.imagesWithMeta.slice(0, 10).map((img, index) => {
                // 尝试使用缩略图URL
                const thumbUrl = img.url.replace('/jetphotos_images/', '/jetphotos_thumbnails/')
                                        .replace('_full_', '_thumb_');
                return `
                    <img src="${thumbUrl}"
                         class="gallery-thumbnail ${index === 0 ? 'active' : ''}"
                         data-index="${index}"
                         data-full-url="${img.url}"
                         alt="缩略图 ${index + 1}"
                         title="${img.photographer || '未知摄影师'}"
                         onclick="detailPage.selectImage(${index})"
                         onerror="this.src='${img.url}'; this.onerror=null;">
                `;
            }).join('');

            // 如果超过10张，显示更多提示
            if (this.imagesWithMeta.length > 10) {
                thumbnailsContainer.innerHTML += `
                    <div class="d-flex align-items-center text-muted" style="font-size: 0.8rem;">
                        +${this.imagesWithMeta.length - 10} 更多
                    </div>
                `;
            }
        } else {
            thumbnailsContainer.innerHTML = '';
        }
    }

    updateCurrentImageMeta(index) {
        if (index < 0 || index >= this.imagesWithMeta.length) return;

        const img = this.imagesWithMeta[index];

        // 清理可能的 JSON 残留数据
        const cleanField = (value) => {
            if (!value) return '';
            if (value.includes('contentUrl') || value.includes('datePublished') || value.includes('{"@')) {
                return '';
            }
            return value;
        };

        const location = cleanField(img.airport_name) || cleanField(img.location);

        // 更新元数据显示
        const metaPhotographer = document.getElementById('metaPhotographer');
        const metaLocation = document.getElementById('metaLocation');
        const metaPhotoDate = document.getElementById('metaPhotoDate');
        const metaUploadDate = document.getElementById('metaUploadDate');

        if (metaPhotographer) metaPhotographer.textContent = img.photographer || '未知';
        if (metaLocation) metaLocation.textContent = location || '未知地点';
        if (metaPhotoDate) metaPhotoDate.textContent = img.photo_date ? this.formatDate(img.photo_date) : '未知日期';
        if (metaUploadDate) metaUploadDate.textContent = img.upload_date ? this.formatDate(img.upload_date) : '未知日期';

        // 显示 notes
        const notesContainer = document.getElementById('metaNotesContainer');
        const notesElement = document.getElementById('metaNotes');
        if (notesContainer) {
            if (img.notes && img.notes.trim() && img.notes !== 'N/A') {
                if (notesElement) notesElement.textContent = img.notes;
                notesContainer.classList.remove('d-none');
            } else {
                notesContainer.classList.add('d-none');
            }
        }

        // JetPhotos 链接
        const linkElement = document.getElementById('metaJetphotosLink');
        if (linkElement) {
            if (img.source_url) {
                linkElement.href = img.source_url;
                linkElement.classList.remove('d-none');
            } else if (img.jetphotos_id) {
                linkElement.href = `https://www.jetphotos.com/photo/${img.jetphotos_id}`;
                linkElement.classList.remove('d-none');
            } else {
                linkElement.classList.add('d-none');
            }
        }
    }

    formatDate(dateStr) {
        if (!dateStr) return '未知';
        try {
            const date = new Date(dateStr);
            return date.toLocaleDateString('zh-CN', { year: 'numeric', month: 'long', day: 'numeric' });
        } catch {
            return dateStr;
        }
    }

    displayAllImagesGrid() {
        const section = document.getElementById('allImagesSection');
        const grid = document.getElementById('allImagesGrid');
        const countElement = document.getElementById('allImagesCount');

        if (!this.imagesWithMeta || this.imagesWithMeta.length === 0) {
            if (section) section.classList.add('d-none');
            return;
        }

        if (section) section.classList.remove('d-none');
        if (countElement) countElement.textContent = `${this.imagesWithMeta.length} 张图片`;
        if (!grid) return;

        // 重置显示计数
        this.displayedImageCount = 0;
        grid.innerHTML = '';

        // 初始加载第一批图片
        this.loadMoreImages();
    }

    loadMoreImages() {
        const grid = document.getElementById('allImagesGrid');
        if (!grid) return;

        const startIndex = this.displayedImageCount;
        const endIndex = Math.min(startIndex + this.imagesPerPage, this.imagesWithMeta.length);

        // 移除现有的"加载更多"按钮
        const existingLoadMore = document.getElementById('loadMoreContainer');
        if (existingLoadMore) {
            existingLoadMore.remove();
        }

        // 渲染新的图片
        const newImages = this.imagesWithMeta.slice(startIndex, endIndex).map((img, i) => {
            const index = startIndex + i;
            return this.renderImageCard(img, index);
        }).join('');

        grid.insertAdjacentHTML('beforeend', newImages);

        // 更新已显示数量
        this.displayedImageCount = endIndex;

        // 更新统计显示
        const countElement = document.getElementById('allImagesCount');
        if (countElement) {
            countElement.textContent = `已显示 ${this.displayedImageCount} / ${this.imagesWithMeta.length} 张`;
        }

        // 如果还有更多图片，显示"加载更多"按钮
        if (this.displayedImageCount < this.imagesWithMeta.length) {
            const remainingCount = this.imagesWithMeta.length - this.displayedImageCount;
            const loadMoreHtml = `
                <div id="loadMoreContainer" class="text-center mt-4" style="grid-column: 1 / -1;">
                    <button class="btn btn-outline-primary btn-lg" onclick="detailPage.loadMoreImages()">
                        <i class="fas fa-plus-circle me-2"></i>加载更多 (还有 ${remainingCount} 张)
                    </button>
                </div>
            `;
            grid.insertAdjacentHTML('beforeend', loadMoreHtml);
        }
    }

    renderImageCard(img, index) {
        const thumbUrl = img.url.replace('/jetphotos_images/', '/jetphotos_thumbnails/')
                                .replace('_full_', '_thumb_');

        // 清理可能的 JSON 残留数据
        const cleanField = (value) => {
            if (!value) return '';
            if (value.includes('contentUrl') || value.includes('datePublished') || value.includes('{"@')) {
                return '';
            }
            return value;
        };

        const notes = cleanField(img.notes);
        const location = cleanField(img.airport_name) || cleanField(img.location);
        const photoDate = img.photo_date ? this.formatDate(img.photo_date) : '';
        const uploadDate = img.upload_date ? this.formatDate(img.upload_date) : '';

        return `
            <div class="image-card">
                <div class="image-card-wrapper">
                    <img src="${thumbUrl}"
                         alt="图片 ${index + 1}"
                         onclick="detailPage.openLightbox('${img.url}')"
                         onerror="this.src='${img.url}'; this.onerror=null;">
                    ${img.is_primary ? '<span class="image-badge badge bg-primary">主图</span>' : ''}
                    <span class="image-order">#${img.display_order || index + 1}</span>
                </div>
                <div class="image-card-body">
                    <div class="image-meta-row">
                        <i class="fas fa-camera"></i>
                        <span class="value">${img.photographer || '未知摄影师'}</span>
                    </div>
                    <div class="image-meta-row">
                        <i class="fas fa-map-marker-alt"></i>
                        <span class="value">${location || '未知地点'}</span>
                    </div>
                    <div class="image-meta-row">
                        <i class="fas fa-camera-retro"></i>
                        <span class="value">${photoDate || '未知日期'}</span>
                    </div>
                    <div class="image-meta-row">
                        <i class="fas fa-cloud-upload-alt"></i>
                        <span class="value">${uploadDate || '未知日期'}</span>
                    </div>
                    ${notes ? `
                    <div class="image-notes" title="${this.escapeHtml(notes)}">${this.escapeHtml(notes)}</div>
                    ` : ''}
                    ${img.jetphotos_id ? `
                    <div class="mt-2">
                        <a href="https://www.jetphotos.com/photo/${img.jetphotos_id}"
                           target="_blank" class="jetphotos-link">
                            <i class="fas fa-external-link-alt me-1"></i>JetPhotos
                        </a>
                    </div>
                    ` : ''}
                </div>
            </div>
        `;
    }

    escapeHtml(text) {
        if (!text) return '';
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    selectImage(index) {
        if (index < 0 || index >= this.imagesWithMeta.length) return;

        this.currentImageIndex = index;
        const mainImage = document.getElementById('galleryMainImage');
        mainImage.src = this.imagesWithMeta[index].url;

        // 更新元数据显示
        this.updateCurrentImageMeta(index);

        // 更新缩略图选中状态
        document.querySelectorAll('.gallery-thumbnail').forEach((thumb, i) => {
            thumb.classList.toggle('active', i === index);
        });
    }

    openLightbox(imageUrl) {
        const lightboxImage = document.getElementById('lightboxImage');
        lightboxImage.src = imageUrl;
        const modal = new bootstrap.Modal(document.getElementById('imageLightboxModal'));
        modal.show();
    }

    showLoading(show) {
        const el = document.getElementById('loadingState');
        if (el) el.classList.toggle('d-none', !show);
    }

    showContent(show) {
        const el = document.getElementById('mainContent');
        if (el) el.classList.toggle('d-none', !show);
    }

    showError(message) {
        const errorState = document.getElementById('errorState');
        const errorMessage = document.getElementById('errorMessage');
        if (errorMessage) errorMessage.textContent = message;
        if (errorState) errorState.classList.remove('d-none');
    }
}

// 初始化页面
let detailPage;
document.addEventListener('DOMContentLoaded', () => {
    if (typeof AIRCRAFT_REGISTRATION !== 'undefined' && AIRCRAFT_REGISTRATION) {
        detailPage = new AircraftDetailPage(AIRCRAFT_REGISTRATION);
    } else {
        console.error('未找到飞机注册号');
    }
});
