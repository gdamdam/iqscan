"""Shared waterfall color maps used by reports, the explorer and Meteor video."""

WATERFALL_STYLES = ('magma', 'youssef')
DEFAULT_WATERFALL_STYLE = 'magma'

# SatDump resources/waterfall/classic.json (author: Youssef Touil):
# https://github.com/SatDump/SatDump/blob/master/resources/waterfall/classic.json
# SatDump's generatePalette() in colormaps.cpp interpolates these stops into
# the 2000-entry LUT used by waterfall_plot.h/.cpp:
# https://github.com/SatDump/SatDump/blob/master/src-core/common/colormaps.cpp
# https://github.com/SatDump/SatDump/blob/master/src-core/common/widgets/waterfall_plot.h
# https://github.com/SatDump/SatDump/blob/master/src-core/common/widgets/waterfall_plot.cpp
# Keep the values here centralized for every iqscan waterfall.
YOUSSEF_STOPS = (
    '#000020', '#000028', '#000030', '#000040', '#000050', '#000070',
    '#000091', '#0F48C8', '#1E90FF', '#8FC8FF', '#FFFFFF', '#FFFF7F',
    '#FFFF00', '#FFB60B', '#FE9110', '#FE6D16', '#FE5210', '#FF370B',
    '#FF1B05', '#FF0000', '#F10000', '#E20000', '#C60000', '#B20000',
    '#9F0000', '#8A0000', '#750000', '#5F0000', '#4A0000',
)


def waterfall_cmap(style=DEFAULT_WATERFALL_STYLE):
    """Return the Matplotlib colormap for a waterfall style.

    SatDump's Youssef Classic palette uses a 2000-entry interpolated LUT.
    Reproduce that LUT and its integer truncation for iqscan renderers.
    """
    if style == 'magma':
        return 'magma'
    if style != 'youssef':
        raise ValueError(f'Unknown waterfall style: {style}')

    import numpy as np
    from matplotlib.colors import ListedColormap

    stops = np.array([[int(color[i:i+2], 16) for i in (1, 3, 5)]
                      for color in YOUSSEF_STOPS], dtype=np.float32)
    resolution = 2000
    positions = np.arange(resolution, dtype=np.float32) / resolution * len(stops)
    lower = np.floor(positions).astype(int)
    upper = np.ceil(positions).astype(int)
    lower = np.clip(lower, 0, len(stops)-1)
    upper = np.clip(upper, 0, len(stops)-1)
    ratio = (positions - np.floor(positions))[:, None]
    lut = (stops[lower] * (1 - ratio) + stops[upper] * ratio).astype(np.uint8)
    return ListedColormap(lut.astype(np.float32) / 255, name='youssef')
