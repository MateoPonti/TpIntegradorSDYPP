# Main resources of the Pilar 3 platform on Google Cloud.
#
# Google Cloud policy of the TPI: NO miner (CPU or GPU) runs in this project.
# The free trial forbids crypto mining and does not allow GPUs. GKE only hosts
# the coordinator, RabbitMQ, Redis, the transaction pool and the HTTP gateway
# for external miners; the miners run in the group k3s, Colab or the course
# GPU cluster (see k3s-local/). Do not add VMs, instance groups or Cloud Run
# jobs for mining here.

# ---------------------------------------------------------------- APIs

resource "google_project_service" "apis" {
  for_each = toset([
    "compute.googleapis.com",
    "container.googleapis.com",
    "artifactregistry.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "sts.googleapis.com",
  ])
  service            = each.value
  disable_on_destroy = false
}

# ---------------------------------------------------------------- network

resource "google_compute_network" "vpc" {
  name                    = "${var.name}-vpc"
  auto_create_subnetworks = false
}

resource "google_compute_subnetwork" "subnet" {
  name                     = "${var.name}-subnet"
  ip_cidr_range            = "10.60.0.0/20"
  region                   = var.region
  network                  = google_compute_network.vpc.id
  private_ip_google_access = true

  secondary_ip_range {
    range_name    = "pods"
    ip_cidr_range = "10.70.0.0/16"
  }

  secondary_ip_range {
    range_name    = "services"
    ip_cidr_range = "10.80.0.0/20"
  }
}

# ---------------------------------------------------------------- GKE

resource "google_service_account" "gke_nodes" {
  account_id   = "${var.name}-gke-nodes"
  display_name = "GKE nodes (${var.name})"
}

resource "google_project_iam_member" "gke_nodes" {
  for_each = toset([
    "roles/logging.logWriter",
    "roles/monitoring.metricWriter",
    "roles/monitoring.viewer",
    "roles/artifactregistry.reader",
  ])
  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.gke_nodes.email}"
}

# Zonal Standard cluster: one per billing account is covered by the GKE free
# tier credit. A regional cluster triples the nodes and is not covered.
resource "google_container_cluster" "main" {
  depends_on = [google_project_service.apis]

  name     = "${var.name}-gke"
  location = var.zone

  network    = google_compute_network.vpc.id
  subnetwork = google_compute_subnetwork.subnet.id

  remove_default_node_pool = true
  initial_node_count       = 1
  deletion_protection      = false

  ip_allocation_policy {
    cluster_secondary_range_name  = "pods"
    services_secondary_range_name = "services"
  }

  release_channel {
    channel = "REGULAR"
  }

  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }

  resource_labels = {
    project    = "sd2026"
    component  = "tpi-pilar3"
    managed-by = "opentofu"
  }
}

# Pool 1: infrastructure services. The taint keeps application pods out;
# RabbitMQ and Redis carry the matching toleration and nodeSelector.
resource "google_container_node_pool" "infra" {
  name       = "infra"
  location   = var.zone
  cluster    = google_container_cluster.main.name
  node_count = 1

  node_config {
    machine_type    = var.infra_machine_type
    disk_size_gb    = 50
    disk_type       = "pd-balanced"
    service_account = google_service_account.gke_nodes.email
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]

    labels = {
      role = "infra"
    }

    taint {
      key    = "dedicated"
      value  = "infra"
      effect = "NO_SCHEDULE"
    }

    workload_metadata_config {
      mode = "GKE_METADATA"
    }
  }

  management {
    auto_repair  = true
    auto_upgrade = true
  }
}

# Pool 2: shared pool for the applications (coordinator, transaction pool and
# HTTP gateway for external miners). The cluster autoscaler adds nodes when the
# HPA of the coordinator or the gateway creates pods that do not fit.
resource "google_container_node_pool" "apps" {
  name     = "apps"
  location = var.zone
  cluster  = google_container_cluster.main.name

  autoscaling {
    min_node_count = var.apps_min_nodes
    max_node_count = var.apps_max_nodes
  }

  node_config {
    machine_type    = var.apps_machine_type
    disk_size_gb    = 50
    disk_type       = "pd-balanced"
    service_account = google_service_account.gke_nodes.email
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]

    labels = {
      role = "apps"
    }

    workload_metadata_config {
      mode = "GKE_METADATA"
    }
  }

  management {
    auto_repair  = true
    auto_upgrade = true
  }
}

resource "google_artifact_registry_repository" "images" {
  depends_on = [google_project_service.apis]

  location      = var.region
  repository_id = var.name
  format        = "DOCKER"
  description   = "TPI images (coordinator, pool, gateway)"
}

# ---------------------------------------------------------------- CI/CD identity

# Workload Identity Federation for GitHub Actions: the pipelines get a
# short-lived token for the deployer service account. No JSON key exists
# anywhere, which is what "zero static keys" in the TPI statement means.

resource "google_iam_workload_identity_pool" "github" {
  depends_on = [google_project_service.apis]

  workload_identity_pool_id = "${var.name}-github"
  display_name              = "GitHub Actions (${var.name})"
}

resource "google_iam_workload_identity_pool_provider" "github" {
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "github-oidc"
  display_name                       = "GitHub OIDC"

  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.repository" = "assertion.repository"
    "attribute.ref"        = "assertion.ref"
  }

  # Without this condition ANY GitHub repository could ask for a token.
  attribute_condition = "assertion.repository == \"${var.github_repo}\""

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

resource "google_service_account" "deployer" {
  account_id   = "${var.name}-deployer"
  display_name = "CI/CD deployer (${var.name})"
}

resource "google_service_account_iam_member" "deployer_wif" {
  service_account_id = google_service_account.deployer.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository/${var.github_repo}"
}

# Narrow roles for the app pipelines. Pipeline 1 (infra) needs more; see README.
resource "google_project_iam_member" "deployer" {
  for_each = toset([
    "roles/container.developer",
    "roles/artifactregistry.writer",
  ])
  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.deployer.email}"
}

# Service account for Pipeline 1 (it creates and changes infrastructure).
# Deliberately separate from the deployer: an app pipeline that gets
# compromised must not be able to delete the cluster.
resource "google_service_account" "infra" {
  account_id   = "${var.name}-infra"
  display_name = "CI/CD infrastructure (${var.name})"
}

resource "google_service_account_iam_member" "infra_wif" {
  service_account_id = google_service_account.infra.name
  role               = "roles/iam.workloadIdentityUser"
  # Any workflow of the repo can impersonate it (the PR plan needs to read
  # state). The protection for apply is the "production" environment with
  # required reviewers. Hardening: a separate read-only SA for plans and a
  # principalSet on attribute.ref/refs/heads/main for this one.
  member = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository/${var.github_repo}"
}

resource "google_project_iam_member" "infra" {
  for_each = toset([
    "roles/editor",
    "roles/resourcemanager.projectIamAdmin",
    "roles/iam.serviceAccountAdmin",
    "roles/iam.workloadIdentityPoolAdmin",
  ])
  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.infra.email}"
}
