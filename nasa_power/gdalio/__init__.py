"""Ring 1.5: NetCDF and GeoTIFF handling with GDAL and numpy, but no QGIS.

Split from ring 1 because it needs ``osgeo.gdal``, and split from ring 2
because none of it needs a running QGIS application -- a mosaic is a mosaic
whether or not there is a map canvas. That makes it testable under any GDAL
Python, and it keeps the QGIS bridge down to layer construction and styling.
"""
