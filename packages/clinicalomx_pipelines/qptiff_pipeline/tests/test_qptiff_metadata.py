"""Mapping a qptiff's own metadata onto this repo's vocabulary, with dataclass doubles."""
# ruff: noqa: DTZ001 - every datetime here is NAIVE on purpose. The vendor stamp carries no timezone,
# and attaching one would invent a fact; test_the_parsed_instant_is_naive asserts exactly that.
from __future__ import annotations

import dataclasses
import datetime

import pytest

from conftest import load_pipeline_module

qptiff = load_pipeline_module("qptiff")
vocab = load_pipeline_module("vocab")


# Doubles: the reader's dataclasses, by field name only.
@dataclasses.dataclass
class Camera:
    """``CameraInfo``, with every field the reader declares."""

    camera_name: str | None = None
    camera_type: str | None = None
    gain: float | None = None
    bit_depth: int | None = None
    binning: int | None = None


@dataclasses.dataclass
class ScanResolution:
    magnification: float | None = None
    objective_name: str | None = None
    binning: int | None = None
    base_pixel_size_um: float | None = None


@dataclasses.dataclass
class ImageInfo:
    image_type: str | None = None
    stored_bits_per_sample: int | None = None
    objective: str | None = None
    bf_lamp_type: str | None = None
    lamp_type: str | None = None
    scan_profile_name: str | None = None
    scan_mode: str | None = None
    is_tma: bool | None = None
    opal_kit_type: str | None = None
    scale_factor: float | None = None
    scale_factor_unit: str | None = None
    compression: str | None = None
    jpeg_quality: int | None = None
    saturation_protection_type: str | None = None
    coverslip_thickness: str | None = None
    is_rna: bool | None = None
    rotate_image: bool | None = None
    mirror_image: bool | None = None
    xposition_um: float | None = None
    yposition_um: float | None = None
    camera: Camera = dataclasses.field(default_factory=Camera)
    scan_resolution: ScanResolution = dataclasses.field(default_factory=ScanResolution)


@dataclasses.dataclass
class ChannelInfo:
    """A subset of ChannelInfo: only the fields these tests read."""

    index: int = 0
    name: str = ""
    fluorophore: str | None = None
    exposure_time_us: float | None = None
    emission_wavelength_nm: float | None = None
    emission_cut_on_nm: float | None = None
    emission_cut_off_nm: float | None = None
    excitation_cut_on_nm: float | None = None
    excitation_cut_off_nm: float | None = None
    n_filter_bands: int | None = None
    is_brightfield: bool = False
    is_unmixed_component: bool | None = None
    color_rgb: tuple[int, int, int] | None = None
    gain: float | None = None
    offset_counts: int | None = None
    roi_x: int | None = None
    responsivity_date: str | None = None


@dataclasses.dataclass
class SlideInfo:
    slide_id: str | None = None
    barcode: str | None = None
    study_name: str | None = None
    operator_name: str | None = None
    computer_name: str | None = None
    datetime: str | None = None
    acquisition_software: str | None = None
    description_version: str | None = None
    instrument_type: str | None = None
    identifier: str | None = None
    validation_code: str | None = None
    sample_description: str | None = None


@dataclasses.dataclass
class SceneMeta:
    image_info: ImageInfo = dataclasses.field(default_factory=ImageInfo)
    channels: list = dataclasses.field(default_factory=list)
    raw_xml: str = ""


@dataclasses.dataclass
class QptiffMeta:
    slide: SlideInfo = dataclasses.field(default_factory=SlideInfo)
    images: list = dataclasses.field(default_factory=list)
    acquisition_format: str | None = None
    channel_locus: str | None = None
    structure_signature: dict = dataclasses.field(default_factory=dict)
    raw_xml: str = ""


def _full() -> QptiffMeta:
    return QptiffMeta(
        slide=SlideInfo(barcode="BC-001", study_name="TNBC", datetime="2026-03-12T09:00:00",
                        instrument_type="Vectra Polaris", operator_name="someone"),
        images=[SceneMeta(
            image_info=ImageInfo(image_type="Multiplex", is_tma=True, scale_factor=0.4977,
                                 scale_factor_unit="um", stored_bits_per_sample=16,
                                 camera=Camera(camera_name="cam0", bit_depth=14),
                                 scan_resolution=ScanResolution(magnification=20.0)),
            channels=[ChannelInfo(index=0, name="DAPI", color_rgb=(0, 0, 255), gain=1.5),
                      ChannelInfo(index=1, name="Opal 520", is_brightfield=False,
                                  is_unmixed_component=True, offset_counts=0)],
            raw_xml="<huge/>")],
        acquisition_format="fusion-paged",
        channel_locus="per_page_root",
        structure_signature={"has_scanbands": False, "scanprofile_is_json": True},
        raw_xml="<huge/>",
    )


