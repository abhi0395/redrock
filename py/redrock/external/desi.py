"""
redrock.external.desi
=====================

redrock wrapper tools for DESI
"""
import os
import sys
import re
import warnings
import traceback

import argparse

import numpy as np

from astropy.io import fits
from astropy.table import Table

from desiutil.io import encode_table
from desiutil.depend import add_dependencies, setdep

from desispec.resolution import Resolution
from desispec.coaddition import coadd_fibermap
from desispec.specscore import compute_coadd_tsnr_scores
from desispec.maskbits import fibermask

from ..utils import elapsed, get_mp, distribute_work, getGPUCountMPI

from ..targets import (Spectrum, Target, DistTargets)

from ..templates import load_dist_templates

from ..results import write_zscan

from ..zfind import zfind

from ..zwarning import ZWarningMask

from .._version import __version__

from ..archetypes import All_archetypes


def write_zbest(outfile, zbest, fibermap, exp_fibermap, tsnr2,
        template_version, archetype_version,
        spec_header=None):
    """Write zbest and fibermap Tables to outfile

    Args:
        outfile (str): output path.
        zbest (Table): best fit table.
        fibermap (Table): the coadded fibermap from the original inputs.
        tsnr2 (Table): table of input coadded TSNR2 values
        exp_fibermap (Table): the per-exposure fibermap from the orig inputs.
        template_version (str): template version used
        archetype_version (str): archetype version used

    Options:
        spec_header (dict-like): header of HDU 0 of input spectra

    Modifies input tables.meta['EXTNAME']
    """
    header = fits.Header()
    header['LONGSTRN'] = 'OGIP 1.0'
    header['RRVER'] = (__version__, 'Redrock version')
    for i, fulltype in enumerate(template_version.keys()):
        header['TEMNAM'+str(i).zfill(2)] = fulltype
        header['TEMVER'+str(i).zfill(2)] = template_version[fulltype]
    if not archetype_version is None:
        for i, fulltype in enumerate(archetype_version.keys()):
            header['ARCNAM'+str(i).zfill(2)] = fulltype
            header['ARCVER'+str(i).zfill(2)] = archetype_version[fulltype]

    # record code versions and key environment variables
    add_dependencies(header)
    for key in ['RR_TEMPLATE_DIR', 'RR_ARCHETYPE_DIR']:
        if key in os.environ:
            setdep(header, key, os.environ[key])

    if spec_header is not None:
        for key in ('SPGRP', 'SPGRPVAL', 'TILEID', 'SPECTRO', 'PETAL',
                'NIGHT', 'EXPID', 'HPXPIXEL', 'HPXNSIDE', 'HPXNEST',
                'SURVEY', 'PROGRAM', 'FAPRGRM'):
            if key in spec_header:
                header[key] = spec_header[key]

    zbest.meta['EXTNAME'] = 'REDSHIFTS'
    fibermap.meta['EXTNAME'] = 'FIBERMAP'
    exp_fibermap.meta['EXTNAME'] = 'EXP_FIBERMAP'
    tsnr2.meta['EXTNAME'] = 'TSNR2'

    hx = fits.HDUList()
    hx.append(fits.PrimaryHDU(header=header))
    hx.append(fits.convenience.table_to_hdu(zbest))
    hx.append(fits.convenience.table_to_hdu(fibermap))
    hx.append(fits.convenience.table_to_hdu(exp_fibermap))
    hx.append(fits.convenience.table_to_hdu(tsnr2))

    outfile = os.path.expandvars(outfile)
    outdir = os.path.dirname(os.path.abspath(outfile))
    if not os.path.exists(outdir):
        os.makedirs(outdir)

    tempfile = outfile + '.tmp'
    hx.writeto(tempfile, overwrite=True)
    os.rename(tempfile, outfile)

    return


