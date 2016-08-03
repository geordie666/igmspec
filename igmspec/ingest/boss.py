""" Module to ingest SDSS III (aka BOSS) data products
"""
from __future__ import print_function, absolute_import, division, unicode_literals


import numpy as np
import os, json
import pdb
import datetime

from astropy.table import Table, Column, vstack
from astropy.time import Time
from astropy.coordinates import SkyCoord, match_coordinates_sky
from astropy import units as u

from linetools import utils as ltu
from linetools.spectra import io as lsio

from igmspec.ingest import utils as iiu

def grab_meta():
    """ Grab BOSS meta Table
    Returns
    -------

    """
    #http://www.sdss.org/dr12/algorithms/boss-dr12-quasar-catalog/
    boss_dr12 = Table.read(os.getenv('RAW_IGMSPEC')+'/BOSS/DR12Q.fits.gz')
    boss_dr12['CAT'] = ['DR12Q']*len(boss_dr12)
    #
    boss_sup = Table.read(os.getenv('RAW_IGMSPEC')+'/BOSS/DR12Q_sup.fits.gz')
    boss_sup['CAT'] = ['SUPGD']*len(boss_sup)
    boss_supbad = Table.read(os.getenv('RAW_IGMSPEC')+'/BOSS/DR12Q_supbad.fits.gz')
    boss_supbad['CAT'] = ['SUPBD']*len(boss_supbad)
    # Collate
    boss_meta = vstack([boss_dr12, boss_sup, boss_supbad], join_type='outer')
    #
    nboss = len(boss_meta)
    # DATE-OBS
    t = Time(list(boss_meta['MJD'].data), format='mjd', out_subfmt='date')  # Fixes to YYYY-MM-DD
    boss_meta.add_column(Column(t.iso, name='DATE-OBS'))
    # Add columns
    boss_meta.add_column(Column(['BOSS']*nboss, name='INSTR'))
    boss_meta.add_column(Column(['BOTH']*nboss, name='GRATING'))
    #http://www.sdss.org/instruments/boss_spectrograph/
    boss_meta.add_column(Column([2100.]*nboss, name='R'))  # RESOLUTION
    boss_meta.add_column(Column(['SDSS 2.5-M']*nboss, name='TELESCOPE'))
    # Redshift logic
    boss_meta['zem'] = boss_meta['Z_PCA']
    boss_meta['sig_zem'] = boss_meta['ERR_ZPCA']
    boss_meta['flag_zem'] = [str('BOSS_PCA ')]*nboss
    # Fix bad redshifts
    bad_pca = boss_meta['Z_PCA'] < 0.
    boss_meta['zem'][bad_pca] = boss_meta['Z_PIPE'][bad_pca]
    boss_meta['sig_zem'][bad_pca] = boss_meta['ERR_ZPIPE'][bad_pca]
    boss_meta['flag_zem'][bad_pca] = str('BOSS_PIPE')
    #
    return boss_meta

def meta_for_build():
    """ Load the meta info
    DR12 quasars : https://data.sdss.org/datamodel/files/BOSS_QSO/DR12Q/DR12Q.html

    Returns
    -------

    """
    boss_meta = grab_meta()
    #
    meta = Table()
    for key in ['RA', 'DEC', 'zem', 'sig_zem', 'flag_zem']:
        meta[key] = boss_meta[key]
    meta['STYPE'] = [str('QSO')]*len(meta)
    # Return
    return meta


def get_specfil(row):
    """Grab the BOSS file name + path
    """
    # Generate file name (DR4 is different)
    pnm = '{0:04d}'.format(row['PLATE'])
    fnm = '{0:04d}'.format(row['FIBERID'])
    mjd = str(row['MJD'])
    path = os.getenv('RAW_IGMSPEC')+'/BOSS/'
    #
    if row['CAT'] == 'SUPGD':
        path += 'Sup12/'
        specfil = path+'spec-{:04d}-{:d}-{:04d}.fits.gz'.format(row['PLATE'], row['MJD'], row['FIBERID'])
    elif row['CAT'] == 'SUPBD':
        path += 'SupBad/'
        specfil = path+'spec-{:04d}-{:d}-{:04d}.fits.gz'.format(row['PLATE'], row['MJD'], row['FIBERID'])
    else:
        specfil = 'None'
    # Finish
    return specfil


