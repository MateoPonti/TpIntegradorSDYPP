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
  # Create the bucket once by hand (see README) and uncomment.
  # backend "gcs" {
  #   bucket = "CHANGE_ME-tfstate"
  #   prefix = "tpi/pilar3"
  # }
}

provider "google" {
  project = var.project_id
  region  = var.region
}
