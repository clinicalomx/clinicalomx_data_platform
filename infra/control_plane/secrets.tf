locals {
  db_env        = sensitive(file("${path.module}/${var.control_plane.db_env_path}"))
  pipelines_env = sensitive(file("${path.module}/${var.control_plane.pipelines_env_path}"))
  ops_env       = sensitive(file("${path.module}/${var.control_plane.ops_env_path}"))
}

resource "openstack_keymanager_secret_v1" "db_env" {
  name                 = "${var.control_plane.instance_name}-db-env"
  payload              = local.db_env
  payload_content_type = "text/plain"
  secret_type          = "opaque"
}

resource "openstack_keymanager_secret_v1" "pipelines_env" {
  name                 = "${var.control_plane.instance_name}-pipelines-env"
  payload              = local.pipelines_env
  payload_content_type = "text/plain"
  secret_type          = "opaque"
}

resource "openstack_keymanager_secret_v1" "ops_env" {
  name                 = "${var.control_plane.instance_name}-ops-env"
  payload              = local.ops_env
  payload_content_type = "text/plain"
  secret_type          = "opaque"
}

resource "openstack_keymanager_secret_v1" "ghcr_packages_read_pat" {
  name                 = "${var.control_plane.instance_name}-ghcr-packages-read-pat"
  payload              = var.ghcr_packages_read_pat
  payload_content_type = "text/plain"
  secret_type          = "opaque"
}

resource "openstack_keymanager_secret_v1" "github_contents_read_pat" {
  name                 = "${var.control_plane.instance_name}-github-contents-read-pat"
  payload              = var.github_contents_read_pat
  payload_content_type = "text/plain"
  secret_type          = "opaque"
}
