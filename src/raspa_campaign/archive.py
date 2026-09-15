from __future__ import annotations

import os
import shutil
import tarfile
import time
from pathlib import Path
from typing import Any

from .atomic import atomic_write_json
from .files import manifest_tree
from .hashing import sha256_file
from .models import utc_now


def archive_campaign(root: Path, destination: Path, *, include_raw: bool = True) -> dict[str, Any]:
    root=root.resolve(); destination=destination.expanduser().resolve(); destination.mkdir(parents=True,exist_ok=True)
    stamp=time.strftime("%Y%m%d_%H%M%S")
    out=destination/f"{root.name}_ARCHIVE_{stamp}.tar.gz"
    def filt(info:tarfile.TarInfo):
        rel=Path(info.name)
        if not include_raw and "raw" in rel.parts: return None
        if ".claim" in rel.parts or "claims" in rel.parts and rel.name=="active": return None
        return info
    with tarfile.open(out,"w:gz") as tf: tf.add(root,arcname=root.name,filter=filt)
    receipt={"created_utc":utc_now(),"campaign_root":str(root),"archive":str(out),"archive_sha256":sha256_file(out),"include_raw":include_raw,"size_bytes":out.stat().st_size}
    atomic_write_json(destination/f"{out.name}.receipt.json",receipt)
    return receipt
