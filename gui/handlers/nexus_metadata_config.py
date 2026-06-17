"""
NeXus Master File Configuration

Maps EPICS PVs to NeXus dataset locations and units for master file creation.
"""

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

# Optics metadata (zone plate) — conditional on checkBox_usOpticsScanSave
ZONE_PLATE_METADATA_MAP = {
    "/entry/instrument/zone_plate/zp_x": {
        "pv": "12idc:m13.RBV",
        "units": "mm",
    },
    "/entry/instrument/zone_plate/zp_y": {
        "pv": "12idc:m12.RBV",
        "units": "mm",
    },
    "/entry/instrument/zone_plate/zp_z": {
        "pv": "12idc:m14.RBV",
        "units": "mm",
    },
    "/entry/instrument/zone_plate/bs_x": {
        "pv": "12idc:m11.RBV",
        "units": "mm",
    },
    "/entry/instrument/zone_plate/bs_y": {
        "pv": "12idc:m10.RBV",
        "units": "mm",
    },
    "/entry/instrument/zone_plate/osa_x": {
        "pv": "12idc:m9.RBV",
        "units": "mm",
    },
    "/entry/instrument/zone_plate/osa_y": {
        "pv": "12idc:m16.RBV",
        "units": "mm",
    },
    "/entry/instrument/zone_plate/osa_z": {
        "pv": "12idc:m15.RBV",
        "units": "mm",
    },
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
            "/entry/instrument/detector/distance": {
                "pv": "12idcACS1:m1.RBV",
                "units": "mm",
            },
            "/entry/instrument/detector/detector_xrayE": {
                "pv": "S12-PILATUS1:cam1:Energy_RBV",
                "units": "eV",
            },
            "/entry/instrument/detector/detector_threshold1": {
                "pv": "S12-PILATUS1:cam1:ThresholdEnergy_RBV",
                "units": "eV",
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
                "units": "eV",
            },
            "/entry/instrument/detector/detector_threshold1": {
                "pv": "S12-PILATUS2:cam1:ThresholdEnergy_RBV",
                "units": "eV",
            },
            "/entry/instrument/detector/acquire_time": {
                "pv": "S12-PILATUS2:cam1:AcquirePeriod_RBV",
                "units": "s",
            },
        }
    }
}
