"""qptiff: the vocabulary this pipeline writes about a qptiff, and the translation onto it.

Holds the qptiff features, the ``QptiffChannels`` Record type, the two tables that map the reader's
field names onto features, and the functions that walk them. ``image_type`` lives in :mod:`vocab`.
Keep the ``qptiff_`` prefix on every feature added here: it marks what the file states, as opposed
to what the pipeline infers.

Pure: no lamindb, no bioio. Every function takes the reader's ``QptiffMetadata`` or the stored
``qptiff_metadata`` blob (the same dataclass after ``dataclasses.asdict``) and returns plain dicts.

The reader's field names appear only in the mapping tables. Four fields are handled outside them:
``color_rgb`` (a 3-tuple to flatten), and ``acquisition_format``, ``channel_locus`` and
``structure_signature``, which sit on the root object.
"""
from __future__ import annotations

import dataclasses
import re
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any

from qptiff_pipeline.domain.declare import Declarer

#: This module's vocabulary. Merged by :mod:`vocab`.
DECLARED = Declarer("qptiff")

# qptiff integrity and modality, set by check-qptiffs and copied from slide to TMA.
INTEGRITY_FEATURE = DECLARED.feature(
    "qptiff_integrity", str,
    'Readability probe on the qptiff Artifact: "ok" or "unreadable". Absent = never checked.')
#: The only two values INTEGRITY_FEATURE takes. check-qptiffs writes them. mint-slides mints only
#: INTEGRITY_OK qptiffs, so a corrupt file never reaches general-detect.
INTEGRITY_OK = "ok"
INTEGRITY_UNREADABLE = "unreadable"
QPTIFF_N_CHANNELS = DECLARED.feature("qptiff_n_channels", int, "Channel count read from the qptiff.")
QPTIFF_CHANNEL_NAMES = DECLARED.feature(
    "qptiff_channel_names", str, "Channel names, comma-joined, in file order.")
QPTIFF_DIMS = DECLARED.feature("qptiff_dims", str, 'Dimensions, e.g. "C=40,Y=1024,X=1024".')
# A str, not an Artifact: it holds the storage key of the thumbnail PNG.
QPTIFF_THUMBNAIL = DECLARED.feature(
    "qptiff_thumbnail", str, "Storage KEY (not the Artifact) of the qptiff's macro thumbnail PNG.")

# qptiff acquisition metadata, read from the file's own PerkinElmer XML.
# Everything above this line the pipeline infers. Everything below, the file states. Keep the
# `qptiff_` prefix on anything added here.
#
# Every field of the reader's slide, image_info, camera and scan_resolution scopes is a flat
# feature. `tests/test_qptiff_metadata.py` checks that this is complete.
QPTIFF_IMAGE_TYPE = DECLARED.feature(
    "qptiff_image_type", str, "Modality as recorded by the scanner. Compare against `image_type`.")
QPTIFF_IS_TMA = DECLARED.feature(
    "qptiff_is_tma", bool, "Scanner's own TMA flag. Compare against what general-detect found.")
# The physical pixel size. Any measurement in microns needs it.
QPTIFF_SCALE_FACTOR = DECLARED.feature(
    "qptiff_scale_factor", float, "Physical size of one full-res pixel, in scale_factor_unit.")
QPTIFF_SCALE_FACTOR_UNIT = DECLARED.feature(
    "qptiff_scale_factor_unit", str, "Unit for scale_factor (typically um). Meaningless without it.")
QPTIFF_MAGNIFICATION = DECLARED.feature(
    "qptiff_magnification", float, "Objective magnification used for the scan.")
QPTIFF_OBJECTIVE = DECLARED.feature("qptiff_objective", str, "Objective, as named by the instrument.")
QPTIFF_BARCODE = DECLARED.feature(
    "qptiff_barcode", str, "Slide barcode read by the scanner. The link to the wet-lab record.")
QPTIFF_SLIDE_ID = DECLARED.feature("qptiff_slide_id", str, "Scanner's own slide identifier.")
QPTIFF_STUDY_NAME = DECLARED.feature("qptiff_study_name", str, "Study the scan was filed under.")
# The vendor stamp twice: `qptiff_datetime` is the text as written, `qptiff_acquired_at` the parsed
# instant (absent when the text does not parse).
# Never change `qptiff_datetime`'s dtype. Stored rows hold strings, and an in-place dtype change
# leaves them mismatched without any error.
QPTIFF_DATETIME = DECLARED.feature(
    "qptiff_datetime", str,
    "Acquisition timestamp exactly as the vendor wrote it, unparsed. Prefer qptiff_acquired_at for "
    "anything that compares or orders; this one is the audit trail for a stamp that will not parse.")
