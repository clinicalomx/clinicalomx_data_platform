# SSH access for admins (restricted to your IP).
resource "openstack_networking_secgroup_v2" "ssh" {
  name        = "${var.control_plane.instance_name}-ssh"
  description = "Admin SSH to the clinicalomxdb hub"
}

resource "openstack_networking_secgroup_rule_v2" "ssh" {
  for_each          = toset(var.control_plane.ssh_ingress_cidrs)
  security_group_id = openstack_networking_secgroup_v2.ssh.id
  direction         = "ingress"
  ethertype         = "IPv4"
  protocol          = "tcp"
  port_range_min    = 22
  port_range_max    = 22
  remote_ip_prefix  = each.value
  description       = "SSH from ${each.value}"
}

resource "openstack_networking_secgroup_v2" "hpc" {
  name        = "${var.control_plane.instance_name}-hpc"
  description = "Ingress from HPC NAT"
}

resource "openstack_networking_secgroup_rule_v2" "hpc" {
  for_each = {
    postgres = 5432
    mlflow   = 5000
    prefect  = 4200
  }
  security_group_id = openstack_networking_secgroup_v2.hpc.id
  direction         = "ingress"
  ethertype         = "IPv4"
  protocol          = "tcp"
  port_range_min    = each.value
  port_range_max    = each.value
  remote_ip_prefix  = local.site.hub.compute_ingress_cidr
  description       = "${each.key} from HPC NAT"
}

resource "openstack_networking_secgroup_v2" "laminhub" {
  name        = "${var.control_plane.instance_name}-laminhub"
  description = "Postgres 5432 for LaminHub replication"
}

resource "openstack_networking_secgroup_rule_v2" "laminhub" {
  for_each          = toset(var.control_plane.laminhub_cidrs)
  security_group_id = openstack_networking_secgroup_v2.laminhub.id
  direction         = "ingress"
  ethertype         = "IPv4"
  protocol          = "tcp"
  port_range_min    = 5432
  port_range_max    = 5432
  remote_ip_prefix  = each.value
  description       = "Postgres from LaminHub ${each.value}"
}

resource "openstack_networking_secgroup_v2" "compute" {
  name        = "${var.control_plane.instance_name}-compute"
  description = "Ingress from auxiliary compute dev boxes on the private subnet"
}

resource "openstack_networking_secgroup_rule_v2" "compute" {
  for_each = {
    postgres = 5432
    mlflow   = 5000
    prefect  = 4200
  }
  security_group_id = openstack_networking_secgroup_v2.compute.id
  direction         = "ingress"
  ethertype         = "IPv4"
  protocol          = "tcp"
  port_range_min    = each.value
  port_range_max    = each.value
  remote_ip_prefix  = var.control_plane.private_subnet_cidr
  description       = "${each.key} from compute subnet"
}

data "openstack_networking_network_v2" "ext" {
  name = var.control_plane.external_network_name
}

# Nectar's per-project default SG (for egress)
data "openstack_networking_secgroup_v2" "default" {
  name = "default"
}

resource "openstack_networking_network_v2" "private" {
  name = "${var.control_plane.instance_name}-net"
}

resource "openstack_networking_subnet_v2" "private" {
  name            = "${var.control_plane.instance_name}-subnet"
  network_id      = openstack_networking_network_v2.private.id
  cidr            = var.control_plane.private_subnet_cidr
  ip_version      = 4
  enable_dhcp     = true
  dns_nameservers = ["8.8.8.8", "1.1.1.1"] # so the box resolves github/registries via the router
}

resource "openstack_networking_router_v2" "hub" {
  name                = "${var.control_plane.instance_name}-router"
  external_network_id = data.openstack_networking_network_v2.ext.id
}

resource "openstack_networking_router_interface_v2" "hub" {
  router_id = openstack_networking_router_v2.hub.id
  subnet_id = openstack_networking_subnet_v2.private.id
}

resource "openstack_networking_port_v2" "hub" {
  name           = "${var.control_plane.instance_name}-port"
  network_id     = openstack_networking_network_v2.private.id
  admin_state_up = true

  fixed_ip {
    subnet_id = openstack_networking_subnet_v2.private.id
  }

  security_group_ids = [
    data.openstack_networking_secgroup_v2.default.id,
    openstack_networking_secgroup_v2.ssh.id,
    openstack_networking_secgroup_v2.hpc.id,
    openstack_networking_secgroup_v2.laminhub.id,
    openstack_networking_secgroup_v2.compute.id,
  ]
}

# Pre-allocated floating IP, created ONCE outside tofu (openstack floating ip create
# <external_network_name>) and named in site.yaml as hub.public_address. 
data "openstack_networking_floatingip_v2" "hub" {
  address = local.site.hub.public_address
}

resource "openstack_networking_floatingip_associate_v2" "hub" {
  floating_ip = data.openstack_networking_floatingip_v2.hub.address
  port_id     = openstack_networking_port_v2.hub.id
  depends_on  = [openstack_networking_router_interface_v2.hub]
}
