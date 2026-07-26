#!/usr/bin/env bash
set -euo pipefail

readonly PYTHON_IMAGE="python@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de"
readonly KIND_VERSION="v0.31.0"
readonly KUBERNETES_VERSION="v1.35.0"
readonly KIND_NODE_IMAGE="kindest/node:v1.35.0@sha256:452d707d4862f52530247495d180205e029056831160e22870e37e3f6c1ac31f"
readonly OPENSSH_FIXTURE_IMAGE="paperforge-openssh-fixture:v3"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "setup_linux_compute_gate.sh must run as root" >&2
  exit 2
fi

case "$(uname -m)" in
  x86_64)
    arch="amd64"
    kind_sha256="eb244cbafcc157dff60cf68693c14c9a75c4e6e6fedaf9cd71c58117cb93e3fa"
    kubectl_sha256="a2e984a18a0c063279d692533031c1eff93a262afcc0afdc517375432d060989"
    ;;
  aarch64|arm64)
    arch="arm64"
    kind_sha256="8e1014e87c34901cc422a1445866835d1e666f2a61301c27e722bdeab5a1f7e4"
    kubectl_sha256="58f82f9fe796c375c5c4b8439850b0f3f4d401a52434052f2df46035a8789e25"
    ;;
  *)
    echo "unsupported Linux architecture: $(uname -m)" >&2
    exit 2
    ;;
esac

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y bubblewrap curl munge openssh-server singularity-container slurm-wlm
chmod u+s "$(command -v bwrap)"
systemctl enable --now munge ssh

curl -fsSL -o /usr/local/bin/kind \
  "https://github.com/kubernetes-sigs/kind/releases/download/${KIND_VERSION}/kind-linux-${arch}"
printf '%s  %s\n' "$kind_sha256" /usr/local/bin/kind | sha256sum -c -
chmod 0755 /usr/local/bin/kind

curl -fsSL -o /usr/local/bin/kubectl \
  "https://dl.k8s.io/release/${KUBERNETES_VERSION}/bin/linux/${arch}/kubectl"
printf '%s  %s\n' "$kubectl_sha256" /usr/local/bin/kubectl | sha256sum -c -
chmod 0755 /usr/local/bin/kubectl

node_name="$(hostname -s)"
cpus="$(nproc)"
memory_mb="$(( $(awk '/MemTotal/ {print $2}' /proc/meminfo) / 1024 - 1024 ))"
if (( memory_mb < 1024 )); then
  memory_mb=1024
fi
install -d -o slurm -g slurm /var/spool/slurmctld /var/log/slurm
install -d /var/spool/slurmd
cat > /etc/slurm/slurm.conf <<EOF
ClusterName=paperforge
SlurmctldHost=${node_name}
AuthType=auth/munge
CryptoType=crypto/munge
SelectType=select/cons_tres
SelectTypeParameters=CR_Core_Memory
ProctrackType=proctrack/linuxproc
TaskPlugin=task/none
ReturnToService=2
SchedulerType=sched/backfill
SchedulerParameters=batch_sched_delay=1,bf_interval=1
AccountingStorageType=accounting_storage/none
JobCompType=jobcomp/none
StateSaveLocation=/var/spool/slurmctld
SlurmdSpoolDir=/var/spool/slurmd
SlurmctldPidFile=/run/slurmctld.pid
SlurmdPidFile=/run/slurmd.pid
SlurmctldLogFile=/var/log/slurm/slurmctld.log
SlurmdLogFile=/var/log/slurm/slurmd.log
NodeName=${node_name} CPUs=${cpus} RealMemory=${memory_mb} State=UNKNOWN
PartitionName=debug Nodes=${node_name} Default=YES MaxTime=INFINITE State=UP
JobAcctGatherType=jobacct_gather/none
PriorityType=priority/basic
SlurmUser=slurm
EOF
systemctl restart munge slurmctld slurmd
scontrol update "NodeName=${node_name}" State=RESUME || true
for _ in {1..30}; do
  if sinfo --noheader --nodes="${node_name}" --format='%T' | grep -Eq 'idle|mix|alloc'; then
    break
  fi
  sleep 1
done
scontrol ping
sinfo --noheader --nodes="${node_name}" --format='%N|%T'

docker pull "$PYTHON_IMAGE"
docker build \
  --tag "$OPENSSH_FIXTURE_IMAGE" \
  tests/fixtures/openssh
rm -f /tmp/paperforge-python.sif
singularity pull /tmp/paperforge-python.sif "docker://${PYTHON_IMAGE}"

kind delete cluster --name paperforge-v3 >/dev/null 2>&1 || true
kind create cluster \
  --name paperforge-v3 \
  --image "$KIND_NODE_IMAGE" \
  --wait 180s
docker exec paperforge-v3-control-plane crictl pull "$PYTHON_IMAGE"
kubectl --context kind-paperforge-v3 create namespace paperforge-v3 \
  --dry-run=client -o yaml | kubectl --context kind-paperforge-v3 apply -f -
printf '%s\n' \
  'apiVersion: v1' \
  'kind: PersistentVolumeClaim' \
  'metadata: {name: paperforge-source, namespace: paperforge-v3}' \
  'spec:' \
  '  accessModes: [ReadWriteOnce]' \
  '  resources: {requests: {storage: 1Gi}}' \
  '---' \
  'apiVersion: v1' \
  'kind: PersistentVolumeClaim' \
  'metadata: {name: paperforge-artifacts, namespace: paperforge-v3}' \
  'spec:' \
  '  accessModes: [ReadWriteOnce]' \
  '  resources: {requests: {storage: 1Gi}}' \
  | kubectl --context kind-paperforge-v3 apply -f -

kind version
kubectl version --client
singularity version