class DistTargetsDESI(DistTargets):
    """Distributed targets for DESI.

    DESI spectral data is grouped by sky location, but is just a random
    collection of spectra for all targets.  Read this into memory while
    grouping by target ID, preserving order in which each target first appears.

    We pass through the spectra files once to compute all the book-keeping
    associated with regrouping the spectra by target.  Then we pass through
    again and actually read and distribute the data.

    Args:
        spectrafiles (str or list): a list of input files or pattern match
            of files.
        coadd (bool): if False, do not compute the coadds.
        targetids (list): (optional) restrict the global set of target IDs
            to this list.
        first_target (int): (optional) integer offset of the first target to
            consider in each file.  Useful for debugging / testing.
        n_target (int): (optional) number of targets to consider in each file.
            Useful for debugging / testing.
        comm (mpi4py.MPI.Comm): (optional) the MPI communicator.
        cache_Rcsr: pre-calculate and cache sparse CSR format of resolution
            matrix R
        cosmics_nsig (float): cosmic rejection threshold used in coaddition
        capacities (list): (optional) list of process capacities. If None,
            use equal capacity per process. A process with higher capacity
            can handle more work.
    """

    ### @profile
    def __init__(self, spectrafiles, coadd=True, targetids=None,
                 first_target=None, n_target=None, comm=None, cache_Rcsr=False,
                 cosmics_nsig=0, capacities=None):

        comm_size = 1
        comm_rank = 0
        if comm is not None:
            comm_size = comm.size
            comm_rank = comm.rank

        # check the file list
        if isinstance(spectrafiles, str):
            import glob
            spectrafiles = glob.glob(spectrafiles)

        assert len(spectrafiles) > 0

        self._spectrafiles = spectrafiles

        self.cosmics_nsig = cosmics_nsig

        # This is the mapping between specs to targets for each file

        self._spec_to_target = {}
        self._target_specs = {}
        self._spec_keep = {}
        self._spec_sliced = {}

        # The bands for each file

        self._bands = {}
        self._wave = {}

        # The full list of targets from all files

        self._alltargetids = list()

        # The fibermaps from all files

        self._coadd_fmaps = {}
        self._exp_fmaps = {}
        self._tsnr2 = {}         #- template signal-to-noise from SCORES
        self.header0 = None      #- header 0 of the first spectrafile

        for sfile in spectrafiles:
            hdus = None
            nhdu = None
            input_coadded = 'unknown'
            coadd_fmap = None
            exp_fmap = None
            tsnr2 = None
            if comm_rank == 0:
                hdus = fits.open(sfile, memmap=False)
                nhdu = len(hdus)

                if self.header0 is None:
                    self.header0 = hdus[0].header.copy()

                if 'EXP_FIBERMAP' in hdus:
                    input_coadded = True
                    coadd_fmap = encode_table(Table(hdus["FIBERMAP"].data,
                        copy=True).as_array())
                    exp_fmap = encode_table(Table(hdus["EXP_FIBERMAP"].data,
                        copy=True).as_array())
                    tsnr2 = encode_table(Table(hdus["SCORES"].data,
                        copy=True).as_array())
                    for col in tsnr2.colnames.copy():
                        if col == 'TARGETID' or col.startswith('TSNR2_'):
                            continue
                        else:
                            tsnr2.remove_column(col)
                else:
                    input_coadded = False
                    tmpfmap = encode_table(Table(hdus["FIBERMAP"].data,
                        copy=True).as_array())
                    assert 'COADD_NUMEXP' not in tmpfmap.dtype.names

                    if np.all(tmpfmap['TILEID'] == tmpfmap['TILEID'][0]):
                        onetile = True
                    else:
                        onetile = False

                    coadd_fmap, exp_fmap = coadd_fibermap(tmpfmap, onetile=onetile)

                    scores = encode_table(Table(hdus["SCORES"].data,
                        copy=True).as_array())
                    tsnr2 = Table(compute_coadd_tsnr_scores(scores)[0])

                    #- we later rely upon exp_fmap having same order as the
                    #- uncoadded input fmap, so check that now
                    assert np.all(exp_fmap['TARGETID'] == tmpfmap['TARGETID'])

            if comm is not None:
                nhdu = comm.bcast(nhdu, root=0)
                input_coadded = comm.bcast(input_coadded, root=0)
                coadd_fmap = comm.bcast(coadd_fmap, root=0)
                exp_fmap = comm.bcast(exp_fmap, root=0)
                tsnr2 = comm.bcast(tsnr2, root=0)
                self.header0 = comm.bcast(self.header0, root=0)

            # Now every process has the fibermap and number of HDUs.  Build the
            # mapping between spectral rows and target IDs.

            if targetids is None:
                keep_targetids = coadd_fmap["TARGETID"]
            else:
                keep_targetids = targetids

            # Select a subset of the target range from each file if desired.

            if first_target is None:
                first_target = 0
            if first_target > len(keep_targetids):
                raise RuntimeError("first_target value \"{}\" is beyond the "
                    "number of selected targets in the file".\
                    format(first_target))

            if n_target is None:
                nkeep = len(keep_targetids)
            else:
                nkeep = n_target

            if first_target + nkeep > len(keep_targetids):
                msg = "Requested first_target ({}) + nkeep ({})".format(
                        first_target, nkeep)
                msg += " is larger than number of selected targets ({})".format(
                        len(keep_targetids))
                raise RuntimeError(msg)

            keep_targetids = keep_targetids[first_target:first_target+nkeep]

            self._alltargetids.extend(keep_targetids)

            # This is the spectral row to target mapping using the original
            # global indices (before slicing).

            if input_coadded:
                input_targetids = coadd_fmap['TARGETID']
            else:
                input_targetids = exp_fmap['TARGETID']

            self._spec_to_target[sfile] = [ x if y in keep_targetids else -1 \
                for x, y in enumerate(input_targetids) ]

            # The reduced set of spectral rows.

            self._spec_keep[sfile] = [ x for x in self._spec_to_target[sfile] \
                if x >= 0 ]

            # The mapping between original spectral indices and the sliced ones

            self._spec_sliced[sfile] = { x : y for y, x in \
                enumerate(self._spec_keep[sfile]) }

            # Slice the fibermap to keep just the requested targets
            keep_coadd = np.isin(coadd_fmap['TARGETID'], keep_targetids)
            self._coadd_fmaps[sfile] = coadd_fmap[keep_coadd]
            self._tsnr2[sfile] = tsnr2[keep_coadd]

            keep_exp = np.isin(exp_fmap['TARGETID'], keep_targetids)
            self._exp_fmaps[sfile] = exp_fmap[keep_exp]

            if input_coadded:
                input_targetids = input_targetids[keep_coadd]
            else:
                input_targetids = input_targetids[keep_exp]

            # For each target, store the sliced row index of all spectra,
            # so that we can do a fast lookup later.

            self._target_specs[sfile] = {}
            for id in keep_targetids:
                self._target_specs[sfile][id] = [ x for x, y in \
                    enumerate(input_targetids) if y == id ]

            # We need some more metadata information for each file-
            # specifically, the bands that are used and their wavelength grids.
            # That information will allow us to pre-allocate our local target
            # list and then fill that with one pass through all HDUs in the
            # files.

            self._bands[sfile] = []
            self._wave[sfile] = dict()

            if comm_rank == 0:
                for h in range(nhdu):
                    name = None
                    if "EXTNAME" not in hdus[h].header:
                        continue
                    name = hdus[h].header["EXTNAME"]
                    mat = re.match(r"(.*)_(.*)", name)
                    if mat is None:
                        continue
                    band = mat.group(1).lower()
                    htype = mat.group(2)
                    if htype == "WAVELENGTH":
                        if band not in self._bands[sfile]:
                            self._bands[sfile].append(band)
                        self._wave[sfile][band] = \
                            hdus[h].data.astype(np.float64).copy()

            if comm is not None:
                self._bands[sfile] = comm.bcast(self._bands[sfile], root=0)
                self._wave[sfile] = comm.bcast(self._wave[sfile], root=0)

            if comm_rank == 0:
                hdus.close()

        # _alltargetids can have repeats from multiple files.  Trim to
        # unique set while retaining order in which they appeared

        sortedidx = np.unique(self._alltargetids, return_index=True)[1]
        ii = np.argsort(sortedidx)
        unique_targetids = np.asarray(self._alltargetids)[sortedidx[ii]]
        self._alltargetids = unique_targetids
        self._keep_targets = unique_targetids.copy()

        # Now we have the metadata for all targets in all files.  Distribute
        # the targets among process weighted by the amount of work to do for
        # each target.  This weight is either "1" if we are going to use coadds
        # or the number of spectra if we are using all the data.

        tweights = None
        if not coadd:
            tweights = dict()
            for t in self._keep_targets:
                tweights[t] = 0
                for sfile in spectrafiles:
                    if t in self._target_specs[sfile]:
                        tweights[t] += len(self._target_specs[sfile][t])

        self.capacities = capacities
        if self.capacities is None:
            self.is_lopsided = False
            self._proc_targets = distribute_work(comm_size,
                self._keep_targets, weights=tweights)
        else:
            self.is_lopsided = True
            self._proc_targets = distribute_work(comm_size,
                self._keep_targets, weights=tweights, capacities=self.capacities)

        self._my_targets = self._proc_targets[comm_rank]

        # Reverse mapping- target ID to index in our list
        self._my_target_indx = {y : x for x, y in enumerate(self._my_targets)}

        # Now every process has its local target IDs assigned.  Pre-create our
        # local target list with empty spectral data (except for wavelengths)

        self._my_data = list()

        for t in self._my_targets:
            speclist = list()
            for sfile in spectrafiles:
                for b in self._bands[sfile]:
                    if t in self._target_specs[sfile]:
                        nspec = len(self._target_specs[sfile][t])
                        for s in range(nspec):
                            sindx = self._target_specs[sfile][t][s]
                            speclist.append(Spectrum(self._wave[sfile][b],
                                None, None, None, None))

            self._my_data.append(Target(t, speclist, coadd=False))

        # Iterate over the data and broadcast.  Every process selects the rows
        # of each table that contain pieces of local target data and copies it
        # into place.

        # these are for tracking offsets within the spectra for each target.
        tspec_flux = { x : 0 for x in self._my_targets }
        tspec_ivar = tspec_flux.copy()
        tspec_mask = tspec_flux.copy()
        tspec_res = tspec_flux.copy()

        for sfile in spectrafiles:
            rows = self._spec_keep[sfile]
            if len(rows) == 0:
                continue

            hdus = None
            if comm_rank == 0:
                hdus = fits.open(sfile, memmap=False)

            for b in self._bands[sfile]:
                extname = "{}_{}".format(b.upper(), "FLUX")
                hdata = None
                badflux = None
                if comm_rank == 0:
                    hdata = hdus[extname].data[rows]
                    # check for NaN and Inf here (should never happen of course)
                    badflux = np.isnan(hdata) | np.isinf(hdata) | np.isneginf(hdata)
                    hdata[badflux] = 0.0
                if comm is not None:
                    hdata = comm.bcast(hdata, root=0)
                    badflux = comm.bcast(badflux, root=0)

                toff = 0
                for t in self._my_targets:
                    if t in self._target_specs[sfile]:
                        for trow in self._target_specs[sfile][t]:
                            self._my_data[toff].spectra[tspec_flux[t]].flux = \
                                hdata[trow].astype(np.float64).copy()
                            tspec_flux[t] += 1
                    toff += 1

                extname = "{}_{}".format(b.upper(), "IVAR")
                hdata = None
                if comm_rank == 0:
                    hdata = hdus[extname].data[rows]
                    # check for NaN and Inf here (should never happen of course)
                    bad = np.isnan(hdata) | np.isinf(hdata) | np.isneginf(hdata)
                    hdata[bad] = 0.0
                    hdata[badflux] = 0.0 # also set ivar=0 to bad flux
                if comm is not None:
                    hdata = comm.bcast(hdata, root=0)

                toff = 0
                for t in self._my_targets:
                    if t in self._target_specs[sfile]:
                        for trow in self._target_specs[sfile][t]:
                            self._my_data[toff].spectra[tspec_ivar[t]].ivar = \
                                hdata[trow].astype(np.float64).copy()
                            tspec_ivar[t] += 1
                    toff += 1

                extname = "{}_{}".format(b.upper(), "MASK")
                hdata = None
                if comm_rank == 0:
                    if extname in hdus:
                        hdata = hdus[extname].data[rows]
                if comm is not None:
                    hdata = comm.bcast(hdata, root=0)

                if hdata is not None:
                    toff = 0
                    for t in self._my_targets:
                        if t in self._target_specs[sfile]:
                            for trow in self._target_specs[sfile][t]:
                                self._my_data[toff].spectra[tspec_mask[t]]\
                                    .ivar *= (hdata[trow] == 0)
                                tspec_mask[t] += 1
                        toff += 1

                extname = "{}_{}".format(b.upper(), "RESOLUTION")
                hdata = None
                if comm_rank == 0:
                    hdata = hdus[extname].data[rows]

                if comm is not None:
                    hdata = comm.bcast(hdata, root=0)

                toff = 0
                for t in self._my_targets:
                    if t in self._target_specs[sfile]:
                        for trow in self._target_specs[sfile][t]:
                            dia = Resolution(hdata[trow].astype(np.float64))
                            self._my_data[toff].spectra[tspec_res[t]].R = dia
                            #- Coadds replace Rcsr so only compute if not coadding
                            if not coadd and cache_Rcsr:
                                self._my_data[toff].spectra[tspec_res[t]].Rcsr = dia.tocsr()
                            tspec_res[t] += 1
                    toff += 1

                del hdata

            if comm_rank == 0:
                hdus.close()

        # Compute the coadds now if we are going to use those

        if coadd:
            for t in self._my_data:
                t.compute_coadd(cache_Rcsr,cosmics_nsig=self.cosmics_nsig)

        self.fibermap = Table(np.hstack([ self._coadd_fmaps[x] \
            for x in self._spectrafiles ]))

        self.exp_fibermap = Table(np.hstack([ self._exp_fmaps[x] \
            for x in self._spectrafiles ]))

        self.tsnr2 = Table(np.hstack([ self._tsnr2[x] \
            for x in self._spectrafiles ]))

        super(DistTargetsDESI, self).__init__(self._keep_targets, comm=comm)


    def _local_target_ids(self):
        return self._my_targets

    def _local_data(self):
        return self._my_data


