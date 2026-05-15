/* global ol, geojsonUrl */
'use strict';

const MAP_REAL_SIZE = 2200000;
const MAP_REAL_X_LEFT = -1280000;
const MAP_REAL_Y_TOP = -320000;

const customProjection = new ol.proj.Projection({
    code: 'customData',
    units: 'pixels',
    extent: [0, 0, MAP_REAL_SIZE, MAP_REAL_SIZE],
    worldExtent: [0, 0, MAP_REAL_SIZE, MAP_REAL_SIZE],
});
ol.proj.addProjection(customProjection);

const baseLayer = new ol.layer.Tile({
    source: new ol.source.XYZ({
        url: "https://www.aseanmotorclub.com/map_tiles/718/{z}_{x}_{y}.avif",
        projection: customProjection,
        minZoom: 2,
        maxZoom: 5,
        wrapX: false,
    }),
});

const map = new ol.Map({
    target: 'replay-map',
    layers: [baseLayer],
    view: new ol.View({
        projection: customProjection,
        center: ol.extent.getCenter(customProjection.getExtent()),
        zoom: 3,
        minZoom: 2,
        maxZoom: 8,
        extent: [
            0 - MAP_REAL_SIZE,
            0 - MAP_REAL_SIZE,
            MAP_REAL_SIZE + MAP_REAL_SIZE,
            MAP_REAL_SIZE + MAP_REAL_SIZE,
        ],
    }),
});

const CHAR_COLORS = [
    '#e6194b', '#3cb44b', '#4363d8', '#f58231', '#911eb4',
    '#42d4f4', '#f032e6', '#bfef45', '#fabed4', '#469990',
    '#dcbeff', '#9A6324', '#800000', '#aaffc3', '#808000',
    '#ffd8b1', '#000075', '#a9a9a9', '#e6beff', '#fffac8',
];

function getCharacterColor(charId) {
    return CHAR_COLORS[charId % CHAR_COLORS.length];
}

const trailSource = new ol.source.Vector();
const trailLayer = new ol.layer.Vector({
    source: trailSource,
    style: function(feature) {
        var charId = feature.get('character_id');
        var color = getCharacterColor(charId);
        if (feature.getGeometry().getType() === 'LineString') {
            return new ol.style.Style({
                stroke: new ol.style.Stroke({ color: color, width: 2, opacity: 0.6 }),
            });
        }
        return new ol.style.Style({
            image: new ol.style.Circle({
                radius: 7,
                fill: new ol.style.Fill({ color: color }),
                stroke: new ol.style.Stroke({ color: '#fff', width: 2 }),
            }),
            text: new ol.style.Text({
                text: feature.get('character_name') || '',
                offsetY: -16,
                font: '11px sans-serif',
                fill: new ol.style.Fill({ color: '#333' }),
                stroke: new ol.style.Stroke({ color: '#fff', width: 3 }),
            }),
        });
    },
});
map.addLayer(trailLayer);

var popup = document.getElementById('popup');
var popupContent = document.getElementById('popup-content');
var popupCloser = document.getElementById('popup-closer');

var overlay = new ol.Overlay({
    element: popup,
    autoPan: true,
    autoPanAnimation: { duration: 250 },
});
map.addOverlay(overlay);

popupCloser.addEventListener('click', function(ev) {
    ev.preventDefault();
    overlay.setPosition(undefined);
});

map.on('singleclick', function(ev) {
    var feature = map.forEachFeatureAtPixel(ev.pixel, function(f) { return f; });
    if (feature && feature.getGeometry().getType() === 'Point') {
        var coords = feature.getGeometry().getCoordinates();
        var name = feature.get('character_name') || '';
        var ts = feature.get('timestamp') || '';
        popupContent.innerHTML = '<strong>' + name + '</strong><br><span style="font-size:11px;color:#666;">' + ts + '</span>';
        overlay.setPosition(coords);
    } else {
        overlay.setPosition(undefined);
    }
});

map.on('pointermove', function(ev) {
    var hit = map.hasFeatureAtPixel(ev.pixel);
    map.getTargetElement().style.cursor = hit ? 'pointer' : '';
});

var characterNames = {};
var characterTimeline = {};
var uniqueTimestamps = [];
var uniqueTimestampsMs = [];
var isPlaying = false;
var isReversed = false;
var playbackSpeed = 1;
var currentFrame = 0;
var playInterval = null;
var TICK_INTERVAL_MS = 50;
var playbackStartWall = 0;
var playbackCursorMs = 0;

