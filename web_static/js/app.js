/**
 * 飞机追踪系统前端JavaScript
 * Aircraft Tracking System Frontend JavaScript
 */

class AircraftTracker {
    constructor() {
        this.map = null;
        this.trackMap = null;
        this.markers = [];
        this.trackMarkers = [];
        this.currentTrackLine = null;
        this.currentRegistration = null;
        
        // 过滤器相关属性
        this.originalData = [];  // 存储原始数据
        this.filteredData = [];  // 存储过滤后的数据
        this.filtersVisible = false;  // 过滤器是否可见
        this.activeFilters = {};  // 当前活跃的过滤器
        
        this.initializeApp();
    }

    /**
     * 初始化应用
     */
    initializeApp() {
        this.initializeMap();
        this.initializeDatePickers();
        this.bindEvents();
        this.loadStatistics();
        // 静态信息列表现在根据搜索结果动态加载，不再初始化时加载
    }

    /**
     * 初始化地图
     */
    initializeMap() {
        // 主地图
        this.map = L.map('map').setView([39.9042, 116.4074], 6); // 默认中心：北京
        
        L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
            attribution: '© OpenStreetMap contributors',
            maxZoom: 18
        }).addTo(this.map);

        // 添加地图控件
        this.addMapControls();
    }

    /**
     * 初始化轨迹地图（模态框中）
     */
    initializeTrackMap() {
        if (this.trackMap) {
            console.log('轨迹地图已存在，跳过初始化');
            return;
        }

        console.log('开始初始化轨迹地图');
        
        try {
            // 检查trackMap容器是否存在
            const mapContainer = document.getElementById('trackMap');
            if (!mapContainer) {
                console.error('轨迹地图容器不存在');
                return;
            }

            this.trackMap = L.map('trackMap').setView([39.9042, 116.4074], 6);
            
            L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
                attribution: '© OpenStreetMap contributors',
                maxZoom: 18
            }).addTo(this.trackMap);
            
            console.log('轨迹地图初始化成功');
            
        } catch (error) {
            console.error('轨迹地图初始化失败:', error);
        }
    }

    /**
     * 添加地图控件
     */
    addMapControls() {
        // 图例控件
        const legend = L.control({position: 'bottomright'});
        legend.onAdd = function(map) {
            const div = L.DomUtil.create('div', 'legend');
            div.style.backgroundColor = 'white';
            div.style.padding = '10px';
            div.style.borderRadius = '5px';
            div.style.boxShadow = '0 0 15px rgba(0,0,0,0.2)';
            div.innerHTML = `
                <h6><strong>图例</strong></h6>
                <div><span style="color: #007bff;">●</span> 民用飞机</div>
                <div><span style="color: #dc3545;">●</span> 军用飞机</div>
                <div><span style="color: #ffc107;">●</span> 特殊飞机</div>
            `;
            return div;
        };
        legend.addTo(this.map);
    }

    /**
     * 初始化日期选择器
     */
    initializeDatePickers() {
        const config = {
            enableTime: true,
            dateFormat: "Y-m-d H:i",
            time_24hr: true,
            locale: "zh"
        };

        flatpickr("#start_date", {
            ...config,
            defaultDate: new Date(Date.now() - 24 * 60 * 60 * 1000) // 默认昨天（北京时间）
        });

        flatpickr("#end_date", {
            ...config,
            defaultDate: new Date() // 默认现在（北京时间）
        });

        flatpickr("#trackStartDate", {
            ...config,
            defaultDate: new Date(Date.now() - 7 * 24 * 60 * 60 * 1000) // 默认7天前（北京时间）
        });
    }

    /**
     * 绑定事件
     */
    bindEvents() {
        // 搜索表单
        document.getElementById('searchForm').addEventListener('submit', (e) => {
            e.preventDefault();
            this.searchAircraft();
        });

        // 清空表单
        document.getElementById('clearForm').addEventListener('click', () => {
            this.clearForm();
        });

        // 快速查询按钮
        document.getElementById('recentBtn').addEventListener('click', () => {
            this.getRecentAircraft();
        });

        document.getElementById('militaryBtn').addEventListener('click', () => {
            this.getMilitaryAircraft();
        });

        document.getElementById('uniqueBtn').addEventListener('click', () => {
            this.getUniqueAircraft();
        });

        // 地图控制按钮
        document.getElementById('clearMapBtn').addEventListener('click', () => {
            this.clearMap();
        });

        document.getElementById('fitBoundsBtn').addEventListener('click', () => {
            this.fitMapBounds();
        });

        // 轨迹模态框
        document.getElementById('loadTrackBtn').addEventListener('click', () => {
            this.loadAircraftTrack();
        });

        // 模态框显示事件
        document.getElementById('trackModal').addEventListener('shown.bs.modal', () => {
            this.initializeTrackMap();
            setTimeout(() => {
                this.trackMap.invalidateSize();
            }, 100);
        });

        // 过滤器相关事件
        document.getElementById('toggleFilters').addEventListener('click', () => {
            this.toggleFilters();
        });

        document.getElementById('clearFilters').addEventListener('click', () => {
            this.clearAllFilters();
        });

        // 为所有列过滤器添加事件监听器
        document.addEventListener('input', (e) => {
            if (e.target.classList.contains('column-filter')) {
                this.applyColumnFilter(e.target);
            }
        });

        document.addEventListener('change', (e) => {
            if (e.target.classList.contains('column-filter')) {
                this.applyColumnFilter(e.target);
            }
        });

        // 从静态信息模态框查看轨迹
        document.getElementById('viewTracksFromStaticBtn').addEventListener('click', () => {
            const registration = document.getElementById('staticInfoRegistration').textContent;
            if (registration) {
                // 关闭当前模态框
                bootstrap.Modal.getInstance(document.getElementById('staticInfoModal')).hide();
                // 打开轨迹模态框
                setTimeout(() => this.showTrackModal(registration), 300);
            }
        });
    }

    /**
     * 搜索飞机
     */
    async searchAircraft() {
        const formData = new FormData(document.getElementById('searchForm'));
        const params = new URLSearchParams();

        for (let [key, value] of formData.entries()) {
            if (value.trim()) {
                params.append(key, value.trim());
            }
        }

        try {
            this.showLoading('正在搜索...');
            const response = await fetch(`/api/aircraft/search?${params}`);
            const data = await response.json();

            if (data.success) {
                this.displayResults(data.data);
                this.updateMap(data.data);
                this.showSuccess(`找到 ${data.count} 条记录`);
            } else {
                this.showError('搜索失败: ' + data.error);
            }
        } catch (error) {
            this.showError('网络错误: ' + error.message);
        } finally {
            this.hideLoading();
        }
    }

    /**
     * 获取最近飞机
     */
    async getRecentAircraft() {
        try {
            this.showLoading('加载最近飞机数据...');
            const response = await fetch('/api/aircraft/recent?hours=1&limit=100');
            const data = await response.json();

            if (data.success) {
                this.displayResults(data.data);
                this.updateMap(data.data);
                this.showSuccess(`找到 ${data.count} 架最近飞机`);
            } else {
                this.showError('加载失败: ' + data.error);
            }
        } catch (error) {
            this.showError('网络错误: ' + error.message);
        } finally {
            this.hideLoading();
        }
    }

    /**
     * 获取军用飞机
     */
    async getMilitaryAircraft() {
        try {
            this.showLoading('加载军用飞机数据...');
            const response = await fetch('/api/aircraft/search?is_military=true&limit=200');
            const data = await response.json();

            if (data.success) {
                this.displayResults(data.data);
                this.updateMap(data.data);
                this.showSuccess(`找到 ${data.count} 架军用飞机`);
            } else {
                this.showError('加载失败: ' + data.error);
            }
        } catch (error) {
            this.showError('网络错误: ' + error.message);
        } finally {
            this.hideLoading();
        }
    }

    /**
     * 获取唯一飞机列表
     */
    async getUniqueAircraft() {
        try {
            this.showLoading('加载唯一飞机列表...');
            const response = await fetch('/api/aircraft/unique?days=7');
            const data = await response.json();

            if (data.success) {
                this.displayUniqueAircraft(data.aircraft);
                this.showSuccess(`找到 ${data.count} 架唯一飞机`);
            } else {
                this.showError('加载失败: ' + data.error);
            }
        } catch (error) {
            this.showError('网络错误: ' + error.message);
        } finally {
            this.hideLoading();
        }
    }

    /**
     * 显示搜索结果
     */
    displayResults(aircraft) {
        // 存储原始数据
        this.originalData = aircraft;
        this.filteredData = [...aircraft];

        // 重置过滤器
        this.activeFilters = {};
        this.clearAllFilters();

        this.renderTable(this.filteredData);
    }

    /**
     * 渲染表格
     */
    renderTable(aircraft) {
        const tbody = document.getElementById('resultsTableBody');
        const countBadge = document.getElementById('resultCount');
        
        // 更新计数显示
        const totalCount = this.originalData.length;
        const filteredCount = aircraft.length;
        
        if (totalCount !== filteredCount) {
            countBadge.textContent = `${filteredCount} / ${totalCount} 条记录`;
            countBadge.className = 'badge bg-warning';
        } else {
            countBadge.textContent = `${aircraft.length} 条记录`;
            countBadge.className = 'badge bg-primary';
        }

        if (aircraft.length === 0) {
            tbody.innerHTML = `
                <tr class="no-results-row">
                    <td colspan="10" class="text-center text-muted">
                        <i class="fas fa-search"></i> ${this.originalData.length > 0 ? '没有符合过滤条件的数据' : '没有找到匹配的飞机数据'}
                    </td>
                </tr>
            `;
            return;
        }

        tbody.innerHTML = aircraft.map(plane => `
            <tr>
                <td>
                    ${plane.r ?
                        `<a href="/aircraft/${plane.r}" class="text-decoration-none"><strong>${plane.r}</strong></a>` :
                        '<strong>未知</strong>'
                    }
                    ${plane.country_of_registration ? `<br><small class="text-muted">${plane.country_of_registration}</small>` : ''}
                </td>
                <td><code>${plane.hex || '未知'}</code></td>
                <td>${plane.flight ? plane.flight.trim() : '-'}</td>
                <td>${plane.t || '-'}</td>
                <td>
                    <span class="badge ${plane.is_military ? 'military-badge' : 'civilian-badge'}">
                        ${plane.is_military ? '军用' : '民用'}
                    </span>
                </td>
                <td>
                    ${plane.lat && plane.lon ? 
                        `${plane.lat.toFixed(4)}, ${plane.lon.toFixed(4)}` : 
                        '-'
                    }
                    ${plane.current_country ? `<br><small class="text-muted">${plane.current_country}</small>` : ''}
                </td>
                <td>${plane.alt_baro ? `${plane.alt_baro} ft` : '-'}</td>
                <td>${plane.gs ? `${plane.gs.toFixed(0)} kts` : '-'}</td>
                <td>
                    <small>${plane.timestamp ? plane.timestamp + ' (北京时间)' : '-'}</small>
                </td>
                <td>
                    <div class="btn-group btn-group-sm">
                        ${plane.r ? `
                            <button class="btn btn-outline-success" onclick="tracker.showStaticInfoModal('${plane.r}')" title="查看档案">
                                <i class="fas fa-id-card"></i>
                            </button>
                            <button class="btn btn-outline-primary" onclick="tracker.showTrackModal('${plane.r}')" title="查看轨迹">
                                <i class="fas fa-route"></i>
                            </button>
                        ` : ''}
                        ${plane.lat && plane.lon ? `
                            <button class="btn btn-outline-info" onclick="tracker.focusOnMap(${plane.lat}, ${plane.lon})" title="定位到地图">
                                <i class="fas fa-crosshairs"></i>
                            </button>
                        ` : ''}
                    </div>
                </td>
            </tr>
        `).join('');
    }

    /**
     * 显示唯一飞机列表
     */
    displayUniqueAircraft(aircraft) {
        // 转换为标准格式以支持过滤器
        const standardizedAircraft = aircraft.map(plane => ({
            r: plane.registration,
            hex: plane.hex,
            flight: null,
            t: plane.aircraft_type,
            is_military: plane.is_military,
            lat: null,
            lon: null,
            alt_baro: null,
            gs: null,
            timestamp: null,
            country_of_registration: plane.country_of_registration,
            current_country: null
        }));

        // 存储原始数据
        this.originalData = standardizedAircraft;
        this.filteredData = [...standardizedAircraft];

        // 重置过滤器
        this.activeFilters = {};
        this.clearAllFilters();

        this.renderUniqueTable(this.filteredData);
    }

    /**
     * 渲染唯一飞机表格
     */
    renderUniqueTable(aircraft) {
        const tbody = document.getElementById('resultsTableBody');
        const countBadge = document.getElementById('resultCount');
        
        // 更新计数显示
        const totalCount = this.originalData.length;
        const filteredCount = aircraft.length;
        
        if (totalCount !== filteredCount) {
            countBadge.textContent = `${filteredCount} / ${totalCount} 架唯一飞机`;
            countBadge.className = 'badge bg-warning';
        } else {
            countBadge.textContent = `${aircraft.length} 架唯一飞机`;
            countBadge.className = 'badge bg-primary';
        }

        if (aircraft.length === 0) {
            tbody.innerHTML = `
                <tr class="no-results-row">
                    <td colspan="10" class="text-center text-muted">
                        <i class="fas fa-search"></i> ${this.originalData.length > 0 ? '没有符合过滤条件的飞机' : '没有找到唯一飞机数据'}
                    </td>
                </tr>
            `;
            return;
        }

        tbody.innerHTML = aircraft.map(plane => `
            <tr>
                <td>
                    ${plane.registration ?
                        `<a href="/aircraft/${plane.registration}" class="text-decoration-none"><strong>${plane.registration}</strong></a>` :
                        '<strong>未知</strong>'
                    }
                    ${plane.country_of_registration ? `<br><small class="text-muted">${plane.country_of_registration}</small>` : ''}
                </td>
                <td><code>${plane.hex || '未知'}</code></td>
                <td>-</td>
                <td>${plane.aircraft_type || '-'}</td>
                <td>
                    <span class="badge ${plane.is_military ? 'military-badge' : 'civilian-badge'}">
                        ${plane.is_military ? '军用' : '民用'}
                    </span>
                </td>
                <td colspan="3">-</td>
                <td>-</td>
                <td>
                    <div class="btn-group btn-group-sm">
                        ${plane.registration ? `
                            <button class="btn btn-outline-success" onclick="tracker.showStaticInfoModal('${plane.registration}')" title="查看档案">
                                <i class="fas fa-id-card"></i>
                            </button>
                            <button class="btn btn-outline-primary" onclick="tracker.showTrackModal('${plane.registration}')" title="查看轨迹">
                                <i class="fas fa-route"></i>
                            </button>
                        ` : ''}
                    </div>
                </td>
            </tr>
        `).join('');
    }

    /**
     * 更新地图显示
     */
    updateMap(aircraft) {
        this.clearMap();

        aircraft.forEach(plane => {
            if (plane.lat && plane.lon) {
                const marker = this.createAircraftMarker(plane);
                this.markers.push(marker);
                marker.addTo(this.map);
            }
        });

        if (this.markers.length > 0) {
            this.fitMapBounds();
        }
    }

    /**
     * 创建飞机标记
     */
    createAircraftMarker(plane) {
        const lat = parseFloat(plane.lat);
        const lon = parseFloat(plane.lon);

        // 确定标记颜色
        let color = '#007bff'; // 默认蓝色（民用）
        if (plane.is_military) {
            color = '#dc3545'; // 红色（军用）
        } else if (plane.is_interesting) {
            color = '#ffc107'; // 黄色（特殊）
        }

        // 创建自定义图标
        const icon = L.divIcon({
            className: 'aircraft-marker',
            html: `<div style="background-color: ${color}; width: 12px; height: 12px; border-radius: 50%; border: 2px solid white;"></div>`,
            iconSize: [16, 16],
            iconAnchor: [8, 8]
        });

        const marker = L.marker([lat, lon], { icon });

        // 弹出窗口内容
        const popupContent = `
            <div class="aircraft-popup">
                <h6><strong>${plane.r || plane.hex || '未知飞机'}</strong></h6>
                ${plane.flight ? `<p><strong>航班:</strong> ${plane.flight.trim()}</p>` : ''}
                ${plane.t ? `<p><strong>机型:</strong> ${plane.t}</p>` : ''}
                <p><strong>类型:</strong> <span class="badge ${plane.is_military ? 'military-badge' : 'civilian-badge'}">${plane.is_military ? '军用' : '民用'}</span></p>
                <p><strong>位置:</strong> ${lat.toFixed(4)}, ${lon.toFixed(4)}</p>
                ${plane.alt_baro ? `<p><strong>高度:</strong> ${plane.alt_baro} ft</p>` : ''}
                ${plane.gs ? `<p><strong>速度:</strong> ${plane.gs.toFixed(0)} kts</p>` : ''}
                ${plane.current_country ? `<p><strong>当前国家:</strong> ${plane.current_country}</p>` : ''}
                ${plane.timestamp ? `<p><strong>时间:</strong> ${plane.timestamp} (北京时间)</p>` : ''}
                ${plane.r ? `
                    <div class="mt-2">
                        <button class="btn btn-primary btn-sm" onclick="tracker.showTrackModal('${plane.r}')">
                            <i class="fas fa-route"></i> 查看轨迹
                        </button>
                    </div>
                ` : ''}
            </div>
        `;

        marker.bindPopup(popupContent);
        return marker;
    }

    /**
     * 清空地图
     */
    clearMap() {
        this.markers.forEach(marker => {
            this.map.removeLayer(marker);
        });
        this.markers = [];
    }

    /**
     * 调整地图视图以适应所有标记
     */
    fitMapBounds() {
        if (this.markers.length > 0) {
            const group = new L.featureGroup(this.markers);
            this.map.fitBounds(group.getBounds().pad(0.1));
        }
    }

    /**
     * 聚焦到地图上的特定位置
     */
    focusOnMap(lat, lon) {
        this.map.setView([lat, lon], 10);
    }

    /**
     * 显示轨迹模态框
     */
    showTrackModal(registration) {
        this.currentRegistration = registration;
        document.getElementById('modalAircraftInfo').textContent = registration;
        
        const modal = new bootstrap.Modal(document.getElementById('trackModal'));
        modal.show();
        
        // 延长等待时间，确保模态框完全显示后再初始化地图和加载轨迹
        setTimeout(() => {
            this.initializeTrackMap();
            setTimeout(() => {
                if (this.trackMap) {
                    this.trackMap.invalidateSize();
                    this.loadAircraftTrack();
                } else {
                    console.error('轨迹地图初始化失败');
                }
            }, 200);
        }, 800);
    }

    /**
     * 加载飞机轨迹
     */
    async loadAircraftTrack() {
        if (!this.currentRegistration) return;

        try {
            this.showTrackLoading();
            
            const startDate = document.getElementById('trackStartDate').value;
            const limit = document.getElementById('trackLimit').value;
            
            let url = `/api/aircraft/tracks/${this.currentRegistration}?limit=${limit}`;
            if (startDate) {
                url += `&start_time=${startDate}`;
            }

            console.log('请求轨迹URL:', url);
            const response = await fetch(url);
            const data = await response.json();
            
            console.log('轨迹API响应:', data);

            if (data.success) {
                console.log(`获取到轨迹数据，共 ${data.tracks ? data.tracks.length : 0} 个点`);
                this.displayTrack(data.tracks);
                this.showTrackInfo(data.tracks);
            } else {
                console.error('轨迹加载失败:', data.error);
                this.showError('加载轨迹失败: ' + data.error);
            }
        } catch (error) {
            this.showError('网络错误: ' + error.message);
        } finally {
            this.hideTrackLoading();
        }
    }

    /**
     * 显示飞行轨迹
     */
    displayTrack(tracks) {
        console.log('开始显示轨迹，数据:', tracks);
        
        // 清除现有轨迹
        this.clearTrack();

        if (!tracks || tracks.length === 0) {
            console.log('没有轨迹数据');
            this.showTrackError('没有找到轨迹数据');
            return;
        }

        console.log(`准备显示 ${tracks.length} 个轨迹点`);

        // 检查轨迹地图是否已初始化
        if (!this.trackMap) {
            console.error('轨迹地图未初始化');
            this.showTrackError('地图未初始化');
            return;
        }

        // 创建轨迹线，过滤无效坐标
        const validTracks = tracks.filter(point => 
            point.lat !== null && point.lon !== null && 
            !isNaN(point.lat) && !isNaN(point.lon)
        );
        
        if (validTracks.length === 0) {
            console.log('没有有效的轨迹坐标');
            this.showTrackError('轨迹坐标无效');
            return;
        }

        const trackPoints = validTracks.map(point => [point.lat, point.lon]);
        console.log('轨迹点坐标:', trackPoints);
        
        this.currentTrackLine = L.polyline(trackPoints, {
            color: '#007bff',
            weight: 3,
            opacity: 0.8
        }).addTo(this.trackMap);
        
        console.log('轨迹线已添加到地图');

        // 添加起点和终点标记
        if (tracks.length > 0) {
            const startPoint = tracks[tracks.length - 1]; // 最早的点
            const endPoint = tracks[0]; // 最新的点

            // 起点标记（绿色）
            const startMarker = L.marker([startPoint.lat, startPoint.lon], {
                icon: L.divIcon({
                    className: 'track-marker start',
                    html: '<div style="background-color: #28a745; width: 12px; height: 12px; border-radius: 50%; border: 2px solid white;"></div>',
                    iconSize: [16, 16],
                    iconAnchor: [8, 8]
                })
            });

            startMarker.bindPopup(`
                <strong>起点</strong><br>
                时间: ${startPoint.timestamp_beijing || new Date(startPoint.timestamp * 1000).toLocaleString('zh-CN')} (北京时间)<br>
                位置: ${startPoint.lat.toFixed(4)}, ${startPoint.lon.toFixed(4)}<br>
                ${startPoint.alt_baro ? `高度: ${startPoint.alt_baro} ft<br>` : ''}
                ${startPoint.ground_speed ? `速度: ${startPoint.ground_speed.toFixed(0)} kts` : ''}
            `);

            // 终点标记（红色）
            const endMarker = L.marker([endPoint.lat, endPoint.lon], {
                icon: L.divIcon({
                    className: 'track-marker end',
                    html: '<div style="background-color: #dc3545; width: 12px; height: 12px; border-radius: 50%; border: 2px solid white;"></div>',
                    iconSize: [16, 16],
                    iconAnchor: [8, 8]
                })
            });

            endMarker.bindPopup(`
                <strong>终点</strong><br>
                时间: ${endPoint.timestamp_beijing || new Date(endPoint.timestamp * 1000).toLocaleString('zh-CN')} (北京时间)<br>
                位置: ${endPoint.lat.toFixed(4)}, ${endPoint.lon.toFixed(4)}<br>
                ${endPoint.alt_baro ? `高度: ${endPoint.alt_baro} ft<br>` : ''}
                ${endPoint.ground_speed ? `速度: ${endPoint.ground_speed.toFixed(0)} kts` : ''}
            `);

            this.trackMarkers.push(startMarker, endMarker);
            startMarker.addTo(this.trackMap);
            endMarker.addTo(this.trackMap);
        }

        // 调整视图
        this.trackMap.fitBounds(this.currentTrackLine.getBounds().pad(0.1));
    }

    /**
     * 显示轨迹信息
     */
    showTrackInfo(tracks) {
        const infoDiv = document.getElementById('trackInfo');
        const detailsSpan = document.getElementById('trackDetails');

        if (tracks.length === 0) {
            infoDiv.classList.add('d-none');
            return;
        }

        const startTime = new Date(tracks[tracks.length - 1].timestamp * 1000);
        const endTime = new Date(tracks[0].timestamp * 1000);
        const duration = (endTime - startTime) / 1000 / 60; // 分钟

        let totalDistance = 0;
        for (let i = 1; i < tracks.length; i++) {
            const prev = tracks[i];
            const curr = tracks[i - 1];
            const dist = this.calculateDistance(prev.lat, prev.lon, curr.lat, curr.lon);
            totalDistance += dist;
        }

        detailsSpan.innerHTML = `
            共 ${tracks.length} 个轨迹点，
            飞行时长约 ${duration.toFixed(0)} 分钟，
            总距离约 ${totalDistance.toFixed(0)} 公里
        `;

        infoDiv.classList.remove('d-none');
    }

    /**
     * 计算两点间距离（公里）
     */
    calculateDistance(lat1, lon1, lat2, lon2) {
        const R = 6371; // 地球半径（公里）
        const dLat = (lat2 - lat1) * Math.PI / 180;
        const dLon = (lon2 - lon1) * Math.PI / 180;
        const a = Math.sin(dLat/2) * Math.sin(dLat/2) +
                  Math.cos(lat1 * Math.PI / 180) * Math.cos(lat2 * Math.PI / 180) *
                  Math.sin(dLon/2) * Math.sin(dLon/2);
        const c = 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1-a));
        return R * c;
    }

    /**
     * 清除轨迹
     */
    clearTrack() {
        if (this.currentTrackLine) {
            this.trackMap.removeLayer(this.currentTrackLine);
            this.currentTrackLine = null;
        }

        this.trackMarkers.forEach(marker => {
            this.trackMap.removeLayer(marker);
        });
        this.trackMarkers = [];

        document.getElementById('trackInfo').classList.add('d-none');
    }

    /**
     * 清空表单
     */
    clearForm() {
        document.getElementById('searchForm').reset();
        document.getElementById('start_date')._flatpickr.clear();
        document.getElementById('end_date')._flatpickr.clear();
    }

    /**
     * 加载统计信息
     */
    async loadStatistics() {
        try {
            const response = await fetch('/api/statistics');
            const data = await response.json();

            if (data.success) {
                this.displayStatistics(data.statistics);
            }
        } catch (error) {
            console.error('加载统计信息失败:', error);
        }
    }


    /**
     * 显示统计信息
     */
    displayStatistics(stats) {
        const container = document.getElementById('statistics');
        
        container.innerHTML = `
            <div class="stat-item">
                <div class="stat-number">${stats.total_snapshots || 0}</div>
                <div class="stat-label">总记录数</div>
            </div>
            <div class="stat-item">
                <div class="stat-number">${stats.recent_snapshots_1h || 0}</div>
                <div class="stat-label">最近1小时</div>
            </div>
            <div class="stat-item">
                <div class="stat-number">${stats.unique_aircraft_total || 0}</div>
                <div class="stat-label">唯一飞机</div>
            </div>
            <div class="stat-item">
                <div class="stat-number">${stats.military_aircraft_total || 0}</div>
                <div class="stat-label">军用飞机</div>
            </div>
        `;
    }

    /**
     * 显示加载状态
     */
    showLoading(message = '加载中...') {
        // 可以添加全局加载指示器
        console.log('Loading:', message);
    }

    /**
     * 隐藏加载状态
     */
    hideLoading() {
        // 隐藏全局加载指示器
        console.log('Loading complete');
    }

    /**
     * 显示轨迹加载状态
     */
    showTrackLoading() {
        const button = document.getElementById('loadTrackBtn');
        button.disabled = true;
        button.innerHTML = '<i class="fas fa-spinner fa-spin"></i> 加载中...';
    }

    /**
     * 隐藏轨迹加载状态
     */
    hideTrackLoading() {
        const button = document.getElementById('loadTrackBtn');
        button.disabled = false;
        button.innerHTML = '<i class="fas fa-sync"></i> 加载';
    }

    /**
     * 显示轨迹错误
     */
    showTrackError(message) {
        document.getElementById('trackMap').innerHTML = `
            <div class="error-state">
                <i class="fas fa-exclamation-triangle"></i>
                <div>${message}</div>
            </div>
        `;
    }

    /**
     * 显示成功消息
     */
    showSuccess(message) {
        this.showMessage(message, 'success');
    }

    /**
     * 显示错误消息
     */
    showError(message) {
        this.showMessage(message, 'danger');
    }

    /**
     * 显示消息
     */
    showMessage(message, type = 'info') {
        // 创建临时提示
        const alert = document.createElement('div');
        alert.className = `alert alert-${type} alert-dismissible fade show position-fixed`;
        alert.style.top = '20px';
        alert.style.right = '20px';
        alert.style.zIndex = '9999';
        alert.style.minWidth = '300px';
        
        alert.innerHTML = `
            ${message}
            <button type="button" class="btn-close" data-bs-dismiss="alert"></button>
        `;
        
        document.body.appendChild(alert);
        
        // 3秒后自动消失
        setTimeout(() => {
            if (alert.parentNode) {
                alert.remove();
            }
        }, 3000);
    }

    /**
     * 切换过滤器显示/隐藏
     */
    toggleFilters() {
        const filtersRow = document.getElementById('tableFilters');
        const toggleBtn = document.getElementById('toggleFilters');
        
        this.filtersVisible = !this.filtersVisible;
        
        if (this.filtersVisible) {
            filtersRow.style.display = 'table-row';
            toggleBtn.classList.add('active');
            toggleBtn.innerHTML = '<i class="fas fa-filter"></i> 隐藏过滤器';
        } else {
            filtersRow.style.display = 'none';
            toggleBtn.classList.remove('active');
            toggleBtn.innerHTML = '<i class="fas fa-filter"></i> 过滤器';
        }
    }

    /**
     * 应用列过滤器
     */
    applyColumnFilter(filterInput) {
        const column = parseInt(filterInput.dataset.column);
        const value = filterInput.value.toLowerCase().trim();
        
        // 更新活跃过滤器
        if (value) {
            this.activeFilters[column] = value;
            filterInput.classList.add('filter-active');
        } else {
            delete this.activeFilters[column];
            filterInput.classList.remove('filter-active');
        }
        
        // 应用所有过滤器
        this.applyAllFilters();
    }

    /**
     * 应用所有过滤器
     */
    applyAllFilters() {
        if (Object.keys(this.activeFilters).length === 0) {
            // 没有过滤器时显示所有数据
            this.filteredData = [...this.originalData];
        } else {
            // 应用所有过滤器
            this.filteredData = this.originalData.filter(plane => {
                return Object.entries(this.activeFilters).every(([column, filterValue]) => {
                    const cellValue = this.getCellValue(plane, parseInt(column));
                    return cellValue.toLowerCase().includes(filterValue);
                });
            });
        }
        
        // 判断是否为唯一飞机列表（检查是否有位置信息）
        const isUniqueAircraftList = this.originalData.length > 0 && 
                                   this.originalData.every(plane => plane.lat === null && plane.lon === null);
        
        if (isUniqueAircraftList) {
            this.renderUniqueTable(this.filteredData);
        } else {
            this.renderTable(this.filteredData);
            // 更新地图（只有在有位置信息时才更新地图）
            this.updateMap(this.filteredData);
        }
    }

    /**
     * 获取单元格值
     */
    getCellValue(plane, column) {
        switch (column) {
            case 0: // 注册号
                return (plane.r || '未知').toString();
            case 1: // ICAO
                return (plane.hex || '未知').toString();
            case 2: // 航班号
                return (plane.flight ? plane.flight.trim() : '-').toString();
            case 3: // 机型
                return (plane.t || '-').toString();
            case 4: // 类型
                return plane.is_military ? '军用' : '民用';
            case 5: // 位置
                if (plane.lat && plane.lon) {
                    const location = `${plane.lat.toFixed(4)}, ${plane.lon.toFixed(4)}`;
                    const country = plane.current_country ? ` ${plane.current_country}` : '';
                    return location + country;
                }
                return '-';
            case 6: // 高度
                return plane.alt_baro ? `${plane.alt_baro} ft` : '-';
            case 7: // 速度
                return plane.gs ? `${plane.gs.toFixed(0)} kts` : '-';
            case 8: // 时间
                return plane.timestamp ? `${plane.timestamp} (北京时间)` : '-';
            default:
                return '';
        }
    }

    /**
     * 清空所有过滤器
     */
    clearAllFilters() {
        this.activeFilters = {};

        // 清空所有过滤器输入框
        document.querySelectorAll('.column-filter').forEach(input => {
            input.value = '';
            input.classList.remove('filter-active');
        });

        // 重新显示所有数据
        this.filteredData = [...this.originalData];

        // 判断表格类型并相应渲染
        const isUniqueAircraftList = this.originalData.length > 0 &&
                                   this.originalData.every(plane => plane.lat === null && plane.lon === null);

        if (isUniqueAircraftList) {
            this.renderUniqueTable(this.filteredData);
        } else {
            this.renderTable(this.filteredData);
            this.updateMap(this.filteredData);
        }
    }

    /**
     * 加载搜索结果中飞机的静态信息
     */
    async loadStaticInfoForResults(aircraft) {
        const listDiv = document.getElementById('staticInfoList');
        const countBadge = document.getElementById('staticInfoCount');

        // 获取所有唯一的注册号
        const registrations = [...new Set(aircraft
            .map(a => a.r || a.registration)
            .filter(r => r && r !== '未知' && r !== 'None')
        )];

        if (registrations.length === 0) {
            listDiv.innerHTML = `
                <div class="text-center text-muted py-3">
                    <i class="fas fa-search"></i>
                    <div>搜索飞机后显示对应档案</div>
                </div>
            `;
            countBadge.textContent = '0';
            countBadge.className = 'badge bg-secondary';
            return;
        }

        // 显示加载状态
        listDiv.innerHTML = `
            <div class="text-center py-3">
                <div class="spinner-border spinner-border-sm" role="status"></div>
                <small>查询档案信息...</small>
            </div>
        `;

        try {
            // 使用批量接口获取指定注册号的静态信息
            const response = await fetch('/api/aircraft/static/batch', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({ registrations: registrations })
            });
            const data = await response.json();

            if (data.success) {
                // 批量接口直接返回匹配的飞机
                const matchedAircraft = data.data;

                // 更新计数
                countBadge.textContent = matchedAircraft.length;
                countBadge.className = matchedAircraft.length > 0 ? 'badge bg-success' : 'badge bg-secondary';

                if (matchedAircraft.length > 0) {
                    listDiv.innerHTML = matchedAircraft.map(info => `
                        <div class="card mb-2 static-info-card" onclick="tracker.showStaticInfoModal('${info.registration}')" style="cursor: pointer;">
                            <div class="card-body p-2">
                                <div class="d-flex justify-content-between align-items-start">
                                    <div>
                                        <strong>${info.registration}</strong>
                                        <span class="text-muted ms-2">${info.aircraft_model || ''}</span>
                                        <div class="small text-muted">${info.owner || '未知所有者'}</div>
                                    </div>
                                    <div class="text-end">
                                        ${info.is_military ? '<span class="badge bg-danger">军用</span>' : ''}
                                        ${info.is_government ? '<span class="badge bg-info">政府</span>' : ''}
                                        ${info.is_vip ? '<span class="badge bg-warning">VIP</span>' : ''}
                                    </div>
                                </div>
                            </div>
                        </div>
                    `).join('');
                } else {
                    // 没有匹配的档案，显示搜索结果中的飞机（提示可以查看）
                    listDiv.innerHTML = `
                        <div class="text-center text-muted py-2">
                            <i class="fas fa-info-circle"></i>
                            <div class="small">搜索到 ${registrations.length} 架飞机，暂无档案记录</div>
                        </div>
                    `;
                }
            }
        } catch (error) {
            console.error('加载飞机档案失败:', error);
            listDiv.innerHTML = `
                <div class="text-center text-danger py-2">
                    <i class="fas fa-exclamation-circle"></i>
                    <div class="small">加载失败</div>
                </div>
            `;
            countBadge.textContent = '!';
            countBadge.className = 'badge bg-danger';
        }
    }

    /**
     * 显示飞机静态信息模态框
     */
    async showStaticInfoModal(registration) {
        const modal = new bootstrap.Modal(document.getElementById('staticInfoModal'));
        const titleSpan = document.getElementById('staticInfoRegistration');
        const contentDiv = document.getElementById('staticInfoContent');

        titleSpan.textContent = registration;
        contentDiv.innerHTML = `
            <div class="text-center py-4">
                <div class="spinner-border" role="status"></div>
                <div>加载档案信息...</div>
            </div>
        `;

        modal.show();

        try {
            // 并行获取静态信息和飞机图片
            const [staticResponse, imagesResponse] = await Promise.all([
                fetch(`/api/aircraft/static/${registration}`),
                fetch(`/api/aircraft/${registration}/images`)
            ]);

            const staticData = await staticResponse.json();
            const imagesData = await imagesResponse.json();

            if (staticData.success) {
                const info = staticData.data;
                const images = imagesData.success ? imagesData.images : [];
                contentDiv.innerHTML = this.renderStaticInfoContent(info, images);
            } else {
                contentDiv.innerHTML = `
                    <div class="alert alert-warning">
                        <i class="fas fa-exclamation-triangle"></i> ${staticData.error || '未找到档案信息'}
                    </div>
                `;
            }
        } catch (error) {
            contentDiv.innerHTML = `
                <div class="alert alert-danger">
                    <i class="fas fa-exclamation-circle"></i> 加载失败: ${error.message}
                </div>
            `;
        }
    }

    /**
     * 渲染静态信息内容
     */
    renderStaticInfoContent(info, images = []) {
        // 构建标签HTML
        let tagsHtml = '';
        if (info.tags && Array.isArray(info.tags) && info.tags.length > 0) {
            tagsHtml = info.tags.map(tag => `<span class="badge bg-secondary me-1">${tag}</span>`).join('');
        }

        // 构建分类徽章
        let badgesHtml = '';
        if (info.is_military) badgesHtml += '<span class="badge bg-danger me-1">军用</span>';
        if (info.is_government) badgesHtml += '<span class="badge bg-info me-1">政府</span>';
        if (info.is_vip) badgesHtml += '<span class="badge bg-warning me-1">VIP</span>';

        // 构建图片HTML - 使用缩略图，点击查看全尺寸
        let imagesHtml = '';
        if (images && images.length > 0) {
            imagesHtml = `
                <div class="mb-3">
                    <h6 class="border-bottom pb-2"><i class="fas fa-images"></i> 飞机图片 <small class="text-muted">(点击查看大图)</small></h6>
                    <div class="row g-2">
                        ${images.map((url, index) => {
                            // 将全尺寸图片URL转换为缩略图URL
                            const thumbUrl = url.replace('/jetphotos_images/', '/jetphotos_thumbnails/')
                                               .replace('_full_', '_thumb_');
                            return `
                            <div class="col-${images.length === 1 ? '12' : images.length === 2 ? '6' : '4'}">
                                <img src="${thumbUrl}" class="img-fluid rounded shadow-sm aircraft-thumbnail"
                                     alt="飞机图片 ${index + 1}"
                                     data-full-url="${url}"
                                     style="width: 100%; height: ${images.length === 1 ? '250px' : '150px'}; object-fit: cover; cursor: pointer;"
                                     onclick="tracker.showImageLightbox('${url}')"
                                     onerror="this.src='${url}'; this.onerror=function(){this.style.display='none';};">
                            </div>
                        `}).join('')}
                    </div>
                </div>
            `;
        }

        return `
            ${imagesHtml}

            <div class="row">
                <div class="col-md-6">
                    <h6 class="border-bottom pb-2"><i class="fas fa-plane"></i> 基本信息</h6>
                    <table class="table table-sm">
                        <tr><th width="40%">注册号</th><td><strong>${info.registration || '-'}</strong></td></tr>
                        <tr><th>ICAO Hex</th><td><code>${info.hex || '-'}</code></td></tr>
                        <tr><th>机型</th><td>${info.aircraft_model || '-'}</td></tr>
                        <tr><th>制造商</th><td>${info.manufacturer || '-'}</td></tr>
                        <tr><th>序列号</th><td>${info.serial_number || '-'}</td></tr>
                        <tr><th>制造年份</th><td>${info.year_built || '-'}</td></tr>
                        <tr><th>注册国</th><td>${info.country || '-'}</td></tr>
                    </table>
                </div>
                <div class="col-md-6">
                    <h6 class="border-bottom pb-2"><i class="fas fa-building"></i> 归属信息</h6>
                    <table class="table table-sm">
                        <tr><th width="40%">所有者</th><td>${info.owner || '-'}</td></tr>
                        <tr><th>运营方</th><td>${info.operator || '-'}</td></tr>
                        <tr><th>分类</th><td>${badgesHtml || '-'}</td></tr>
                        <tr><th>标签</th><td>${tagsHtml || '-'}</td></tr>
                    </table>
                </div>
            </div>

            ${info.summary ? `
                <div class="mt-3">
                    <h6 class="border-bottom pb-2"><i class="fas fa-file-alt"></i> 摘要</h6>
                    <p class="text-muted" style="white-space: pre-wrap;">${info.summary}</p>
                </div>
            ` : ''}

            ${info.previous_owners ? `
                <div class="mt-3">
                    <h6 class="border-bottom pb-2"><i class="fas fa-history"></i> 历史所有者</h6>
                    <p class="text-muted">${typeof info.previous_owners === 'object' ? JSON.stringify(info.previous_owners, null, 2) : info.previous_owners}</p>
                </div>
            ` : ''}

            <div class="mt-3">
                <h6 class="border-bottom pb-2"><i class="fas fa-clock"></i> 元数据</h6>
                <div class="row">
                    <div class="col-md-4">
                        <small class="text-muted">创建时间</small>
                        <div>${info.created_at || '-'}</div>
                    </div>
                    <div class="col-md-4">
                        <small class="text-muted">更新时间</small>
                        <div>${info.updated_at || '-'}</div>
                    </div>
                    <div class="col-md-4">
                        <small class="text-muted">访问次数</small>
                        <div>${info.hit_count || 0}</div>
                    </div>
                </div>
            </div>
        `;
    }

    /**
     * 显示图片灯箱
     */
    showImageLightbox(imageUrl) {
        const lightboxModal = document.getElementById('imageLightboxModal');
        const lightboxImage = document.getElementById('lightboxImage');

        if (lightboxModal && lightboxImage) {
            lightboxImage.src = imageUrl;
            const modal = new bootstrap.Modal(lightboxModal);
            modal.show();
        }
    }
}

// 初始化应用
let tracker;
document.addEventListener('DOMContentLoaded', () => {
    tracker = new AircraftTracker();
});
