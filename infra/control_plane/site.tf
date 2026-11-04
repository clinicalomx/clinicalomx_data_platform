variable "site_file" {
  description = "Path to site.yaml, relative to this module (site.example.yaml documents every key)."
  type        = string
  default     = "../../site.yaml"
}

locals {
  site_yaml = file("${path.module}/${var.site_file}")
  site      = yamldecode(local.site_yaml)
}