QPTIFF_ACQUIRED_AT = DECLARED.feature(
    "qptiff_acquired_at", datetime,
    "When the slide was acquired, parsed from the vendor stamp. Naive: the file records no timezone, "
    "and inventing UTC would make two scanners in different places compare as if they agreed.")
QPTIFF_INSTRUMENT_TYPE = DECLARED.feature(
    "qptiff_instrument_type", str, "Scanner model, as the instrument reported it.")
QPTIFF_ACQUISITION_SOFTWARE = DECLARED.feature(
    "qptiff_acquisition_software", str, "Software version that produced the file.")
QPTIFF_ACQUISITION_FORMAT = DECLARED.feature(
    "qptiff_acquisition_format", str,
    "File layout family (brightfield / fusion-paged / polaris-scanband / unknown). DEPRECATED as a "
    "dispatch key - see qptiff_channel_locus, which is what actually decides how pages map to "
    "channels. Still recorded verbatim: it is what already-written stores hold.")
#: The reader's dispatch key: where the per-channel fields live. This, not ``acquisition_format``,
#: decides how pages map to channels. See ``pipeline.qptiff_channel_locus_is_known``.
QPTIFF_CHANNEL_LOCUS = DECLARED.feature(
    "qptiff_channel_locus", str,
    "Where the file keeps its per-channel metadata (rgb-samples / shared-scanbands / per-page-root / "
    "unknown). This, NOT qptiff_acquisition_format, is what decides how pages map to channels.")
#: The values :data:`QPTIFF_CHANNEL_LOCUS` takes. These are the reader's own ``LOCUS_*`` strings,
#: respelled here because ``domain`` never imports bioio.
LOCUS_RGB_SAMPLES = "rgb_samples"
LOCUS_SHARED_SCANBANDS = "shared_scanbands"
LOCUS_PER_PAGE_ROOT = "per_page_root"
#: The reader recognised no locus and tried strategies in order. Channel metadata is least
#: trustworthy for these files.
LOCUS_UNKNOWN = "unknown"
#: Every locus that identifies a dialect. Excludes LOCUS_UNKNOWN.
LOCUS_KNOWN = frozenset({LOCUS_RGB_SAMPLES, LOCUS_SHARED_SCANBANDS, LOCUS_PER_PAGE_ROOT})

#: The structural signals the locus was derived from. A locus can be re-checked from these without
#: reopening the files.
QPTIFF_STRUCTURE_SIGNATURE = DECLARED.feature(
    "qptiff_structure_signature", dict,
    "Structural signals the channel locus was derived from, as the reader reported them.")
QPTIFF_SCAN_PROFILE_NAME = DECLARED.feature(
    "qptiff_scan_profile_name", str, "Named acquisition profile. Groups slides run the same way.")
QPTIFF_SCAN_MODE = DECLARED.feature("qptiff_scan_mode", str, "Scan mode recorded by the instrument.")
QPTIFF_OPAL_KIT_TYPE = DECLARED.feature(
    "qptiff_opal_kit_type", str, "Opal staining kit, when the acquisition declared one.")
QPTIFF_STORED_BITS = DECLARED.feature(
    "qptiff_stored_bits_per_sample", int, "Bits actually stored per sample.")
QPTIFF_COMPRESSION = DECLARED.feature("qptiff_compression", str, "Pixel compression scheme.")
QPTIFF_CAMERA_NAME = DECLARED.feature("qptiff_camera_name", str, "Camera used for the scan.")
QPTIFF_CAMERA_BIT_DEPTH = DECLARED.feature(
    "qptiff_camera_bit_depth", int, "Camera bit depth, which bounds the usable signal range.")

# The rest of the four slide/image scopes.
#
# operator_name and computer_name identify a person and a machine. They are the only personal data
# in this vocabulary.
QPTIFF_OPERATOR_NAME = DECLARED.feature(
    "qptiff_operator_name", str, "Instrument operator who ran the scan. PERSONAL DATA.")
