"""Recreate ``clinical-roi/core/annotated_images`` version "1" from core label lineage.

Rebuilds the Collection ``python -m croi tma_core ingest-labels`` reads, from the image each
``core_labels/`` artifact records as an input of its registering run. Config comes from
``manifest.yaml`` and ``1_ingest_labels.yaml``.

- ``check`` is read-only. It runs preconditions P1-P7, prints PASS, FAIL or WARN for each, then
  a verdict. Exit 0: the verdict names the next step. Exit 1: stop, or P6's by-name links need
  ``--accept-by-name-links``.
- ``apply`` writes one Collection and its Run, and changes no artifact. It writes nothing if any
  Collection already has the key.

Run as a module or a file, never from a stdin heredoc: ``ln.track()`` refuses an interactive
session. Runbook: ``roi_detection/docs/recover-annotated-images.md``. A run input counts only
when its stem matches the label's the way ``labels.match_core_labels`` matches.
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

KEY = "clinical-roi/core/annotated_images"
VERSION = "1"
CORE_KEY = "clinical-roi/core"  # what ingest-labels creates from this Collection
DESCRIPTION = (  # stored on the Collection
    "Recovered 2026-09-29: the images named by core_labels/* run lineage "
    "(roi_detection/tools/recover_annotated_images.py)."
)



def label_stem(name: str) -> str:
    """The stem ingest matches on: Label Studio's ``<uuid>-`` prefix stripped."""
    return Path(name).stem.split("-", 1)[-1]


def names_same_image(label_name: str, image_key: str) -> bool:
    """Whether a label and an image pair under ``match_core_labels``'s rule."""
    from croi.objectives.tma_core.dataset import normalize

    stem = label_stem(label_name)
    return normalize(Path(image_key).stem) in (stem, normalize(stem))


@dataclass
class Lineage:
    labels: list = field(default_factory=list)      # latest core_labels/*.txt records
    others: list = field(default_factory=list)      # latest core_labels/ records that are not
    image_of: dict = field(default_factory=dict)    # label key -> the one image it names
    problems: list = field(default_factory=list)    # why a label names no single image

    @property
    def images(self) -> list:
        """The Collection's members, ordered by key."""
        return sorted({a.uid: a for a in self.image_of.values()}.values(), key=lambda a: a.key)


def read_lineage(labels_prefix: str) -> Lineage:
    import lamindb as ln

    found = Lineage()
    for a in ln.Artifact.filter(key__startswith=labels_prefix, is_latest=True).order_by("key"):
        name = Path(a.key).name
        if a.suffix != ".txt" or name == "classes.txt":  # ingest skips classes.txt too
            found.others.append(a)
            continue
        found.labels.append(a)
        if a.run is None:
            found.problems.append(f"{a.key}: records no run, so no lineage")
            continue
        inputs = list(a.run.input_artifacts.all())
        hits = [i for i in inputs if names_same_image(name, i.key)]
        if len(hits) == 1:
            found.image_of[a.key] = hits[0]
        else:
            found.problems.append(
                f"{a.key}: run {a.run.uid} has {len(inputs)} input(s), {len(hits)} matching "
                f"by name {[i.key for i in hits][:4]}"
            )

    # ingest indexes images by normalised stem, so a later image with the same stem replaces the earlier.
    from croi.objectives.tma_core.dataset import normalize

    stems = Counter(normalize(Path(i.key).stem) for i in found.images)
    found.problems += [f"two images share the matching stem {s!r}" for s, n in stems.items() if n > 1]
    per_image = Counter(i.uid for i in found.image_of.values())
    found.problems += [
        f"{k}: shares its image {i.key} with another label"
        for k, i in found.image_of.items() if per_image[i.uid] > 1
    ]
    return found



@dataclass
class Result:
    id: str
    ok: bool
    title: str
    lines: list = field(default_factory=list)
    blocking: bool = True  # False only for the FAIL `apply` fixes
    needs_ack: bool = False  # a WARN that only `--accept-by-name-links` clears


