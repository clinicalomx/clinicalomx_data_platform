"""Extract a <=640px single-channel PNG thumbnail from every QPTIFF Artifact and register it.

Skips QPTIFFs whose thumbnail exists.

    python extract.py <storage root>    the project folder the thumbnails are registered under
"""
import sys

from imagecodecs._imcd import ImcdError
import lamindb as ln
import bioio_qptiff
from bioio import BioImage
import matplotlib.pyplot as plt
import imageio
from pathlib import Path

@ln.step()
def collect_qptiff_artifacts():
    return ln.Artifact.filter(suffix=".qptiff")

@ln.step()
def exclude_dmso_labels(queryset):
    return queryset.filter(~ln.Q(key__icontains="dmso"))

@ln.step()
def get_thumbnail(qptiff_artifact):
    image = BioImage(
       qptiff_artifact.path, 
       reader=bioio_qptiff.Reader, 
       ome_metadata=False, 
       include_aux_series=True
    )
    image.reader.set_scene(1)
    assert image.reader.current_scene == 'Thumbnail'
    return image.reader.xarray_dask_data

@ln.step()
def get_name(qptiff_artifact):
    return qptiff_artifact.path.stem

@ln.step()
def save_as_png(
    thumbnail, 
    name, 
    output_dir,
    output_storage,
):
    output_path = f"{output_dir}/{name}.png"
    key=f"thumbnails/{name}.png"

    try:
        png_art = ln.Artifact.get(
            key=key,
            storage=output_storage
        )
    except ln.Artifact.DoesNotExist:
        imageio.v3.imwrite(
            output_path, 
            thumbnail,
            plugin=None, 
            extension=None, 
            format_hint=None, 
        )
        png_art = ln.Artifact(
            output_path, 
            key=key,
            storage=output_storage
        ).save()

    return png_art

@ln.step()
def create_png_collection(
    artifacts,
    key
):
    ln.Collection(
        artifacts=artifacts, 
        key=key,
        description="Collection of qptiff thumbnails"
    ).save()

@ln.flow()
def extract_thumbnails_from_qptiffs(storage_root: str):
    qptiff_artifacts = collect_qptiff_artifacts()
    qptiff_artifacts = exclude_dmso_labels(qptiff_artifacts)
    output_storage = ln.Storage(
        root=storage_root,
        description="Project folder for deep learning based object detection on QPTIFF Thumbnails.",
        region="hpc"
    ).save()

    png_arts = []
    for qp in qptiff_artifacts:
        thumbnail = get_thumbnail(qp)
        name = get_name(qp)
        try:
            art = save_as_png(
                thumbnail, 
                name,
                output_storage.path / Path("data"),
                output_storage
            )
            png_arts.append(art)
        except Exception as e:
            print(f"Encountered data failure for {name}: {e}")
            
            ln.Feature(
                name="data_failure",
                dtype=str,
                description="Failure codes, errors, exceptions associated with corruption in the data.",
            ).save()

            qp.features.set_values({
                "data_failure": str(e)
            })
            print("Skipping...")

    create_png_collection(
        png_arts, 
        "clinical-roi/thumbnails"
    )

if __name__ == "__main__":
    extract_thumbnails_from_qptiffs(sys.argv[1])
      

