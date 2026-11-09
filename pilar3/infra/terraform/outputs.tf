output "get_credentials_command" {
  value = "gcloud container clusters get-credentials ${google_container_cluster.main.name} --zone ${var.zone} --project ${var.project_id}"
}

output "registry" {
  value = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}"
}

output "wif_provider" {
  description = "Paste into the GCP_WIF_PROVIDER repository variable"
  value       = google_iam_workload_identity_pool_provider.github.name
}

output "deployer_service_account" {
  description = "Paste into the GCP_DEPLOYER_SA repository variable"
  value       = google_service_account.deployer.email
}

output "infra_service_account" {
  description = "Paste into the GCP_INFRA_SA repository variable"
  value       = google_service_account.infra.email
}