QPTIFF_COMPUTER_NAME = DECLARED.feature(
    "qptiff_computer_name", str, "Acquisition workstation hostname. Identifies a machine.")
QPTIFF_VALIDATION_CODE = DECLARED.feature(
    "qptiff_validation_code", str, "Vendor validation code recorded with the acquisition.")
QPTIFF_IDENTIFIER = DECLARED.feature(
    "qptiff_identifier", str, "Vendor identifier for this acquisition, distinct from slide_id.")
QPTIFF_DESCRIPTION_VERSION = DECLARED.feature(
    "qptiff_description_version", str, "Schema version of the embedded XML description block.")
QPTIFF_SAMPLE_DESCRIPTION = DECLARED.feature(
    "qptiff_sample_description", str, "Free-text sample description entered at acquisition.")

QPTIFF_BF_LAMP_TYPE = DECLARED.feature("qptiff_bf_lamp_type", str, "Brightfield lamp type, when used.")
QPTIFF_LAMP_TYPE = DECLARED.feature("qptiff_lamp_type", str, "Lamp type for the acquisition.")
QPTIFF_JPEG_QUALITY = DECLARED.feature(
    "qptiff_jpeg_quality", int, "JPEG quality, where the pixels are JPEG-compressed.")
QPTIFF_SATURATION_PROTECTION = DECLARED.feature(
    "qptiff_saturation_protection_type", str, "Saturation-protection mode used during acquisition.")
QPTIFF_COVERSLIP_THICKNESS = DECLARED.feature(
    "qptiff_coverslip_thickness", str,
    "Coverslip thickness as recorded. A str, not a float: the vendor writes a label, not a measure.")
QPTIFF_IS_RNA = DECLARED.feature("qptiff_is_rna", bool, "Scanner's RNA-assay flag for this acquisition.")
QPTIFF_ROTATE_IMAGE = DECLARED.feature(
    "qptiff_rotate_image", bool, "Acquisition asked for the image to be rotated on read.")
QPTIFF_MIRROR_IMAGE = DECLARED.feature(
    "qptiff_mirror_image", bool, "Acquisition asked for the image to be mirrored on read.")
# Stage coordinates of this image on the slide - what relates two scans of the same physical slide.
QPTIFF_XPOSITION_UM = DECLARED.feature("qptiff_xposition_um", float, "Stage x position, microns.")
QPTIFF_YPOSITION_UM = DECLARED.feature("qptiff_yposition_um", float, "Stage y position, microns.")

QPTIFF_CAMERA_TYPE = DECLARED.feature("qptiff_camera_type", str, "Camera type, as the instrument reports it.")
QPTIFF_CAMERA_GAIN = DECLARED.feature("qptiff_camera_gain", float, "Camera gain for the acquisition.")
QPTIFF_CAMERA_BINNING = DECLARED.feature("qptiff_camera_binning", int, "Camera binning for the acquisition.")

# Prefixed by scope: `binning` and `objective_name` collide with camera and image_info fields.
QPTIFF_SCAN_RES_OBJECTIVE_NAME = DECLARED.feature(
    "qptiff_scan_resolution_objective_name", str, "Objective named by the scan-resolution block.")
QPTIFF_SCAN_RES_BINNING = DECLARED.feature(
    "qptiff_scan_resolution_binning", int, "Binning named by the scan-resolution block.")
# Deprecated in the reader in favour of scale_factor + unit. Some files carry only this.
QPTIFF_BASE_PIXEL_SIZE_UM = DECLARED.feature(
    "qptiff_base_pixel_size_um", float, "Legacy pixel size, microns. Prefer qptiff_scale_factor.")

#: The complete parsed metadata, nested, as the reader returned it, minus ``raw_xml``. Stored as
#: lamindb JsonValue: retrievable, but reachable only through JSONB lookups.
# The builtin `dict`, not the string "dict": lamindb's `serialize_dtype` rejects the string.
QPTIFF_METADATA = DECLARED.feature(
    "qptiff_metadata", dict, "Complete parsed qptiff metadata, nested, minus raw_xml.")

# One Record per qptiff channel: the N x M acquisition table.
# Not an ROI type: declared parentless, outside the ROI invariants.
#
# Each Record points at its qptiff through the foundation's `source_artifact`. Do not declare an
# Artifact-dtype feature for this in this vocabulary: the checks treat one as an ROI role.
QPTIFF_CHANNELS_TYPE = DECLARED.record_type(
    "QptiffChannels", "One channel of one qptiff, with the acquisition settings it was taken under.")
