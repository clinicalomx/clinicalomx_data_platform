# home for compute nodes/dev boxes
data "openstack_blockstorage_volume_v3" "home" {
  for_each = var.compute_nodes
  name     = each.value.volume_name
}

# Attach the node's volume to its VM
resource "openstack_compute_volume_attach_v2" "home" {
  for_each    = var.compute_nodes
  instance_id = openstack_compute_instance_v2.node[each.key].id
  volume_id   = data.openstack_blockstorage_volume_v3.home[each.key].id
}

locals {
  home_device = {
    for n, v in data.openstack_blockstorage_volume_v3.home : n => "/dev/disk/by-id/virtio-${substr(v.id, 0, 20)}"
  }
}
