# Mainly for the dev boxes in infra/compute

variable "state_encryption_passphrase" {
  description = "Passphrase for OpenTofu state encryption (TF_VAR_state_encryption_passphrase)."
  type        = string
  sensitive   = true
}

variable "hub_instance_name" {
  description = "instance_name of the control-plane hub (infra/control_plane control_plane.instance_name). Used to resolve the hub network/subnet/port by name, e.g. clinicalomxhub or clinicalomxhub-staging."
  type        = string
  default     = "clinicalomxhub"
}

variable "external_network_name" {
  description = "FIP-capable external network for per-box floating IPs: the control plane's control_plane.external_network_name."
  type        = string
}

variable "node_name_prefix" {
  description = "Prefix for the VM / security-group / port names: <prefix>-<node>. Node \"tau\" becomes VM \"compute-tau\"."
  type        = string
  default     = "compute"
}

variable "default_image_name" {
  description = "Glance image a box boots from unless a node overrides it."
  type        = string
  default     = "NeCTAR Ubuntu 22.04 LTS (Jammy) amd64 (with Docker)"
}

variable "users" {
  description = "Dev-box users keyed by Linux username. Each carries their SSH public keys and the source CIDRs they may connect from. Referenced by name from compute_nodes."
  type = map(object({
    ssh_authorized_keys = list(string) # this user's public keys
    ssh_ingress_cidrs   = list(string) # CIDRs this user may SSH from (their office/home/VPN)
  }))
  default = {}

  validation {
    condition     = alltrue([for u in keys(var.users) : can(regex("^[a-z_][a-z0-9_-]{0,31}$", u))])
    error_message = "Each users key is a Linux username: lowercase, start with a letter or underscore, only letters/digits/_/-."
  }
}

variable "compute_nodes" {
  description = "Dev boxes keyed by node name. Each has a dedicated volume (mounted at /home) and a list of usernames (referencing the users directory) to give accounts on the box."
  type = map(object({
    flavor      = string
    image_name  = optional(string) # falls back to default_image_name
    volume_name = string           # pre-created Cinder volume, referenced not created, mounted at /home
    users       = list(string)     # usernames referencing the top-level users map
  }))
  default = {}

  validation {
    condition     = alltrue([for n in keys(var.compute_nodes) : can(regex("^[a-z0-9][a-z0-9-]{0,61}$", n))])
    error_message = "Each compute_nodes key is used in the VM/host name: lowercase letters, digits, and hyphens only."
  }

  validation {
    condition = alltrue([
      for node in values(var.compute_nodes) : alltrue([
        for u in node.users : contains(keys(var.users), u)
      ])
    ])
    error_message = "Every username listed in a compute_nodes.users must be defined in the top-level users map."
  }
}
