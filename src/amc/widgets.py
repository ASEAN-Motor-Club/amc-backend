from django.contrib.gis.forms.widgets import OSMWidget
from django import forms

class AMCOpenLayersWidget(OSMWidget):
    template_name = 'amc/openlayers.html'
    map_srid = 3857
    default_lon = 0 
    default_lat = 0
    default_zoom = 1

    def get_context(self, name, value, attrs):
        context = super().get_context(name, value, attrs)
        attrs = context['widget']['attrs']
        # Ensure we have the map options expected by our custom JS
        context['map_options'] = {
            'geom_name': attrs.get('geom_name', 'Geometry'),
            'map_id': attrs['id'] + '_map',
            'map_srid': self.map_srid,
            'default_lon': self.default_lon,
            'default_lat': self.default_lat,
            'default_zoom': self.default_zoom,
            'base_layer': 'osm',
        }
        context['map_options_id'] = attrs['id'] + '_mapwidget_options'
        context['id'] = attrs['id']
        return context
    
    def serialize(self, value):
        if value:
            # DB (Real World) -> Map (Pixel Space)
            # Transformation from utils.ts:
            # x' = x + 1280000
            # y' = 1880000 - y
            
            MAP_REAL_X_LEFT = -1280000
            MAP_REAL_Y_TOP = -320000
            MAP_REAL_SIZE = 2200000
            
            def transform_coords(coords):
                if isinstance(coords, (list, tuple)) and isinstance(coords[0], (int, float)):
                   x, y = coords[0], coords[1]
                   new_x = x - MAP_REAL_X_LEFT
                   new_y = -(y - MAP_REAL_Y_TOP) + MAP_REAL_SIZE
                   return (new_x, new_y) + coords[2:]
                else:
                   return tuple(transform_coords(item) for item in coords)

            scaled_coords = transform_coords(value.coords)
            
            from django.contrib.gis.geos import Polygon
            try:
                if isinstance(value, Polygon):
                    value = type(value)(*scaled_coords, srid=value.srid)
                else:
                    value = type(value)(scaled_coords, srid=value.srid)
            except TypeError:
                 value = type(value)(scaled_coords, srid=value.srid)

        return super().serialize(value)

    def deserialize(self, value):
        geom = super().deserialize(value)
        if geom:
            # Map (Pixel Space) -> DB (Real World)
            # Inverse Transformation:
            # x = x' - 1280000
            # y = 1880000 - y'
            
            MAP_REAL_X_LEFT = -1280000
            MAP_REAL_Y_TOP = -320000
            MAP_REAL_SIZE = 2200000

            def transform_coords(coords):
                if isinstance(coords, (list, tuple)) and isinstance(coords[0], (int, float)):
                   x, _y = coords[0], coords[1]
                   new_x = x + MAP_REAL_X_LEFT
                   new_y = -(coords[1] - MAP_REAL_SIZE) + MAP_REAL_Y_TOP
                   return (new_x, new_y) + coords[2:]
                else:
                   return tuple(transform_coords(item) for item in coords)

            scaled_coords = transform_coords(geom.coords)
            from django.contrib.gis.geos import Polygon
            try:
                if isinstance(geom, Polygon):
                    geom = type(geom)(*scaled_coords, srid=geom.srid)
                else:
                    geom = type(geom)(scaled_coords, srid=geom.srid)
            except TypeError:
                geom = type(geom)(scaled_coords, srid=geom.srid)
            
        return geom

    def format_value(self, value):
        if value is None:
            return None
        return self.serialize(value)
    
    @property
    def media(self):
        return forms.Media(
            css={
                "all": (
                    "https://cdn.jsdelivr.net/npm/ol@v7.2.2/ol.css",
                    "gis/css/ol3.css",
                )
            },
            js=(
                "https://cdn.jsdelivr.net/npm/ol@v7.2.2/dist/ol.js",
                "amc/js/OLMapWidget.js",
            )
        )