def _in_trash(a) -> bool:
    return getattr(a, "branch_id", 1) == -1


def check_p1(lin: Lineage) -> Result:
    import lamindb as ln

    found = list(ln.Collection.objects.filter(key=KEY))
    if not found:
        return Result("P1", False, f"{KEY} v{VERSION} does not exist",
                      ["`apply` creates it. This FAIL is expected before `apply` and only then."],
                      blocking=False)
    want = {i.uid for i in lin.images}
    lines, ok = [], False
    for c in found:
        have = {a.uid for a in c.artifacts.all()}
        match = c.version == VERSION and not _in_trash(c) and have == want
        ok = ok or match
        lines.append(f"{c.uid} version={c.version} branch_id={c.branch_id} members={len(have)} "
                     f"{'= the label lineage' if have == want else '!= the label lineage'}")
    title = (f"{KEY} v{VERSION} exists and holds exactly the images the labels name" if ok
             else f"{KEY} exists but is NOT the v{VERSION} this recovery would build")
    return Result("P1", ok, title, lines)


def check_p2(lin: Lineage) -> Result:
    missing = [a.key for a in lin.labels + lin.others if not a.path.exists()]
    missing += [f"{i.key} (image)" for i in lin.images if not i.path.exists()]
    trashed = [f"{i.key} (image, in trash)" for i in lin.images if _in_trash(i)]
    lines = [f"no bytes: {k}" for k in missing] + trashed
    if missing:
        lines.append("ingest-labels PERMANENTLY DELETES a label record whose file is missing "
                     "(materialize heal='delete') and re-registers it from disk.")
    return Result("P2", not lines,
                  f"every core_labels/ record and every named image has its bytes on disk "
                  f"({len(lin.labels) + len(lin.others)} records, {len(lin.images)} images)",
                  lines)


def check_p3(lin: Lineage, labels_dir: Path, labels_prefix: str) -> Result:
    if not labels_dir.is_dir():
        return Result("P3", False, f"{labels_dir} is not a directory on this node", [])
    # ingest globs *.txt in that directory and nothing else.
    on_disk = {f"{labels_prefix}{p.name}" for p in labels_dir.glob("*.txt")
               if p.name != "classes.txt"}
    records = {a.key for a in lin.labels}
    extra, absent = sorted(on_disk - records), sorted(records - on_disk)
    lines = [f"ON DISK, NO RECORD - ingest-labels WILL NEWLY REGISTER it and it SILENTLY JOINS "
             f"{CORE_KEY}: {k}" for k in extra]
    lines += [f"RECORD, NO FILE - ingest-labels SILENTLY LEAVES IT OUT of {CORE_KEY}: {k}"
              for k in absent]
    return Result("P3", not lines,
                  f"the .txt files in {labels_dir} are exactly the {len(records)} label records",
                  lines)


def check_p4(storage_root: str, labels_dir: Path) -> Result:
    import lamindb as ln

    lines = []
    if ln.Storage.filter(root=storage_root.rstrip("/")).count() == 0:
        lines.append(f"no Storage record has root {storage_root}")
    marker = Path(storage_root) / ".lamindb" / "storage_uid.txt"
    if not marker.exists():
        lines.append(f"{marker} is not readable here: the storage root is not mounted")
    if not labels_dir.is_dir():
        lines.append(f"{labels_dir} does not exist here")
    return Result("P4", not lines, f"this node mounts the croi storage root {storage_root}", lines)


def check_p5(lin: Lineage) -> Result:
    """lamindb returns and re-keys any collection with the same members. Ingest bundles each
    paired image and label."""
    import lamindb as ln

    want = {i.uid for i in lin.images} | {a.uid for a in lin.labels if a.key in lin.image_of}
    clash = []
    for c in ln.Collection.objects.exclude(branch_id=-1).exclude(key=CORE_KEY):
        if {a.uid for a in c.artifacts.all()} == want:
            clash.append(f"{c.key} v{c.version} ({c.uid}) holds exactly those members and "
                         f"WOULD BE RE-KEYED to {CORE_KEY} by ingest-labels")
    return Result("P5", not clash,
                  f"no other collection holds the {len(want)} members {CORE_KEY} will hold", clash)


