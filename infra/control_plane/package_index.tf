# Private PEP 503 Python package index, served in the Nectar object store. 
# potentially deprecate as this was used for private packages 

data "openstack_identity_auth_scope_v3" "current" {
  name = "clinicalomxdb-package-index"
}

resource "openstack_objectstorage_container_v1" "package_index" {
  name = var.package_index.container_name

  container_read = "${data.openstack_identity_auth_scope_v3.current.project_id}:*"

  metadata = {
    purpose = "private-pep503-python-package-index"
  }

  force_destroy = false
}

resource "openstack_identity_ec2_credential_v3" "package_index" {}

resource "openstack_keymanager_secret_v1" "package_index_credential" {
  name = "${var.control_plane.instance_name}-package-index-credential"
  payload = sensitive(join("\n", [
    "AWS_ACCESS_KEY_ID=${openstack_identity_ec2_credential_v3.package_index.access}",
    "AWS_SECRET_ACCESS_KEY=${openstack_identity_ec2_credential_v3.package_index.secret}",
    "AWS_REGION=${var.package_index.region}",
    "UV_S3_ENDPOINT_URL=${var.package_index.s3_endpoint}",
    "",
  ]))
  payload_content_type = "text/plain"
  secret_type          = "opaque"
}
