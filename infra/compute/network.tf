data "openstack_networking_network_v2" "hub" {
  name = "${var.hub_instance_name}-net"
}

data "openstack_networking_subnet_v2" "hub" {
  name = "${var.hub_instance_name}-subnet"
}

data "openstack_networking_port_v2" "hub" {
  name = "${var.hub_instance_name}-port"
}

data "openstack_networking_network_v2" "ext" {
  name = var.external_network_name
}

# Tenant default SG for egress, attached to each box port alongside its ssh SG.
data "openstack_networking_secgroup_v2" "default" {
  name = "default"
}

locals {
  hub_private_ip = data.openstack_networking_port_v2.hub.all_fixed_ips[0]
}

locals {
  # Union of every attached user's allowed SSH source CIDRs, per node, deduped. Keyed
  # "node/cidr" so each becomes one security-group rule on that node's ssh SG.
  ssh_ingress = merge([
    for nname, node in var.compute_nodes : {
      for cidr in distinct(flatten([for uname in node.users : var.users[uname].ssh_ingress_cidrs])) :
      "${nname}/${cidr}" => { node = nname, cidr = cidr }
    }
  ]...)
}

# Per-box SSH security group. One rule per allowed source CIDR (union across users)
resource "openstack_networking_secgroup_v2" "ssh" {
  for_each    = var.compute_nodes
  name        = "${var.node_name_prefix}-${each.key}-ssh"
  description = "SSH to dev box ${each.key}"
}

resource "openstack_networking_secgroup_rule_v2" "ssh" {
  for_each          = local.ssh_ingress
  security_group_id = openstack_networking_secgroup_v2.ssh[each.value.node].id
  direction         = "ingress"
  ethertype         = "IPv4"
  protocol          = "tcp"
  port_range_min    = 22
  port_range_max    = 22
  remote_ip_prefix  = each.value.cidr
  description       = "SSH from ${each.value.cidr}"
}

# Per-box port on the hub's private subnet.
resource "openstack_networking_port_v2" "node" {
  for_each       = var.compute_nodes
  name           = "${var.node_name_prefix}-${each.key}-port"
  network_id     = data.openstack_networking_network_v2.hub.id
  admin_state_up = true

  fixed_ip {
    subnet_id = data.openstack_networking_subnet_v2.hub.id
  }

  security_group_ids = [
    data.openstack_networking_secgroup_v2.default.id,
    openstack_networking_secgroup_v2.ssh[each.key].id,
  ]
}

# Per-box floating IP, allocated on create and released on teardown
resource "openstack_networking_floatingip_v2" "node" {
  for_each    = var.compute_nodes
  pool        = var.external_network_name
  description = "${var.node_name_prefix}-${each.key}"
}

resource "openstack_networking_floatingip_associate_v2" "node" {
  for_each    = var.compute_nodes
  floating_ip = openstack_networking_floatingip_v2.node[each.key].address
  port_id     = openstack_networking_port_v2.node[each.key].id
}