_BY_NAME_WARNING = [
    ("ingest-labels WILL LINK these labels to Records matched BY NAME ALONE. A re-minted pipeline "
     "TMA can carry the name of a different physical TMA. Accepting this is your call: "
     "`check --accept-by-name-links`."),
]


def check_p6(lin: Lineage) -> Result:
    """Predict what ``cx.update`` -> ``resolve_roi`` does for each image without calling it,
    since the real call raises on the case this reports."""
    import lamindb as ln
    from clinicalomx.roi import ROI_NAME, ROI_UID

    from croi.objectives.tma_core.labels import _LEGACY_NAME, _LEGACY_UID

    live, by_name, aborts, dangling, no_pointer = 0, [], [], 0, 0
    for img in lin.images:
        vals = img.features.get_values()
        uid = vals.get(ROI_UID) or next((vals[f] for f in _LEGACY_UID if vals.get(f)), None)
        if uid and ln.Record.filter(uid=uid).exists():
            live += 1  # still points at the Record it was made for
            continue
        name = vals.get(ROI_NAME) or next((vals[f] for f in _LEGACY_NAME if vals.get(f)), None)
        hits = list(ln.Record.filter(name=name)) if name else []
        if len(hits) >= 2:
            aborts.append(f"{img.key}: {len(hits)} Records are named {name!r} "
                          f"({', '.join(r.uid for r in hits)})")
        elif len(hits) == 1:
            r = hits[0]
            by_name.append(f"{img.key}: roi_uid {uid or '(none)'} is gone; name {name!r} -> "
                           f"Record {r.uid} type={getattr(r.type, 'name', None)} "
                           f"created {r.created_at:%Y-%m-%d}")
        elif uid or name:
            dangling += 1  # names a ROI that no longer exists; ingest prints "not linked"
        else:
            no_pointer += 1  # never named a ROI; ingest prints "not linked"
    # Unlinked images do not block (split and train read no ROI link) but never count as live.
    n, unlinked = len(lin.images), dangling + no_pointer
    title = (f"{live} of {n} images keep a live ROI; {len(by_name)} resolve by name alone; "
             f"{unlinked} of {n} have NO ROI and ingest will link none")
    info = ([(f"no ROI: {dangling} dangling (roi_uid or name names a Record that is gone), "
              f"{no_pointer} with no ROI pointer at all")] if unlinked else [])
    if by_name and not aborts:
        return Result("P6", False, title,
                      _BY_NAME_WARNING + [f"  {x}" for x in by_name] + info,
                      blocking=False, needs_ack=True)
    lines = []
    if aborts:
        lines.append("ingest-labels WILL ABORT PARTWAY (MultipleResultsFound) on these; labels "
                     "before them are already linked and the collection is never saved:")
        lines += [f"  {x}" for x in aborts]
    if by_name:
        lines += _BY_NAME_WARNING + [f"  {x}" for x in by_name]
    return Result("P6", not lines, title, lines + info)


def check_p7(lin: Lineage) -> Result:
    return Result("P7", not lin.problems and bool(lin.image_of),
                  f"every label's lineage names exactly one image "
                  f"({len(lin.image_of)} of {len(lin.labels)} labels)",
                  lin.problems or ([] if lin.image_of else ["no label names any image"]))


def run_checks(storage_root: str, labels_dir: Path, labels_prefix: str) -> list[Result]:
    lin = read_lineage(labels_prefix)
    return [check_p1(lin), check_p2(lin), check_p3(lin, labels_dir, labels_prefix),
            check_p4(storage_root, labels_dir), check_p5(lin), check_p6(lin), check_p7(lin)]


