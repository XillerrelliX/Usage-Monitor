"""Verify exact upstream originals and recorded harness/patch bytes, offline."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path


def verify(native: Path, generated: Path | None = None) -> dict:
    manifest = json.loads((native / "upstream-manifest.json").read_text(encoding="utf-8"))
    for relative, expected in {**manifest["files"], **manifest["harnessFiles"]}.items():
        path = native / relative
        payload = path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != expected:
            raise ValueError("Source SHA-256 mismatch: " + relative)
        if relative in manifest["gitBlobs"]:
            blob = b"blob " + str(len(payload)).encode("ascii") + b"\0" + payload
            if hashlib.sha1(blob).hexdigest() != manifest["gitBlobs"][relative]:
                raise ValueError("Upstream Git blob mismatch: " + relative)
    if generated is not None:
        for name, expected in manifest["generatedFiles"].items():
            if hashlib.sha256((generated / name).read_bytes()).hexdigest() != expected:
                raise ValueError("Generated offline patch mismatch: " + name)
    return {"sourceCommit": manifest["sourceCommit"], "upstreamFiles": len(manifest["files"]),
            "harnessFiles": len(manifest["harnessFiles"]), "generatedFiles": len(manifest["generatedFiles"]) if generated else 0}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--generated-dir", type=Path)
    arguments = parser.parse_args()
    print(json.dumps(verify(Path(__file__).resolve().parent, arguments.generated_dir), sort_keys=True))
