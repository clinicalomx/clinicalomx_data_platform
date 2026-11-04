# The site's shared values, from the same site.yaml scripts/site/render.py validates (see
# infra/control_plane/site.tf). A dev box needs only the LaminHub instance slug from it.
variable "site_file" {
  description = "Path to site.yaml, relative to this module (site.example.yaml documents every key)."
  type        = string
  default     = "../../site.yaml"
}

locals {
  site = yamldecode(file("${path.module}/${var.site_file}"))
}