def verdict(results: list[Result], accept_by_name: bool = False) -> tuple[int, str]:
    """``(exit code, next-step line)``."""
    blocking = [r.id for r in results if not r.ok and r.blocking]
    if blocking:
        return 1, f"STOP: {', '.join(blocking)} failed. Run nothing that writes."
    ack = [r.id for r in results if not r.ok and r.needs_ack]
    if ack and not accept_by_name:
        return 1, (f"ACKNOWLEDGE: {', '.join(ack)} lists labels that will attach to Records "
                   f"matched by name alone. Re-run `check --accept-by-name-links` to accept them.")
    if all(r.ok or r.needs_ack for r in results):
        return 0, "CLEAR: the collection is in place. Next: python -m croi tma_core ingest-labels."
    return 0, "CLEAR: next step is `apply`."


def print_results(results: list[Result], accept_by_name: bool = False) -> int:
    for r in results:
        print(f"{r.id} {'PASS' if r.ok else 'WARN' if r.needs_ack else 'FAIL'}  {r.title}")
        for line in r.lines:
            print(f"         {line}")
    code, line = verdict(results, accept_by_name)
    print(f"\n{line}")
    return code



def existing() -> list:
    import lamindb as ln

    return list(ln.Collection.objects.filter(key=KEY))  # every version, every branch


def build(labels_prefix: str):
    """Save the Collection. The caller has already refused if one exists."""
    import lamindb as ln

    lin = read_lineage(labels_prefix)
    if lin.problems or not lin.image_of:
        raise SystemExit("refusing: the label lineage is not one image per label (run `check`, "
                         "P7):\n  " + "\n  ".join(lin.problems or ["no label names any image"]))
    # skip_hash_lookup: never reuse and re-key a collection with the same members.
    c = ln.Collection(lin.images, key=KEY, version=VERSION, description=DESCRIPTION,
                      skip_hash_lookup=True).save()
    got = ln.Collection.get(key=KEY, version=VERSION)  # the lookup ingest makes
    assert got.uid == c.uid and {a.uid for a in got.artifacts.all()} == {i.uid for i in lin.images}
    print(f"saved {KEY} v{got.version} uid={got.uid} with {got.artifacts.count()} images "
          f"for {len(lin.image_of)} labels")
    return got


def apply(labels_prefix: str, track: bool = True):
    found = existing()
    if found:
        print(f"{KEY} already exists; nothing written:")
        for c in found:
            print(f"  {c.uid} version={c.version} branch_id={c.branch_id} "
                  f"members={c.artifacts.count()}")
        return None
    import lamindb as ln

    if track:
        # After the existence test, so a second run leaves no Run. `path` keeps the Transform key
        # the file name however this module was loaded.
        ln.track(path=__file__)
    try:
        return build(labels_prefix)
    finally:
        if track:
            ln.finish()



def locate() -> tuple[str, Path, str]:
    """Connect to the objective's instance and return ``(storage root, labels dir, labels prefix)``."""
    from croi.objectives.tma_core import environment
    from croi.objectives.tma_core.config import IngestLabelsConfig

    manifest = environment.load_manifest(environment.resolve_config("manifest.yaml"))
    manifest.connect()
    storage_root = manifest.storage.storage_root
    data_dir = environment.load_config("1_ingest_labels.yaml", IngestLabelsConfig)["data_dir"]
    labels_dir = Path(storage_root) / data_dir
    print(f"instance {manifest.instance}, labels {labels_dir}\n")
    return storage_root, labels_dir, f"{data_dir}/"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=("check", "apply"))
    parser.add_argument("--accept-by-name-links", action="store_true",
                        help="check: accept P6's labels linking to Records matched by name alone")
    args = parser.parse_args(argv)
    if args.accept_by_name_links and args.command != "check":
        parser.error("--accept-by-name-links applies to `check` only")

    storage_root, labels_dir, labels_prefix = locate()
    if args.command == "check":
        return print_results(run_checks(storage_root, labels_dir, labels_prefix),
                             args.accept_by_name_links)
    apply(labels_prefix)
    return 0


if __name__ == "__main__":
    sys.exit(main())