# scalar_features
def test_scalars_are_promoted_from_every_nested_source() -> None:
    got = qptiff.scalar_features(_full())
    assert got[qptiff.QPTIFF_BARCODE] == "BC-001"              # slide
    assert got[qptiff.QPTIFF_IMAGE_TYPE] == "Multiplex"        # image_info
    assert got[qptiff.QPTIFF_MAGNIFICATION] == 20.0            # image_info.scan_resolution
    assert got[qptiff.QPTIFF_CAMERA_NAME] == "cam0"            # image_info.camera
    assert got[qptiff.QPTIFF_ACQUISITION_FORMAT] == "fusion-paged"  # root


def test_absent_fields_are_omitted_not_written_as_none() -> None:
    got = qptiff.scalar_features(_full())
    assert qptiff.QPTIFF_SCAN_MODE not in got
    assert None not in got.values()


def test_every_field_of_every_slide_scope_is_promoted() -> None:
    scopes = {"slide": SlideInfo, "image_info": ImageInfo,
              "camera": Camera, "scan_resolution": ScanResolution}
    # Excluded by name: with postponed annotations, f.type is a string.
    nested = set(scopes)
    mapped = set(qptiff.QPTIFF_SCALAR_FIELDS)
    missing = [
        (scope, f.name) for scope, cls in scopes.items()
        for f in dataclasses.fields(cls)
        if f.name not in nested and (scope, f.name) not in mapped
    ]
    assert not missing, f"reader fields with no feature: {missing}"


def test_personal_fields_are_promoted_deliberately() -> None:
    got = qptiff.scalar_features(_full())
    assert got[qptiff.QPTIFF_OPERATOR_NAME] == "someone"
    described = {n: d for n, _dt, d in vocab._declared()}
    assert "PERSONAL DATA" in described[qptiff.QPTIFF_OPERATOR_NAME]


def test_the_scanners_answer_does_not_land_on_the_pipelines_feature() -> None:
    got = qptiff.scalar_features(_full())
    assert vocab.IMAGE_TYPE not in got
    assert got[qptiff.QPTIFF_IMAGE_TYPE] == "Multiplex"


def test_an_empty_metadata_object_yields_no_features() -> None:
    assert qptiff.scalar_features(QptiffMeta()) == {}
    assert qptiff.channels_of(QptiffMeta()) == []


def test_a_false_flag_is_kept() -> None:
    meta = QptiffMeta(images=[SceneMeta(image_info=ImageInfo(is_tma=False))])
    assert qptiff.scalar_features(meta)[qptiff.QPTIFF_IS_TMA] is False


def test_an_empty_string_is_dropped() -> None:
    meta = QptiffMeta(slide=SlideInfo(barcode="", study_name="real"))
    got = qptiff.scalar_features(meta)
    assert qptiff.QPTIFF_BARCODE not in got
    assert got[qptiff.QPTIFF_STUDY_NAME] == "real"


# channel_features
def test_channel_fields_map_to_their_features() -> None:
    got = qptiff.channel_features(_full().images[0].channels[0])
    assert got[qptiff.QPTIFF_CH_INDEX] == 0
    assert got[qptiff.QPTIFF_CH_NAME] == "DAPI"
    assert got[qptiff.QPTIFF_CH_GAIN] == 1.5


def test_color_rgb_is_flattened_to_a_string() -> None:
    got = qptiff.channel_features(_full().images[0].channels[0])
    assert got[qptiff.QPTIFF_CH_COLOR_RGB] == "0,0,255"


def test_zero_and_false_survive_on_a_channel() -> None:
    ch = _full().images[0].channels[1]
    got = qptiff.channel_features(ch)
    assert got[qptiff.QPTIFF_CH_OFFSET_COUNTS] == 0
    assert got[qptiff.QPTIFF_CH_IS_BRIGHTFIELD] is False
    assert got[qptiff.QPTIFF_CH_IS_UNMIXED] is True


