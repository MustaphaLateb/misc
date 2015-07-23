#!/usr/bin/env python2
""" Image compositing script

TODO:
    - allow images to not be stacked
    - specify <projwin> for output composite
    - allow user to specify algorithm, rather than using predefined ones
    - parse more complicated expressions (e.g., min BLUE and max NDVI)
"""
from __future__ import division, print_function

import logging

import click
import numpy as np
import rasterio
from rasterio.rio.options import _cb_key_val as _valid_keyval
import snuggs

__version__ = '0.99.0'

logging.basicConfig(format='%(asctime)s.%(levelname)s: %(message)s',
                    level=logging.INFO,
                    datefmt='%H:%M:%S')
logger = logging.getLogger(__name__)

# Predefined algorithms -- name: snuggs expression
_ALGO = {
    'maxNDVI': '(max (/ (- nir red) (+ nir red)))',
    'medianNDVI': '(median (/ (- nir red) (+ nir red)))',
    'ZheZhu': '(max (/ nir blue))',
    'minBlue': '(min blue)',
    'maxNIR': '(max nir)'
}

_context = dict(
    token_normalize_func=lambda x: x.lower(),
    help_option_names=['--help', '-h']
)


def _valid_band(ctx, param, value):
    try:
        band = int(value)
        assert band >= 1
    except:
        raise click.BadParameter('Band must be integer above 1')
    return band


@click.command(context_settings=_context)
@click.argument('inputs', nargs=-1,
                type=click.Path(dir_okay=False, readable=True,
                                resolve_path=True))
@click.option('--algo', help='Create composite based on specific algorithm',
              type=click.Choice(_ALGO.keys()))
@click.option('--expr', help='Create composite based on an expression',
              type=str)
@click.option('-o', '--output', help='Output image composite',
              type=click.Path(dir_okay=False, writable=True,
                              resolve_path=True),
              default='composite.gtif')
@click.option('-of', '--format', help='Output image format',
              default='GTiff')
@click.option('--blue', callback=_valid_band, default=1, metavar='<int>',
              help='Band number for blue band in <src> (default: 1)')
@click.option('--green', callback=_valid_band, default=2, metavar='<int>',
              help='Band number for green band in <src> (default: 2)')
@click.option('--red', callback=_valid_band, default=3, metavar='<int>',
              help='Band number for red band in <src> (default: 3)')
@click.option('--nir', callback=_valid_band, default=4, metavar='<int>',
              help='Band number for near IR band in <src> (default: 4)')
@click.option('--swir1', callback=_valid_band, default=5, metavar='<int>',
              help='Band number for first SWIR band in <src> (default: 5)')
@click.option('--swir2', callback=_valid_band, default=6, metavar='<int>',
              help='Band number for second SWIR band in <src> (default: 6)')
@click.option('--band', callback=_valid_keyval, multiple=True, type=str,
              help='Band name and index for additional bands')
@click.option('-v', '--verbose', is_flag=True, help='Show verbose messages')
@click.version_option(__version__)
def image_composite(inputs, algo, expr, output, format,
                    blue, green, red, nir, swir1, swir2, band,
                    verbose):
    """ Create image composites based on some criteria

    Output image composites retain original values from input images that meet
    a certain criteria. For example, in a maximum NDVI composite with 10 input
    images, all bands for a given pixel will contain the band values from the
    input raster that had the highest NDVI value.

    Users can choose from a set of predefined compositing algorithms or may
    specify an Snuggs S-expression that defines the compositing criteria. See
    https://github.com/mapbox/snuggs for more information on Snuggs
    expressions.

    The indexes for common optical bands (e.g., red, nir, blue) within the
    input rasters are included as optional arguments and are indexed in
    wavelength sequential order. You may need to overwrite the default indexes
    of bands used in a given S-expression with the correct band index.
    Additional bands may be identified and indexed using the
    '--band NAME=INDEX' option.

    Currently, input images must be "stacked", meaning that they contain the
    same bands and are the same shape and extent.

    Example:

    1. Create a composite based on maximum NDVI

        $ image_composite.py --algo maxNDVI image1.gtif image2.gtif image3.gtif

    """
    if verbose:
        logger.setLevel(logging.DEBUG)

    # Prefer built-in algorithms to expressions if both are specified
    if not algo and not expr:
        raise click.UsageError('Error: must specify either --algo or --expr')
    elif algo is not None and expr is not None:
        logger.warning('Predefined algorithm and expression both defined. '
                       'Composite will be generated with predefined algorithm')
        expr = _ALGO[algo]
    elif algo is not None:
        logger.debug('Using predefined algorithm: {}'.format(algo))
        expr = _ALGO[algo]
    logger.info('Compositing criteria S-expression: {}'.format(expr))

    # Setup band keywords
    _bands = {'blue': blue, 'green': green, 'red': red,
              'nir': nir, 'swir1': swir1, 'swir2': swir2}
    # Parse any additional, user specified bands
    if band:
        for k, v in band.iteritems():
            try:
                _bands.update({k: int(v)})
            except ValueError:
                raise click.BadParameter(
                    'Value specified as KEY=VAL pair in --band must be an int')

    # Find only the band names and indexes required for the composite criteria
    crit_indices = {k: v - 1 for k, v in _bands.iteritems() if k in expr}

    # Enhance snuggs expressions to return index of value matching function
    snuggs.func_map['max'] = lambda a: np.argmax(a, axis=0)
    snuggs.func_map['min'] = lambda a: np.argmin(a, axis=0)
    snuggs.func_map['median'] = lambda a: np.argmin(
        np.abs(a - np.median(a, axis=0)), axis=0)

    with rasterio.drivers():

        # Read in the first image to fetch metadata
        with rasterio.open(inputs[0]) as first:
            meta = first.meta
            meta.pop('transform')
            meta.update(driver=format)
            if len(set(first.block_shapes)) != 1:
                click.echo('Cannot process input files - '
                           'All bands must have same block shapes')
                raise click.Abort()
            block_nrow, block_ncol = first.block_shapes[0]
            windows = first.block_windows(1)

        # Initialize output data and create composite
        with rasterio.open(output, 'w', **meta) as dst:
            # Process by block
            dat = np.ma.empty((len(inputs), meta['count'],
                               block_nrow, block_ncol),
                              dtype=np.dtype(meta['dtype']))
            mi, mj = np.meshgrid(np.arange(block_nrow), np.arange(block_ncol),
                                 indexing='ij')

            for i, (idx, window) in enumerate(windows):
                for j, fname in enumerate(inputs):
                    with rasterio.open(fname) as src:
                        dat[j, ...] = src.read(masked=True, window=window)

                # Find indices of files for composite
                crit = {k: dat[:, v, ...] for k, v in crit_indices.iteritems()}
                crit_idx = snuggs.eval(expr, **crit)

                # Create output composite
                # Use np.rollaxis to get (nimage, nrow, ncol, nband) shape
                composite = np.rollaxis(dat, 1, 4)[crit_idx, mi, mj]

                # Write out
                for i_b in range(composite.shape[-1]):
                    dst.write(composite[:, :, i_b], indexes=i_b + 1,
                              window=window)

if __name__ == '__main__':
    image_composite()