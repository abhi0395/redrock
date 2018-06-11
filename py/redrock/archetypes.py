"""
Classes and functions for archetypes.
"""

import os
from glob import glob
from astropy.io import fits
import scipy as sp
from scipy.interpolate import interp1d
from scipy.integrate import trapz, quad
from scipy import special

from .zscan import spectral_data, calc_zchi2_one

from ._zscan import _zchi2_one

from .rebin import trapz_rebin, centers2edges

from .fitz import get_dv

from .zwarning import ZWarningMask as ZW

from . import constants

from .utils import native_endian

class Archetype():
    """Class to store all different archetypes from the same spectype.

    The archetype data are read from a redrock-format archetype file.

    Args:
        filename (str): the path to the archetype file

    """
    def __init__(self, filename):

        # Load the file
        h = fits.open(filename, memmap=False)

        hdr = h['ARCHETYPES'].header
        self.flux = sp.asarray(native_endian(h['ARCHETYPES'].data['ARCHETYPE']))
        self._rrtype = hdr['RRTYPE'].strip()
        self._subtype = sp.array(sp.char.strip(h['ARCHETYPES'].data['SUBTYPE'].astype(str)))

        self.wave = sp.asarray(hdr['CRVAL1'] + hdr['CDELT1']*sp.arange(self.flux.shape[1]))
        if 'LOGLAM' in hdr and hdr['LOGLAM'] != 0:
            self.wave = 10**self.wave

        h.close()

        self._narch = self.flux.shape[0]
        self._nwave = self.flux.shape[1]
        self._full_type = sp.char.add(self._rrtype+':::',self._subtype)
        self._full_type[self._subtype==''] = self._rrtype

        # Dic of templates
        self._archetype = {}
        self._archetype['INTERP'] = sp.array([None]*self._narch)
        for i in range(self._narch):
            self._archetype['INTERP'][i] = interp1d(self.wave,self.flux[i,:],fill_value='extrapolate',kind='linear')

        return
    def rebin_template(self,index,z,dwave):
        """
        """
        #result = {}
        #for hs, wave in dwave.items():
        #    binned = sp.zeros((wave.shape[0], 1), dtype=sp.float64)
        #    binned[:,0] = trapz_rebin((1.+z)*self.wave, self.flux[index], wave)
        #    result[hs] = binned
        #return result
        return {hs:trapz_rebin((1.+z)*self.wave, self.flux[index], wave) for hs, wave in dwave.items()}
        #return {hs:self._archetype['INTERP'][index](wave/(1.+z)) for hs, wave in dwave.items()}

    def get_best_archetype(self,spectra,weights,flux,wflux,dwave,z,legendre):
        """Get the best archetype for the given redshift and spectype.

        Args:
            spectra (list): list of Spectrum objects.
            weights (array): concatenated spectral weights (ivar).
            flux (array): concatenated flux values.
            wflux (array): concatenated weighted flux values.
            dwave (dic): dictionary of wavelength grids
            z (float): best redshift
            legendre (dic): legendre polynomial

        Returns:
            chi2 (float): chi2 of best archetype
            zcoef (array): zcoef of best archetype
            subtype (str): subtype of best archetype

        """

        nleg = legendre[list(legendre.keys())[0]].shape[0]
        leg = sp.array([sp.concatenate( [legendre[k][i] for k in legendre.keys()] ) for i in range(nleg)])
        Tb = sp.append( sp.zeros((flux.size,1)),leg.transpose(), axis=1 )

        zzchi2 = sp.zeros(self._narch, dtype=sp.float64)
        zzcoeff = sp.zeros((self._narch, Tb.shape[1]), dtype=sp.float64)
        zcoeff = sp.zeros(Tb.shape[1], dtype=sp.float64)

        for i in range(self._narch):
            # TODO: use rebin_template and calc_zchi2_one to use
            #   the resolution matrix and the different spectrograph
            binned = self.rebin_template(i, z, dwave)
            #zzchi2[i], zzcoeff[i] = calc_zchi2_one(spectra, weights, flux, wflux, binned)
            Tb[:,0] = sp.concatenate([ spec for spec in binned.values()])
            zzchi2[i] = _zchi2_one(Tb, weights, flux, wflux, zcoeff)
            zzcoeff[i] = zcoeff

        iBest = sp.argmin(zzchi2)
        # TODO: should we look at the value of zzcoeff[0] and if negative
        #   set the chi2 to very big?

        return zzchi2[iBest], zzcoeff[iBest], self._subtype[iBest]