QPTIFF_CHANNELS = DECLARED.typed(
    "qptiff_channels", QPTIFF_CHANNELS_TYPE,
    "The channel Records of this qptiff. MULTI-valued: one value per channel.")

# `qptiff_ch_` prefix, distinct from `qptiff_channel_names`. Feature names are global in lamindb.
QPTIFF_CH_INDEX = DECLARED.feature("qptiff_ch_index", int, "Zero-based position of this channel in the file.")
QPTIFF_CH_NAME = DECLARED.feature("qptiff_ch_name", str, "Channel name as recorded.")
QPTIFF_CH_FLUOROPHORE = DECLARED.feature("qptiff_ch_fluorophore", str, "Fluorophore for this channel.")
QPTIFF_CH_IS_BRIGHTFIELD = DECLARED.feature(
    "qptiff_ch_is_brightfield", bool, "True for a brightfield channel.")
# Set for a spectrally unmixed component. The table holds both those and physical channels.
QPTIFF_CH_IS_UNMIXED = DECLARED.feature(
    "qptiff_ch_is_unmixed_component", bool, "True for an unmixed component, not a physical channel.")
QPTIFF_CH_EMISSION_NM = DECLARED.feature(
    "qptiff_ch_emission_wavelength_nm", float, "Emission wavelength, nm.")
QPTIFF_CH_EXCITATION_NM = DECLARED.feature(
    "qptiff_ch_excitation_wavelength_nm", float, "Excitation wavelength, nm.")
#: The passband edges of the active band, beside the midpoints. The edges keep the passband width,
#: which the midpoint alone loses.
QPTIFF_CH_EMISSION_CUT_ON_NM = DECLARED.feature(
    "qptiff_ch_emission_cut_on_nm", float, "Emission passband cut-on edge, nm.")
QPTIFF_CH_EMISSION_CUT_OFF_NM = DECLARED.feature(
    "qptiff_ch_emission_cut_off_nm", float, "Emission passband cut-off edge, nm.")
QPTIFF_CH_EXCITATION_CUT_ON_NM = DECLARED.feature(
    "qptiff_ch_excitation_cut_on_nm", float, "Excitation passband cut-on edge, nm.")
QPTIFF_CH_EXCITATION_CUT_OFF_NM = DECLARED.feature(
    "qptiff_ch_excitation_cut_off_nm", float, "Excitation passband cut-off edge, nm.")
#: More than one band means a multi-pass cube, which a single midpoint describes incompletely.
QPTIFF_CH_N_FILTER_BANDS = DECLARED.feature(
    "qptiff_ch_n_filter_bands", int, "Bands the filter cube declares; >1 is a multi-pass cube.")
QPTIFF_CH_EXPOSURE_US = DECLARED.feature("qptiff_ch_exposure_time_us", float, "Exposure time, microseconds.")
QPTIFF_CH_GAIN = DECLARED.feature("qptiff_ch_gain", float, "Detector gain for this channel.")
QPTIFF_CH_BINNING = DECLARED.feature("qptiff_ch_binning", int, "Detector binning for this channel.")
QPTIFF_CH_BIT_DEPTH = DECLARED.feature("qptiff_ch_bit_depth", int, "Bit depth for this channel.")
QPTIFF_CH_OBJECTIVE = DECLARED.feature("qptiff_ch_objective", str, "Objective used for this channel.")
QPTIFF_CH_SIGNAL_UNITS = DECLARED.feature(
    "qptiff_ch_signal_units", int, "Signal units code, as the acquisition recorded it.")
QPTIFF_CH_OFFSET_COUNTS = DECLARED.feature("qptiff_ch_offset_counts", int, "Detector offset, counts.")
# A 3-vector stored as "R,G,B". A display colour, never filtered numerically.
QPTIFF_CH_COLOR_RGB = DECLARED.feature("qptiff_ch_color_rgb", str, 'Display colour as "R,G,B".')
QPTIFF_CH_AF_SUBTRACTED = DECLARED.feature(
    "qptiff_ch_autofluorescence_subtracted", bool, "Autofluorescence was subtracted.")
