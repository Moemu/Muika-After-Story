"""将节点资源保存为内容寻址文件，并在接收端生成本地路径。"""

import hashlib
import mimetypes
import re
from pathlib import Path
from uuid import uuid4

from muika.models import Resource

from .models import ResourceReference

MAX_RESOURCE_BYTES = 64 * 1024 * 1024


class ResourceVault:
    """保存不可变副本；引用有效性由实际文件、长度和散列共同决定。"""

    def __init__(self, directory: Path) -> None:
        self.directory = directory.resolve()
        self.directory.mkdir(parents=True, exist_ok=True)

    def path(self, reference: ResourceReference) -> Path:
        return self.directory / reference.sha256

    def materialize(self, reference: ResourceReference) -> Resource:
        path = self.path(reference)
        content = path.read_bytes()
        if len(content) != reference.size or hashlib.sha256(content).hexdigest() != reference.sha256:
            raise ValueError("Resource content does not match its reference.")
        suffix = Path(reference.name).suffix.lower()
        if not re.fullmatch(r"\.[a-z0-9]{1,10}", suffix):
            suffix = mimetypes.guess_extension(reference.media_type) or ".bin"
        materialized = path.with_name(reference.sha256 + suffix)
        if not materialized.is_file() or hashlib.sha256(materialized.read_bytes()).hexdigest() != reference.sha256:
            temporary = path.parent / (uuid4().hex + ".tmp")
            try:
                temporary.write_bytes(content)
                temporary.replace(materialized)
            finally:
                temporary.unlink(missing_ok=True)
        return Resource(type=reference.kind, path=str(materialized), mimetype=reference.media_type)

    def preserve(self, resource: Resource) -> ResourceReference:
        if resource.path:
            content = Path(resource.path).read_bytes()
            name = Path(resource.path).name
        elif isinstance(resource.raw, bytes):
            content, name = resource.raw, "resource" + (resource.extension or ".bin")
        elif resource.raw is not None:
            content, name = resource.raw.getvalue(), "resource" + (resource.extension or ".bin")
        else:
            raise ValueError("Resource has no local content.")
        if len(content) > MAX_RESOURCE_BYTES:
            raise ValueError("Resource exceeds the 64 MiB transfer limit.")
        reference = ResourceReference(
            sha256=hashlib.sha256(content).hexdigest(),
            size=len(content),
            media_type=resource.mimetype or "application/octet-stream",
            name=name,
            kind=resource.type,
        )
        path = self.path(reference)
        if not path.is_file():
            temporary = self.directory / (uuid4().hex + ".tmp")
            try:
                temporary.write_bytes(content)
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
        return reference