def test_every_mapped_channel_feature_is_declared() -> None:
    declared = set(vocab.declared_names())
    assert set(qptiff.QPTIFF_CHANNEL_FIELDS.values()) <= declared
    assert set(qptiff.QPTIFF_SCALAR_FIELDS.values()) <= declared


# channel_record_name
def test_channel_record_name_is_deterministic_and_zero_padded() -> None:
    key = "projects/Mater_ovarian/Arutha_YTMA 404-2_Scan1-16-bit.qptiff"
    assert qptiff.channel_record_name(key, 0) == "Arutha_YTMA 404-2_Scan1-16-bit#ch00"
    assert qptiff.channel_record_name(key, 7) == "Arutha_YTMA 404-2_Scan1-16-bit#ch07"
    assert qptiff.channel_record_name(key, 0) == qptiff.channel_record_name(key, 0)


def test_channel_record_name_shares_the_slides_name_prefix() -> None:
    from pathlib import PurePosixPath

    key = "projects/x/SLIDE_001_Scan1.qptiff"
    slide_name = PurePosixPath(key).stem
    assert qptiff.channel_record_name(key, 3).startswith(slide_name + "#")


def test_channel_record_name_does_not_use_the_roi_spine() -> None:
    assert ":" not in qptiff.channel_record_name("a/b/c.qptiff", 1)


# metadata_blob
def test_raw_xml_never_reaches_the_blob() -> None:
    blob = qptiff.metadata_blob(_full())
    assert "raw_xml" not in blob
    assert "raw_xml" not in blob["images"][0]


def test_the_blob_keeps_the_fields_the_curated_list_drops() -> None:
    blob = qptiff.metadata_blob(_full())
    assert blob["slide"]["operator_name"] == "someone"


def test_the_blob_drops_absent_values_but_keeps_false() -> None:
    meta = QptiffMeta(images=[SceneMeta(image_info=ImageInfo(is_tma=False, scan_mode=None))])
    blob = qptiff.metadata_blob(meta)
    info = blob["images"][0]["image_info"]
    assert info["is_tma"] is False
    assert "scan_mode" not in info


def test_an_empty_metadata_object_yields_an_empty_blob() -> None:
    assert qptiff.metadata_blob(QptiffMeta()) == {}


def test_a_non_dataclass_yields_an_empty_blob_rather_than_raising() -> None:
    assert qptiff.metadata_blob(object()) == {}


# Reconciling from the stored blob.
def test_the_blob_reproduces_every_scalar_the_dataclass_did() -> None:
    meta = _full()
    from_object = qptiff.scalar_features(meta)
    from_blob = qptiff.scalar_features(qptiff.metadata_blob(meta))
    assert from_blob == from_object, {
        "only in object": {k: v for k, v in from_object.items() if k not in from_blob},
        "only in blob": {k: v for k, v in from_blob.items() if k not in from_object},
    }


def test_a_blob_yields_a_field_declared_after_it_was_written() -> None:
    blob = qptiff.metadata_blob(
        QptiffMeta(slide=SlideInfo(computer_name="ACQ-WS-04", barcode="BC-9")))
    got = qptiff.scalar_features(blob)
    assert got[qptiff.QPTIFF_COMPUTER_NAME] == "ACQ-WS-04"


def test_channel_features_read_a_blob_channel_too() -> None:
    meta = _full()
    blob_channel = qptiff.channels_of(qptiff.metadata_blob(meta))[0]
    assert qptiff.channel_features(blob_channel) == qptiff.channel_features(
        qptiff.channels_of(meta)[0])


def test_an_empty_or_absent_blob_yields_nothing_rather_than_raising() -> None:
    assert qptiff.scalar_features({}) == {}
    assert qptiff.channels_of({}) == []


# The vendor timestamp: kept verbatim, and parsed alongside.
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026:03:12 09:00:00", datetime.datetime(2026, 3, 12, 9, 0)),   # TIFF tag 306
        ("2026-03-12 09:00:00", datetime.datetime(2026, 3, 12, 9, 0)),
        ("2026-03-12T09:00:00", datetime.datetime(2026, 3, 12, 9, 0)),   # ISO
    ],
    ids=["tiff-tag", "dashed", "iso"],
)
def test_the_vendor_stamp_parses(raw, expected) -> None:
    assert qptiff.parse_timestamp(raw) == expected


