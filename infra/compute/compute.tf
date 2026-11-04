locals {
  node_users = {
    for n, node in var.compute_nodes : n => { for uname in node.users : uname => var.users[uname] }
  }
}

data "openstack_compute_flavor_v2" "node" {
  for_each = var.compute_nodes
  name     = each.value.flavor
}

data "openstack_images_image_v2" "node" {
  for_each    = var.compute_nodes
  name        = coalesce(each.value.image_name, var.default_image_name)
  most_recent = true
}

resource "openstack_compute_instance_v2" "node" {
  for_each  = var.compute_nodes
  name      = "${var.node_name_prefix}-${each.key}"
  image_id  = data.openstack_images_image_v2.node[each.key].id
  flavor_id = data.openstack_compute_flavor_v2.node[each.key].id

  network {
    port = openstack_networking_port_v2.node[each.key].id
  }

  user_data = templatefile("${path.module}/compute_init.yaml.tftpl", {
    node_name           = each.key
    users               = local.node_users[each.key]
    home_device         = local.home_device[each.key]
    hub_private_ip      = local.hub_private_ip
    mlflow_tracking_uri = "http://${local.hub_private_ip}:5000"
    lamin_instance      = local.site.lamin.instance
  })

  lifecycle {
    ignore_changes = [user_data]
  }
}