def rrdesi(options=None, comm=None):
    """Estimate redshifts for DESI targets.

    This loads distributed DESI targets from one or more spectra grouping
    files and computes the redshifts.  The outputs are written to a redrock
    scan file and a DESI redshift catalog.

    Args:
        options (list): optional list of commandline options to parse.
        comm (mpi4py.Comm): MPI communicator to use.

    """
    global_start = elapsed(None, "", comm=comm)

    parser = argparse.ArgumentParser(description="Estimate redshifts from"
        " DESI target spectra.")

    parser.add_argument("-t", "--templates", type=str, default=None,
        required=False, help="template file or directory")

    parser.add_argument("--archetypes", type=str, default=None,
        required=False,
        help="archetype file or directory for final redshift comparison")

    parser.add_argument("-d", "--details", type=str, default=None,
        required=False, help="output file for full redrock fit details")

    parser.add_argument("-o", "--outfile", type=str, default=None,
        required=False, help="output FITS file with best redshift per target")

    parser.add_argument("--targetids", type=str, default=None,
        required=False, help="comma-separated list of target IDs")

    parser.add_argument("--mintarget", type=int, default=None,
        required=False, help="first target to process in each file")

    parser.add_argument("--priors", type=str, default=None,
        required=False, help="optional redshift prior file")

    parser.add_argument("--chi2-scan", type=str, default=None,
        required=False, help="Load the chi2-scan from the input file")

    parser.add_argument("-n", "--ntargets", type=int,
        required=False, help="the number of targets to process in each file")

    parser.add_argument("--nminima", type=int, default=3,
        required=False, help="the number of redshift minima to search")

    parser.add_argument("--allspec", default=False, action="store_true",
        required=False, help="use individual spectra instead of coadd")

    parser.add_argument("--ncpu", type=int, default=None,
        required=False, help="DEPRECATED: the number of multiprocessing"
            " processes; use --mp instead")

    parser.add_argument("--mp", type=int, default=0,
        required=False, help="if not using MPI, the number of multiprocessing"
            " processes to use (defaults to half of the hardware threads)")

    parser.add_argument("--no-skymask", default=False, action="store_true",
        required=False, help="Do not do extra masking of sky lines")

    parser.add_argument("--no-mpi-abort", default=False, action="store_true",
        required=False, help="Do not call MPI Abort upon failure of a single rank")

    parser.add_argument("--debug", default=False, action="store_true",
        required=False, help="debug with ipython (only if communicator has a "
        "single process)")

    parser.add_argument("--cosmics-nsig", type=float, default=0,
        required=False, help="n sigma cosmic ray threshold in coaddition")

    parser.add_argument("-i", "--infiles", nargs='+', required=True,
            help="Input spectra, coadd, or cframe files")

    parser.add_argument("--gpu", action="store_true",
        required=False, help="use GPUs")

    parser.add_argument("--max-gpuprocs", type=int, default=None,
        required=False, help="limit number of MPI processes using GPUs")

    args = None
    if options is None:
        args = parser.parse_args()
    else:
        args = parser.parse_args(options)

    if args.ncpu is not None:
        print('WARNING: --ncpu is deprecated; use --mp instead')
        args.mp = args.ncpu

    comm_size = 1
    comm_rank = 0
    if comm is not None:
        comm_size = comm.size
        comm_rank = comm.rank

    # Check arguments- all processes have this, so just check on the first
    # process

    if comm_rank == 0:
        if args.debug and comm_size != 1:
            print("--debug can only be used if the communicator has one "
                " process")
            sys.stdout.flush()
            if comm is not None:
                comm.Abort()

        if (args.details is None) and (args.outfile is None):
            parser.print_help()
            print("ERROR: --details or --outfile required")
            sys.stdout.flush()
            if comm is not None:
                comm.Abort()
            else:
                sys.exit(1)

        if len(args.infiles) == 0:
            print("ERROR: must provide input files")
            sys.stdout.flush()
            if comm is not None:
                comm.Abort()
            else:
                sys.exit(1)

        if (args.targetids is not None) and ((args.mintarget is not None) \
            or (args.ntargets is not None)):
            print("ERROR: cannot select targets by both ID and range")
            sys.stdout.flush()
            if comm is not None:
                comm.Abort()
            else:
                sys.exit(1)

        if args.gpu:
            try:
                import cupy
                gpu_ok = cupy.is_available()
            except ImportError:
                gpu_ok = False
            if not gpu_ok:
                print("ERROR: cupy or GPU not available")
                sys.stdout.flush()
                if comm is not None:
                    comm.Abort()
                else:
                    sys.exit(1)


    targetids = None
    if args.targetids is not None:
        targetids = [ int(x) for x in args.targetids.split(",") ]

    n_target = None
    if args.ntargets is not None:
        n_target = args.ntargets

    first_target = None
    if args.mintarget is not None:
        first_target = args.mintarget
    elif n_target is not None:
        first_target = 0

    # Multiprocessing processes to use if MPI is disabled.
    mpprocs = 0
    if comm is None:
        mpprocs = get_mp(args.mp)
        print("Running with {} processes".format(mpprocs))
        if "OMP_NUM_THREADS" in os.environ:
            nthread = int(os.environ["OMP_NUM_THREADS"])
            if nthread != 1:
                print("WARNING:  {} multiprocesses running, each with "
                    "{} threads ({} total)".format(mpprocs, nthread,
                    mpprocs*nthread))
                print("WARNING:  Please ensure this is <= the number of "
                    "physical cores on the system")
        else:
            print("WARNING:  using multiprocessing, but the OMP_NUM_THREADS")
            print("WARNING:  environment variable is not set- your system may")
            print("WARNING:  be oversubscribed.")
        sys.stdout.flush()
    elif comm_rank == 0:
        print("Running with {} processes".format(comm_size))
        sys.stdout.flush()

    # GPU configuration
    if args.gpu:
        # Determine which processes will use a GPU
        if args.max_gpuprocs is not None:
            max_gpuprocs = args.max_gpuprocs
        else:
            #Check actual number of GPUs available
            if (comm is not None):
                #Use custom method that checks PCI ids for MPI
                max_gpuprocs = getGPUCountMPI(comm)
            else:
                #cupy getDeviceCount works for non MPI
                import cupy
                max_gpuprocs = cupy.cuda.runtime.getDeviceCount()
        use_gpu = comm_rank < max_gpuprocs

        # Determine cpu/gpu process capacities for target distribution
        if comm is not None:
            gpu_proc_flags = comm.allgather(use_gpu)
        else:
            gpu_proc_flags = [use_gpu, ]
            if (mpprocs > 1):
                #Force mpprocs == 1 for multiprocessing mode with GPU
                print("WARNING:  using GPU mode without MPI requires --mp 1")
                print("WARNING:  Overriding {} multiprocesses to force this.".format(mpprocs))
                print("WARNING:  Running with 1 process.")
                mpprocs = 1
        ngpu_procs = sum(gpu_proc_flags)
        ncpu_procs = comm_size - ngpu_procs
        if ngpu_procs > 0 and ncpu_procs > 0:
            # On Perlmutter, 1:15 seems like a good ratio
            #capacities = [1 if is_gpu_proc else 1.0/15 for is_gpu_proc in gpu_proc_flags]
            #With new GPU implementation of zscan, use 1:10000 so that only GPU-enabled
            #procs get allocated targets
            capacities = [1 if is_gpu_proc else 1.0/10000 for is_gpu_proc in gpu_proc_flags]
        else:
            capacities = None

        # Redistribute templates after rebinning when using GPUs
        redistribute_templates = True
    else:
        use_gpu = False
        capacities = None
        redistribute_templates = False

    try:
        # Load and distribute the targets
        if comm_rank == 0:
            print("Loading targets...")
            sys.stdout.flush()

        start = elapsed(None, "", comm=comm)

        # Load the targets.  If comm is None, then the target data will be
        # stored in shared memory.
        targets = DistTargetsDESI(args.infiles, coadd=(not args.allspec),
                                  targetids=targetids, first_target=first_target, n_target=n_target,
                                  comm=comm, cache_Rcsr=True, cosmics_nsig=args.cosmics_nsig,
                                  capacities=capacities)

        #- Mask some problematic sky lines
        if not args.no_skymask:
            for t in targets.local():
                for s in t.spectra:
                    ii = (5572. <= s.wave) & (s.wave <= 5582.)
                    ii |= (9792. <= s.wave) & (s.wave <= 9795.)
                    s.ivar[ii] = 0.0

        # Get the dictionary of wavelength grids
        dwave = targets.wavegrids()

        stop = elapsed(start, "Read and distribution of {} targets"\
            .format(len(targets.all_target_ids)), comm=comm)

        # Read the template data
        # Pass both use_gpu (this proc) and args.gpu (if any proc is using GPU)
        dtemplates = load_dist_templates(dwave, templates=args.templates,
            comm=comm, mp_procs=mpprocs, redistribute=redistribute_templates, use_gpu=use_gpu, gpu_mode=args.gpu)

        # Compute the redshifts, including both the coarse scan and the
        # refinement.  This function only returns data on the rank 0 process.

        start = elapsed(None, "", comm=comm)

        scandata, zfit = zfind(targets, dtemplates, mpprocs,
            nminima=args.nminima, archetypes=args.archetypes,
            priors=args.priors, chi2_scan=args.chi2_scan, use_gpu=use_gpu)

        stop = elapsed(start, "Computing redshifts", comm=comm)

        # Set some DESI-specific ZWARN bits from input fibermap
        if comm_rank == 0:
            fiberstatus = targets.fibermap['COADD_FIBERSTATUS']
            poorpos = (fiberstatus & fibermask.POORPOSITION) != 0
            badpos = (fiberstatus & fibermask.BADPOSITION) != 0
            broken = (fiberstatus & fibermask.BROKENFIBER) != 0
            unassigned = (fiberstatus & fibermask.UNASSIGNED) != 0
            bad = targets.fibermap['OBJTYPE'] == 'BAD'
            sky = targets.fibermap['OBJTYPE'] == 'SKY'

            badcoverage = np.zeros(len(fiberstatus), dtype=bool)
            for key in ('BADCOLUMN', 'BADAMPB', 'BADAMPR', 'BADAMPZ'):
                if key in fibermask.names():
                    badcoverage |= (fiberstatus & fibermask.mask(key)) != 0

            targetids = targets.fibermap['TARGETID']

            ii = np.isin(zfit['targetid'], targetids[poorpos])
            zfit['zwarn'][ii] |= ZWarningMask.POORDATA

            ii = np.isin(zfit['targetid'], targetids[badpos | broken | unassigned | bad])
            zfit['zwarn'][ii] |= ZWarningMask.NODATA

            ii = np.isin(zfit['targetid'], targetids[broken])
            zfit['zwarn'][ii] |= ZWarningMask.UNPLUGGED

            ii = np.isin(zfit['targetid'], targetids[sky])
            zfit['zwarn'][ii] |= ZWarningMask.SKY

            ii = np.isin(zfit['targetid'], targetids[badcoverage])
            zfit['zwarn'][ii] |= ZWarningMask.LITTLE_COVERAGE

        # Write the outputs

        if args.details is not None:
            start = elapsed(None, "", comm=comm)
            if comm_rank == 0:
                write_zscan(args.details, scandata, zfit, clobber=True)
            stop = elapsed(start, "Writing zscan data took", comm=comm)

        if args.outfile:
            start = elapsed(None, "", comm=comm)
            if comm_rank == 0:
                zbest = zfit[zfit['znum'] == 0]

                # Remove extra columns not needed for zbest
                zbest.remove_columns(['zz', 'zzchi2', 'znum'])

                # Change to upper case like DESI
                for colname in zbest.colnames:
                    if colname.islower():
                        zbest.rename_column(colname, colname.upper())

                template_version = {t._template.full_type:t._template._version for t in dtemplates}
                archetype_version = None
                if not args.archetypes is None:
                    archetypes = All_archetypes(archetypes_dir=args.archetypes).archetypes
                    archetype_version = {name:arch._version for name, arch in archetypes.items() }

                write_zbest(args.outfile, zbest,
                        targets.fibermap, targets.exp_fibermap,
                        targets.tsnr2,
                        template_version, archetype_version,
                        spec_header=targets.header0)

            stop = elapsed(start, f"Writing {args.outfile} took", comm=comm)

    except Exception as err:
        exc_type, exc_value, exc_traceback = sys.exc_info()
        lines = traceback.format_exception(exc_type, exc_value, exc_traceback)
        lines = [ "Proc {}: {}".format(comm_rank, x) for x in lines ]
        print("--- Process {} raised an exception ---".format(comm_rank))
        print("".join(lines))
        sys.stdout.flush()
        if comm is None or args.no_mpi_abort:
            raise err
        else:
            comm.Abort()

    global_stop = elapsed(global_start, "Total run time", comm=comm)

    if args.debug:
        import IPython
        IPython.embed()

    return