var endTimeInput = document.getElementById('end-time');
var durationSlider = document.getElementById('duration-slider');
var durationLabel = document.getElementById('duration-label');
var btnEndNow = document.getElementById('btn-end-now');
var rangeText = document.getElementById('range-text');
var btnLoad = document.getElementById('btn-load');
var btnPlay = document.getElementById('btn-play');
var btnReverse = document.getElementById('btn-reverse');
var btnRewind = document.getElementById('btn-rewind');
var btnForward = document.getElementById('btn-forward');
var speedSelect = document.getElementById('speed-select');
var timelineSlider = document.getElementById('timeline-slider');
var timelineLabel = document.getElementById('timeline-label');
var loadingIndicator = document.getElementById('loading-indicator');
var legendDiv = document.getElementById('replay-legend');

function setToTimezoneOffset(d) {
    var offset = d.getTimezoneOffset() * 60000;
    var local = new Date(d.getTime() - offset);
    return local.toISOString().slice(0, 16);
}

function formatDuration(minutes) {
    var h = Math.floor(minutes / 60);
    var m = minutes % 60;
    if (h === 0) return m + 'm';
    if (m === 0) return h + 'h';
    return h + 'h ' + m + 'm';
}

function computeRange() {
    var endDt = new Date(endTimeInput.value);
    var minutes = parseInt(durationSlider.value);
    var startDt = new Date(endDt.getTime() - minutes * 60000);
    return { startDt: startDt, endDt: endDt };
}

function updateRangeDisplay() {
    durationLabel.textContent = formatDuration(parseInt(durationSlider.value));
    if (!endTimeInput.value) { rangeText.textContent = '\u2014'; return; }
    var r = computeRange();
    rangeText.textContent = r.startDt.toLocaleString() + '  \u2192  ' + r.endDt.toLocaleString();
}

btnEndNow.addEventListener('click', function() {
    endTimeInput.value = setToTimezoneOffset(new Date());
    updateRangeDisplay();
});
endTimeInput.addEventListener('input', updateRangeDisplay);
durationSlider.addEventListener('input', updateRangeDisplay);

function readUrlParams() {
    var params = new URLSearchParams(window.location.search);
    var end = params.get('end');
    var duration = params.get('duration');
    if (end) {
        endTimeInput.value = end;
    } else {
        endTimeInput.value = setToTimezoneOffset(new Date());
    }
    if (duration) {
        var dur = parseInt(duration, 10);
        if (dur >= 5 && dur <= 360) {
            durationSlider.value = dur;
        }
    }
    updateRangeDisplay();
}

function updateUrlParams() {
    var params = new URLSearchParams(window.location.search);
    params.set('end', endTimeInput.value);
    params.set('duration', durationSlider.value);
    var newUrl = window.location.pathname + '?' + params.toString();
    window.history.replaceState(null, '', newUrl);
}

readUrlParams();

var initialParams = new URLSearchParams(window.location.search);
if (initialParams.has('end')) {
    loadData();
}

function toLocalISOString(d) {
    var pad = function(n) { return n < 10 ? '0' + n : n; };
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) +
        'T' + pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
}

function bisectRight(arr, ts) {
    var lo = 0, hi = arr.length;
    while (lo < hi) {
        var mid = (lo + hi) >> 1;
        if (arr[mid] <= ts) lo = mid + 1;
        else hi = mid;
    }
    return lo;
}

btnLoad.addEventListener('click', function() {
    if (!endTimeInput.value) {
        alert('Please set an end time.');
        return;
    }

    updateUrlParams();
    loadData();
});

