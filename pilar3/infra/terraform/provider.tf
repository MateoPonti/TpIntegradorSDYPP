terraform {
  required_version = ">= 1.6.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }

  # Remote state is mandatory once the pipeline runs this code: two runners
  # applying against a local state file is how clusters get duplicated.
  #
  # B3 (27/11/2026): está en un bucket de GCS y no en el disco del que corre el pipeline. El bucket **no** va
  # acá: es una configuración parcial y el nombre entra por `-backend-config`, porque un bucket es global en
  # todo GCP y su nombre es, de hecho, el proyecto donde vive el estado. Queda en el secret TF_STATE_BUCKET.
  #
  #   gcloud storage buckets create gs://<proyecto>-tfstate \
  #     --location=us-central1 --uniform-bucket-level-access --public-access-prevention
  #   gcloud storage buckets update gs://<proyecto>-tfstate --versioning   # un apply mal no borra el estado
  #   cd pilar3/infra/terraform
  #   tofu init -backend-config="bucket=<proyecto>-tfstate" -migrate-state
  #
  # El estado tiene las IP, los nombres de las service accounts y los ids del Workload Identity: nunca se
  # commitea (está en .gitignore).
  backend "gcs" {
    prefix = "tpi/pilar3"
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}