QPTIFF_CH_AUTO_EXPOSE_TYPE = DECLARED.feature(
    "qptiff_ch_auto_expose_type", str, "Auto-exposure mode used.")
QPTIFF_CH_CAMERA_ORIENTATION = DECLARED.feature(
    "qptiff_ch_camera_orientation", str, "Camera orientation for this channel.")
QPTIFF_CH_RESPONSIVITY = DECLARED.feature("qptiff_ch_responsivity", float, "Responsivity calibration value.")
QPTIFF_CH_RESPONSIVITY_FILTER_ID = DECLARED.feature(
    "qptiff_ch_responsivity_filter_id", str, "Filter id the responsivity was calibrated against.")
QPTIFF_CH_RESPONSIVITY_FILTER_NAME = DECLARED.feature(
    "qptiff_ch_responsivity_filter_name", str, "Filter name for the responsivity calibration.")
# Same pair as the slide stamp: stored rows hold strings under the first name.
QPTIFF_CH_RESPONSIVITY_DATE = DECLARED.feature(
    "qptiff_ch_responsivity_date", str,
    "Responsivity calibration timestamp exactly as written, unparsed. Prefer "
    "qptiff_ch_responsivity_at for anything that orders or compares.")
QPTIFF_CH_RESPONSIVITY_AT = DECLARED.feature(
    "qptiff_ch_responsivity_at", datetime,
    "When the responsivity was calibrated, in UTC. Naive, but UTC: the source states Z, so unlike "
    "qptiff_acquired_at (scanner-local) this one is an absolute instant. Do not compare the two.")
QPTIFF_CH_EXCITATION_FILTER_NAME = DECLARED.feature(
    "qptiff_ch_excitation_filter_name", str, "Excitation filter name.")
QPTIFF_CH_EXCITATION_FILTER_MANUFACTURER = DECLARED.feature(
    "qptiff_ch_excitation_filter_manufacturer", str, "Excitation filter manufacturer.")
QPTIFF_CH_EXCITATION_FILTER_PART_NO = DECLARED.feature(
    "qptiff_ch_excitation_filter_part_no", str, "Excitation filter part number.")
QPTIFF_CH_EMISSION_FILTER_NAME = DECLARED.feature(
    "qptiff_ch_emission_filter_name", str, "Emission filter name.")
QPTIFF_CH_EMISSION_FILTER_MANUFACTURER = DECLARED.feature(
    "qptiff_ch_emission_filter_manufacturer", str, "Emission filter manufacturer.")
QPTIFF_CH_EMISSION_FILTER_PART_NO = DECLARED.feature(
    "qptiff_ch_emission_filter_part_no", str, "Emission filter part number.")
QPTIFF_CH_ROI_X = DECLARED.feature("qptiff_ch_roi_x", int, "Detector ROI x origin, px.")
QPTIFF_CH_ROI_Y = DECLARED.feature("qptiff_ch_roi_y", int, "Detector ROI y origin, px.")
QPTIFF_CH_ROI_WIDTH = DECLARED.feature("qptiff_ch_roi_width", int, "Detector ROI width, px.")
QPTIFF_CH_ROI_HEIGHT = DECLARED.feature("qptiff_ch_roi_height", int, "Detector ROI height, px.")