function loadData() {
    stopPlayback();
    characterNames = {};
    characterTimeline = {};
    uniqueTimestamps = [];
    currentFrame = 0;
    trailSource.clear();
    legendDiv.innerHTML = '';

    var r = computeRange();
    var startISO = toLocalISOString(r.startDt);
    var endISO = toLocalISOString(r.endDt);

    loadingIndicator.style.display = 'inline';

    var url = geojsonUrl + '?start=' + encodeURIComponent(startISO) + '&end=' + encodeURIComponent(endISO);

    fetch(url)
        .then(function(response) {
            if (!response.ok) return response.json().then(function(err) { throw new Error(err.error || 'Load failed'); });
            return response.json();
        })
        .then(function(geojson) {
            var format = new ol.format.GeoJSON();
            var features = format.readFeatures(geojson, {
                dataProjection: customProjection,
                featureProjection: customProjection,
            });

            if (features.length === 0) {
                timelineLabel.textContent = 'No locations found in this time range.';
                loadingIndicator.style.display = 'none';
                return;
            }

            var tsSet = new Set();
            features.forEach(function(f) {
                var cid = f.get('character_id');
                var ts = f.get('timestamp');
                var coords = f.getGeometry().getCoordinates();
                if (!characterTimeline[cid]) characterTimeline[cid] = [];
                characterTimeline[cid].push({ ts: ts, tsMs: new Date(ts).getTime(), coords: coords });
                if (!characterNames[cid]) characterNames[cid] = f.get('character_name');
                tsSet.add(ts);
            });

            Object.keys(characterTimeline).forEach(function(cid) {
                characterTimeline[cid].sort(function(a, b) { return a.tsMs - b.tsMs; });
            });

            uniqueTimestamps = Array.from(tsSet).sort();
            uniqueTimestampsMs = uniqueTimestamps.map(function(ts) { return new Date(ts).getTime(); });
            var charIds = Object.keys(characterTimeline).map(Number).sort(function(a, b) { return a - b; });

            charIds.forEach(function(cid) {
                var color = getCharacterColor(cid);
                var name = characterNames[cid] || ('Char ' + cid);
                var item = document.createElement('span');
                item.className = 'legend-item';
                item.innerHTML = '<span class="legend-dot" style="background:' + color + ';"></span>' + name;
                legendDiv.appendChild(item);
            });

            timelineSlider.min = 0;
            timelineSlider.max = uniqueTimestamps.length - 1;
            timelineSlider.value = 0;
            timelineSlider.disabled = false;
            currentFrame = 0;

            updateTimelineLabel();
            showFrame(0);

            btnPlay.disabled = false;
            btnReverse.disabled = false;
            btnRewind.disabled = false;
            btnForward.disabled = false;
            speedSelect.disabled = false;

            var allCoords = [];
            Object.keys(characterTimeline).forEach(function(cid) {
                characterTimeline[cid].forEach(function(entry) {
                    allCoords.push(entry.coords);
                });
            });
            if (allCoords.length > 0) {
                var extent = allCoords.reduce(function(ext, coords) {
                    ol.extent.extend(ext, new ol.geom.Point(coords).getExtent());
                    return ext;
                }, ol.extent.createEmpty());
                map.getView().fit(extent, { minResolution: 1, padding: [40, 40, 40, 40] });
            }

            loadingIndicator.style.display = 'none';
        })
        .catch(function(err) {
            alert('Error loading data: ' + err.message);
            loadingIndicator.style.display = 'none';
        });
}

var TELEPORT_THRESHOLD = 10000;
var showTrailsCheckbox = document.getElementById('show-trails');
var showTrails = true;

showTrailsCheckbox.addEventListener('change', function() {
    showTrails = showTrailsCheckbox.checked;
    if (uniqueTimestamps.length > 0) showFrame(currentFrame);
});

function olDistance(c1, c2) {
    var dx = c1[0] - c2[0];
    var dy = c1[1] - c2[1];
    return Math.sqrt(dx * dx + dy * dy);
}

function showFrame(frameIndex) {
    trailSource.clear();
    var frameTsMs = new Date(uniqueTimestamps[frameIndex]).getTime();

    Object.keys(characterTimeline).forEach(function(cid) {
        var entries = characterTimeline[cid];
        var tsMsArr = entries.map(function(e) { return e.tsMs; });
        var idx = bisectRight(tsMsArr, frameTsMs);
        if (idx === 0) return;

        var visibleEntries = entries.slice(0, idx);
        var latestEntry = visibleEntries[visibleEntries.length - 1];
        var charId = parseInt(cid);

        var segments = [];
        var current = [visibleEntries[0].coords];
        for (var i = 1; i < visibleEntries.length; i++) {
            if (olDistance(visibleEntries[i - 1].coords, visibleEntries[i].coords) > TELEPORT_THRESHOLD) {
                if (current.length >= 2) segments.push(current);
                current = [visibleEntries[i].coords];
            } else {
                current.push(visibleEntries[i].coords);
            }
        }
        if (current.length >= 2) segments.push(current);

        if (showTrails) {
            segments.forEach(function(seg) {
                trailSource.addFeature(new ol.Feature({
                    geometry: new ol.geom.LineString(seg),
                    character_id: charId,
                }));
            });
        }

        trailSource.addFeature(new ol.Feature({
            geometry: new ol.geom.Point(latestEntry.coords),
            character_id: charId,
            character_name: characterNames[cid] || '',
            timestamp: latestEntry.ts,
        }));
    });
}