@pytest.mark.parametrize("raw", ["not a date", "", "   ", None, 20260312, "2026:13:45 99:99:99"])
def test_an_unreadable_stamp_declines_rather_than_guessing(raw) -> None:
    assert qptiff.parse_timestamp(raw) is None


def test_both_the_raw_text_and_the_parsed_instant_are_written() -> None:
    meta = QptiffMeta(slide=SlideInfo(datetime="2026:03:12 09:00:00"))
    got = qptiff.scalar_features(meta)
    assert got[vocab_qptiff_datetime := qptiff.QPTIFF_DATETIME] == "2026:03:12 09:00:00"
    assert got[qptiff.QPTIFF_ACQUIRED_AT] == datetime.datetime(2026, 3, 12, 9, 0)
    assert vocab_qptiff_datetime != qptiff.QPTIFF_ACQUIRED_AT


def test_an_unparseable_stamp_still_records_the_raw_text() -> None:
    got = qptiff.scalar_features(QptiffMeta(slide=SlideInfo(datetime="whenever")))
    assert got[qptiff.QPTIFF_DATETIME] == "whenever"
    assert qptiff.QPTIFF_ACQUIRED_AT not in got


def test_the_parsed_instant_is_naive() -> None:
    assert qptiff.parse_timestamp("2026:03:12 09:00:00").tzinfo is None


def test_acquired_at_is_declared_as_a_datetime_not_a_string() -> None:
    dtypes = {n: d for n, d, _desc in qptiff.DECLARED.features}
    assert dtypes[qptiff.QPTIFF_ACQUIRED_AT] is datetime.datetime
    assert dtypes[qptiff.QPTIFF_DATETIME] is str


def test_the_blob_reconcile_backfills_the_parsed_instant() -> None:
    blob = qptiff.metadata_blob(QptiffMeta(slide=SlideInfo(datetime="2026:03:12 09:00:00")))
    assert qptiff.scalar_features(blob)[qptiff.QPTIFF_ACQUIRED_AT] == datetime.datetime(2026, 3, 12, 9, 0)


def test_the_channel_calibration_stamp_parses() -> None:
    got = qptiff.channel_features(ChannelInfo(responsivity_date="2026-03-11T22:57:06.0058601Z"))
    assert got[qptiff.QPTIFF_CH_RESPONSIVITY_DATE] == "2026-03-11T22:57:06.0058601Z"
    assert got[qptiff.QPTIFF_CH_RESPONSIVITY_AT] == datetime.datetime(2026, 3, 11, 22, 57, 6, 5860)


def test_an_offset_is_converted_to_utc_not_discarded() -> None:
    assert qptiff.parse_timestamp("2026-03-11T22:57:06.123456+05:30") == \
        datetime.datetime(2026, 3, 11, 17, 27, 6, 123456)


def test_more_fractional_digits_than_fromisoformat_takes() -> None:
    assert qptiff.parse_timestamp("2026-03-11T22:57:06.0058601Z") is not None
    assert qptiff.parse_timestamp("2026-03-11T22:57:06.00586012345Z") is not None


# Reconciling channel records from the blob.
def test_a_blob_channel_yields_what_the_dataclass_channel_does() -> None:
    meta = _full()
    from_objects = [qptiff.channel_features(c) for c in qptiff.channels_of(meta)]
    from_blob = [qptiff.channel_features(c)
                 for c in qptiff.channels_of(qptiff.metadata_blob(meta))]
    assert from_blob == from_objects


def test_channel_index_reads_both_shapes() -> None:
    meta = _full()
    obj = qptiff.channels_of(meta)[1]
    blob = qptiff.channels_of(qptiff.metadata_blob(meta))[1]
    assert qptiff.channel_index(obj, 99) == qptiff.channel_index(blob, 99) == 1


def test_channel_index_falls_back_to_position_only_when_absent() -> None:
    assert qptiff.channel_index({}, 7) == 7
    assert qptiff.channel_index({"index": 0}, 7) == 0     # 0 is an answer, not a missing value


def test_a_blob_channel_resolves_the_same_record_name() -> None:
    key = "projects/x/slide.qptiff"
    meta = _full()
    for i, (obj, blob) in enumerate(zip(qptiff.channels_of(meta),
                                        qptiff.channels_of(qptiff.metadata_blob(meta)),
                                        strict=True)):
        assert (qptiff.channel_record_name(key, qptiff.channel_index(obj, i))
                == qptiff.channel_record_name(key, qptiff.channel_index(blob, i)))


