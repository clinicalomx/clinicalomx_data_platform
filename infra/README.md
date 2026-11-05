# Infrastructure

OpenTofu roots and host configuration for the platform.

| Directory | Holds |
|---|---|
| `control_plane/` | The hub VM, its network, volumes and secrets. Deploy it first: [`control_plane/deployment.md`](control_plane/deployment.md) |
| `compute/` | On-demand dev boxes, a separate root with its own state: [`compute/deployment.md`](compute/deployment.md) |
| `secrets/` | The credential catalogue and how to rotate one. No value is stored here |
| `systemd/` | Unit files the hub VM installs at first boot |

The two roots keep separate state. Deploy the control plane before the dev boxes;
the boxes join its network and read its instance name.