function updateTimelineLabel() {
    if (uniqueTimestamps.length === 0) {
        timelineLabel.textContent = 'No data loaded';
        return;
    }
    timelineLabel.textContent = uniqueTimestamps[currentFrame] +
        '  (' + (currentFrame + 1) + ' / ' + uniqueTimestamps.length + ')';
}

function stopPlayback() {
    if (playInterval) {
        clearInterval(playInterval);
        playInterval = null;
    }
    isPlaying = false;
    btnPlay.textContent = '\u25B6';
    btnPlay.classList.remove('active');
    btnReverse.classList.remove('active');
}

function findFrameForMs(targetMs) {
    // Find the last frame whose timestamp <= targetMs
    var lo = 0, hi = uniqueTimestampsMs.length;
    while (lo < hi) {
        var mid = (lo + hi) >> 1;
        if (uniqueTimestampsMs[mid] <= targetMs) lo = mid + 1;
        else hi = mid;
    }
    return Math.max(0, lo - 1);
}

function startPlayback() {
    stopPlayback();
    isPlaying = true;
    if (isReversed) {
        btnReverse.classList.add('active');
    } else {
        btnPlay.classList.add('active');
        btnPlay.textContent = '\u23F8';
    }

    playbackStartWall = performance.now();
    playbackCursorMs = uniqueTimestampsMs[currentFrame];

    playInterval = setInterval(function() {
        var elapsedWall = performance.now() - playbackStartWall;
        var deltaMs = elapsedWall * playbackSpeed;
        if (isReversed) {
            playbackCursorMs -= deltaMs;
            if (playbackCursorMs <= uniqueTimestampsMs[0]) {
                playbackCursorMs = uniqueTimestampsMs[0];
                currentFrame = 0;
                timelineSlider.value = 0;
                showFrame(0);
                updateTimelineLabel();
                stopPlayback();
                return;
            }
        } else {
            playbackCursorMs += deltaMs;
            if (playbackCursorMs >= uniqueTimestampsMs[uniqueTimestampsMs.length - 1]) {
                currentFrame = uniqueTimestamps.length - 1;
                timelineSlider.value = currentFrame;
                showFrame(currentFrame);
                updateTimelineLabel();
                stopPlayback();
                return;
            }
        }
        playbackStartWall = performance.now();
        currentFrame = findFrameForMs(playbackCursorMs);
        timelineSlider.value = currentFrame;
        showFrame(currentFrame);
        updateTimelineLabel();
    }, TICK_INTERVAL_MS);
}

btnPlay.addEventListener('click', function() {
    if (isPlaying && !isReversed) {
        stopPlayback();
        return;
    }
    isReversed = false;
    if (currentFrame >= uniqueTimestamps.length - 1) {
        currentFrame = 0;
    }
    startPlayback();
});

btnReverse.addEventListener('click', function() {
    if (isPlaying && isReversed) {
        stopPlayback();
        return;
    }
    isReversed = true;
    if (currentFrame <= 0) {
        currentFrame = uniqueTimestamps.length - 1;
    }
    startPlayback();
});

btnRewind.addEventListener('click', function() {
    stopPlayback();
    currentFrame = 0;
    playbackCursorMs = uniqueTimestampsMs[0] || 0;
    isReversed = false;
    timelineSlider.value = 0;
    showFrame(0);
    updateTimelineLabel();
});

btnForward.addEventListener('click', function() {
    stopPlayback();
    currentFrame = uniqueTimestamps.length - 1;
    playbackCursorMs = uniqueTimestampsMs[currentFrame] || 0;
    isReversed = false;
    timelineSlider.value = currentFrame;
    showFrame(currentFrame);
    updateTimelineLabel();
});

speedSelect.addEventListener('change', function() {
    playbackSpeed = parseFloat(speedSelect.value);
    if (isPlaying) {
        startPlayback();
    }
});

timelineSlider.addEventListener('input', function() {
    currentFrame = parseInt(timelineSlider.value);
    playbackCursorMs = uniqueTimestampsMs[currentFrame] || 0;
    playbackStartWall = performance.now();
    showFrame(currentFrame);
    updateTimelineLabel();
});
