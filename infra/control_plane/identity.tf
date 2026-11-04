resource "openstack_identity_application_credential_v3" "barbican" {
  name        = "${var.control_plane.instance_name}-barbican-read"
  description = "Boot-time read of the hub .env secrets from Barbican"
  roles       = ["member", "reader"]

  # NOTE: Barbican + access_rules path matching is fiddly. Verify at first boot that
  # cloud-init can GET the payloads (no 403). If so, this scopes the credential to
  # exactly these secrets. If it fails, widen to service/method only, or drop rules.
  access_rules {
    service = "key-manager"
    method  = "GET"
    path    = "${regex("/v1/secrets/[^/]+", openstack_keymanager_secret_v1.db_env.secret_ref)}/payload"
  }
  access_rules {
    service = "key-manager"
    method  = "GET"
    path    = "${regex("/v1/secrets/[^/]+", openstack_keymanager_secret_v1.pipelines_env.secret_ref)}/payload"
  }
  access_rules {
    service = "key-manager"
    method  = "GET"
    path    = "${regex("/v1/secrets/[^/]+", openstack_keymanager_secret_v1.ops_env.secret_ref)}/payload"
  }
  access_rules {
    service = "key-manager"
    method  = "GET"
    path    = "${regex("/v1/secrets/[^/]+", openstack_keymanager_secret_v1.ghcr_packages_read_pat.secret_ref)}/payload"
  }
  access_rules {
    service = "key-manager"
    method  = "GET"
    path    = "${regex("/v1/secrets/[^/]+", openstack_keymanager_secret_v1.github_contents_read_pat.secret_ref)}/payload"
  }
}
