# Login details per box
output "dev_boxes" {
  description = "Per-node dev box login details (floating IP + per-user ssh commands)."
  value = {
    for n, node in var.compute_nodes : n => {
      vm_name     = "${var.node_name_prefix}-${n}"
      floating_ip = openstack_networking_floatingip_v2.node[n].address
      private_ip  = openstack_networking_port_v2.node[n].all_fixed_ips[0]
      flavor      = node.flavor
      volume      = node.volume_name
      ssh = {
        for uname in node.users : uname => "ssh ${uname}@${openstack_networking_floatingip_v2.node[n].address}"
      }
    }
  }
}

output "hub_private_ip" {
  description = "Control-plane private IP the boxes point `postgres` at (for verification)."
  value       = local.hub_private_ip
}
