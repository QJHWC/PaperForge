"""Safe, plan-first compute backends for PaperForge v3."""

from .base import (
    CommandOutcome,
    CommandRunner,
    ComputeBackend,
    JobStateError,
    SensitiveEnvironmentError,
    SubprocessCommandRunner,
    UnknownJobError,
)
from .cloud_ssh import (
    CloudSSHBackend,
    CloudSSHComputeBackend,
    CloudSSHConfig,
)
from .contracts import (
    ArtifactDirection,
    ArtifactRecord,
    CommandPlan,
    JobResult,
    JobSpec,
    JobStatus,
    ResourceSpec,
)
from .docker import DockerBackend, DockerComputeBackend, DockerConfig
from .kubernetes import (
    KubernetesBackend,
    KubernetesComputeBackend,
    KubernetesConfig,
)
from .local import LocalBackend, LocalComputeBackend
from .registry import (
    ComputeBackendRegistry,
    backend_registry,
    create_backend,
)
from .slurm import SlurmBackend, SlurmComputeBackend, SlurmConfig
from .ssh import (
    SSHBackend,
    SSHComputeBackend,
    SSHConfig,
    SSHSecurityError,
)

__all__ = [
    "ArtifactDirection",
    "ArtifactRecord",
    "CloudSSHBackend",
    "CloudSSHComputeBackend",
    "CloudSSHConfig",
    "CommandOutcome",
    "CommandPlan",
    "CommandRunner",
    "ComputeBackend",
    "ComputeBackendRegistry",
    "DockerBackend",
    "DockerComputeBackend",
    "DockerConfig",
    "JobResult",
    "JobSpec",
    "JobStateError",
    "JobStatus",
    "KubernetesBackend",
    "KubernetesComputeBackend",
    "KubernetesConfig",
    "LocalBackend",
    "LocalComputeBackend",
    "ResourceSpec",
    "SensitiveEnvironmentError",
    "SSHBackend",
    "SSHComputeBackend",
    "SSHConfig",
    "SSHSecurityError",
    "SlurmBackend",
    "SlurmComputeBackend",
    "SlurmConfig",
    "SubprocessCommandRunner",
    "UnknownJobError",
    "backend_registry",
    "create_backend",
]
