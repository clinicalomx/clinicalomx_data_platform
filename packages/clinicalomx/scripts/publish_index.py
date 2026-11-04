"""Publish built artifacts to the lab's private PEP 503 index on the object store.

Usage:

    export INDEX_BUCKET=<bucket>                  # the bucket in packages.index_url
    export UV_S3_ENDPOINT_URL="<packages.index_url origin>"
    export AWS_REGION=us-east-1
    export AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=...     # an OpenStack EC2 credential
    uv run --with boto3 python scripts/publish_index.py dist/*

The object layout is the index:

    <bucket>/packages/<filename>      the sdists and wheels, flat
    <bucket>/simple/<project>/        per-project page, one anchor per file
    <bucket>/simple/                  root page, one anchor per project

Constraints of the store:

  1. No object versioning: re-uploading a filename replaces that release permanently. An
     existing key refuses unless ``--allow-overwrite``, and nothing uploads until every key is
     clear.
  2. An object key may hold at most one slash, so artifacts sit flat under ``packages/``. Test
     installs with uv; a boto3 upload succeeding proves nothing.
  3. The client uses ``request_checksum_calculation="when_required"`` and passes bytes, not a
     file object.
  4. Pages are served as ``text/html``.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

PACKAGES = "packages/"
INDEX = "simple/"


def build_client(endpoint_url: str, region: str):
    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        region_name=region,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )


def key_exists(s3, bucket: str, key: str) -> bool:
    try:
        s3.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        # 404 means absent; any other error is a real failure.
        if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404:
            return False
        raise
    return True


def project_of(filename: str) -> str:
    """PEP 503 normalised project name of an sdist or wheel filename."""
    return re.sub(r"[-_.]+", "-", filename.split("-")[0]).lower()


def rewrite_pages(s3, bucket: str) -> dict[str, list[str]]:
    """Regenerate every project page and the root page from what is under ``packages/``."""
    pages: dict[str, list[str]] = {}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=PACKAGES):
        for obj in page.get("Contents", []):
            name = obj["Key"].split("/", 1)[1]
            if not name:
                continue
            pages.setdefault(project_of(name), []).append(name)

    for project, names in sorted(pages.items()):
        links = []
        for name in sorted(names):
            # Hash the stored bytes, so the page is right for files this run did not upload.
            body = s3.get_object(Bucket=bucket, Key=f"{PACKAGES}{name}")["Body"].read()
            digest = hashlib.sha256(body).hexdigest()
            links.append(
                f'<a href="/{bucket}/{PACKAGES}{name}#sha256={digest}">{name}</a><br>'
            )
        s3.put_object(
            Bucket=bucket,
            Key=f"{INDEX}{project}/",
            ContentType="text/html",
            Body=("<!DOCTYPE html><html><body>" + "".join(links) + "</body></html>").encode(),
        )

    root = "".join(f'<a href="{p}/">{p}</a><br>' for p in sorted(pages))
    s3.put_object(
        Bucket=bucket,
        Key=INDEX,
        ContentType="text/html",
        Body=("<!DOCTYPE html><html><body>" + root + "</body></html>").encode(),
    )
    return pages


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Upload sdists and wheels to the private PEP 503 index.",
    )
    parser.add_argument("artifacts", nargs="+", type=Path, help="paths to sdists and wheels")
    parser.add_argument(
        "--allow-overwrite",
        action="store_true",
        help="replace a filename that is already published. The store has no versioning, "
        "so the release it replaces is gone. Cut a new version instead.",
    )
    parser.add_argument("--bucket", default=os.environ.get("INDEX_BUCKET"))
    parser.add_argument("--endpoint-url", default=os.environ.get("UV_S3_ENDPOINT_URL"))
    # The RGW zonegroup name used in the SigV4 signature.
    parser.add_argument("--region", default=os.environ.get("AWS_REGION"))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    missing = [
        flag
        for flag, value in (
            ("--bucket / INDEX_BUCKET", args.bucket),
            ("--endpoint-url / UV_S3_ENDPOINT_URL", args.endpoint_url),
            ("--region / AWS_REGION", args.region),
        )
        if not value
    ]
    if missing:
        print("not set: " + ", ".join(missing), file=sys.stderr)
        return 2

    absent = [p for p in args.artifacts if not p.is_file()]
    if absent:
        print("no such file: " + ", ".join(str(p) for p in absent), file=sys.stderr)
        return 2

    s3 = build_client(args.endpoint_url, args.region)

    # Check every key before uploading any.
    targets = {f"{PACKAGES}{p.name}": p for p in args.artifacts}
    published = [key for key in sorted(targets) if key_exists(s3, args.bucket, key)]
    if published and not args.allow_overwrite:
        print("refusing to overwrite, already published:", file=sys.stderr)
        for key in published:
            print(f"  {args.bucket}/{key}", file=sys.stderr)
        print(
            "\nThis store has no object versioning, so an overwrite destroys that release.\n"
            "Cut a new version, or pass --allow-overwrite if you are sure.",
            file=sys.stderr,
        )
        return 1

    for key, path in sorted(targets.items()):
        if key in published:
            print(f"overwriting {key}")
        else:
            print(f"uploading   {key}")
        s3.put_object(
            Bucket=args.bucket,
            Key=key,
            Body=path.read_bytes(),
            ContentType="application/octet-stream",
        )

    pages = rewrite_pages(s3, args.bucket)
    total = sum(len(names) for names in pages.values())
    print(f"index rewritten: {len(pages)} project(s), {total} file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
