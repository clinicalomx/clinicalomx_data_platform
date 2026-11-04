"""Image preprocessing over an ``xr.DataTree``: pyramid levels, box crops, projection, 8-bit."""
import numpy as np
import xarray as xr


def _yx_sizes(node):
    """(y, x) pixel sizes of a node's `image` variable, whatever the axis names."""
    sizes = {d.lower(): s for d, s in node.ds["image"].sizes.items()}
    return sizes["y"], sizes["x"]


def _scale_factors_attr(node):
    """The `scale_factors` attr from the node's image var or dataset, or None."""
    for attrs in (node.ds["image"].attrs, node.ds.attrs):
        if "scale_factors" in attrs:
            return attrs["scale_factors"]
    return None


def get_smallest_scale_image(datatree: xr.DataTree, return_factor: bool = False):
    """Smallest pyramid level by pixel count holding an `image` variable."""
    def n_pixels(node):
        y, x = _yx_sizes(node)
        return y * x

    nodes = [n for n in datatree.subtree if "image" in n.ds.variables]
    if not nodes:
        raise ValueError("No nodes in datatree")

    smallest = min(nodes, key=n_pixels)
    if not return_factor:
        return smallest.image

    sf = _scale_factors_attr(smallest)
    if sf is not None:
        # Per-level downsample factors from full res; the smallest level has the largest.
        arr = np.atleast_1d(np.asarray(sf, dtype=float))
        factor = float(arr.max()) if arr.size else None
    else:
        factor = None

    if factor is None:
        # Infer from the longest edges of full res and the returned level.
        largest = max(nodes, key=n_pixels)
        factor = float(max(_yx_sizes(largest)) / max(_yx_sizes(smallest)))

    return smallest.image, factor


def _smallest_yx(datatree: xr.DataTree):
    """(y, x) pixel sizes of the smallest `image`-bearing level."""
    nodes = [n for n in datatree.subtree if "image" in n.ds.variables]
    if not nodes:
        raise ValueError("No nodes in datatree")
    smallest = min(nodes, key=lambda n: np.prod(_yx_sizes(n)))
    return _yx_sizes(smallest)


def _largest_scale_image(datatree: xr.DataTree):
    """`image` DataArray of the largest (full-resolution) `image`-bearing level."""
    nodes = [n for n in datatree.subtree if "image" in n.ds.variables]
    if not nodes:
        raise ValueError("No nodes in datatree")
    return max(nodes, key=lambda n: np.prod(_yx_sizes(n))).image


def crop_datatree_to_box(
    datatree: xr.DataTree,
    box,
    box_shape=None,
    pad_ratio: float = 1.0,
    full_scale_only: bool = False,
):
    """Crop every pyramid level of a DataTree to the same physical box. Stays lazy.

    ``box`` is ``(x0, y0, x1, y1)`` in pixels of the frame detection ran on, whose ``(H, W)`` is
    ``box_shape`` (default: the smallest `image` level). ``pad_ratio`` scales the longest edge
    about the center (1.0 = none), clamped to the image. ``full_scale_only`` returns only the
    full-resolution ``image`` DataArray. Starts floor and stops ceil, so each crop contains the
    box. Each level's ``image`` carries a ``translation`` attr: the crop's ``[y, x]`` offset in
    full-resolution pixels.
    """
    if box_shape is None:
        box_shape = _smallest_yx(datatree)
    x0, y0, x1, y1 = box
    if pad_ratio != 1.0:
        margin = (pad_ratio - 1.0) * max(x1 - x0, y1 - y0) / 2.0
        x0, y0, x1, y1 = x0 - margin, y0 - margin, x1 + margin, y1 + margin
    Hs, Ws = box_shape
    fx0, fx1 = x0 / Ws, x1 / Ws
    fy0, fy1 = y0 / Hs, y1 / Hs
    Hf, Wf = _yx_sizes(max(
        (n for n in datatree.subtree if "image" in n.ds.variables),
        key=lambda n: np.prod(_yx_sizes(n)),
    ))

    def crop(ds):
        if "image" not in ds:
            return ds
        ynm = next(d for d in ds["image"].dims if d.lower() == "y")
        xnm = next(d for d in ds["image"].dims if d.lower() == "x")
        H, W = ds.sizes[ynm], ds.sizes[xnm]
        X0, X1 = max(0, int(np.floor(fx0 * W))), min(W, int(np.ceil(fx1 * W)))
        Y0, Y1 = max(0, int(np.floor(fy0 * H))), min(H, int(np.ceil(fy1 * H)))
        out = ds.isel({ynm: slice(Y0, Y1), xnm: slice(X0, X1)})
        out["image"] = out["image"].assign_attrs(
            dims=[ynm, xnm],
            translation=[Y0 * Hf / H, X0 * Wf / W],
        )
        return out

    cropped = datatree.map_over_datasets(crop)
    return _largest_scale_image(cropped) if full_scale_only else cropped


def crop_datatree_to_boxes(
    datatree: xr.DataTree,
    gdf,
    box_shape=None,
    pad_ratio: float = 1.0,
    full_scale_only: bool = False,
):
    """Yield ``(index, fov)`` per row of a detection GeoDataFrame, cropping each row's bounds.

    ``box_shape``, ``pad_ratio`` and ``full_scale_only`` are as in `crop_datatree_to_box`. Lazy.
    """
    if box_shape is None:
        box_shape = _smallest_yx(datatree)
    for idx, geom in gdf.geometry.items():
        if geom is None or geom.is_empty:
            continue
        yield idx, crop_datatree_to_box(
            datatree, geom.bounds, box_shape, pad_ratio, full_scale_only
        )


def max_intensity_projection(dataarray: xr.DataArray, dim="c"):
    return dataarray.max(dim=dim)

def mean_intensity_projection(dataarray: xr.DataArray, dim="c"):
    return dataarray.mean(dim=dim)

def image_to_8bit(img):
    img_min, img_max = img.min(), img.max()
    norm = (img - img_min) / (img_max - img_min)
    return (norm * 255).astype(np.uint8)

def grayscale_to_rgb(arr):
    return np.stack((arr,) * 3, axis=-1)