#: ``ChannelInfo`` field name -> feature. A reader field missing here is not written as a feature.
#: The full blob still keeps it.
QPTIFF_CHANNEL_FIELDS: dict[str, str] = {
    "index": QPTIFF_CH_INDEX,
    "name": QPTIFF_CH_NAME,
    "fluorophore": QPTIFF_CH_FLUOROPHORE,
    "is_brightfield": QPTIFF_CH_IS_BRIGHTFIELD,
    "is_unmixed_component": QPTIFF_CH_IS_UNMIXED,
    "emission_wavelength_nm": QPTIFF_CH_EMISSION_NM,
    "excitation_wavelength_nm": QPTIFF_CH_EXCITATION_NM,
    "emission_cut_on_nm": QPTIFF_CH_EMISSION_CUT_ON_NM,
    "emission_cut_off_nm": QPTIFF_CH_EMISSION_CUT_OFF_NM,
    "excitation_cut_on_nm": QPTIFF_CH_EXCITATION_CUT_ON_NM,
    "excitation_cut_off_nm": QPTIFF_CH_EXCITATION_CUT_OFF_NM,
    "n_filter_bands": QPTIFF_CH_N_FILTER_BANDS,
    "exposure_time_us": QPTIFF_CH_EXPOSURE_US,
    "gain": QPTIFF_CH_GAIN,
    "binning": QPTIFF_CH_BINNING,
    "bit_depth": QPTIFF_CH_BIT_DEPTH,
    "objective": QPTIFF_CH_OBJECTIVE,
    "signal_units": QPTIFF_CH_SIGNAL_UNITS,
    "offset_counts": QPTIFF_CH_OFFSET_COUNTS,
    "autofluorescence_subtracted": QPTIFF_CH_AF_SUBTRACTED,
    "auto_expose_type": QPTIFF_CH_AUTO_EXPOSE_TYPE,
    "camera_orientation": QPTIFF_CH_CAMERA_ORIENTATION,
    "responsivity": QPTIFF_CH_RESPONSIVITY,
    "responsivity_filter_id": QPTIFF_CH_RESPONSIVITY_FILTER_ID,
    "responsivity_filter_name": QPTIFF_CH_RESPONSIVITY_FILTER_NAME,
    "responsivity_date": QPTIFF_CH_RESPONSIVITY_DATE,
    "excitation_filter_name": QPTIFF_CH_EXCITATION_FILTER_NAME,
    "excitation_filter_manufacturer": QPTIFF_CH_EXCITATION_FILTER_MANUFACTURER,
    "excitation_filter_part_no": QPTIFF_CH_EXCITATION_FILTER_PART_NO,
    "emission_filter_name": QPTIFF_CH_EMISSION_FILTER_NAME,
    "emission_filter_manufacturer": QPTIFF_CH_EMISSION_FILTER_MANUFACTURER,
    "emission_filter_part_no": QPTIFF_CH_EMISSION_FILTER_PART_NO,
    "roi_x": QPTIFF_CH_ROI_X,
    "roi_y": QPTIFF_CH_ROI_Y,
    "roi_width": QPTIFF_CH_ROI_WIDTH,
    "roi_height": QPTIFF_CH_ROI_HEIGHT,
}

#: ``(dataclass path, field) -> feature`` for the scalars promoted off the nested metadata.
QPTIFF_SCALAR_FIELDS: dict[tuple[str, str], str] = {
    ("slide", "barcode"): QPTIFF_BARCODE,
    ("slide", "slide_id"): QPTIFF_SLIDE_ID,
    ("slide", "study_name"): QPTIFF_STUDY_NAME,
    ("slide", "datetime"): QPTIFF_DATETIME,
    ("slide", "instrument_type"): QPTIFF_INSTRUMENT_TYPE,
    ("slide", "acquisition_software"): QPTIFF_ACQUISITION_SOFTWARE,
    ("image_info", "image_type"): QPTIFF_IMAGE_TYPE,
    ("image_info", "is_tma"): QPTIFF_IS_TMA,
    ("image_info", "scale_factor"): QPTIFF_SCALE_FACTOR,
    ("image_info", "scale_factor_unit"): QPTIFF_SCALE_FACTOR_UNIT,
    ("image_info", "objective"): QPTIFF_OBJECTIVE,
    ("image_info", "scan_profile_name"): QPTIFF_SCAN_PROFILE_NAME,
    ("image_info", "scan_mode"): QPTIFF_SCAN_MODE,
    ("image_info", "opal_kit_type"): QPTIFF_OPAL_KIT_TYPE,
    ("image_info", "stored_bits_per_sample"): QPTIFF_STORED_BITS,
    ("image_info", "compression"): QPTIFF_COMPRESSION,
    ("slide", "operator_name"): QPTIFF_OPERATOR_NAME,
    ("slide", "computer_name"): QPTIFF_COMPUTER_NAME,
    ("slide", "validation_code"): QPTIFF_VALIDATION_CODE,
    ("slide", "identifier"): QPTIFF_IDENTIFIER,
    ("slide", "description_version"): QPTIFF_DESCRIPTION_VERSION,
    ("slide", "sample_description"): QPTIFF_SAMPLE_DESCRIPTION,
    ("image_info", "bf_lamp_type"): QPTIFF_BF_LAMP_TYPE,
    ("image_info", "lamp_type"): QPTIFF_LAMP_TYPE,
    ("image_info", "jpeg_quality"): QPTIFF_JPEG_QUALITY,
    ("image_info", "saturation_protection_type"): QPTIFF_SATURATION_PROTECTION,
    ("image_info", "coverslip_thickness"): QPTIFF_COVERSLIP_THICKNESS,
    ("image_info", "is_rna"): QPTIFF_IS_RNA,
    ("image_info", "rotate_image"): QPTIFF_ROTATE_IMAGE,
    ("image_info", "mirror_image"): QPTIFF_MIRROR_IMAGE,
    ("image_info", "xposition_um"): QPTIFF_XPOSITION_UM,
    ("image_info", "yposition_um"): QPTIFF_YPOSITION_UM,
    ("scan_resolution", "magnification"): QPTIFF_MAGNIFICATION,
    ("scan_resolution", "objective_name"): QPTIFF_SCAN_RES_OBJECTIVE_NAME,
    ("scan_resolution", "binning"): QPTIFF_SCAN_RES_BINNING,
    ("scan_resolution", "base_pixel_size_um"): QPTIFF_BASE_PIXEL_SIZE_UM,
    ("camera", "camera_name"): QPTIFF_CAMERA_NAME,
    ("camera", "camera_type"): QPTIFF_CAMERA_TYPE,
    ("camera", "gain"): QPTIFF_CAMERA_GAIN,
    ("camera", "bit_depth"): QPTIFF_CAMERA_BIT_DEPTH,
    ("camera", "binning"): QPTIFF_CAMERA_BINNING,
}


