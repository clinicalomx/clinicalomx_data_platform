# Control-plane (hub) VM configuration, grouped under one namespace so the single
# control-plane instance is described by one object in tfvars. 
variable "control_plane" {
  description = "Configuration for the single control-plane (hub) VM and its networking, volumes, and service wiring."
  type = object({
    # Instance
    instance_name             = optional(string, "clinicalomxdb-hub")
    image_name                = optional(string, "NeCTAR Ubuntu 22.04 LTS (Jammy) amd64 (with Docker)")
    flavor_name               = optional(string, "t3.medium")
    extra_ssh_authorized_keys = optional(list(string), [])

    # Networking / security groups
    ssh_ingress_cidrs     = list(string) # CIDRs allowed to reach port 22 (admin IPs as /32)
    external_network_name = string       # the cloud's FIP-capable external network
    private_subnet_cidr   = optional(string, "10.0.0.0/24")
    qld_data_network_name = optional(string, "qld-data")
    laminhub_cidrs        = optional(list(string), ["52.37.250.47/32", "54.190.129.28/32"])

    # Pre-existing Cinder volumes (referenced, never created)
    volume_postgres_name  = optional(string, "data-postgres")
    volume_artifacts_name = optional(string, "data-artifacts")
    volume_docker_name    = optional(string, "docker-root")

    platform_release_tag = string


    db_env_path        = optional(string, "../../packages/clinicalomxdb/hub-database/.env")
    pipelines_env_path = optional(string, "../../packages/clinicalomxdb/hub-pipelines/.env")
    ops_env_path       = optional(string, "../../packages/clinicalomxdb/hub-ops/.env")

    # Keystone URL the VM uses to get a token for the Barbican fetch (trailing slash)
    os_auth_url = string
  })
}

# Private PEP 503 package index on the Nectar object store. Grouped under one
# namespace like control_plane. 
variable "package_index" {
  description = "Private PEP 503 Python package index served from the Nectar object store (Ceph RGW)."
  type = object({
    container_name = string

    index_prefix = optional(string, "simple")

    s3_endpoint = string

    region = optional(string, "us-east-1")
  })
}

variable "ghcr_username" {
  description = "GHCR username (machine-user) for docker login to pull the private review-app image."
  type        = string
}

variable "state_encryption_passphrase" {
  description = "Passphrase for OpenTofu state encryption (TF_VAR_state_encryption_passphrase)."
  type        = string
  sensitive   = true
}

variable "ghcr_packages_read_pat" {
  description = "Classic PAT, read:packages only, no repository access. Used for docker login ghcr.io to pull the private review-app image."
  type        = string
  sensitive   = true
}

variable "github_contents_read_pat" {
  description = "Fine-grained PAT, Contents: read on the private repos. Used to pip-install clinicalomx and clinicalomx_markers into the python-lamindb image and to clone clinicalomx_pipeline."
  type        = string
  sensitive   = true
}