class All_archetypes():
    """Class to store all different archetypes of all the different spectype.

    Args:
        lstfilename (lst str): List of file to get the templates from
        archetypes_dir (str): Directory to the archetypes

    """
    def __init__(self, lstfilename=None, archetypes_dir=None):

        # Get list of path to archetype
        if lstfilename is None:
            lstfilename = find_archetypes(archetypes_dir)

        # Load archetype
        self.archetypes = {}
        for f in lstfilename:
            archetype = Archetype(f)
            self.archetypes[archetype._rrtype] = archetype

        return
    def get_best_archetype(self,spectra,spectype,z):#,tzfit):
        """Rearange tzfit according to chi2 from archetype

        Args:
            spectra (list): list of Spectrum objects.
            tzfit (astropy.table): attributes of all the different minima

        Returns:
            tzfit (astropy.table): attributes of all the different minima

        """

        # TODO: set this as a parameter
        deg_legendre = 3

        # Build dictionary of wavelength grids
        dwave = { s.wavehash:s.wave for s in spectra }
        wave = sp.concatenate([ w for w in dwave.values() ])
        wave_min = wave.min()
        wave_max = wave.max()
        legendre = { hs:sp.array([special.legendre(i)( (w-wave_min)/(wave_max-wave_min)*2.-1. ) for i in range(deg_legendre)]) for hs, w in dwave.items() }

        (weights, flux, wflux) = spectral_data(spectra)

        chi2, _, subtype = self.archetypes[spectype].get_best_archetype(spectra,
                weights, flux, wflux, dwave, z, legendre)

        '''
        # Fit each archetype
        for res in tzfit:
            # TODO: Keep coeff archetype?
            res['chi2'], _, res['subtype'] = self.archetypes[res['spectype']].get_best_archetype(spectra,
                weights, flux, wflux, dwave, res['z'], legendre)

        tzfit.sort('chi2')
        tzfit['znum'] = sp.arange(len(tzfit))
        tzfit['deltachi2'] = sp.ediff1d(tzfit['chi2'], to_end=0.0)

        #- set ZW.SMALL_DELTA_CHI2 flag
        for i in range(len(tzfit)-1):
            noti = sp.arange(len(tzfit))!=i
            alldeltachi2 = sp.absolute(tzfit['chi2'][noti]-tzfit['chi2'][i])
            alldv = sp.absolute(get_dv(z=tzfit['z'][noti],zref=tzfit['z'][i]))
            zwarn = sp.any( (alldeltachi2<constants.min_deltachi2) & (alldv>=constants.max_velo_diff) )
            if zwarn:
                tzfit['zwarn'][i] |= ZW.SMALL_DELTA_CHI2
            elif tzfit['zwarn'][i]&ZW.SMALL_DELTA_CHI2:
                tzfit['zwarn'][i] &= ~ZW.SMALL_DELTA_CHI2
        '''
        return

def find_archetypes(archetypes_dir=None):
    """Return list of rrarchetype-\*.fits archetype files

    Search directories in this order, returning results from first one found:
        - archetypes_dir
        - $RR_ARCHETYPE_DIR
        - <redrock_code>/archetypes/

    Args:
        archetypes_dir (str): optional directory containing the archetypes.

    Returns:
        list: a list of archetype files.

    """
    if archetypes_dir is None:
        if 'RR_ARCHETYPE_DIR' in os.environ:
            archetypes_dir = os.environ['RR_ARCHETYPE_DIR']
        else:
            thisdir = os.path.dirname(__file__)
            archdir = os.path.join(os.path.abspath(thisdir), 'archetypes')
            if os.path.exists(archdir):
                archetypes_dir = archdir
            else:
                raise IOError("ERROR: can't find archetypes_dir, $RR_ARCHETYPE_DIR, or {rrcode}/archetypes/")

    return sorted(glob(os.path.join(archetypes_dir, 'rrarchetype-*.fits')))