#: ``root field -> feature`` for scalars on ``QptiffMetadata`` itself, with no nested path.
_ROOT_FIELDS: dict[str, str] = {
    "acquisition_format": QPTIFF_ACQUISITION_FORMAT,
    "channel_locus": QPTIFF_CHANNEL_LOCUS,
    "structure_signature": QPTIFF_STRUCTURE_SIGNATURE,
}


#: Every feature :func:`scalar_features` and :func:`channel_features` can produce, derived from the
#: tables.
#:
#: A metadata refresh subtracts the fresh read from these sets and clears the difference, so a value
#: the new reader no longer returns is removed. Features outside these sets are never cleared.
SCALAR_OWNED_FEATURES: frozenset[str] = frozenset(QPTIFF_SCALAR_FIELDS.values()) | frozenset(
    _ROOT_FIELDS.values()) | {QPTIFF_ACQUIRED_AT}
CHANNEL_OWNED_FEATURES: frozenset[str] = frozenset(QPTIFF_CHANNEL_FIELDS.values()) | {
    QPTIFF_CH_COLOR_RGB, QPTIFF_CH_RESPONSIVITY_AT}


#: Stripped from the blob at every level. Large, and always recoverable from the file.
_BLOB_DROP = ("raw_xml",)


def _get(obj: Any, name: str) -> Any:
    """Read ``name`` off the reader's dataclass or off a stored ``qptiff_metadata`` blob."""
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _present(value: Any) -> bool:
    """Whether a metadata value is worth writing: not ``None`` and not ``""``."""
    return value is not None and value != ""


#: Vendor timestamp formats. The slide stamp (TIFF tag 306) has colons in the date part, which
#: ``datetime.fromisoformat`` rejects. The channel responsivity date is UTC ISO with seven fraction
#: digits.
_TIMESTAMP_FORMATS = ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M:%S.%f")

#: Trailing ``Z`` and long fractions, normalised before parsing. Python 3.11 rejects them.
_ISO_Z = re.compile(r"^(?P<head>.*?)(?:\.(?P<frac>\d+))?(?P<tz>Z|[+-]\d{2}:?\d{2})?$")