# The root-level fields, and the owned sets a refresh clears.
def test_the_root_level_fields_are_promoted() -> None:
    got = qptiff.scalar_features(_full())
    assert got[qptiff.QPTIFF_ACQUISITION_FORMAT] == "fusion-paged"
    assert got[qptiff.QPTIFF_CHANNEL_LOCUS] == "per_page_root"
    assert got[qptiff.QPTIFF_STRUCTURE_SIGNATURE] == {
        "has_scanbands": False, "scanprofile_is_json": True}


def test_an_empty_structure_signature_is_dropped_not_stored_as_an_empty_dict() -> None:
    meta = QptiffMeta(channel_locus="unknown")
    got = qptiff.scalar_features(meta)
    assert qptiff.QPTIFF_STRUCTURE_SIGNATURE not in got
    assert got[qptiff.QPTIFF_CHANNEL_LOCUS] == "unknown"  # a real answer, still written


def test_the_root_fields_survive_the_blob_round_trip() -> None:
    meta = _full()
    assert qptiff.scalar_features(qptiff.metadata_blob(meta)) == qptiff.scalar_features(meta)


def test_an_unrecognised_locus_is_not_in_the_known_set() -> None:
    assert qptiff.LOCUS_UNKNOWN not in qptiff.LOCUS_KNOWN
    assert qptiff.LOCUS_PER_PAGE_ROOT in qptiff.LOCUS_KNOWN
    assert len(qptiff.LOCUS_KNOWN) == 3


def test_every_feature_the_scalar_walk_can_write_is_owned() -> None:
    written = set(qptiff.scalar_features(_full()))
    assert written <= qptiff.SCALAR_OWNED_FEATURES, written - qptiff.SCALAR_OWNED_FEATURES


def test_every_feature_the_channel_walk_can_write_is_owned() -> None:
    channel = ChannelInfo(index=0, name="CD8", fluorophore="Opal 520", color_rgb=(1, 2, 3),
                          responsivity_date="2026-01-02T03:04:05.1234567Z", gain=1.0,
                          offset_counts=0, roi_x=0, is_unmixed_component=False,
                          exposure_time_us=1.0, emission_wavelength_nm=520.0)
    written = set(qptiff.channel_features(channel))
    assert written <= qptiff.CHANNEL_OWNED_FEATURES, written - qptiff.CHANNEL_OWNED_FEATURES


def test_the_owned_sets_claim_nothing_they_should_not_clear() -> None:
    for owned in (qptiff.SCALAR_OWNED_FEATURES, qptiff.CHANNEL_OWNED_FEATURES):
        assert "source_artifact" not in owned
        assert vocab.IMAGE_TYPE not in owned
        assert qptiff.QPTIFF_METADATA not in owned
        assert qptiff.QPTIFF_CHANNELS not in owned


def test_a_fluorophore_the_reader_declines_to_answer_is_omitted() -> None:
    got = qptiff.channel_features(ChannelInfo(index=3, name="CD8", fluorophore=None))
    assert qptiff.QPTIFF_CH_FLUOROPHORE not in got
    assert got[qptiff.QPTIFF_CH_NAME] == "CD8"
    assert qptiff.QPTIFF_CH_FLUOROPHORE in qptiff.CHANNEL_OWNED_FEATURES


def test_the_passband_edges_are_recorded_beside_the_midpoint() -> None:
    got = qptiff.channel_features(ChannelInfo(
        index=0, name="CD8", emission_wavelength_nm=540.0,
        emission_cut_on_nm=520.0, emission_cut_off_nm=560.0, n_filter_bands=1))
    assert got[qptiff.QPTIFF_CH_EMISSION_NM] == 540.0
    assert got[qptiff.QPTIFF_CH_EMISSION_CUT_ON_NM] == 520.0
    assert got[qptiff.QPTIFF_CH_EMISSION_CUT_OFF_NM] == 560.0


def test_a_multi_pass_cube_is_identifiable() -> None:
    got = qptiff.channel_features(ChannelInfo(index=0, name="CD8", n_filter_bands=3))
    assert got[qptiff.QPTIFF_CH_N_FILTER_BANDS] == 3


def test_a_single_band_count_is_kept_not_dropped_as_uninformative() -> None:
    got = qptiff.channel_features(ChannelInfo(index=0, name="CD8", n_filter_bands=1))
    assert got[qptiff.QPTIFF_CH_N_FILTER_BANDS] == 1