def hdf5_adddata(hdf, IDs, sname, debug=False, chk_meta_only=False):
    """ Add BOSS data to the DB

    Parameters
    ----------
    hdf : hdf5 pointer
    IDs : ndarray
      int array of IGM_ID values in mainDB
    sname : str
      Survey name
    chk_meta_only : bool, optional
      Only check meta file;  will not write

    Returns
    -------

    """
    # Add Survey
    print("Adding {:s} survey to DB".format(sname))
    boss_grp = hdf.create_group(sname)
    # Load up
    meta = grab_meta()
    bmeta = meta_for_build()
    # Checks
    if sname != 'BOSS_DR12':
        raise IOError("Not expecting this survey..")
    if np.sum(IDs < 0) > 0:
        raise ValueError("Bad ID values")
    # Open Meta tables
    if len(bmeta) != len(IDs):
        raise ValueError("Wrong sized table..")

    # Generate ID array from RA/DEC
    c_cut = SkyCoord(ra=bmeta['RA'], dec=bmeta['DEC'], unit='deg')
    c_all = SkyCoord(ra=meta['RA'], dec=meta['DEC'], unit='deg')
    # Find new sources
    idx, d2d, d3d = match_coordinates_sky(c_all, c_cut, nthneighbor=1)
    if np.sum(d2d > 1.2*u.arcsec):  # There is one system offset by 1.1"
        raise ValueError("Bad matches in BOSS")
    meta_IDs = IDs[idx]
    meta.add_column(Column(meta_IDs, name='IGM_ID'))

    # Add zem

    # Build spectra (and parse for meta)
    nspec = len(meta)
    max_npix = 4650  # Just needs to be large enough
    data = np.ma.empty((1,),
                       dtype=[(str('wave'), 'float64', (max_npix)),
                              (str('flux'), 'float32', (max_npix)),
                              (str('sig'),  'float32', (max_npix)),
                              #(str('co'),   'float32', (max_npix)),
                              ])
    # Init
    spec_set = hdf[sname].create_dataset('spec', data=data, chunks=True,
                                         maxshape=(None,), compression='gzip')
    spec_set.resize((nspec,))
    wvminlist = []
    wvmaxlist = []
    speclist = []
    npixlist = []
    # Loop
    maxpix = 0
    for jj,row in enumerate(meta):
        full_file = get_specfil(row)
        if full_file == 'None':
            continue
        # Extract
        print("BOSS: Reading {:s}".format(full_file))
        # Parse name
        fname = full_file.split('/')[-1]
        # Generate full file
        spec = lsio.readspec(full_file)
        # npix
        npix = spec.npix
        if npix > max_npix:
            raise ValueError("Not enough pixels in the data... ({:d})".format(npix))
        else:
            maxpix = max(npix,maxpix)
        # Some fiddling about
        for key in ['wave','flux','sig']:
            data[key] = 0.  # Important to init (for compression too)
        data['flux'][0][:npix] = spec.flux.value
        data['sig'][0][:npix] = spec.sig.value
        data['wave'][0][:npix] = spec.wavelength.value
        # Meta
        speclist.append(str(fname))
        wvminlist.append(np.min(data['wave'][0][:npix]))
        wvmaxlist.append(np.max(data['wave'][0][:npix]))
        npixlist.append(npix)
        # Only way to set the dataset correctly
        if chk_meta_only:
            continue
        spec_set[jj] = data

    #
    print("Max pix = {:d}".format(maxpix))
    # Add columns
    meta.add_column(Column(speclist, name='SPEC_FILE'))
    meta.add_column(Column(npixlist, name='NPIX'))
    meta.add_column(Column(wvminlist, name='WV_MIN'))
    meta.add_column(Column(wvmaxlist, name='WV_MAX'))
    meta.add_column(Column(np.arange(nspec,dtype=int),name='SURVEY_ID'))

    # Add HDLLS meta to hdf5
    if iiu.chk_meta(meta):
        if chk_meta_only:
            pdb.set_trace()
        hdf[sname]['meta'] = meta
    else:
        raise ValueError("meta file failed")
    # References
    refs = [dict(url='http://adsabs.harvard.edu/abs/2015ApJS..219...12A',
                 bib='boss_qso_dr12'),
            ]
    jrefs = ltu.jsonify(refs)
    hdf[sname]['meta'].attrs['Refs'] = json.dumps(jrefs)
    pdb.set_trace()
    #
    return