def parse_timestamp(value: Any) -> datetime | None:
    """A vendor timestamp as a naive datetime, or ``None`` if it does not parse.

    * The slide stamp has no timezone. It is stored as written: local time at the scanner.
    * The responsivity date is ``Z``. It is converted to UTC, then made naive.

    Do not compare the two features directly. One is scanner-local and one is UTC.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    for fmt in _TIMESTAMP_FORMATS:
        try:
            # Naive: the tag-306 form has no timezone.
            return datetime.strptime(text, fmt)  # noqa: DTZ007
        except ValueError:
            continue

    m = _ISO_Z.match(text)
    if m:
        frac = (m.group("frac") or "")[:6]          # fromisoformat takes at most microseconds
        tz = m.group("tz") or ""
        normalised = m.group("head") + (f".{frac}" if frac else "") + ("+00:00" if tz == "Z" else tz)
        try:
            parsed = datetime.fromisoformat(normalised)
        except ValueError:
            return None
        return parsed.astimezone(UTC).replace(tzinfo=None) if parsed.tzinfo else parsed
    return None


def _first_image(meta: Any) -> Any | None:
    """The full-resolution scene, or ``None`` when the reader found no image scenes."""
    images = _get(meta, "images") or []
    return images[0] if images else None


def _source_for(meta: Any, path: str) -> Any | None:
    """Resolve a :data:`QPTIFF_SCALAR_FIELDS` path to the object holding the field."""
    if path == "slide":
        return _get(meta, "slide")
    image = _first_image(meta)
    if image is None:
        return None
    info = _get(image, "image_info")
    if path == "image_info":
        return info
    return _get(info, path) if info is not None else None


def scalar_features(meta: Any) -> dict[str, Any]:
    """The curated slide/image scalars, as ``{feature name: value}``. Absent fields are omitted."""
    out: dict[str, Any] = {}
    for (path, field), feature in QPTIFF_SCALAR_FIELDS.items():
        source = _source_for(meta, path)
        if source is None:
            continue
        value = _get(source, field)
        if _present(value):
            out[feature] = value

    # Root-level fields, outside the table.
    for field, feature in _ROOT_FIELDS.items():
        value = _get(meta, field)
        # Drop empty containers too: an empty dict would read as "measured, no signals".
        if isinstance(value, (dict, list, tuple)) and not value:
            continue
        if _present(value):
            out[feature] = value

    # Derived: one source field yields two features.
    acquired_at = parse_timestamp(out.get(QPTIFF_DATETIME))
    if acquired_at is not None:
        out[QPTIFF_ACQUIRED_AT] = acquired_at
    return out


def channel_features(channel: Any) -> dict[str, Any]:
    """One channel's settings, as ``{feature name: value}``."""
    out: dict[str, Any] = {}
    for field, feature in QPTIFF_CHANNEL_FIELDS.items():
        value = _get(channel, field)
        if _present(value):
            out[feature] = value

    rgb = _get(channel, "color_rgb")  # a 3-tuple, flattened to "R,G,B"
    if rgb:
        out[QPTIFF_CH_COLOR_RGB] = ",".join(str(int(c)) for c in rgb)

    # Derived: the raw text and the parsed instant.
    calibrated = parse_timestamp(out.get(QPTIFF_CH_RESPONSIVITY_DATE))
    if calibrated is not None:
        out[QPTIFF_CH_RESPONSIVITY_AT] = calibrated
    return out


def channels_of(meta: Any) -> list[Any]:
    """The full-resolution scene's channels, or an empty list."""
    image = _first_image(meta)
    return list(_get(image, "channels") or []) if image is not None else []


def channel_index(channel: Any, position: int = 0) -> int:
    """A channel's index from either shape, falling back to its position.

    The probe path and the reconcile path both use it, so they agree on which Record a channel is.
    """
    index = _get(channel, "index")
    return position if index is None else int(index)


def channel_record_name(qptiff_key: str, index: int) -> str:
    """Deterministic Record name for one channel: ``<qptiff stem>#ch<NN>``.

    ``#``, not ``:``, because ``:`` marks ROI names. Keyed on the index: channel names can repeat.
    The stem matches the WholeSlide name ``mint_slides`` gives.
    """
    stem = PurePosixPath(qptiff_key).stem
    return f"{stem}#ch{index:02d}"


def metadata_blob(meta: Any) -> dict:
    """The complete nested metadata, minus ``raw_xml``, with empty values dropped.

    Returns ``{}`` when there is nothing to record. The caller skips the write.
    """
    if not dataclasses.is_dataclass(meta):
        return {}
    return _prune(dataclasses.asdict(meta))


def _prune(value: Any) -> Any:
    """Recursively drop ``_BLOB_DROP`` keys and absent values."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k in _BLOB_DROP:
                continue
            pruned = _prune(v)
            # Keep False/0; drop None, "" and containers that pruned to nothing.
            if pruned is None or pruned == "" or (isinstance(pruned, (dict, list)) and not pruned):
                continue
            out[k] = pruned
        return out
    if isinstance(value, (list, tuple)):
        return [_prune(v) for v in value]
    return value
