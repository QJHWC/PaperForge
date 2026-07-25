from __future__ import annotations

import re
from dataclasses import dataclass

from .ssh import SSHBackend, SSHConfig, SSHSecurityError

_CLOUD_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}$")


@dataclass(frozen=True)
class CloudSSHConfig:
    ssh: SSHConfig
    provider: str
    instance_id: str
    region: str

    def __post_init__(self) -> None:
        if not isinstance(self.ssh, SSHConfig):
            raise TypeError("ssh must be an SSHConfig")
        for label, value in (
            ("provider", self.provider),
            ("instance_id", self.instance_id),
            ("region", self.region),
        ):
            if not _CLOUD_ID_PATTERN.fullmatch(value):
                raise SSHSecurityError(f"cloud {label} contains unsafe characters")

    def to_safe_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "instance_id": self.instance_id,
            "region": self.region,
            "ssh": self.ssh.to_safe_dict(),
        }


class CloudSSHBackend(SSHBackend):
    name = "cloud-ssh"

    def __init__(self, config: CloudSSHConfig, **kwargs: object) -> None:
        self.cloud_config = config
        super().__init__(config.ssh, **kwargs)


CloudSSHComputeBackend = CloudSSHBackend
