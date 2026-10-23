"""
NeXus Master File Configuration

Maps EPICS PVs to NeXus dataset locations and units for master file creation.
"""

from gui import optics_status as _optics_status

# Shared metadata queried once per scan (same for all detectors)
SHARED_METADATA_MAP = {
    "/entry/instrument/beam/incident_wavelength": {
        "pv": "12ida2:LambdaCalc",
        "units": "angstrom",
    },
    "/entry/instrument/monochromator/monoE": {
        "pv": "12ida2:EnCalc",
        "units": "keV",
    },
    "/entry/instrument/source/current": {
        "pv": "S:SRcurrentAI",
        "units": "mA",
    },
    "/entry/instrument/source/undE": {
        "pv": "S12ID:USID:EnergyM.VAL",
        "units": "keV",
    },
    "/entry/sample/sth": {
        "pv": "12idc:m8.RBV",
        "units": "mm",
    },
    "/entry/sample/stv": {
        "pv": "12idc:m7.RBV",
        "units": "mm",
    },
    "/entry/sample/theta": {
        "pv": "12idc:m5.RBV",
        "units": "degree",
    },
    # "/entry/sample/sth": {
    #     "pv": "12idc:m8.RBV",
    #     "units": "mm",
    # },
    # "/entry/sample/sth": {
    #     "pv": "12idc:m8.RBV",
    #     "units": "mm",
    # },
    # "/entry/sample/sth": {
    #     "pv": "12idc:m8.RBV",
    #     "units": "mm",
    # },
    "/entry/scalars/IC": {
        "pv": "12idc:3820:scaler1.S2",
        "units": "counts",
    },
    "/entry/scalars/BS2": {
        "pv": "12idc:3820:scaler1.S3",
        "units": "counts",
    },
    "/entry/scalars/BS": {
        "pv": "12idc:3820:scaler1.S4",
        "units": "counts",
    },
    "/entry/scalars/IfCRL": {
        "pv": "12idc:3820:scaler1.S5",
        "units": "counts",
    },
}

# Optics metadata (slits) — conditional on checkBox_usOpticsScanSave
SLITS_METADATA_MAP = {
    "/entry/instrument/slits/wbs_hor": {
        "pv": "12ida2:SL2:hSize.RBV",
        "units": "mm",
    },
    "/entry/instrument/slits/wbs_ver": {
        "pv": "12ida2:SL2:vSize.RBV",
        "units": "mm",
    },
    "/entry/instrument/slits/mh2_hor": {
        "pv": "12ida2:MH2_Slit_Ht2.C",
        "units": "mm",
    },
    "/entry/instrument/slits/mh2_ver": {
        "pv": "12ida2:MH2_Slit_Vt2.C",
        "units": "mm",
    },
    "/entry/instrument/slits/crl_hor": {
        "pv": "usxLAX:m58:c1:m8.RBV",
        "units": "mm",
    },
    "/entry/instrument/slits/crl_ver": {
        "pv": "usxLAX:m58:c1:m7.RBV",
        "units": "mm",
    },
    "/entry/instrument/slits/cl_hor": {
        "pv": "12idc:CL_SlitHt2.C",
        "units": "mm",
    },
    "/entry/instrument/slits/cl_ver": {
        "pv": "12idc:CL_SlitVt2.C",
        "units": "mm",
    },
}

# Optics metadata (zone plate) — conditional on checkBox_usOpticsScanSave.
#
# PVs come from optics_status, the same table the optics panel drives and the
# Check Upstream Optics report reads, so these cannot drift out of step with
# the hardware map. Only the NeXus naming lives here, and it is not a
# one-to-one transliteration -- NeXus x/y are the motors' hor/ver, so keep
# this mapping explicit rather than deriving the names from the motor names.
_ZONE_PLATE_MOTORS = {
    "zp_x":  "ZP_hor",
    "zp_y":  "ZP_ver",
    "zp_z":  "BSZP_Ztrans",
    "bs_x":  "BS_hor",
    "bs_y":  "BS_ver",
    "osa_x": "OSA_X",
    "osa_y": "OSA_Y",
    "osa_z": "OSA_Z",
}

ZONE_PLATE_METADATA_MAP = {
    "/entry/instrument/zone_plate/%s" % _name: {
        "pv": _optics_status.PV_BASES[_motor] + ".RBV",
        "units": "mm",
    }
    for _name, _motor in _ZONE_PLATE_MOTORS.items()
}

# Detector-specific configurations
DETECTOR_CONFIGS = {
    "SAXS": {
        "name": "SAXS/GISAXS",
        "description": "Dectris Pilatus 2M",
        "type": "pixel array",
        "sensor_material": "Si",
        "sensor_thickness": 0.32,
        "x_pixel_size": 0.172,
        "y_pixel_size": 0.172,
        "bit_depth_image": 20,
        "bit_depth_readout": 20,
        "saturation_value": 1048575,
        "detector_readout_time": 2.3,
        "detector_pvs": {
            "/entry/instrument/detector/ExposureTime": {
                "pv": "S12-PILATUS1:cam1:AcquireTime_RBV",
                "units": "s",
            },
            "/entry/instrument/detector/distance": {
                "pv": "12idcACS1:m1.RBV",
                "units": "mm",
            },
            "/entry/instrument/detector/detector_xrayE": {
                "pv": "S12-PILATUS1:cam1:Energy_RBV",
                "units": "keV",
            },
            "/entry/instrument/detector/detector_threshold1": {
                "pv": "S12-PILATUS1:cam1:ThresholdEnergy_RBV",
                "units": "keV",
            },
            "/entry/instrument/detector/acquire_time": {
                "pv": "S12-PILATUS1:cam1:AcquirePeriod_RBV",
                "units": "s",
            },
            "/entry/instrument/detector/SAXS_x": {
                "pv": "12idcACS1:m5.RBV",
                "units": "mm",
            },
            "/entry/instrument/detector/SAXS_y": {
                "pv": "12idcACS1:m3.RBV",
                "units": "mm",
            },
            "/entry/instrument/detector/SAXS_beam_block": {
                "pv": "12idcACS1:m7.RBV",
                "units": "mm",
            },
            "/entry/instrument/detector/SAXS_beam_stop": {
                "pv": "12ideSFT:m4.RBV",
                "units": "mm",
            },
        }
    },
    "WAXS": {
        "name": "WAXS",
        "description": "Dectris Pilatus CdTe 300K",
        "type": "pixel array",
        "sensor_material": "CdTe",
        "sensor_thickness": 0.75,
        "x_pixel_size": 0.172,
        "y_pixel_size": 0.172,
        "bit_depth_image": 20,
        "bit_depth_readout": 20,
        "saturation_value": 1048575,
        "detector_readout_time": 2.3,
        "detector_pvs": {
            "/entry/instrument/detector/distance": {
                "pv": "12idcACS1:m2.RBV",
                "units": "mm",
            },
            "/entry/instrument/detector/detector_xrayE": {
                "pv": "S12-PILATUS2:cam1:Energy_RBV",
                "units": "keV",
            },
            "/entry/instrument/detector/detector_threshold1": {
                "pv": "S12-PILATUS2:cam1:ThresholdEnergy_RBV",
                "units": "keV",
            },
            "/entry/instrument/detector/acquire_time": {
                "pv": "S12-PILATUS2:cam1:AcquirePeriod_RBV",
                "units": "s",
            },
        }
    }
}
