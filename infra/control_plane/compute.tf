resource "tls_private_key" "deploy" {
  algorithm = "ED25519"
}

resource "openstack_compute_keypair_v2" "deploy" {
  name       = var.control_plane.instance_name
  public_key = tls_private_key.deploy.public_key_openssh
}

data "openstack_images_image_v2" "base" {
  name        = var.control_plane.image_name
  most_recent = true
}

data "openstack_compute_flavor_v2" "base" {
  name = var.control_plane.flavor_name
}

resource "openstack_compute_instance_v2" "hub" {
  name      = var.control_plane.instance_name
  image_id  = data.openstack_images_image_v2.base.id
  flavor_id = data.openstack_compute_flavor_v2.base.id
  key_pair  = openstack_compute_keypair_v2.deploy.name

  network {
    port = openstack_networking_port_v2.hub.id
  }
  network {
    name = var.control_plane.qld_data_network_name
  }

  user_data = templatefile("${path.module}/cloud_init.yaml.tftpl", {
    device_postgres      = local.device_postgres
    device_artifacts     = local.device_artifacts
    device_docker        = local.device_docker
    ssh_public_keys      = var.control_plane.extra_ssh_authorized_keys
    source_repository    = local.site.source.repository
    platform_release_tag = var.control_plane.platform_release_tag
    # HPC ssh alias (ControlMaster socket the Prefect worker reuses for sbatch)
    hpc_login_host = local.site.hpc.login_host
    hpc_user       = local.site.hpc.users[0]
    site_yaml      = local.site_yaml
    # Barbican fetch 
    os_auth_url              = var.control_plane.os_auth_url
    barbican_app_cred_id     = openstack_identity_application_credential_v3.barbican.id
    barbican_app_cred_secret = openstack_identity_application_credential_v3.barbican.secret
    db_env_secret_ref        = openstack_keymanager_secret_v1.db_env.secret_ref
    pipelines_env_secret_ref = openstack_keymanager_secret_v1.pipelines_env.secret_ref
    ops_env_secret_ref       = openstack_keymanager_secret_v1.ops_env.secret_ref
    # Two machine tokens: registry pulls take the packages one, git reads the contents one.
    ghcr_packages_pat_secret_ref   = openstack_keymanager_secret_v1.ghcr_packages_read_pat.secret_ref
    github_contents_pat_secret_ref = openstack_keymanager_secret_v1.github_contents_read_pat.secret_ref
    ghcr_username                  = var.ghcr_username
  })

  lifecycle {
    ignore_changes = [user_data]
  }
}
