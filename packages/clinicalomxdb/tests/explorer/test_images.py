"""Base64 image columns, detected by decoded magic number rather than column name."""
import base64
import sys

import lamindb_setup as ln_setup
ln_setup.init(storage="/tmp/imgstore", name="img")
import lamindb as ln

# Minimal real files in each format a browser draws.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
JPEG = bytes.fromhex("ffd8ffe000104a46494600010100000100010000ffdb004300"
                     "0806060706050807070709090806") + b"\x00" * 40
GIF = b"GIF89a" + b"\x01\x00\x01\x00\x80\x00\x00" + b"\x00" * 40
WEBP = b"RIFF" + (40).to_bytes(4, "little") + b"WEBPVP8 " + b"\x00" * 40
TIFF = b"II*\x00" + b"\x00" * 40          # real magic, but no browser draws it

sys.path.insert(0, "/explorer")
import lamin_api as api

fails = []
def eq(label, got, want):
    ok = got == want
    print(f"{'OK  ' if ok else 'FAIL'} {label}: {got!r} (want {want!r})")
    if not ok: fails.append(label)

b64 = lambda raw: base64.b64encode(raw).decode()

eq("png sniffed",  api._image_mime(b64(PNG)),  "image/png")
eq("jpeg sniffed", api._image_mime(b64(JPEG)), "image/jpeg")
eq("gif sniffed",  api._image_mime(b64(GIF)),  "image/gif")
eq("webp sniffed", api._image_mime(b64(WEBP)), "image/webp")
eq("data uri passed through", api._image_mime("data:image/png;base64," + b64(PNG)), "data-uri")

# TIFF has a magic number but no browser draws it, so it stays text.
eq("tiff NOT claimed", api._image_mime(b64(TIFF)), None)

for label, value in [
    ("plain text", "STUDY-A"),
    ("a long non-image string", "x" * 500),
    ("base64 of plain text", b64(b"just some ordinary text, long enough to look like data")),
    ("a uid", "Xev4W84NKB2nbtPk0000"),
    ("empty", ""),
    ("a number", 42),
    ("None", None),
]:
    eq(f"not an image: {label}", api._image_mime(value), None)

# End to end: a feature holding a PNG is marked as an image column in the query payload.
ln.Feature(name="qptiff_thumbnail_b64", dtype=str).save()
ln.Feature(name="note", dtype=str).save()
for i, raw in enumerate([PNG, JPEG]):
    p = f"/tmp/img{i}.qptiff"; open(p, "w").write(f"payload {i}")
    a = ln.Artifact(p, key=f"img/t{i}.qptiff").save()
    a.features.add_values({"qptiff_thumbnail_b64": b64(raw), "note": "not an image"})

r = api.run_query("Artifact", None, False, 10, "all", ["qptiff_thumbnail_b64", "note"])
eq("thumbnail column detected", "qptiff_thumbnail_b64" in r["image_columns"], True)
eq("text column not detected", "note" in r["image_columns"], False)
eq("uid column not detected", "uid" in r["image_columns"], False)
eq("nothing withheld at this size", r["images_omitted"], 0)

# A JPEG and a PNG in one column each get their own MIME type.
i = r["columns"].index("qptiff_thumbnail_b64")
eq("cells are complete data URIs",
   sorted(row[i].split(";")[0] for row in r["rows"]),
   ["data:image/jpeg", "data:image/png"])
eq("text column left alone",
   [row[r["columns"].index("note")] for row in r["rows"]], ["not an image", "not an image"])

# Oversized cells are withheld.
api.MAX_IMAGE_CHARS = 10
r = api.run_query("Artifact", None, False, 10, "all", ["qptiff_thumbnail_b64"])
eq("oversized cells withheld", r["images_omitted"], 2)
i = r["columns"].index("qptiff_thumbnail_b64")
eq("withheld cells are empty", [row[i] for row in r["rows"]], ["", ""])

print("\nFAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
