output "vm_ip" {
  description = "Public floating IP of the hub (pre-allocated, referenced as data; survives rebuilds). Give this to the HPC + the external DB client; point the domain here."
  value       = data.openstack_networking_floatingip_v2.hub.address
}

output "private_ip" {
  description = "Private (routed) IP on the self-service subnet."
  value       = openstack_networking_port_v2.hub.all_fixed_ips[0]
}

output "ssh_private_key" {
  description = "Generated ed25519 admin key. Retrieve: tofu output -raw ssh_private_key > key && chmod 600 key"
  value       = tls_private_key.deploy.private_key_openssh
  sensitive   = true
}

output "package_index_url" {
  description = "PEP 503 index URL. Pass to uv as --index-url, with UV_S3_ENDPOINT_URL set so uv signs the request."
  value       = "${trimsuffix(var.package_index.s3_endpoint, "/")}/${openstack_objectstorage_container_v1.package_index.name}/${var.package_index.index_prefix}/"
}

output "package_index_credential_secret_ref" {
  description = "Barbican href of the read credential for the package index. Fetch the payload with: openstack secret get --payload <href>"
  value       = openstack_keymanager_secret_v1.package_index_credential.secret_ref
}
