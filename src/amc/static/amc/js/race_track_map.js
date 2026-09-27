/* global ol */
'use strict';

var MAP_REAL_SIZE = 2200000;
var MAP_REAL_X_LEFT = -1280000;
var MAP_REAL_Y_TOP = -320000;

function gameToMap(gameX, gameY) {
    return [
        gameX - MAP_REAL_X_LEFT,
        -(gameY - MAP_REAL_Y_TOP) + MAP_REAL_SIZE,
    ];
}

var configEl = document.getElementById('racesetup-config');
if (!configEl) {
    document.getElementById('track-map-container').innerHTML =
        '<p style="padding:20px;color:#999;">No config available.</p>';
} else {
    var config = JSON.parse(configEl.textContent);
    var route = (config && config.Route) || {};
    var waypoints = route.Waypoints;

    if (!waypoints || (typeof waypoints === 'object' && !Array.isArray(waypoints) && Object.keys(waypoints).length === 0) || (Array.isArray(waypoints) && waypoints.length === 0)) {
        document.getElementById('track-map-container').innerHTML =
            '<p style="padding:20px;color:#999;">No waypoints in config.</p>';
    } else {
        if (!Array.isArray(waypoints)) {
            waypoints = Object.values(waypoints);
        }

        var customProjection = new ol.proj.Projection({
            code: 'customData',
            units: 'pixels',
            extent: [0, 0, MAP_REAL_SIZE, MAP_REAL_SIZE],
            worldExtent: [0, 0, MAP_REAL_SIZE, MAP_REAL_SIZE],
        });
        ol.proj.addProjection(customProjection);

        var baseLayer = new ol.layer.Tile({
            source: new ol.source.XYZ({
                url: 'https://www.aseanmotorclub.com/map_tiles/719a/colors/{z}_{x}_{y}.avif',
                projection: customProjection,
                minZoom: 2,
                maxZoom: 5,
                wrapX: false,
            }),
        });

        var map = new ol.Map({
            target: 'track-map',
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

        var lineCoords = [];
        for (var i = 0; i < waypoints.length; i++) {
            var wp = waypoints[i];
            var loc = wp.Location || {};
            if (loc.X != null && loc.Y != null) {
                lineCoords.push(gameToMap(loc.X, loc.Y));
            }
        }

        var vectorSource = new ol.source.Vector();

        if (lineCoords.length >= 2) {
            vectorSource.addFeature(new ol.Feature({
                geometry: new ol.geom.LineString(lineCoords),
            }));
        }

        var gateSource = new ol.source.Vector();

        for (var j = 0; j < waypoints.length; j++) {
            var w = waypoints[j];
            var l = w.Location || {};
            if (l.X == null || l.Y == null) continue;
            var mapCoords = gameToMap(l.X, l.Y);
            vectorSource.addFeature(new ol.Feature({
                geometry: new ol.geom.Point(mapCoords),
                waypoint_index: j,
                game_x: l.X,
                game_y: l.Y,
                game_z: l.Z,
                scale: w.Scale3D || {},
                rotation: w.Rotation || {},
            }));

            var rot = w.Rotation || {};
            var sc = w.Scale3D || {};
            if (rot.Z != null && rot.W != null && sc.Y != null) {
                var yaw = 2 * Math.atan2(rot.Z, rot.W);
                var scaleY = sc.Y;
                var halfWidth = (scaleY * 100) / 2;
                var perpAngle = -(yaw + Math.PI / 2);
                var g1 = gameToMap(l.X + Math.cos(perpAngle) * halfWidth, l.Y + Math.sin(perpAngle) * halfWidth);
                var g2 = gameToMap(l.X - Math.cos(perpAngle) * halfWidth, l.Y - Math.sin(perpAngle) * halfWidth);
                gateSource.addFeature(new ol.Feature({
                    geometry: new ol.geom.LineString([g1, g2]),
                    waypoint_index: j,
                    game_x: l.X,
                    game_y: l.Y,
                    scale: sc,
                }));
            }
        }

        var vectorLayer = new ol.layer.Vector({
            source: vectorSource,
            style: function (feature) {
                if (feature.getGeometry().getType() === 'LineString') {
                    return new ol.style.Style({
                        stroke: new ol.style.Stroke({ color: '#417690', width: 3 }),
                    });
                }
                var idx = feature.get('waypoint_index');
                var isStart = idx === 0;
                return new ol.style.Style({
                    image: new ol.style.Circle({
                        radius: isStart ? 8 : 5,
                        fill: new ol.style.Fill({ color: isStart ? '#2e8b57' : '#417690' }),
                        stroke: new ol.style.Stroke({ color: '#fff', width: 2 }),
                    }),
                });
            },
        });
        map.addLayer(vectorLayer);

        var gateLayer = new ol.layer.Vector({
            source: gateSource,
            style: function (feature) {
                var idx = feature.get('waypoint_index');
                var isStart = idx === 0;
                return [
                    new ol.style.Style({
                        stroke: new ol.style.Stroke({ color: isStart ? '#2e8b57' : '#f5a623', width: 4 }),
                    }),
                    new ol.style.Style({
                        text: new ol.style.Text({
                            text: String(idx + 1),
                            font: (isStart ? 'bold 11px' : '10px') + ' sans-serif',
                            fill: new ol.style.Fill({ color: '#333' }),
                            stroke: new ol.style.Stroke({ color: '#fff', width: 3 }),
                            offsetY: -14,
                        }),
                    }),
                ];
            },
        });
        map.addLayer(gateLayer);

        var popup = document.getElementById('popup');
        var popupContent = document.getElementById('popup-content');
        var popupCloser = document.getElementById('popup-closer');

        var overlay = new ol.Overlay({
            element: popup,
            autoPan: true,
            autoPanAnimation: { duration: 250 },
        });
        map.addOverlay(overlay);

        popupCloser.addEventListener('click', function (ev) {
            ev.preventDefault();
            overlay.setPosition(undefined);
        });

        map.on('singleclick', function (ev) {
            var feature = map.forEachFeatureAtPixel(ev.pixel, function (f) { return f; });
            if (feature && feature.get('waypoint_index') != null) {
                var geom = feature.getGeometry();
                var coords = geom.getType() === 'Point' ? geom.getCoordinates() : ol.extent.getCenter(geom.getExtent());
                var idx = feature.get('waypoint_index');
                var gx = feature.get('game_x');
                var gy = feature.get('game_y');
                var scale = feature.get('scale') || {};
                var scaleY = scale.Y;
                popupContent.innerHTML =
                    '<strong>Waypoint ' + (idx + 1) + '</strong><br>' +
                    '<span style="font-size:12px;color:#555;">X: ' + gx + ', Y: ' + gy + '</span>' +
                    (scaleY != null ? '<br><span style="font-size:12px;color:#555;">Gate width: ' + (scaleY * 100).toFixed(0) + '</span>' : '');
                overlay.setPosition(coords);
            } else {
                overlay.setPosition(undefined);
            }
        });

        map.on('pointermove', function (ev) {
            var hit = map.hasFeatureAtPixel(ev.pixel);
            map.getTargetElement().style.cursor = hit ? 'pointer' : '';
        });

        if (lineCoords.length > 0) {
            var extent = ol.extent.createEmpty();
            ol.extent.extend(extent, vectorSource.getExtent());
            ol.extent.extend(extent, gateSource.getExtent());
            map.getView().fit(extent, { minResolution: 1, padding: [40, 40, 40, 40] });
        }

        var routeName = route.RouteName || 'Unknown Route';
        var numLaps = config.NumLaps || '?';
        var summaryEl = document.getElementById('track-summary');
        if (summaryEl) {
            summaryEl.textContent = routeName + ' — ' + waypoints.length + ' waypoints, ' + numLaps + ' laps';
        }
    }
}
