# Pre-existing Cinder volumes
data "openstack_blockstorage_volume_v3" "postgres" {
  name = var.control_plane.volume_postgres_name
}

data "openstack_blockstorage_volume_v3" "artifacts" {
  name = var.control_plane.volume_artifacts_name
}

data "openstack_blockstorage_volume_v3" "docker" {
  name = var.control_plane.volume_docker_name
}

# Attach to the hub VM. A volume attaches to only ONE instance at a time, so the
# first apply (cutover) requires these detached from the old manual VM first.
resource "openstack_compute_volume_attach_v2" "postgres" {
  instance_id = openstack_compute_instance_v2.hub.id
  volume_id   = data.openstack_blockstorage_volume_v3.postgres.id
}

resource "openstack_compute_volume_attach_v2" "artifacts" {
  instance_id = openstack_compute_instance_v2.hub.id
  volume_id   = data.openstack_blockstorage_volume_v3.artifacts.id
}

resource "openstack_compute_volume_attach_v2" "docker" {
  instance_id = openstack_compute_instance_v2.hub.id
  volume_id   = data.openstack_blockstorage_volume_v3.docker.id
}

locals {
  device_postgres  = "/dev/disk/by-id/virtio-${substr(data.openstack_blockstorage_volume_v3.postgres.id, 0, 20)}"
  device_artifacts = "/dev/disk/by-id/virtio-${substr(data.openstack_blockstorage_volume_v3.artifacts.id, 0, 20)}"
  device_docker    = "/dev/disk/by-id/virtio-${substr(data.openstack_blockstorage_volume_v3.docker.id, 0, 20)}"
}
