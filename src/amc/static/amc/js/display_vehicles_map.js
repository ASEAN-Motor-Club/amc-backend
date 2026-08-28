/* global ol */
'use strict';

const MAP_REAL_SIZE = 2200000;

const customProjection = new ol.proj.Projection({
    code: 'customData',
    units: 'pixels',
    extent: [0, 0, MAP_REAL_SIZE, MAP_REAL_SIZE],
    worldExtent: [0, 0, MAP_REAL_SIZE, MAP_REAL_SIZE],
});
ol.proj.addProjection(customProjection);

const baseLayer = new ol.layer.Tile({
    source: new ol.source.XYZ({
        url: "https://www.aseanmotorclub.com/map_tiles/719a/colors/{z}_{x}_{y}.avif",
        projection: customProjection,
        minZoom: 2,
        maxZoom: 5,
        wrapX: false
    })
});

const map = new ol.Map({
    target: 'display-vehicles-map',
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
    })
});

const vehicleSource = new ol.source.Vector();
const vehicleLayer = new ol.layer.Vector({
    source: vehicleSource,
    style: function(feature) {
        return new ol.style.Style({
            image: new ol.style.Circle({
                radius: 8,
                fill: new ol.style.Fill({ color: '#417690' }),
                stroke: new ol.style.Stroke({ color: '#fff', width: 2 }),
            }),
            text: new ol.style.Text({
                text: feature.get('name'),
                offsetY: -18,
                font: '12px sans-serif',
                fill: new ol.style.Fill({ color: '#333' }),
                stroke: new ol.style.Stroke({ color: '#fff', width: 3 }),
            }),
        });
    }
});
map.addLayer(vehicleLayer);

const popup = document.getElementById('popup');
const popupContent = document.getElementById('popup-content');
const popupCloser = document.getElementById('popup-closer');

const overlay = new ol.Overlay({
    element: popup,
    autoPan: true,
    autoPanAnimation: { duration: 250 },
});
map.addOverlay(overlay);

popupCloser.addEventListener('click', function(ev) {
    ev.preventDefault();
    overlay.setPosition(undefined);
});

function deleteVehicle(feature) {
    const id = feature.get('id');
    const name = feature.get('name');
    if (!confirm('Delete "' + name + '" (ID: ' + id + ')?')) return;

    const url = deleteUrlPattern.replace('0', id);
    fetch(url, {
        method: 'POST',
        headers: {
            'X-CSRFToken': csrfToken,
            'Content-Type': 'application/json',
        },
    })
    .then(function(response) {
        if (!response.ok) return response.json().then(function(err) { throw new Error(err.error || 'Delete failed'); });
        vehicleSource.removeFeature(feature);
        overlay.setPosition(undefined);
    })
    .catch(function(err) {
        alert('Error: ' + err.message);
    });
}

map.on('singleclick', function(ev) {
    const feature = map.forEachFeatureAtPixel(ev.pixel, function(f) { return f; });
    if (feature) {
        const coords = feature.getGeometry().getCoordinates();
        const id = feature.get('id');
        const name = feature.get('name');
        const editUrl = feature.get('edit_url');
        popupContent.innerHTML = '<strong>' + name + '</strong><br><span class="vehicle-id">ID: ' + id + '</span><br><a href="' + editUrl + '">Edit</a> | <a href="#" class="popup-delete">Delete</a>';
        overlay.setPosition(coords);
        var deleteLink = popupContent.querySelector('.popup-delete');
        deleteLink.addEventListener('click', function(ev) {
            ev.preventDefault();
            deleteVehicle(feature);
        });
    } else {
        overlay.setPosition(undefined);
    }
});

map.on('pointermove', function(ev) {
    const hit = map.hasFeatureAtPixel(ev.pixel);
    map.getTargetElement().style.cursor = hit ? 'pointer' : '';
});

fetch(geojsonUrl)
    .then(function(response) { return response.json(); })
    .then(function(geojson) {
        const format = new ol.format.GeoJSON();
        const features = format.readFeatures(geojson, {
            dataProjection: customProjection,
            featureProjection: customProjection,
        });
        vehicleSource.addFeatures(features);

        if (features.length > 0) {
            const extent = vehicleSource.getExtent();
            map.getView().fit(extent, { minResolution: 1, padding: [40, 40, 40, 40] });
        }
    });
