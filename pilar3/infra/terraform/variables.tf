variable "project_id" {
  description = "GCP project ID"
  type        = string
}

variable "region" {
  description = "GCP region"
  type        = string
  default     = "us-central1"
}

variable "zone" {
  description = "Zone for the zonal cluster (a zonal cluster is covered by the GKE free tier credit)"
  type        = string
  default     = "us-central1-a"
}

variable "name" {
  description = "Prefix for every resource"
  type        = string
  default     = "tpi"
}

variable "github_repo" {
  description = "GitHub repository allowed to impersonate the deployer service account, as owner/repo"
  type        = string
}

variable "infra_machine_type" {
  description = "Machine type for the infra node pool (RabbitMQ, Redis, observability)"
  type        = string
  default     = "e2-standard-2"
}

variable "apps_machine_type" {
  description = "Machine type for the shared apps node pool"
  type        = string
  default     = "e2-standard-2"
}

variable "apps_min_nodes" {
  description = "Cluster autoscaler lower bound for the apps pool"
  type        = number
  default     = 1
}

variable "apps_max_nodes" {
  description = "Cluster autoscaler upper bound for the apps pool"
  type        = number
  default     = 3
}
